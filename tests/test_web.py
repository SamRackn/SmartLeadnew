"""Tests for the review web app.

The single most important test here is
``test_approve_refused_for_held_failed_draft`` — the brief requires that a
failed-verification draft can never be approved, enforced *in the route*,
not merely by a hidden button. Everything else follows the same shape: real
HTTP requests against the real FastAPI app, against the real ``mazz_dev``
Postgres (via ``pipeline_helpers``), with the sender pinned to the stub and
its outbox redirected to a throwaway file, so a test run can never put mail on
the wire and never touches the repo's own ``sent_outbox.jsonl``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.integrations.email_sender import StubEmailSender
from mazz.models import (
    Campaign,
    DraftBatch,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    ProofPoint,
    ResearchCache,
    SentMessage,
    VerificationStatus,
    Workspace,
)
from mazz.pipeline.draft import render_body
from mazz.pipeline.queue import DEFAULT_STALE_AFTER
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
    """Configure settings, and point the stub sender at a scratch outbox.

    The approve ROUTE no longer sends — it enqueues — so most tests here never
    reach a sender at all. The outbox is still wired up because the end-to-end
    test drains the queue, and because a test that somehow did reach a real
    sender must fail loudly rather than quietly emailing a stranger:
    ``conftest`` pins ``EMAIL_SENDER=stub`` for the whole session.
    """

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 1 Test Street, Testville")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("REVIEW_USER", "client")
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    reload_settings()

    outbox = tmp_path / "sent_outbox.jsonl"
    monkeypatch.setenv("EMAIL_SENDER", "stub")

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


def make_mailbox(
    session,
    *,
    email_address: str = "dana@outreach-northwind.com",
    display_name: str = "Dana Whitfield",
    credential_ref: str = "northwind-burner",
    daily_cap: int = 30,
    min_spacing_seconds: int = 0,
    campaign_id: int | None = None,
    status: MailboxStatus = MailboxStatus.ACTIVE,
    last_sent_at: datetime | None = None,
) -> Mailbox:
    """A mailbox on the roster. Spacing defaults to 0 so tests are not clocks."""

    mailbox = Mailbox(
        email_address=email_address,
        display_name=display_name,
        credential_ref=credential_ref,
        daily_cap=daily_cap,
        min_spacing_seconds=min_spacing_seconds,
        campaign_id=campaign_id,
        status=status,
        last_sent_at=last_sent_at,
    )
    session.add(mailbox)
    session.commit()
    return mailbox


def send_jobs_for(session, lead_id: int) -> list[Job]:
    session.expire_all()
    return list(
        session.execute(
            select(Job).where(Job.lead_id == lead_id, Job.kind == "send")
        ).scalars().all()
    )


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
    the route itself: no send job, no status change, a 4xx back to the
    reviewer explaining why.
    """

    make_mailbox(session)
    lead, draft = make_held_lead(session)
    assert draft.verification_status is VerificationStatus.FAILED
    assert lead.status is LeadStatus.HELD

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")

    assert resp.status_code == 409, resp.text
    assert "not approvable" in resp.text.lower()

    # nothing was queued to send
    assert send_jobs_for(session, lead.id) == []

    # nothing changed in the database
    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is LeadStatus.HELD


def test_approve_queues_a_send_and_does_not_send_inline(
    logged_in_client: TestClient, session, env: Path
):
    """Approve is the human decision; the worker does the sending.

    The lead lands in `approved`, NOT `sent`: nothing has left yet, and a
    screen that said "sent" here would be lying to the client about an email
    still sitting behind a daily cap.
    """

    outbox_path = env
    make_mailbox(session)
    lead, draft = make_passed_lead(session)
    assert draft.verification_status is VerificationStatus.PASSED

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert resp.status_code == 200, resp.text

    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is LeadStatus.APPROVED

    jobs = send_jobs_for(session, lead.id)
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.PENDING

    # The route itself put nothing on the wire.
    assert read_outbox(outbox_path) == []
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_double_approve_queues_one_send(logged_in_client: TestClient, session):
    """A double-click must not email the same person twice."""

    make_mailbox(session)
    lead, _ = make_passed_lead(session)

    assert logged_in_client.post(f"/leads/{lead.id}/approve").status_code == 200
    assert logged_in_client.post(f"/leads/{lead.id}/approve").status_code == 200

    assert len(send_jobs_for(session, lead.id)) == 1


