"""WP2 launch compliance plumbing: every control here fails closed.

Footer address per workspace (confirmed or refused), the public unsubscribe
route and its host isolation, the placeholder guard, the removal page,
delete-by-person tombstones, workspace-scoped suppression, and the
SEND_ENABLED kill switch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.ingest.csv_import import SuppressionChecker, email_sha256, import_rows
from mazz.models import (
    AuditEvent,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    SentMessage,
    SuppressionEntry,
    VerificationStatus,
)
from mazz.pipeline.draft import (
    MissingSenderAddress,
    render_body,
    render_footer,
    unsubscribe_token,
)
from mazz.pipeline.queue import PermanentJobError, process_next
from mazz.pipeline.send import SendDisabled, send_lead
from mazz.privacy import delete_person
from mazz.web.app import create_app
from test_web import ensure_account, login

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Env Fallback Ltd, 9 Env Road, Envtown")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DRYRUN_DIR", str(tmp_path / "dryrun"))
    reload_settings()
    yield tmp_path
    reload_settings()


@pytest.fixture()
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def routed_lead(session, *, email="ada@firm-law.com", status=LeadStatus.READY, **ws):
    campaign = h.make_campaign(session)
    for key, value in ws.items():
        setattr(campaign.workspace, key, value)
    lead = h.make_lead(
        session, email=email, company_domain="firm-law.com", campaign_id=campaign.id, status=status
    )
    session.flush()
    return lead


def approved_with_draft(session, lead, **draft_kw):
    lead.status = LeadStatus.APPROVED
    draft = h.make_draft(
        session,
        lead,
        subject=draft_kw.pop("subject", "Quick Question"),
        body=render_body(draft_kw.pop("text", "Hey Ada,\n\nWorth a look?"), lead),
        verification_status=VerificationStatus.PASSED,
    )
    session.add(Mailbox(email_address="sean@getsearchsitemarketing.com",
                        display_name="Sean Gough", credential_ref="m1", min_spacing_seconds=0))
    session.flush()
    return draft


# --------------------------------------------------------------------------
# Footer address
# --------------------------------------------------------------------------


def test_an_unconfirmed_workspace_address_refuses_to_render(session):
    lead = routed_lead(session, postal_address="Search Site Marketing, Inc, 137 Erie St W, Blauvelt, NY 10913",
                       postal_address_confirmed_at=None, postal_address_confirmed_by=None)
    with pytest.raises(MissingSenderAddress, match="not been confirmed"):
        render_footer(lead)


@pytest.mark.parametrize("address", ["TBD", "123 Example Road, Nowhere", "Lorem, 4 Street", ""])
def test_placeholder_like_addresses_refuse_even_when_confirmed(session, address):
    lead = routed_lead(session, postal_address=address)
    with pytest.raises(MissingSenderAddress):
        render_footer(lead)


def test_a_confirmed_address_renders_with_the_clients_opt_out_line(session):
    lead = routed_lead(session)
    lines = render_footer(lead).splitlines()
    assert lines[:2] == [
        h.CONFIRMED_ADDRESS["postal_address"],
        "If this isn't relevant, just let me know and I won't follow up.",
    ]
    assert lines[2].startswith("Unsubscribe (one click): ") and len(lines) == 3
    assert "Env Fallback" not in "\n".join(lines)


# --------------------------------------------------------------------------
# /u/<token>
# --------------------------------------------------------------------------


def test_get_confirms_without_suppressing_and_post_suppresses_globally(session, client):
    lead = routed_lead(session)
    session.commit()
    token = unsubscribe_token(lead)

    page = client.get(f"/u/{token}")
    assert page.status_code == 200 and "Unsubscribe" in page.text
    assert "ada@firm-law.com" not in page.text  # masked
    assert session.execute(select(SuppressionEntry)).first() is None

    # RFC 8058 one-click: a mail client POSTs this body, no cookie, no CSRF token.
    done = client.post(f"/u/{token}", data={"List-Unsubscribe": "One-Click"})
    assert done.status_code == 200 and "unsubscribed" in done.text.lower()

    session.expire_all()
    entry = session.execute(select(SuppressionEntry)).scalar_one()
    assert entry.email == "ada@firm-law.com" and entry.workspace_id is None
    assert entry.email_sha256 == email_sha256("ada@firm-law.com")
    assert session.get(Lead, lead.id).status is LeadStatus.HELD
    event = session.execute(select(AuditEvent)).scalar_one()
    assert (event.action, event.user_email) == ("unsubscribe", "system")

    client.post(f"/u/{token}")  # idempotent
    assert len(session.execute(select(SuppressionEntry)).all()) == 1


def test_an_unknown_token_is_a_dead_link_not_an_error(session, client):
    resp = client.post("/u/not-a-real-token")
    assert resp.status_code == 200 and "no longer active" in resp.text
    assert session.execute(select(SuppressionEntry)).first() is None


def test_the_unsubscribe_host_serves_only_u_and_health(session, client, monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_HOST", "outreach.getsearchsitemarketing.com")
    reload_settings()
    public = {"host": "outreach.getsearchsitemarketing.com"}
    for path in ("/login", "/leads", "/removals", "/static/style.css", "/"):
        assert client.get(path, headers=public, follow_redirects=False).status_code == 404, path
    assert client.get("/u/whatever", headers=public).status_code == 200
    assert client.get("/health", headers=public).json() == {"status": "ok"}
    assert client.get("/login").status_code == 200  # the app host is untouched


# --------------------------------------------------------------------------
# Placeholder guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subject,text", [
    ("Quick Question", "Hey Ada, are you free {day} at {time}?"),
    ("", "Hey Ada, worth a look?"),
])
def test_unfilled_merge_fields_or_no_subject_block_approve_and_send(session, client, subject, text):
    lead = routed_lead(session)
    approved_with_draft(session, lead, subject=subject, text=text)
    lead.status = LeadStatus.READY
    session.commit()
    ensure_account("client")
    login(client, "client")

    assert client.post(f"/leads/{lead.id}/approve").status_code == 409
    session.expire_all()
    lead = session.get(Lead, lead.id)
    assert lead.status is LeadStatus.READY

    lead.status = LeadStatus.APPROVED
    session.flush()
    with pytest.raises(PermanentJobError, match="merge fields or no subject"):
        send_lead(session, lead, now=NOW)
    assert session.execute(select(SentMessage)).first() is None


# --------------------------------------------------------------------------
# Removals, tombstones, scoped suppression
# --------------------------------------------------------------------------


def test_a_removal_takes_effect_at_once_and_is_attributed(session, client):
    lead = routed_lead(session)
    session.commit()
    ensure_account("client")
    login(client, "client")

    resp = client.post("/removals", data={"value": "ADA@firm-law.com", "reason": "asked by reply"})
    assert resp.status_code == 200 and "1 pending lead(s) held" in resp.text
    session.expire_all()
    assert session.get(Lead, lead.id).status is LeadStatus.HELD
    entry = session.execute(select(SuppressionEntry)).scalar_one()
    assert (entry.email, entry.added_by, entry.workspace_id) == ("ada@firm-law.com", "client", None)
    actions = {e.action for e in session.execute(select(AuditEvent)).scalars()}
    assert {"suppress", "hold_suppressed"} <= actions


def test_a_scoped_user_cannot_block_for_another_workspace(session, client):
    mine = h.make_campaign(session)
    other = h.make_campaign(session, name="Other")
    session.commit()
    ensure_account("sean@example.test", operator=False, workspace_ids=(mine.workspace_id,))
    login(client, "sean@example.test")

    def post(**data):
        return client.post("/removals", data=data).status_code

    assert post(value="x.com", scope=str(other.workspace_id)) == 403
    assert post(value="a@x.com", scope=str(mine.workspace_id)) == 400
    assert post(value="x.com", scope=str(mine.workspace_id)) == 200
    resp = client.post("/removals/delete-person", data={"email": "a@x.com", "confirm": "DELETE"})
    assert resp.status_code == 403


@pytest.mark.parametrize("forget_entirely", [False, True])
def test_delete_by_person_leaves_a_tombstone_that_blocks_reimport(session, forget_entirely):
    h.make_lead(session, email="gone@firm-law.com", company_domain="firm-law.com")
    session.commit()

    delete_person("gone@firm-law.com", session=session, remove_suppression=forget_entirely)
    session.commit()

    tomb = session.execute(select(SuppressionEntry)).scalar_one()
    assert tomb.email_sha256 == email_sha256("gone@firm-law.com")
    assert tomb.email == (None if forget_entirely else "gone@firm-law.com")

    report = import_rows(session, [{
        "full_name": "Gone Person", "email": "Gone@Firm-Law.com",
        "company": "Firm Law", "website": "https://firm-law.com",
    }])
    assert not report.accepted and report.rejected[0].kind == "suppressed"


def test_a_workspace_block_applies_to_that_workspace_only(session):
    a = h.make_campaign(session, name="A")
    b = h.make_campaign(session, name="B")
    session.add(SuppressionEntry(domain="client-of-sean.com", workspace_id=a.workspace_id,
                                 reason="Sean's current client"))
    lead_a = h.make_lead(session, email="p@client-of-sean.com",
                         company_domain="client-of-sean.com", campaign_id=a.id)
    lead_b = h.make_lead(session, email="q@client-of-sean.com",
                         company_domain="client-of-sean.com", campaign_id=b.id)
    session.flush()
    checker = SuppressionChecker(session)
    assert checker.check_lead(lead_a) is not None
    assert checker.check_lead(lead_b) is None
    assert checker.check("p@client-of-sean.com", "client-of-sean.com") is None  # global gate


# --------------------------------------------------------------------------
# SEND_ENABLED
# --------------------------------------------------------------------------


def test_the_kill_switch_renders_mime_and_sends_nothing(session, env, monkeypatch):
    monkeypatch.setenv("SEND_ENABLED", "false")
    monkeypatch.setenv("EMAIL_SENDER", "google")  # would be live if reached
    reload_settings()
    import mazz.pipeline.send as send_module

    def _no_sender(*a, **k):
        raise AssertionError("no sender may be built while SEND_ENABLED is false")

    monkeypatch.setattr(send_module, "get_email_sender", _no_sender)
    lead = routed_lead(session)
    draft = approved_with_draft(session, lead)
    session.commit()

    with pytest.raises(SendDisabled) as exc:
        send_lead(session, lead, now=NOW)
    eml = exc.value.path.read_text(encoding="utf-8")
    assert "ada@firm-law.com" in eml
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in eml
    assert session.execute(select(SentMessage)).first() is None
    assert lead.status is LeadStatus.APPROVED and draft.send_attempted_at is None

    # Through the queue: the job completes, nothing is booked, it is audited.
    session.add(Job(lead_id=lead.id, kind="send"))
    session.commit()
    process_next()
    session.expire_all()
    assert session.execute(select(Job)).scalar_one().status is JobStatus.DONE
    assert session.get(Lead, lead.id).status is LeadStatus.APPROVED
    assert session.execute(select(AuditEvent.action)).scalars().all() == ["dry_run_send"]
    assert len(list((env / "dryrun").glob("*.eml"))) >= 1
    assert session.execute(select(SentMessage)).first() is None


class _Live:
    provider = "google"

    def send(self, *a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("reached the wire")


def test_live_sending_refuses_a_placeholder_unsubscribe_host(session):
    lead = routed_lead(session)
    approved_with_draft(session, lead)  # rendered with the default .example host
    session.commit()
    with pytest.raises(PermanentJobError, match="not a live https unsubscribe host"):
        send_lead(session, lead, sender=_Live(), now=NOW)


def test_live_sending_refuses_an_unrouted_lead(session):
    lead = h.make_lead(session, status=LeadStatus.APPROVED)
    h.make_draft(session, lead, body=render_body("Hi.", lead),
                 verification_status=VerificationStatus.PASSED)
    session.commit()
    with pytest.raises(PermanentJobError, match="no campaign"):
        send_lead(session, lead, sender=_Live(), now=NOW)
