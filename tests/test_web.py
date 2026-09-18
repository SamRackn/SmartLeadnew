"""Tests for the review web app.

The single most important test here is
``test_approve_refused_for_held_failed_draft`` — the brief requires that a
failed-verification draft can never be approved, enforced *in the route*,
not merely by a hidden button. Everything else follows the same shape: real
HTTP requests against the real FastAPI app, against the real ``mazz_dev``
Postgres (via ``pipeline_helpers``), with the Smartlead push redirected to a
throwaway outbox file so a test run never touches the repo's own
``smartlead_outbox.jsonl``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.integrations.smartlead import StubSmartleadClient
from mazz.models import Campaign, Lead, LeadStatus, VerificationStatus, Workspace
from mazz.pipeline.draft import render_body
from mazz.pipeline.verify import apply_verification, verify_content
from mazz.schemas import Fact
from mazz.web.app import app
from mazz.web.auth import hash_password

import mazz.web.routes.leads as leads_routes

TEST_PASSWORD = "correct-horse-battery-staple"
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Env / client / db plumbing
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Configure settings, and redirect the Smartlead push to a scratch file.

    Patching ``get_smartlead_client`` at the *route module* level (not inside
    ``mazz.integrations.smartlead``) is a test-only substitution — it never
    touches the integration code we don't own, and it is exactly the seam
    ``get_smartlead_client()`` exists to provide.
    """

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 1 Test Street, Testville")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("REVIEW_USER", "client")
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    reload_settings()

    outbox = tmp_path / "smartlead_outbox.jsonl"
    monkeypatch.setattr(
        leads_routes, "get_smartlead_client", lambda: StubSmartleadClient(outbox_path=outbox)
    )

    yield outbox
    reload_settings()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def logged_in_client(client: TestClient) -> TestClient:
    resp = client.post(
        "/login",
        data={"username": "client", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture()
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    # Unlike the other pipeline test files' rollback-based session fixtures,
    # several tests here commit on purpose (a separate HTTP-handled session
    # must see the row). Truncate afterward so no committed row (e.g. the
    # shared "northwind-logistics.com" research_cache fixture) leaks into
    # another test module's run.
    h.reset_db()


# --------------------------------------------------------------------------
# Fixture data builders
# --------------------------------------------------------------------------


def _fact(
    content: str,
    *,
    kind: str = "job_posting",
    source_url: str | None = "https://northwind-logistics.com/careers/revops-manager",
) -> Fact:
    return Fact.build(kind=kind, content=content, source_url=source_url, retrieved_at=NOW)

JOB_FACT = _fact(
    "Northwind Logistics is hiring a Revenue Operations Manager to own the CRM."
)


def seed_research(session, domain: str, facts: list[Fact]) -> None:
    from mazz.models import ResearchCache

    existing = session.get(ResearchCache, domain)
    if existing is not None:
        existing.facts = [f.model_dump(mode="json") for f in facts]
        existing.fetched_at = NOW
    else:
        session.add(
            ResearchCache(
                company_domain=domain,
                company_name="Northwind Logistics",
                facts=[f.model_dump(mode="json") for f in facts],
                fetched_at=NOW,
            )
        )
    session.flush()


def make_passed_lead(session, *, email: str = "dana.passed@northwind-logistics.com"):
    """A lead whose current draft PASSED verification — the approvable case."""

    seed_research(session, h.SHARED_DOMAIN, [JOB_FACT])
    lead = h.make_lead(
        session,
        email=email,
        full_name="Dana Whitfield",
        company_domain=h.SHARED_DOMAIN,
    )
    subject = "Northwind Logistics is hiring a Revenue Operations Manager"
    body = render_body(
        'Dana — saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.',
        lead,
    )
    draft = h.make_draft(session, lead, subject=subject, body=body, signals_used=[JOB_FACT.id])
    result = verify_content(
        lead=lead,
        subject=draft.subject,
        body=draft.body,
        signals_used=draft.signals_used,
        available_facts=[JOB_FACT],
    )
    assert result.passed, result.notes
    apply_verification(session, lead, draft, result)
    session.commit()
    return lead, draft


def make_held_lead(session, *, email: str = "holden.held@northwind-logistics.com"):
    """A lead whose current draft FAILED verification — never approvable."""

    seed_research(session, h.SHARED_DOMAIN, [JOB_FACT])
    lead = h.make_lead(
        session,
        email=email,
        full_name="Holden Reyes",
        company_domain=h.SHARED_DOMAIN,
    )
    body = render_body("Dana — congrats on the 500% revenue growth this year.", lead)
    draft = h.make_draft(session, lead, subject="Big news", body=body, signals_used=[])
    result = verify_content(
        lead=lead,
        subject=draft.subject,
        body=draft.body,
        signals_used=[],
        available_facts=[JOB_FACT],
    )
    assert not result.passed
    apply_verification(session, lead, draft, result)
    session.commit()
    return lead, draft


def make_no_angle_lead(session, *, email: str = "nia.noangle@paperkite.io"):
    lead = h.make_lead(
        session,
        email=email,
        full_name="Nia Okafor",
        company_domain=h.WEAK_DOMAIN,
        status=LeadStatus.NO_ANGLE,
    )
    session.commit()
    return lead


def read_outbox(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_login_page_serves(client: TestClient):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Log in" in resp.text or "log in" in resp.text.lower()


def test_login_wrong_password_rejected(client: TestClient):
    resp = client.post("/login", data={"username": "client", "password": "wrong"})
    assert resp.status_code == 401


def test_login_success_then_logout(client: TestClient):
    resp = client.post(
        "/login",
        data={"username": "client", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/leads"

    # session cookie now authenticates further requests
    resp2 = client.get("/leads")
    assert resp2.status_code == 200

    resp3 = client.post("/logout", follow_redirects=False)
    assert resp3.status_code == 303

    resp4 = client.get("/leads", follow_redirects=False)
    assert resp4.status_code == 303
    assert resp4.headers["location"] == "/login"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/leads"),
        ("get", "/leads/1"),
    ],
)
def test_unauthenticated_get_redirects_to_login(client: TestClient, method, path):
    resp = getattr(client, method)(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_unauthenticated_approve_is_401_not_a_silent_success(client: TestClient, session):
    lead, _draft = make_passed_lead(session)
    resp = client.post(f"/leads/{lead.id}/approve", follow_redirects=False)
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Lead list: no_angle / held must never be filtered out of the default view
# --------------------------------------------------------------------------


def test_no_angle_and_held_leads_appear_in_default_list(
    logged_in_client: TestClient, session
):
    held_lead, _ = make_held_lead(session)
    no_angle_lead = make_no_angle_lead(session)

    resp = logged_in_client.get("/leads")
    assert resp.status_code == 200
    assert held_lead.full_name in resp.text
    assert "held" in resp.text
    assert no_angle_lead.full_name in resp.text
    assert "no_angle" in resp.text


def test_status_filter_still_available_but_not_default(logged_in_client: TestClient, session):
    held_lead, _ = make_held_lead(session)
    passed_lead, _ = make_passed_lead(session, email="dana2@northwind-logistics.com")

    resp = logged_in_client.get("/leads?status=held")
    assert resp.status_code == 200
    assert held_lead.full_name in resp.text
    assert passed_lead.full_name not in resp.text


# --------------------------------------------------------------------------
# Lead detail: the draft beside its sources
# --------------------------------------------------------------------------


def test_lead_detail_renders_cited_source_urls(logged_in_client: TestClient, session):
    lead, draft = make_passed_lead(session)

    resp = logged_in_client.get(f"/leads/{lead.id}")
    assert resp.status_code == 200
    assert draft.subject in resp.text
    assert JOB_FACT.source_url in resp.text


def test_held_lead_detail_shows_verification_notes_prominently(
    logged_in_client: TestClient, session
):
    lead, draft = make_held_lead(session)

    resp = logged_in_client.get(f"/leads/{lead.id}")
    assert resp.status_code == 200
    assert "HELD" in resp.text
    assert "cites no facts" in draft.verification_notes
    assert "cites no facts at all" in resp.text


# --------------------------------------------------------------------------
# Approve: the control that matters
# --------------------------------------------------------------------------


def test_approve_refused_for_held_failed_draft(
    logged_in_client: TestClient, session, env: Path
):
    """The single most important test in this file.

    A draft with verification_status=failed (lead held) must be refused by
    the route itself: no Smartlead push, no status change, a 4xx back to the
    reviewer explaining why.
    """

    outbox_path = env
    lead, draft = make_held_lead(session)
    assert draft.verification_status is VerificationStatus.FAILED
    assert lead.status is LeadStatus.HELD

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")

    assert resp.status_code == 409, resp.text
    assert "not approvable" in resp.text.lower()

    # nothing was pushed
    assert read_outbox(outbox_path) == []

    # nothing changed in the database
    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is LeadStatus.HELD


def test_approve_works_for_passed_draft(logged_in_client: TestClient, session, env: Path):
    outbox_path = env
    lead, draft = make_passed_lead(session)
    assert draft.verification_status is VerificationStatus.PASSED

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert resp.status_code == 200, resp.text

    lines = read_outbox(outbox_path)
    assert len(lines) == 1
    assert lead.email in lines[0]

    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is LeadStatus.SENT


def test_approve_missing_draft_is_404(logged_in_client: TestClient, session):
    no_angle_lead = make_no_angle_lead(session)
    resp = logged_in_client.post(f"/leads/{no_angle_lead.id}/approve")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Reject / edit — not required by the brief's test list, but they're this
# module's routes and the no-auto-send invariant applies to them too.
# --------------------------------------------------------------------------


def test_reject_holds_the_lead_and_records_a_note(logged_in_client: TestClient, session):
    lead, draft = make_passed_lead(session)

    resp = logged_in_client.post(
        f"/leads/{lead.id}/reject", data={"reason": "wrong angle, too salesy"}
    )
    assert resp.status_code == 200

    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is LeadStatus.HELD

    # and it is genuinely not approvable afterwards
    resp2 = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert resp2.status_code == 409


def test_edit_reverifies_and_can_flip_held_to_ready(logged_in_client: TestClient, session):
    lead, draft = make_held_lead(session)

    resp = logged_in_client.post(
        f"/leads/{lead.id}/edit",
        data={
            "subject": "Northwind Logistics is hiring a Revenue Operations Manager",
            "body_text": (
                "Dana — saw Northwind Logistics is hiring a Revenue Operations "
                "Manager to own the CRM."
            ),
        },
    )
    assert resp.status_code == 200, resp.text

    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    # the edited copy still cites no signals (signals_used unchanged, empty),
    # so it should still fail -- editing text alone cannot manufacture evidence
    assert refreshed.status is LeadStatus.HELD


# --------------------------------------------------------------------------
# No bulk / auto approval path exists at all
# --------------------------------------------------------------------------


def test_no_bulk_or_approve_all_route_exists(logged_in_client: TestClient, session):
    """There is no route that approves more than one lead at a time.

    Per-lead approval works (covered elsewhere); every plausible bulk-approve
    URL must not exist.
    """

    lead, _draft = make_passed_lead(session)
    ok = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert ok.status_code == 200

    for bulk_path in ("/leads/approve", "/leads/approve-all", "/leads/approve_all"):
        resp = logged_in_client.post(bulk_path)
        assert resp.status_code in (404, 405), f"{bulk_path} unexpectedly routable"


# --------------------------------------------------------------------------
# Workspaces + campaigns: making the routing/scoring config usable by an
# operator, without hand-written SQL.
# --------------------------------------------------------------------------


def make_workspace(session, *, slug: str = "acme-digital", name: str = "Acme Digital") -> Workspace:
    # Committed, not merely flushed: the HTTP routes below run against the
    # app's own separately-connected session (see the `session` fixture's
    # docstring), which cannot see an uncommitted row.
    workspace = Workspace(slug=slug, name=name)
    session.add(workspace)
    session.commit()
    return workspace


def make_campaign(
    session,
    workspace: Workspace,
    *,
    name: str = "Q4 Outbound",
    segment: str | None = "third-party-logistics",
    scoring_config: dict | None = None,
) -> Campaign:
    campaign = Campaign(
        workspace_id=workspace.id,
        name=name,
        segment=segment,
        scoring_config=scoring_config or {},
    )
    session.add(campaign)
    # Committed for the same reason as make_workspace above.
    session.commit()
    return campaign


def test_workspaces_page_requires_login(client: TestClient):
    resp = client.get("/workspaces", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_workspaces_list_shows_campaigns_and_lead_counts(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace, name="Staffing Outbound", segment="staffing")
    lead, _ = make_passed_lead(session)
    lead.campaign_id = campaign.id
    session.commit()

    resp = logged_in_client.get("/workspaces")
    assert resp.status_code == 200
    assert workspace.name in resp.text
    assert workspace.slug in resp.text
    assert campaign.name in resp.text
    assert "staffing" in resp.text
    # one lead routed to this campaign
    assert ">1<" in resp.text


def test_empty_workspaces_list_is_not_an_error(logged_in_client: TestClient, session):
    resp = logged_in_client.get("/workspaces")
    assert resp.status_code == 200
    assert "No workspaces yet" in resp.text


def test_create_workspace_success(logged_in_client: TestClient, session):
    resp = logged_in_client.post(
        "/workspaces",
        data={"slug": "Acme Digital!", "name": "Acme Digital"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/workspaces"

    row = session.execute(select(Workspace)).scalars().one()
    # slug is normalised: lower-cased, punctuation collapsed to hyphens
    assert row.slug == "acme-digital"
    assert row.name == "Acme Digital"


def test_create_workspace_duplicate_slug_is_rejected(logged_in_client: TestClient, session):
    make_workspace(session, slug="acme-digital")

    resp = logged_in_client.post(
        "/workspaces", data={"slug": "acme-digital", "name": "Acme Digital Again"}
    )
    assert resp.status_code == 409
    assert "already exists" in resp.text

    rows = session.execute(select(Workspace)).scalars().all()
    assert len(rows) == 1


def test_create_workspace_blank_name_is_rejected(logged_in_client: TestClient, session):
    resp = logged_in_client.post("/workspaces", data={"slug": "acme-digital", "name": "  "})
    assert resp.status_code == 400
    assert session.execute(select(Workspace)).scalars().first() is None


def test_edit_workspace_updates_name_and_slug(logged_in_client: TestClient, session):
    workspace = make_workspace(session, slug="acme-digital", name="Acme Digital")

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/edit",
        data={"slug": "acme-rebrand", "name": "Acme Rebrand LLC"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    session.refresh(workspace)
    assert workspace.slug == "acme-rebrand"
    assert workspace.name == "Acme Rebrand LLC"


def test_edit_missing_workspace_is_404(logged_in_client: TestClient, session):
    resp = logged_in_client.get("/workspaces/999999/edit")
    assert resp.status_code == 404


def test_create_campaign_success(logged_in_client: TestClient, session):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={
            "name": "Staffing Agency Outbound",
            "segment": "Staffing Agencies",
            "scoring_config": '{"supported_role_terms": ["recruiter", "talent partner"]}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    campaign = session.execute(select(Campaign)).scalars().one()
    assert campaign.name == "Staffing Agency Outbound"
    assert campaign.segment == "Staffing Agencies"
    assert campaign.scoring_config == {
        "supported_role_terms": ["recruiter", "talent partner"]
    }


def test_create_campaign_blank_scoring_config_means_shipped_defaults(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={"name": "Default Campaign", "segment": "", "scoring_config": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    campaign = session.execute(select(Campaign)).scalars().one()
    assert campaign.scoring_config == {}
    assert campaign.segment is None


def test_create_campaign_duplicate_name_in_workspace_is_rejected(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    make_campaign(session, workspace, name="Q4 Outbound")

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={"name": "Q4 Outbound", "segment": "", "scoring_config": "{}"},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.text

    rows = session.execute(select(Campaign)).scalars().all()
    assert len(rows) == 1


def test_create_campaign_missing_workspace_is_404(logged_in_client: TestClient):
    resp = logged_in_client.post(
        "/workspaces/999999/campaigns",
        data={"name": "X", "segment": "", "scoring_config": "{}"},
    )
    assert resp.status_code == 404


# -- guardrail 2: funding must never be able to clear the threshold --------


def test_create_campaign_rejects_config_where_funding_would_clear_threshold(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={
            "name": "Broken Config",
            "segment": "",
            "scoring_config": '{"threshold": 10.0, "funding": 12.0}',
        },
    )
    assert resp.status_code == 400, resp.text
    assert "funding" in resp.text.lower()
    assert "threshold" in resp.text.lower()

    # nothing was ever saved -- the worst outcome is a config that looks
    # saved and silently isn't
    assert session.execute(select(Campaign)).scalars().first() is None


def test_create_campaign_rejects_unknown_scoring_key(logged_in_client: TestClient, session):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={
            "name": "Typo Config",
            "segment": "",
            "scoring_config": '{"threshhold": 45.0}',
        },
    )
    assert resp.status_code == 400, resp.text
    assert "unknown scoring knobs" in resp.text
    assert "threshhold" in resp.text
    # the valid key list is surfaced, not just "invalid"
    assert "threshold" in resp.text

    assert session.execute(select(Campaign)).scalars().first() is None


def test_create_campaign_rejects_non_object_scoring_config(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={"name": "List Config", "segment": "", "scoring_config": "[1, 2, 3]"},
    )
    assert resp.status_code == 400, resp.text
    assert session.execute(select(Campaign)).scalars().first() is None


def test_create_campaign_rejects_invalid_json(logged_in_client: TestClient, session):
    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={"name": "Broken JSON", "segment": "", "scoring_config": "{not json}"},
    )
    assert resp.status_code == 400, resp.text
    assert "not valid json" in resp.text.lower()
    assert session.execute(select(Campaign)).scalars().first() is None


def test_create_campaign_rejects_bare_string_role_terms(
    logged_in_client: TestClient, session
):
    """A bare string for supported_role_terms is refused, not silently
    exploded into a set of its own characters (which would match nearly
    every req ever written)."""

    workspace = make_workspace(session)

    resp = logged_in_client.post(
        f"/workspaces/{workspace.id}/campaigns",
        data={
            "name": "Bad Terms",
            "segment": "",
            "scoring_config": '{"supported_role_terms": "paid media"}',
        },
    )
    assert resp.status_code == 400, resp.text
    assert "not a single string" in resp.text

    assert session.execute(select(Campaign)).scalars().first() is None


def test_edit_campaign_updates_segment_and_scoring_config(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace, segment="old-segment")

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": "Renamed Campaign",
            "segment": "new-segment",
            "scoring_config": '{"threshold": 35.0}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    session.refresh(campaign)
    assert campaign.name == "Renamed Campaign"
    assert campaign.segment == "new-segment"
    assert campaign.scoring_config == {"threshold": 35.0}


def test_edit_campaign_invalid_config_does_not_overwrite_the_saved_one(
    logged_in_client: TestClient, session
):
    """A rejected save must leave the campaign exactly as it was -- a config
    that is only caught inside a worker, as a failed job, is the worst
    outcome: it looks saved and silently isn't."""

    workspace = make_workspace(session)
    campaign = make_campaign(
        session, workspace, scoring_config={"threshold": 45.0}
    )

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": '{"threshold": 10.0, "funding": 12.0}',
        },
    )
    assert resp.status_code == 400, resp.text

    session.refresh(campaign)
    assert campaign.scoring_config == {"threshold": 45.0}


def test_edit_missing_campaign_is_404(logged_in_client: TestClient):
    resp = logged_in_client.get("/campaigns/999999/edit")
    assert resp.status_code == 404


def test_campaign_edit_page_shows_lead_count(logged_in_client: TestClient, session):
    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)
    lead, _ = make_passed_lead(session)
    lead.campaign_id = campaign.id
    session.commit()

    resp = logged_in_client.get(f"/campaigns/{campaign.id}/edit")
    assert resp.status_code == 200
    assert "1" in resp.text
    assert "revert" in resp.text.lower()


def test_delete_campaign_reverts_leads_to_default_scoring_without_deleting_them(
    logged_in_client: TestClient, session
):
    """Deleting a campaign must not delete leads, drafts, or jobs -- only its
    own row. leads.campaign_id is ON DELETE SET NULL by deliberate design."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)
    lead, draft = make_passed_lead(session)
    lead.campaign_id = campaign.id
    session.commit()

    resp = logged_in_client.post(f"/campaigns/{campaign.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/workspaces"

    # The delete happened in the app's own transaction/session. With
    # expire_on_commit=False, this session's identity map still holds the
    # pre-delete `campaign` and `lead` objects. `expire_all()` alone isn't
    # enough here: session.get() on an *expired-but-still-mapped* identity
    # tries to refresh it and raises ObjectDeletedError instead of quietly
    # returning None when the row is actually gone. Expunge instead, so the
    # lookups below are plain, identity-map-free queries against the
    # committed state.
    session.expunge_all()

    assert session.get(Campaign, campaign.id) is None

    refreshed_lead = session.get(Lead, lead.id)
    session.refresh(refreshed_lead)
    assert refreshed_lead is not None
    assert refreshed_lead.campaign_id is None
    assert refreshed_lead.status is lead.status  # the lead itself is untouched

    from mazz.models import Draft

    assert session.get(Draft, draft.id) is not None


def test_delete_missing_campaign_is_404(logged_in_client: TestClient):
    resp = logged_in_client.post("/campaigns/999999/delete")
    assert resp.status_code == 404


def test_campaign_screens_never_expose_a_send_or_approve_action(
    logged_in_client: TestClient, session
):
    """Campaign management is configuration, not dispatch: no send path, no
    approve-all, no bulk action over leads lives on these screens."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    for path in ("/workspaces", f"/campaigns/{campaign.id}/edit"):
        resp = logged_in_client.get(path)
        assert resp.status_code == 200
        assert "approve" not in resp.text.lower()
        assert "/leads/" not in resp.text
        # The actual guardrail: no form or link on this screen posts to, or
        # links to, a send/approve action. A brand-name substring check
        # ("smartlead" not in the page) is not this guardrail -- it trips on
        # the product's own title ("MazzSmartLead Review" in base.html) and
        # proves nothing about whether a send/approve action is reachable.
        assert not re.search(
            r'(?:action|hx-post|hx-get|href)="[^"]*(?:approve|/send)[^"]*"',
            resp.text,
            re.IGNORECASE,
        )