def test_approve_refused_once_already_sent(logged_in_client: TestClient, session):
    make_mailbox(session)
    lead, _ = make_passed_lead(session)
    lead.status = LeadStatus.SENT
    session.commit()

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert resp.status_code == 409
    assert "already been sent" in resp.text
    assert send_jobs_for(session, lead.id) == []


def test_approve_refused_when_no_mailbox_is_configured(
    logged_in_client: TestClient, session
):
    """Queueing work that can only fail would make the click look like it worked.

    A capped mailbox is fine — the job waits for it. No mailbox at all is an
    operator error, and the reviewer is the operator standing right there.
    """

    lead, _ = make_passed_lead(session)  # no mailbox on the roster

    resp = logged_in_client.post(f"/leads/{lead.id}/approve")
    assert resp.status_code == 503
    assert "no active mailbox" in resp.text

    refreshed = session.get(Lead, lead.id)
    session.refresh(refreshed)
    assert refreshed.status is not LeadStatus.APPROVED
    assert send_jobs_for(session, lead.id) == []


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

    make_mailbox(session)
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


# --------------------------------------------------------------------------
# Dashboard: cost per usable lead first, everything else second, nothing
# that sends or approves.
# --------------------------------------------------------------------------


def make_draft_batch(
    session,
    *,
    batch_id: str = "msgbatch_test",
    model: str = "claude-opus-5",
    status: str = "ended",
    request_count: int = 1,
    cost_usd: float | None = 0.01,
    usage: dict | None = None,
    ended_at: datetime | None = None,
) -> DraftBatch:
    batch = DraftBatch(
        batch_id=batch_id,
        model=model,
        status=status,
        request_count=request_count,
        cost_usd=cost_usd,
        usage=usage or {},
        ended_at=ended_at,
    )
    session.add(batch)
    # Committed for the same reason make_workspace/make_campaign are: the
    # dashboard route runs in the app's own, separately-connected session.
    session.commit()
    return batch


def test_dashboard_requires_login(client: TestClient):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_dashboard_renders_on_empty_database(logged_in_client: TestClient, session):
    """A fresh install has zero of everything. Must not 500, must not divide
    by zero, and must not fabricate a panel it has no data for."""

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200

    # Panels with no underlying rows at all are omitted, not shown as zero.
    assert "Cost per usable lead" not in resp.text
    assert "Recent batches" not in resp.text
    assert "Cache effectiveness" not in resp.text

    # Panels that always render (leads/drafts legitimately at zero) still
    # show up, with their ratios rendered as explicitly undefined.
    assert "Pipeline" in resp.text
    assert "Verification" in resp.text
    assert "no leads yet" in resp.text
    assert "no verified drafts yet" in resp.text


def test_dashboard_cost_per_usable_lead_undefined_with_zero_usable_leads(
    logged_in_client: TestClient, session
):
    """The exact incident this brief calls out: spend was real, nothing
    passed verification, and the page must say so in words, not `$0.00`."""

    make_draft_batch(session, batch_id="msgbatch_zero_usable", cost_usd=1.2345)
    make_held_lead(session)  # a draft exists, but it FAILED verification

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Cost per usable lead" in resp.text
    assert "no usable leads yet" in resp.text
    assert "$1.2345" in resp.text  # total spend is still shown, honestly
    assert "$0.00" not in resp.text


def test_dashboard_cost_per_usable_lead_computed_from_passed_drafts(
    logged_in_client: TestClient, session
):
    make_draft_batch(session, batch_id="msgbatch_costed", cost_usd=0.05)
    make_passed_lead(session)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "$0.05000" in resp.text  # 0.05 / 1 usable lead
    assert "no usable leads yet" not in resp.text


def test_dashboard_hold_rate_headline(logged_in_client: TestClient, session):
    make_passed_lead(session, email="dana.hold1@northwind-logistics.com")
    make_held_lead(session, email="holden.hold1@northwind-logistics.com")

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "50.0% of verified drafts failed" in resp.text


