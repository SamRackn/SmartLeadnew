"""Go-live readiness: the client's 2026-10-14 answers and audit M1/M4/M5/M7/L10.

Seeded mailboxes at the launch cap, an unsubscribe stopping a live sequence,
the boot refusals for live sending, the preflight gate, and the login throttle.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import Settings, deployment_problems, reload_settings
from mazz.launch_copy import MAILBOXES
from mazz.models import Job, JobStatus, Lead, LeadStatus, Mailbox, MailboxStatus, Workspace
from mazz.ops import check_preflight
from mazz.pipeline.draft import unsubscribe_token
from mazz.users import seed_launch
from mazz.web.app import create_app
from test_compliance_launch import routed_lead
from test_web import TEST_PASSWORD, ensure_account

STRONG = "s" * 40
LIVE = dict(
    session_secret=STRONG,
    send_enabled=True,
    email_sender="google",
    send_window=True,  # live sending requires it; conftest turns it off
    unsubscribe_base_url="https://outreach.getsearchsitemarketing.com",
    unsubscribe_host="outreach.getsearchsitemarketing.com",
    unsubscribe_secret="u" * 40,
    app_base_url="https://app.getsearchsitemarketing.com",
    google_oauth_client_id="id",
    google_oauth_client_secret="secret",
    oauth_token_key="key",
)


@pytest.fixture()
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


def test_seed_launch_adds_the_four_mailboxes_pinned_at_the_launch_cap(session):
    seed_launch(session)
    boxes = session.execute(select(Mailbox).order_by(Mailbox.id)).scalars().all()
    assert [b.email_address for b in boxes] == list(MAILBOXES)
    assert {(b.daily_cap, b.display_name, b.status) for b in boxes} == {
        (15, "Sean Gough", MailboxStatus.NEEDS_RECONNECT)}  # never picked until connected
    assert len({b.campaign_id for b in boxes}) == 1 and boxes[0].campaign_id is not None

    boxes[0].daily_cap = 10  # a human's change survives a re-seed
    seed_launch(session)
    assert session.get(Mailbox, boxes[0].id).daily_cap == 10
    assert len(session.execute(select(Mailbox)).all()) == 4


def test_unsubscribing_mid_sequence_stops_the_sequence(session):
    lead = routed_lead(session, status=LeadStatus.SENT)
    lead.sequence_state = "active"
    session.commit()
    TestClient(create_app()).post(f"/u/{unsubscribe_token(lead)}")
    session.expire_all()
    lead = session.get(Lead, lead.id)
    assert (lead.status, lead.sequence_state) == (LeadStatus.SENT, "stopped")


@pytest.mark.parametrize("override, word", [
    ({}, None),
    ({"unsubscribe_host": ""}, "UNSUBSCRIBE_HOST"),
    ({"unsubscribe_host": "app.getsearchsitemarketing.com"}, "UNSUBSCRIBE_HOST"),
    ({"unsubscribe_base_url": "http://outreach.getsearchsitemarketing.com"}, "UNSUBSCRIBE_BASE_URL"),
    ({"unsubscribe_secret": ""}, "UNSUBSCRIBE_SECRET"),
    ({"app_base_url": ""}, "APP_BASE_URL"),
    ({"oauth_token_key": ""}, "OAUTH_TOKEN_KEY"),
])
def test_live_sending_refuses_to_boot_without_its_settings(override, word):
    problems = deployment_problems(Settings(**{**LIVE, **override}))
    if word is None:
        assert problems == []
    else:
        assert len(problems) == 1 and word in problems[0], problems


def test_preflight_lists_every_unmet_gate_the_code_can_see(session):
    lead = routed_lead(session, postal_address_confirmed_at=None)
    session.add(Mailbox(email_address=MAILBOXES[0], display_name="X", credential_ref="o",
                        daily_cap=30))
    session.add(Mailbox(email_address="clientrise@x.com", display_name="Y", credential_ref="o",
                        daily_cap=40))  # another workspace's mailbox: not ours to judge
    session.add(Job(lead_id=lead.id, kind="send", status=JobStatus.PENDING))
    session.flush()
    codes = {a.code for a in check_preflight(session, settings=Settings(session_secret=STRONG))}
    assert codes == {"config", "postal_address", "mailbox_missing", "mailbox_not_connected",
                     "mailbox_cap", "send_jobs_pending"}


def test_preflight_passes_when_every_gate_is_met(session):
    seed_launch(session)
    for ws in session.execute(select(Workspace)).scalars():
        for key, value in h.CONFIRMED_ADDRESS.items():
            setattr(ws, key, value)
    for box in session.execute(select(Mailbox)).scalars():
        box.oauth_refresh_token_enc, box.status = "enc", MailboxStatus.ACTIVE
    session.flush()
    assert check_preflight(session, settings=Settings(**LIVE)) == []


def test_the_sixth_login_attempt_is_throttled_even_with_the_right_password():
    ensure_account("throttle@example.test")
    client = TestClient(create_app())
    for _ in range(5):
        bad = client.post("/login", data={"username": "throttle@example.test", "password": "nope"})
        assert bad.status_code == 401
    good = client.post("/login", data={"username": "Throttle@example.test",
                                       "password": TEST_PASSWORD}, follow_redirects=False)
    assert good.status_code == 429


def test_the_session_cookie_is_secure_when_served_over_https(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://app.getsearchsitemarketing.com")
    reload_settings()
    ensure_account("cookie@example.test")
    resp = TestClient(create_app(), base_url="https://testserver").post(
        "/login", data={"username": "cookie@example.test", "password": TEST_PASSWORD},
        follow_redirects=False)
    assert "secure" in resp.headers["set-cookie"].lower()


def test_a_draft_whose_unsubscribe_link_no_longer_resolves_is_not_sent_live(session, monkeypatch):
    from types import SimpleNamespace

    from mazz.pipeline.queue import PermanentJobError
    from mazz.pipeline.send import check_live_send_ready

    lead = routed_lead(session)
    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", "https://outreach.getsearchsitemarketing.com")
    reload_settings()
    base = "https://outreach.getsearchsitemarketing.com/u/"
    footer = "Hi.\n\n-- \nUnsubscribe (one click): "
    google = SimpleNamespace(provider="google")
    stale = h.make_draft(session, lead, body=footer + base + "0" * 32)
    with pytest.raises(PermanentJobError, match="no longer resolves"):
        check_live_send_ready(lead, stale, google)
    fresh = h.make_draft(session, lead, body=footer + base + unsubscribe_token(lead))
    check_live_send_ready(lead, fresh, google)