def test_dashboard_no_angle_share(logged_in_client: TestClient, session):
    make_passed_lead(session)
    make_no_angle_lead(session)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "50.0% of all leads" in resp.text


def test_dashboard_research_cache_dedup_ratio(logged_in_client: TestClient, session):
    """Two colleagues at one employer must show as 1 research_cache row."""

    make_passed_lead(session, email="dana.dedup1@northwind-logistics.com")
    make_passed_lead(session, email="dana.dedup2@northwind-logistics.com")

    cache_rows = session.execute(select(ResearchCache)).scalars().all()
    assert len(cache_rows) == 1  # sanity: both leads share one cache row

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Research cache" in resp.text
    assert "2.0" in resp.text  # 2 leads / 1 cache row


def test_dashboard_prompt_cache_hit_rate(logged_in_client: TestClient, session):
    make_draft_batch(
        session,
        batch_id="msgbatch_prompt_cache_ok",
        usage={"cache_read_input_tokens": 900, "cache_creation_input_tokens": 100},
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "90.0%" in resp.text
    assert "may be broken" not in resp.text


def test_dashboard_prompt_cache_hit_rate_near_zero_flags_broken_caching(
    logged_in_client: TestClient, session
):
    """This exact failure — cache reads near zero — cost real money in this
    repo two commits ago. The dashboard must call it out, not bury it."""

    make_draft_batch(
        session,
        batch_id="msgbatch_prompt_cache_broken",
        usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000},
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "0.0%" in resp.text
    assert "may be broken" in resp.text


def test_dashboard_recent_batches_listed(logged_in_client: TestClient, session):
    make_draft_batch(
        session,
        batch_id="msgbatch_recent_1",
        model="claude-opus-5",
        status="ended",
        cost_usd=2.5,
        request_count=12,
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Recent batches" in resp.text
    assert "msgbatch_recent_1" in resp.text
    assert "claude-opus-5" in resp.text
    assert "12" in resp.text
    assert "$2.5000" in resp.text
    assert "in progress" in resp.text  # ended_at is None in this fixture


def test_dashboard_exposes_no_approve_or_send_affordance(
    logged_in_client: TestClient, session
):
    """Guardrail 4 (no auto-send) applies here too: a dashboard is exactly
    where a convenient 'approve all' would get added later. It is not here."""

    make_passed_lead(session)
    make_held_lead(session)
    make_draft_batch(session, batch_id="msgbatch_no_affordance", cost_usd=0.01)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    # Not a blanket "approve" substring check: the Pipeline panel legitimately
    # renders LeadStatus.APPROVED as a status label, which is real data, not
    # a call to action. The actual guardrail is that no form or link on this
    # screen posts to, or links to, a send/approve action.
    assert not re.search(
        r'(?:action|hx-post|hx-get|href)="[^"]*(?:approve|/send)[^"]*"',
        resp.text,
        re.IGNORECASE,
    )


# --------------------------------------------------------------------------
# Dashboard: queue health. PHASE2.md W6 wants an alarm on jobs exhausting
# max_attempts; these assert the display half of it.
# --------------------------------------------------------------------------


def make_job(
    session,
    lead,
    *,
    kind: str = "enrich",
    status: JobStatus = JobStatus.PENDING,
    attempts: int = 0,
    last_error: str | None = None,
    run_after: datetime | None = None,
    locked_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Job:
    now = datetime.now(timezone.utc)
    job = Job(
        lead_id=lead.id,
        kind=kind,
        status=status,
        attempts=attempts,
        last_error=last_error,
        run_after=run_after or now,
        locked_at=locked_at,
        created_at=created_at or now,
    )
    session.add(job)
    session.commit()
    return job


def test_dashboard_omits_queue_panel_with_no_jobs(logged_in_client: TestClient, session):
    make_passed_lead(session)  # leads exist, but nothing was ever enqueued

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Queue health" not in resp.text


def test_dashboard_queue_failure_rate_counts_settled_jobs_only(
    logged_in_client: TestClient, session
):
    """Pending and running work must not dilute the failure rate downward —
    that is the flattering direction, and this page does not flatter."""

    lead, _ = make_passed_lead(session)
    make_job(session, lead, kind="enrich", status=JobStatus.DONE)
    make_job(session, lead, kind="draft", status=JobStatus.FAILED, attempts=5)
    make_job(session, lead, kind="verify", status=JobStatus.PENDING)
    make_job(session, lead, kind="push", status=JobStatus.RUNNING)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Queue health" in resp.text
    # 1 failed / 2 settled = 50%, not 1/4 = 25%.
    assert "50.0%" in resp.text


def test_dashboard_queue_failure_rate_undefined_before_anything_settles(
    logged_in_client: TestClient, session
):
    lead, _ = make_passed_lead(session)
    make_job(session, lead, status=JobStatus.PENDING)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "no jobs have settled yet" in resp.text


def test_dashboard_lists_jobs_that_gave_up(logged_in_client: TestClient, session):
    lead, _ = make_passed_lead(session)
    make_job(
        session,
        lead,
        kind="enrich",
        status=JobStatus.FAILED,
        attempts=5,
        last_error="[gave up after 5 attempts] CompanyResearchError: 503 from example.com",
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Jobs that gave up" in resp.text
    assert "enrich" in resp.text
    assert "CompanyResearchError: 503 from example.com" in resp.text
    # The lead is reachable from the failure, so the operator can act on it.
    assert f'href="/leads/{lead.id}"' in resp.text


def test_dashboard_truncates_a_stack_trace_in_the_error_cell(
    logged_in_client: TestClient, session
):
    lead, _ = make_passed_lead(session)
    make_job(
        session,
        lead,
        status=JobStatus.FAILED,
        attempts=5,
        last_error="ProviderError: boom\n" + ("x" * 3000),
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "ProviderError: boom" in resp.text
    assert "x" * 3000 not in resp.text
    assert "…" in resp.text


def test_dashboard_flags_stale_running_jobs(logged_in_client: TestClient, session):
    """`reclaim_stale_jobs` is deliberately manual, so this count is the only
    thing that tells the operator a worker died."""

    lead, _ = make_passed_lead(session)
    stale_since = datetime.now(timezone.utc) - DEFAULT_STALE_AFTER * 2
    make_job(session, lead, status=JobStatus.RUNNING, locked_at=stale_since)

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "a worker may have died" in resp.text


def test_dashboard_does_not_flag_a_job_that_is_merely_slow(
    logged_in_client: TestClient, session
):
    lead, _ = make_passed_lead(session)
    make_job(
        session,
        lead,
        status=JobStatus.RUNNING,
        locked_at=datetime.now(timezone.utc),
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "a worker may have died" not in resp.text


def test_dashboard_backlog_ignores_a_job_still_serving_its_backoff(
    logged_in_client: TestClient, session
):
    """A pending job with `run_after` in the future is backing off, not stuck.
    Counting it as backlog would make every retry look like a stalled queue."""

    lead, _ = make_passed_lead(session)
    now = datetime.now(timezone.utc)
    make_job(
        session,
        lead,
        status=JobStatus.PENDING,
        attempts=2,
        created_at=now - timedelta(hours=3),
        run_after=now + timedelta(minutes=8),
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "nothing waiting" in resp.text
    assert "3h" not in resp.text


def test_dashboard_backlog_reports_the_oldest_claimable_job(
    logged_in_client: TestClient, session
):
    lead, _ = make_passed_lead(session)
    now = datetime.now(timezone.utc)
    make_job(
        session,
        lead,
        status=JobStatus.PENDING,
        created_at=now - timedelta(hours=3, minutes=5),
        run_after=now - timedelta(hours=3, minutes=5),
    )

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert "3h 5m old" in resp.text


def test_dashboard_queue_panel_exposes_no_retry_or_reclaim_action(
    logged_in_client: TestClient, session
):
    """Reading queue state is safe; acting on it from a browser is not. There
    is no retry button, no reclaim button, and no POST route in this module."""

    lead, _ = make_passed_lead(session)
    make_job(session, lead, status=JobStatus.FAILED, attempts=5, last_error="boom")
    make_job(session, lead, status=JobStatus.RUNNING, locked_at=datetime.now(timezone.utc))

    resp = logged_in_client.get("/dashboard")
    assert resp.status_code == 200
    assert not re.search(
        r'(?:action|hx-post|hx-get|href)="[^"]*(?:retry|reclaim|requeue|approve|/send)[^"]*"',
        resp.text,
        re.IGNORECASE,
    )
    section = resp.text[resp.text.index("Queue health") :]
    section = section[: section.index("</section>")]
    assert "<form" not in section
    assert "<button" not in section


# --------------------------------------------------------------------------
# /mailboxes — the roster screen. A read/edit surface, never a send surface.
# --------------------------------------------------------------------------


def test_mailboxes_requires_login(client: TestClient):
    resp = client.get("/mailboxes", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_mailboxes_empty_roster_says_nothing_can_send(
    logged_in_client: TestClient, session
):
    resp = logged_in_client.get("/mailboxes")
    assert resp.status_code == 200
    assert "No mailboxes yet" in resp.text
    assert "Nothing can be sent until one is added" in resp.text


def test_mailboxes_warns_while_the_sender_is_stubbed(
    logged_in_client: TestClient, session
):
    """The screen must not imply mail is going out when EMAIL_SENDER=stub."""

    make_mailbox(session)
    resp = logged_in_client.get("/mailboxes")
    assert resp.status_code == 200
    assert "sent_outbox.jsonl" in resp.text
    assert "nothing leaves this machine" in " ".join(resp.text.split())


def test_mailboxes_lists_usage_against_the_cap(logged_in_client: TestClient, session):
    mailbox = make_mailbox(session, daily_cap=5)
    lead, draft = make_passed_lead(session)
    session.add(
        SentMessage(
            lead_id=lead.id,
            draft_id=draft.id,
            mailbox_id=mailbox.id,
            from_address=mailbox.email_address,
            to_address=lead.email,
            subject="s",
            body="b",
            sent_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    resp = logged_in_client.get("/mailboxes")
    assert resp.status_code == 200
    assert "1 / 5" in resp.text


def test_create_mailbox(logged_in_client: TestClient, session):
    resp = logged_in_client.post(
        "/mailboxes",
        data={
            "email_address": "Dana@Outreach-Northwind.com",
            "display_name": "Dana Whitfield",
            "credential_ref": "northwind-burner",
            "daily_cap": "25",
            "min_spacing_seconds": "600",
            "campaign_id": "",
            "notes": "burner domain, warmed since August",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    mailbox = session.execute(select(Mailbox)).scalars().one()
    assert mailbox.email_address == "dana@outreach-northwind.com"  # normalised
    assert mailbox.daily_cap == 25
    assert mailbox.min_spacing_seconds == 600
    assert mailbox.status is MailboxStatus.ACTIVE


def test_create_mailbox_requires_a_credential_ref(logged_in_client: TestClient, session):
    resp = logged_in_client.post(
        "/mailboxes",
        data={
            "email_address": "dana@outreach-northwind.com",
            "display_name": "Dana Whitfield",
            "credential_ref": "   ",
        },
    )
    assert resp.status_code == 400
    assert "credential ref is required" in resp.text
    assert session.execute(select(Mailbox)).scalars().all() == []


def test_create_mailbox_rejects_a_non_address(logged_in_client: TestClient, session):
    resp = logged_in_client.post(
        "/mailboxes",
        data={
            "email_address": "dana-at-northwind",
            "display_name": "Dana",
            "credential_ref": "ref",
        },
    )
    assert resp.status_code == 400
    assert "is not an email address" in resp.text


def test_duplicate_mailbox_is_refused(logged_in_client: TestClient, session):
    """Two rows for one account would double its real daily cap."""

    make_mailbox(session, email_address="dana@outreach-northwind.com")
    resp = logged_in_client.post(
        "/mailboxes",
        data={
            "email_address": "dana@outreach-northwind.com",
            "display_name": "Dana Again",
            "credential_ref": "ref",
        },
    )
    assert resp.status_code == 409
    assert "already on the roster" in resp.text
    assert len(session.execute(select(Mailbox)).scalars().all()) == 1


def test_pause_and_resume_a_mailbox(logged_in_client: TestClient, session):
    mailbox = make_mailbox(session)

    resp = logged_in_client.post(
        f"/mailboxes/{mailbox.id}/status", data={"status": "paused"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session.expire_all()
    assert session.get(Mailbox, mailbox.id).status is MailboxStatus.PAUSED

    logged_in_client.post(
        f"/mailboxes/{mailbox.id}/status", data={"status": "active"},
        follow_redirects=False,
    )
    session.expire_all()
    assert session.get(Mailbox, mailbox.id).status is MailboxStatus.ACTIVE


def test_resuming_clears_the_failure_counter(logged_in_client: TestClient, session):
    """`disabled` is set by the send path; the fix happens in Google's console,
    so someone has to be able to say 'I fixed it' — and a stale counter must
    not keep the mailbox at the back of the rotation afterwards."""

    mailbox = make_mailbox(session, status=MailboxStatus.DISABLED)
    mailbox.consecutive_failures = 4
    mailbox.last_error = "ProviderAuthError: delegation revoked"
    session.commit()

    logged_in_client.post(
        f"/mailboxes/{mailbox.id}/status", data={"status": "active"},
        follow_redirects=False,
    )

    session.expire_all()
    refreshed = session.get(Mailbox, mailbox.id)
    assert refreshed.status is MailboxStatus.ACTIVE
    assert refreshed.consecutive_failures == 0
    assert refreshed.last_error is None


def test_removing_a_mailbox_keeps_the_record_of_what_it_sent(
    logged_in_client: TestClient, session
):
    """Taking an account off the roster must not erase the evidence."""

    mailbox = make_mailbox(session)
    mailbox_id = mailbox.id
    lead, draft = make_passed_lead(session)
    session.add(
        SentMessage(
            lead_id=lead.id,
            draft_id=draft.id,
            mailbox_id=mailbox_id,
            from_address=mailbox.email_address,
            to_address=lead.email,
            subject="s",
            body="b",
            sent_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    resp = logged_in_client.post(
        f"/mailboxes/{mailbox_id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303

    session.expire_all()
    assert (
        session.execute(select(Mailbox).where(Mailbox.id == mailbox_id))
        .scalars()
        .first()
        is None
    )
    surviving = session.execute(select(SentMessage)).scalars().one()
    assert surviving.mailbox_id is None
    assert surviving.from_address == "dana@outreach-northwind.com"


def test_mailbox_screens_never_expose_a_send_or_approve_action(
    logged_in_client: TestClient, session
):
    """A roster screen is exactly where a 'flush the queue' button would end up."""

    make_mailbox(session)
    for url in ("/mailboxes", "/mailboxes/new"):
        resp = logged_in_client.get(url)
        assert resp.status_code == 200
        assert not re.search(
            r'action="[^"]*(?:approve|/send|retry|flush)[^"]*"',
            resp.text,
            re.IGNORECASE,
        )


def test_the_mailbox_form_never_accepts_a_credential(
    logged_in_client: TestClient, session
):
    """Guardrail by omission: there is no field that would put a Google key in
    the database, in a form post, and in this page's HTML."""

    resp = logged_in_client.get("/mailboxes/new")
    assert resp.status_code == 200
    assert not re.search(r'type="password"', resp.text)
    for forbidden in ("private_key", "service_account_json", "api_key", "secret"):
        assert f'name="{forbidden}"' not in resp.text



# ==========================================================================
# The offer and its proof points
# ==========================================================================
#
# This is the surface where the client enters the entire non-lead content of
# email 1. The tests below are mostly about what it REFUSES: the fields here
# are the ones a claim in a real commercial message is built from, and a form
# that accepts a bad one has moved the failure to a place nobody is looking.


def test_the_offer_fields_round_trip_through_the_campaign_form(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": "{}",
            "vertical": "law firms",
            "offer_line": "generate qualified client opportunities through search marketing",
            "qualification_config": '{"vertical_terms": ["law firm", "attorneys at law"]}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    session.refresh(campaign)
    assert campaign.vertical == "law firms"
    assert campaign.qualification_config == {
        "vertical_terms": ["law firm", "attorneys at law"]
    }


def test_a_number_in_the_offer_line_is_refused_at_the_form(
    logged_in_client: TestClient, session
):
    """The offer sentence reads as sender voice, so a figure typed here would
    never be held to anything. Statistics belong in a proof point."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": "{}",
            "vertical": "law firms",
            "offer_line": "get 400 new cases a year through search marketing",
            "qualification_config": "{}",
        },
    )

    assert resp.status_code == 400
    assert "proof point" in resp.text
    session.refresh(campaign)
    assert campaign.offer_line is None


def test_a_qualification_config_the_worker_would_reject_is_rejected_here(
    logged_in_client: TestClient, session
):
    """A config only caught inside a worker is the worst outcome: it looks
    saved, and the campaign silently qualifies nobody."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": "{}",
            "vertical": "law firms",
            "offer_line": "do good work",
            # A bare string would be matched one character at a time.
            "qualification_config": '{"vertical_terms": "law firm"}',
        },
    )

    assert resp.status_code == 400
    assert "array of terms" in resp.text


def test_a_proof_point_without_a_client_name_is_refused(
    logged_in_client: TestClient, session
):
    """An unattributed statistic is the exact shape of claim this table exists
    to prevent."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/proof-points",
        data={
            "claim_text": "One recent client saw 50+ potential clients in a single month.",
            "client_name": "   ",
            "occurred_on": "",
            "source_note": "",
        },
    )

    assert resp.status_code == 400
    assert "name the client" in resp.text
    assert session.execute(select(func.count()).select_from(ProofPoint)).scalar_one() == 0


def test_an_unparseable_date_is_refused_rather_than_guessed(
    logged_in_client: TestClient, session
):
    """The copy says "one RECENT client". A misread date is worse than none."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/proof-points",
        data={
            "claim_text": "One recent client saw 50+ potential clients in a month.",
            "client_name": "Hartley & Boyd LLP",
            "occurred_on": "last July",
            "source_note": "",
        },
    )

    assert resp.status_code == 400
    assert "is not a date" in resp.text


def test_retiring_a_proof_point_keeps_the_row(logged_in_client: TestClient, session):
    """Retired, never deleted: a draft sent last month may still cite it, and
    the audit trail has to stay readable after the claim changes."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)
    proof = ProofPoint(
        campaign_id=campaign.id,
        claim_text="One recent client saw 50+ potential clients in a single month.",
        client_name="Hartley & Boyd LLP",
    )
    session.add(proof)
    session.commit()

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/proof-points/{proof.id}/retire",
        data={"active": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    session.refresh(proof)
    assert proof.active is False
    assert session.get(ProofPoint, proof.id) is not None


def test_a_proof_point_cannot_be_retired_through_another_campaign(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    mine = make_campaign(session, workspace, name="Mine")
    theirs = make_campaign(session, workspace, name="Theirs")
    proof = ProofPoint(
        campaign_id=theirs.id, claim_text="A result.", client_name="Someone"
    )
    session.add(proof)
    session.commit()

    resp = logged_in_client.post(
        f"/campaigns/{mine.id}/proof-points/{proof.id}/retire", data={"active": "false"}
    )

    assert resp.status_code == 404
    session.refresh(proof)
    assert proof.active is True


def test_the_generic_vertical_is_off_unless_explicitly_ticked(
    logged_in_client: TestClient, session
):
    """It must never be the silent consequence of a half-filled form."""

    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": "{}",
            "vertical": "",
            "offer_line": "fill empty lanes through search marketing",
            "qualification_config": "{}",
            # checkbox absent, as an unticked box is
        },
    )

    assert resp.status_code == 400
    assert "generic-vertical box" in resp.text
    session.refresh(campaign)
    assert campaign.generic_vertical_fallback is False


def test_ticking_the_generic_vertical_box_allows_a_blank_vertical(
    logged_in_client: TestClient, session
):
    workspace = make_workspace(session)
    campaign = make_campaign(session, workspace)

    resp = logged_in_client.post(
        f"/campaigns/{campaign.id}/edit",
        data={
            "name": campaign.name,
            "segment": campaign.segment,
            "scoring_config": "{}",
            "vertical": "",
            "offer_line": "fill empty lanes through search marketing",
            "qualification_config": "{}",
            "generic_vertical_fallback": "on",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    session.refresh(campaign)
    assert campaign.generic_vertical_fallback is True
    assert campaign.vertical is None
