"""WP5: per-mailbox OAuth, reply sync, deterministic classification, the stop /
pause / suppress rules, and the inbox. Gmail is faked; nothing touches Google."""

from __future__ import annotations

import base64
import types
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.integrations import google_oauth
from mazz.integrations.email_sender import GmailSender, MailboxNeedsReconnect
from mazz.integrations.gmail_reader import parse_gmail_message
from mazz.models import (
    AuditEvent,
    Job,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    Reply,
    ReplyTag,
    SentMessage,
    SuppressionEntry,
)
from mazz.pipeline import replies as R
from mazz.pipeline.mailbox import select_mailbox
from mazz.pipeline.queue import JobDeferred, PermanentJobError
from mazz.pipeline.send import send_lead
from mazz.pipeline.sequence import business_hours_between, handle_followup
from mazz.web.app import create_app
from test_sequence import (
    ET,
    TUE,
    Recording,
    drafted_and_approved,
    law_lead,
    mailbox,
    run_jobs,
    ssm_campaign,
)
from test_web import ensure_account, login

KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {
        "SESSION_SECRET": "test-session-secret",
        "OAUTH_TOKEN_KEY": KEY,
        "GOOGLE_OAUTH_CLIENT_ID": "client-id.apps.example",
        "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
        # https, so the session cookie is Secure: clients here use https://testserver.
        "APP_BASE_URL": "https://app.example.test",
    }.items():
        monkeypatch.setenv(k, v)
    reload_settings()
    yield
    reload_settings()


@pytest.fixture()
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


def gmsg(mid, thread, frm, subject, body, *, headers=None, ts=TUE, labels=("INBOX",)):
    data = base64.urlsafe_b64encode(body.encode()).decode()
    hdrs = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    hdrs += [{"name": k, "value": v} for k, v in (headers or {}).items()]
    return {
        "id": mid, "threadId": thread, "internalDate": str(int(ts.timestamp() * 1000)),
        "labelIds": list(labels),
        "payload": {"mimeType": "text/plain", "headers": hdrs, "body": {"data": data}},
    }


def sent_lead(session, tmp_path, **kw):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign, **kw)
    send_lead(session, lead, sender=Recording(tmp_path), now=TUE)
    session.commit()
    box = session.execute(select(Mailbox)).scalar_one()
    sent = session.execute(select(SentMessage)).scalars().first()
    return lead, box, sent


def followup_draft(session, tmp_path):
    """A sent lead whose follow-up 1 has been drafted and verified (READY)."""

    lead, box, sent = sent_lead(session, tmp_path)
    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    job.run_after = h.utcnow() - timedelta(minutes=1)
    run_jobs(session)
    return session.get(Lead, lead.id), box, sent


def inbound(session, box, sent, lead, body, *, mid="m1", subject="Re: Quick Question",
            headers=None, ts=None, frm=None):
    msg = parse_gmail_message(gmsg(mid, sent.provider_thread_id, frm or lead.email, subject, body,
                                   headers=headers, ts=ts or TUE + timedelta(hours=1)))
    report = R.SyncReport()
    R.process_message(session, box, msg, report)
    session.flush()
    return report


# --------------------------------------------------------------------------
# Classification (no model, no DB)
# --------------------------------------------------------------------------

QUOTED = (
    "On Tue, Oct 6, 2026 at 11:00 AM Sean Gough <sean@getsearchsitemarketing.com> wrote:\n"
    "> Hey Louis,\n> Worth sending over a little more info?\n>\n"
    "> If this isn't relevant, just let me know and I won't follow up.\n"
    "> Unsubscribe (one click): https://outreach.example/u/abc\n"
)


@pytest.mark.parametrize(
    "headers,subject,body,expected",
    [
        ({}, "Re: Quick Question", "Sure, send more info.\n\n" + QUOTED, R.NEEDS_REVIEW),
        ({}, "Re: Quick Question", "Yes, what does it cost?", R.NEEDS_REVIEW),
        ({"auto-submitted": "auto-replied"}, "Re: Quick Question", "I am away.", R.OOO),
        ({"x-autoreply": "yes"}, "Hi", "x", R.OOO),
        ({"precedence": "auto_reply"}, "Hi", "x", R.OOO),
        ({}, "Automatic reply: Quick Question", "I am out.", R.OOO),
        ({}, "Out of Office", "I am out.", R.OOO),
        ({"auto-submitted": "no"}, "Re: Quick Question", "Tell me more", R.NEEDS_REVIEW),
        ({}, "Re: Quick Question", "Please unsubscribe me.", R.UNSUBSCRIBE),
        ({}, "Re: Quick Question", "Remove me from your list\n" + QUOTED, R.UNSUBSCRIBE),
        ({}, "Re: Quick Question", "stop emailing me", R.UNSUBSCRIBE),
        # an opt-out outranks auto-reply headers: still a person asking to stop
        ({"auto-submitted": "auto-replied"}, "Automatic reply: Quick Question",
         "Remove me from your list.", R.UNSUBSCRIBE),
        ({}, "Re: Quick Question", "Not interested, thanks.", R.NEGATIVE),
        ({}, "Re: Quick Question", "We're all set.\n" + QUOTED, R.NEGATIVE),
        # long: a conversation, not a brush-off
        ({}, "Re: Quick Question", "Not interested right now. " + "Maybe later in the year. " * 30,
         R.NEEDS_REVIEW),
    ],
)
def test_classification_is_deterministic_and_ignores_our_quoted_footer(headers, subject, body, expected):
    own = R.strip_quoted(body)
    assert "Unsubscribe (one click)" not in own
    assert R.classify(headers, subject, own).kind == expected


def test_only_an_unsubscribe_asks_a_person_to_confirm():
    assert R.classify({}, "x", "unsubscribe").needs_confirmation
    assert not R.classify({}, "x", "not interested").needs_confirmation


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I will be out and returning on October 14.", date(2026, 10, 14)),
        ("Back in the office on Oct 14th, 2026", date(2026, 10, 14)),
        ("I am away until 14 October.", date(2026, 10, 14)),
        ("Out of office, back 10/14", date(2026, 10, 14)),
        ("away until 12/2", date(2026, 12, 2)),
        ("returning January 5", date(2027, 1, 5)),  # already past this year -> next
        ("I'm out of the office. Back Monday.", None),
        ("I am away. Thanks.", None),
        ("back on February 30", None),
    ],
)
def test_ooo_return_dates(text, expected):
    assert R.parse_return_date(text, date(2026, 10, 6)) == expected


def test_business_hours_between():
    assert business_hours_between(TUE, TUE + timedelta(hours=2, minutes=30)) == 2.5
    fri = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
    mon = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    assert business_hours_between(fri, mon) == 2.0
    assert business_hours_between(mon, fri) == 0.0


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def test_a_genuine_reply_stops_the_sequence_and_blocks_a_pending_follow_up(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    assert lead.sequence_state == "active"
    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    job.run_after = h.utcnow() - timedelta(minutes=1)
    run_jobs(session)  # the follow-up draft is now in review
    lead = session.get(Lead, lead.id)
    assert lead.status is LeadStatus.READY

    lead.status = LeadStatus.APPROVED  # a human approved it...
    session.commit()
    box = session.execute(select(Mailbox)).scalar_one()
    inbound(session, box, sent, lead, "Sure, send me more info please.")  # ...then they replied
    assert (lead.sequence_state, lead.status) == ("stopped", LeadStatus.HELD)
    session.commit()
    with pytest.raises(PermanentJobError):
        send_lead(session, lead, sender=Recording(tmp_path), now=TUE + timedelta(days=3))

    reply = session.execute(select(Reply)).scalar_one()
    assert reply.classification == R.NEEDS_REVIEW and reply.body == "Sure, send me more info please."
    # The system never decides "interested".
    assert session.execute(select(ReplyTag)).first() is None
    actions = session.execute(select(AuditEvent.user_email, AuditEvent.action)).all()
    assert ("system", "reply_needs_review") in actions


def test_an_approved_follow_up_is_refused_if_the_sequence_stopped_after_approval(session, tmp_path):
    lead, box, sent = followup_draft(session, tmp_path)
    lead.sequence_state = "stopped"
    lead.status = LeadStatus.APPROVED
    session.commit()
    with pytest.raises(PermanentJobError, match="stopped"):
        send_lead(session, lead, sender=Recording(tmp_path), now=TUE + timedelta(days=3))
    assert lead.status is LeadStatus.HELD


def test_the_same_message_is_stored_once(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    first = inbound(session, box, sent, lead, "Tell me more", mid="dup")
    second = inbound(session, box, sent, lead, "Tell me more", mid="dup")
    assert (first.replies, second.replies) == (1, 0)
    assert len(session.execute(select(Reply)).scalars().all()) == 1


def test_our_own_sent_copy_and_unrelated_mail_are_ignored(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    ours = inbound(session, box, sent, lead, "Hey Louis", mid="o", frm=box.email_address)
    other = parse_gmail_message(gmsg("x", "unrelated-thread", "newsletter@x.com", "Hi", "hello"))
    rep = R.SyncReport()
    R.process_message(session, box, other, rep)
    assert (ours.ignored, rep.ignored) == (1, 1)
    assert session.execute(select(Reply)).first() is None


def test_an_unsubscribe_reply_suppresses_at_once_and_asks_for_confirmation(session, tmp_path):
    from mazz.ingest.csv_import import SuppressionChecker

    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "Please remove me from your list.\n\n" + QUOTED)
    reply = session.execute(select(Reply)).scalar_one()
    assert reply.classification == R.UNSUBSCRIBE and reply.needs_confirmation
    assert lead.sequence_state == "stopped"
    entry = session.execute(
        select(SuppressionEntry).where(SuppressionEntry.email == lead.email)
    ).scalar_one()
    assert entry.workspace_id is None and entry.email_sha256 and entry.added_by == "system:reply-sync"
    assert SuppressionChecker(session).check_lead(lead) is not None


def test_an_ooo_pauses_until_the_business_day_after_return_then_resumes(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "I am out of the office and returning on October 14.",
            headers={"Auto-Submitted": "auto-replied"})
    resume = datetime(2026, 10, 15, 9, 0, tzinfo=ET)  # NY lead: 9:00 Eastern, next business day
    assert lead.sequence_state == "paused" and lead.next_step_due_at == resume
    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    assert job.run_after == resume
    assert session.execute(select(Reply)).scalar_one().ooo_return_date == date(2026, 10, 14)
    session.commit()

    # Too early: the follow-up job defers instead of writing a draft.
    from mazz.pipeline.queue import JobContext

    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    with pytest.raises(JobDeferred):
        handle_followup(JobContext(session=session, job=job, lead=lead, handlers={}))
    # After the return date it resumes and writes the draft.
    lead.next_step_due_at = h.utcnow() - timedelta(minutes=1)
    job.run_after = h.utcnow() - timedelta(minutes=1)
    run_jobs(session)
    lead = session.get(Lead, lead.id)
    assert (lead.sequence_state, lead.sequence_step, lead.status) == ("active", 2, LeadStatus.READY)


def test_an_ooo_without_a_date_waits_seven_days_and_pauses_at_most_twice(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    ooo = {"Auto-Submitted": "auto-replied"}
    inbound(session, box, sent, lead, "I am away.", mid="a", headers=ooo)
    assert lead.sequence_state == "paused"
    assert lead.next_step_due_at.astimezone(ET).date() == date(2026, 10, 13)  # +7 days
    lead.sequence_state = "active"
    inbound(session, box, sent, lead, "Still away.", mid="b", headers=ooo)
    assert lead.sequence_state == "paused"
    lead.sequence_state = "active"
    inbound(session, box, sent, lead, "Away again.", mid="c", headers=ooo)
    assert lead.sequence_state == "active"  # third OOO is recorded, not obeyed
    assert len(session.execute(select(Reply)).scalars().all()) == 3


def test_a_paused_lead_cannot_send_before_the_pause_ends(session, tmp_path):
    lead, box, sent = followup_draft(session, tmp_path)
    lead.sequence_state, lead.next_step_due_at = "paused", TUE + timedelta(days=5)
    lead.status = LeadStatus.APPROVED
    session.commit()
    with pytest.raises(JobDeferred):
        send_lead(session, lead, sender=Recording(tmp_path), now=TUE + timedelta(days=1))


# --------------------------------------------------------------------------
# Bounces
# --------------------------------------------------------------------------


def test_a_hard_bounce_dsn_suppresses_stops_and_is_matched_by_message_id(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    dsn = parse_gmail_message(gmsg(
        "dsn1", "dsn-thread", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "Delivery Status Notification (Failure)",
        "Delivery to the following recipient failed permanently:\n\n  someone@else.com\n\n"
        "550 5.1.1 The email account that you tried to reach does not exist.\n\n"
        f"Message-ID: {sent.rfc_message_id}\n",
    ))
    rep = R.SyncReport()
    R.process_message(session, box, dsn, rep)
    assert rep.bounces == 1
    assert (sent.bounced_at is not None, sent.bounce_code) == (True, "5.1.1")
    assert lead.sequence_state == "stopped"
    assert session.execute(
        select(SuppressionEntry).where(SuppressionEntry.email == lead.email)
    ).scalar_one().workspace_id is None
    assert session.execute(select(Reply)).first() is None  # a DSN is not a reply


def test_a_soft_bounce_does_not_suppress(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    dsn = parse_gmail_message(gmsg(
        "dsn2", "t", "mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)",
        f"452 4.2.2 mailbox full for {lead.email}",
    ))
    rep = R.SyncReport()
    R.process_message(session, box, dsn, rep)
    assert rep.bounces == 0 and sent.bounced_at is None and lead.sequence_state != "stopped"


# --------------------------------------------------------------------------
# Sync against a fake Gmail
# --------------------------------------------------------------------------


class Err(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.resp = types.SimpleNamespace(status=status)


class Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeGmail:
    def __init__(self, address, *, head="500", messages=None, threads=None, history=None):
        self.address, self.head = address, head
        self.msgs = {m["id"]: m for m in (messages or [])}
        self.thread_msgs = threads or {}
        self.history_result = history
        self.calls = []

    def users(self):
        return self

    def getProfile(self, userId):
        return Call({"emailAddress": self.address, "historyId": self.head})

    def history(self):
        def _list(**kw):
            self.calls.append(("history", kw))
            return Call(self.history_result)

        return types.SimpleNamespace(list=_list)

    def messages(self):
        return types.SimpleNamespace(
            get=lambda userId, id, format: Call(self.msgs[id]),
            list=lambda **kw: Call({"messages": []}),
        )

    def threads(self):
        return types.SimpleNamespace(
            get=lambda userId, id, format: Call({"messages": self.thread_msgs[id]}))


def test_first_sync_scans_recent_threads_then_follows_history(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    reply = gmsg("r1", sent.provider_thread_id, lead.email, "Re: Quick Question", "Tell me more",
                 ts=TUE + timedelta(hours=1))
    svc = FakeGmail(box.email_address, head="500", threads={sent.provider_thread_id: [reply]})
    rep = R.sync_mailbox(session, box, svc, now=TUE + timedelta(days=1))
    assert (rep.scanned, rep.replies, box.gmail_history_id) == (True, 1, "500")

    later = gmsg("r2", sent.provider_thread_id, lead.email, "Re: Quick Question", "Also, pricing?",
                 ts=TUE + timedelta(hours=2))
    svc = FakeGmail(
        box.email_address, head="520", messages=[later],
        history={"historyId": "520", "history": [{"messagesAdded": [{"message": {"id": "r2"}}]}]},
    )
    rep = R.sync_mailbox(session, box, svc, now=TUE + timedelta(days=1))
    assert (rep.scanned, rep.replies, box.gmail_history_id) == (False, 1, "520")
    assert svc.calls[0][1]["startHistoryId"] == "500" and svc.calls[0][1]["labelId"] == "INBOX"


def test_expired_history_falls_back_to_a_scan_and_resets_the_cursor(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    box.gmail_history_id = "1"
    svc = FakeGmail(box.email_address, head="900", history=Err(404),
                    threads={sent.provider_thread_id: []})
    rep = R.sync_mailbox(session, box, svc, now=TUE + timedelta(days=1))
    assert rep.scanned and box.gmail_history_id == "900"


def test_a_token_for_the_wrong_account_is_refused(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    with pytest.raises(R.WrongAccount):
        R.sync_mailbox(session, box, FakeGmail("someone.else@example.com"))


def test_a_revoked_grant_during_sync_flags_the_mailbox_and_stops_sending(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    box.oauth_refresh_token_enc = google_oauth.encrypt_token("rt")
    session.commit()

    def boom(address):
        raise Exception("RefreshError: invalid_grant: Token has been expired or revoked.")

    R.sync_all(service_for=boom)
    session.expire_all()
    box = session.execute(select(Mailbox)).scalar_one()
    assert box.status is MailboxStatus.NEEDS_RECONNECT and "invalid_grant" in box.last_error
    from mazz.pipeline.mailbox import NoMailboxConfigured

    with pytest.raises(NoMailboxConfigured):
        select_mailbox(session, now=TUE)


def test_sync_all_reads_only_connected_active_mailboxes(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    session.add(Mailbox(email_address="b@getsearchsitemarketing.com", display_name="Sean Gough",
                        credential_ref="m2", oauth_refresh_token_enc=google_oauth.encrypt_token("rt")))
    session.commit()
    seen = []

    def svc(address):
        seen.append(address)
        return FakeGmail(address, threads={})

    R.sync_all(service_for=svc)
    assert seen == ["b@getsearchsitemarketing.com"]  # the first has no token


# --------------------------------------------------------------------------
# OAuth: tokens, the link, the callback, the sender
# --------------------------------------------------------------------------


def test_the_authorization_link_asks_for_send_and_read_only():
    from urllib.parse import parse_qs, urlparse

    url = google_oauth.authorization_url("sean.gough@getsearchsitemarketing.com", "STATE123")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["scope"].split() == [google_oauth.GMAIL_SEND_SCOPE, google_oauth.GMAIL_READONLY_SCOPE]
    assert q["state"] == "STATE123" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == "https://app.example.test/oauth/google/callback"
    assert "mail.google.com" not in url


def test_tokens_are_encrypted_and_the_wrong_key_cannot_read_them(monkeypatch):
    enc = google_oauth.encrypt_token("refresh-secret")
    assert "refresh-secret" not in enc and google_oauth.decrypt_token(enc) == "refresh-secret"
    monkeypatch.setenv("OAUTH_TOKEN_KEY", Fernet.generate_key().decode())
    reload_settings()
    with pytest.raises(google_oauth.OAuthNotConfigured, match="reconnect"):
        google_oauth.decrypt_token(enc)
    monkeypatch.setenv("OAUTH_TOKEN_KEY", "")
    reload_settings()
    with pytest.raises(google_oauth.OAuthNotConfigured):
        google_oauth.encrypt_token("x")


def test_code_exchange_demands_a_refresh_token_and_both_scopes():
    both = f"{google_oauth.GMAIL_SEND_SCOPE} {google_oauth.GMAIL_READONLY_SCOPE}"
    ok = {"refresh_token": "rt", "scope": both}
    assert google_oauth.exchange_code("c", post=lambda url, data: ok)["refresh_token"] == "rt"
    for bad in ({"error": "invalid_grant"}, {"scope": both},
                {"refresh_token": "rt", "scope": google_oauth.GMAIL_SEND_SCOPE}):
        with pytest.raises(google_oauth.OAuthExchangeError):
            google_oauth.exchange_code("c", post=lambda url, data, b=bad: b)


def _connect_box(session, **kw):
    box = Mailbox(email_address="sean.gough@getsearchsitemarketing.com", display_name="Sean Gough",
                  credential_ref="m1", oauth_state="STATE1", oauth_state_at=h.utcnow(), **kw)
    session.add(box)
    session.commit()
    return box


def _stub_google(monkeypatch, who="sean.gough@getsearchsitemarketing.com"):
    monkeypatch.setattr(google_oauth, "exchange_code", lambda code: {"refresh_token": "rt-secret-123"})
    monkeypatch.setattr(google_oauth, "mailbox_identity", lambda token: who)


def test_the_callback_stores_only_an_encrypted_token_and_burns_the_state(session, monkeypatch):
    box = _connect_box(session, status=MailboxStatus.NEEDS_RECONNECT, consecutive_failures=3)
    _stub_google(monkeypatch)
    client = TestClient(create_app(), base_url="https://testserver")
    resp = client.get("/oauth/google/callback", params={"code": "abc", "state": "STATE1"})
    assert resp.status_code == 200 and "connected" in resp.text.lower()
    assert "rt-secret-123" not in resp.text and "abc" not in resp.text
    session.expire_all()
    box = session.get(Mailbox, box.id)
    assert box.oauth_refresh_token_enc != "rt-secret-123"
    assert google_oauth.decrypt_token(box.oauth_refresh_token_enc) == "rt-secret-123"
    assert (box.oauth_state, box.status, box.consecutive_failures) == (None, MailboxStatus.ACTIVE, 0)
    # single use
    again = client.get("/oauth/google/callback", params={"code": "abc", "state": "STATE1"})
    assert again.status_code == 400


def test_the_callback_refuses_the_wrong_account_a_stale_link_and_a_denied_consent(session, monkeypatch):
    box = _connect_box(session)
    client = TestClient(create_app(), base_url="https://testserver")
    _stub_google(monkeypatch, who="intruder@example.com")
    assert client.get("/oauth/google/callback",
                      params={"code": "c", "state": "STATE1"}).status_code == 400
    session.expire_all()
    assert session.get(Mailbox, box.id).oauth_refresh_token_enc is None
    assert client.get("/oauth/google/callback",
                      params={"error": "access_denied", "state": "STATE1"}).status_code == 400
    assert client.get("/oauth/google/callback",
                      params={"code": "c", "state": "nope"}).status_code == 400
    assert client.get("/oauth/google/callback", params={"code": "c"}).status_code == 400

    box = session.get(Mailbox, box.id)
    box.oauth_state_at = h.utcnow() - google_oauth.STATE_TTL - timedelta(minutes=1)
    session.commit()
    _stub_google(monkeypatch)
    assert client.get("/oauth/google/callback",
                      params={"code": "c", "state": "STATE1"}).status_code == 400


def test_the_connect_screen_is_operator_only_and_never_shows_a_token(session):
    box = _connect_box(session)
    box.oauth_refresh_token_enc = google_oauth.encrypt_token("rt-visible")
    box.oauth_connected_at = h.utcnow()
    session.commit()
    mine = h.make_campaign(session)
    session.commit()
    ensure_account("op@example.test")
    ensure_account("sean@example.test", operator=False, workspace_ids=(mine.workspace_id,))

    op = login(TestClient(create_app(), base_url="https://testserver"), "op@example.test")
    page = op.get(f"/mailboxes/{box.id}/connect")
    assert page.status_code == 200 and "STATE1" in page.text and "connected" in page.text
    assert "rt-visible" not in page.text and "gAAAA" not in page.text
    assert "gmail.send" in page.text and "gmail.readonly" in page.text

    assert login(TestClient(create_app(), base_url="https://testserver"), "sean@example.test").get(
        f"/mailboxes/{box.id}/connect").status_code == 403
    assert op.post(f"/mailboxes/{box.id}/connect/new", follow_redirects=False).status_code == 303
    session.expire_all()
    assert session.get(Mailbox, box.id).oauth_state != "STATE1"


def test_a_needs_reconnect_mailbox_cannot_be_resumed_by_hand(session):
    box = _connect_box(session, status=MailboxStatus.NEEDS_RECONNECT)
    ensure_account("op@example.test")
    op = login(TestClient(create_app(), base_url="https://testserver"), "op@example.test")
    assert op.post(f"/mailboxes/{box.id}/status", data={"status": "active"}).status_code == 400


def test_a_revoked_refresh_token_at_send_marks_needs_reconnect_not_disabled(session, monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", "https://outreach.getsearchsitemarketing.com")
    reload_settings()
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)

    class RefreshError(Exception):
        pass

    class Dead:
        def users(self):
            return self

        def messages(self):
            return self

        def send(self, **kw):
            return Call(RefreshError("('invalid_grant: Token has been expired or revoked.', {})"))

    sender = GmailSender(service_factory=lambda **kw: Dead())
    with pytest.raises(MailboxNeedsReconnect):
        send_lead(session, lead, sender=sender, now=TUE)
    box = session.execute(select(Mailbox)).scalar_one()
    assert box.status is MailboxStatus.NEEDS_RECONNECT
    assert session.execute(select(SentMessage)).first() is None
    assert lead.status is LeadStatus.APPROVED  # nothing left; the lead is not lost


def test_service_for_a_mailbox_with_no_grant_asks_for_a_reconnect(session):
    session.add(Mailbox(email_address="a@x.example", display_name="A", credential_ref="x"))
    session.commit()
    with pytest.raises(MailboxNeedsReconnect):
        google_oauth.service_for_mailbox("a@x.example")


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------


def _reply(session, lead, *, mid, cls=R.NEEDS_REVIEW, ago=timedelta(minutes=1), **kw):
    r = Reply(lead_id=lead.id, provider_message_id=mid, from_address=lead.email, subject="Re: x",
              body="Tell me more", received_at=h.utcnow() - ago, classification=cls, **kw)
    session.add(r)
    session.commit()
    return r


def test_inbox_tags_are_human_attributed_dated_and_workspace_scoped(session):
    campaign = ssm_campaign(session)
    mine = law_lead(session, campaign, email="a@damelegal.com")
    other_campaign = h.make_campaign(session, name="Other")
    theirs = h.make_lead(session, email="z@else.com", company_domain="else.com",
                         campaign_id=other_campaign.id)
    r_mine, r_theirs = _reply(session, mine, mid="m"), _reply(session, theirs, mid="t")
    ensure_account("maaz@example.test")
    ensure_account("sean@example.test", operator=False, workspace_ids=(campaign.workspace_id,))

    sean = login(TestClient(create_app(), base_url="https://testserver"), "sean@example.test")
    page = sean.get("/inbox?show=all").text
    assert f"/leads/{mine.id}" in page and f"/leads/{theirs.id}" not in page
    assert sean.post(f"/inbox/{r_theirs.id}/tag", data={"tag": "interested"}).status_code == 404
    assert sean.post(f"/inbox/{r_mine.id}/tag", data={"tag": "bogus"}).status_code == 400

    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")
    for tag in ("interested", "qualified"):
        resp = maaz.post(f"/inbox/{r_mine.id}/tag", data={"tag": tag}, follow_redirects=False)
        assert resp.status_code == 303
    session.expire_all()
    rows = session.execute(select(ReplyTag).order_by(ReplyTag.id)).scalars().all()
    assert [(t.tag, t.tagged_by) for t in rows] == [("interested", "maaz@example.test"),
                                                    ("qualified", "maaz@example.test")]
    assert all(t.tagged_at for t in rows)
    maaz.post(f"/inbox/{r_mine.id}/tag", data={"tag": "qualified", "on": "0"})
    session.expire_all()
    assert [t.tag for t in session.execute(select(ReplyTag)).scalars()] == ["interested"]
    actions = [a for (a,) in session.execute(select(AuditEvent.action)).all()]
    assert actions.count("tag") == 2 and "untag" in actions


def test_an_interested_reply_unhandled_past_two_business_hours_is_highlighted(session):
    campaign = ssm_campaign(session)
    old = law_lead(session, campaign, email="old@damelegal.com")
    fresh = law_lead(session, campaign, email="new@fresh.com", domain="fresh.com")
    r_old = _reply(session, old, mid="o", ago=timedelta(days=10))
    r_fresh = _reply(session, fresh, mid="f", ago=timedelta(minutes=1))
    for r in (r_old, r_fresh):
        session.add(ReplyTag(reply_id=r.id, tag="interested", tagged_by="maaz@example.test"))
    session.commit()
    ensure_account("maaz@example.test")
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")

    page = maaz.get("/inbox").text
    assert "1 interested reply has waited" in page
    maaz.post(f"/inbox/{r_old.id}/handled")
    assert "waited" not in maaz.get("/inbox").text
    session.expire_all()
    assert session.get(Reply, r_old.id).handled_by == "maaz@example.test"
    assert "handled by maaz@example.test" in maaz.get("/inbox?show=all").text


def test_ooo_stays_out_of_the_attention_list_and_unsubscribes_ask_for_confirmation(session):
    campaign = ssm_campaign(session)
    lead = law_lead(session, campaign)
    _reply(session, lead, mid="a", cls=R.OOO)
    unsub = _reply(session, lead, mid="b", cls=R.UNSUBSCRIBE, needs_confirmation=True)
    ensure_account("maaz@example.test")
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")
    body = maaz.get("/inbox").text.split("<tbody>")[1]
    assert "suppressed automatically" in body and body.count("<tr") == 1
    maaz.post(f"/inbox/{unsub.id}/confirm")
    session.expire_all()
    assert session.get(Reply, unsub.id).needs_confirmation is False


def test_delete_by_person_erases_their_replies(session, tmp_path):
    from mazz.privacy import delete_person, find_person_references

    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "Tell me more")
    session.commit()
    assert find_person_references(session, lead.email).reply_ids
    delete_person(lead.email, session=session)
    session.commit()
    assert session.execute(select(Reply)).first() is None


# --------------------------------------------------------------------------
# Audit fixes (WP5 compliance-auditor pass)
# --------------------------------------------------------------------------


def test_a_hostile_megabyte_reply_cannot_stall_the_worker():
    import time

    hostile = "On\n" * 3_000_000 + "Not interested."
    started = time.monotonic()
    own = R.strip_quoted(hostile)
    assert time.monotonic() - started < 2.0
    assert len(own) <= R.MAX_REPLY_CHARS
    # A wrapped Gmail attribution still strips; a one-line one too.
    wrapped = "Yes please.\n\nOn Tue, Oct 6, 2026 at 11:00 AM Sean Gough <s@x.com>\nwrote:\n> hi"
    assert R.strip_quoted(wrapped) == "Yes please."


def test_a_transient_refresh_failure_is_not_a_revocation():
    class RefreshError(Exception):
        pass

    assert google_oauth.is_revoked(RefreshError("invalid_grant: Token has been revoked"))
    assert not google_oauth.is_revoked(RefreshError("Unable to refresh: 503 service unavailable"))
    assert not google_oauth.is_revoked(RefreshError("invalid_client"))
    assert not google_oauth.is_revoked(ConnectionError("reset"))


def test_the_callback_survives_google_failing_to_say_who_consented(session, monkeypatch):
    box = _connect_box(session)
    monkeypatch.setattr(google_oauth, "exchange_code", lambda code: {"refresh_token": "rt"})

    def boom(token):
        raise RuntimeError("HttpError 503")

    monkeypatch.setattr(google_oauth, "mailbox_identity", boom)
    resp = TestClient(create_app(), base_url="https://testserver").get("/oauth/google/callback", params={"code": "c", "state": "STATE1"})
    assert resp.status_code == 400
    session.expire_all()
    assert session.get(Mailbox, box.id).oauth_refresh_token_enc is None


def test_deleting_a_forwarder_erases_replies_they_wrote_on_someone_elses_thread(session, tmp_path):
    from mazz.privacy import delete_person

    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "Please talk to Jane.", frm="jane@damelegal.com")
    session.commit()
    delete_person("jane@damelegal.com", session=session)  # must not raise DeletionIncomplete
    session.commit()
    assert session.execute(select(Reply)).first() is None
    assert session.get(Lead, lead.id) is not None  # the lead on that thread is untouched


def test_a_bounce_audit_does_not_keep_the_address(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    dsn = parse_gmail_message(gmsg(
        "dsn3", "t", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
        f"550 5.1.1 user unknown\nMessage-ID: {sent.rfc_message_id}\n"))
    R.process_message(session, box, dsn, R.SyncReport())
    detail = session.execute(select(AuditEvent.detail).where(AuditEvent.action == "hard_bounce")).scalar_one()
    assert lead.email not in str(detail)


def test_send_sees_a_stop_committed_by_another_session(session, tmp_path):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)
    with h.db_session() as other:  # the reply sync, a separate connection
        other.get(Lead, lead.id).sequence_state = "stopped"
        other.commit()
    with pytest.raises(PermanentJobError, match="stopped"):
        send_lead(session, lead, sender=Recording(tmp_path), now=TUE)  # stale copy says active/None


# --------------------------------------------------------------------------
# Open WP5 audit items closed in the launch plan's Task 4
# --------------------------------------------------------------------------


def test_only_an_operator_can_lift_an_automatic_unsubscribe_and_only_that_kind(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "Please don't stop emailing me, this is useful")
    session.commit()
    reply = session.execute(select(Reply)).scalar_one()
    assert reply.classification == R.UNSUBSCRIBE  # the broad keyword rule, wrong here
    auto = select(SuppressionEntry).where(SuppressionEntry.email == lead.email)
    assert session.execute(auto).scalar_one().added_by == R.AUTO_SUPPRESSED_BY

    ensure_account("maaz@example.test")
    ensure_account("sean@example.test", operator=False, workspace_ids=(lead.campaign.workspace_id,))
    sean = login(TestClient(create_app(), base_url="https://testserver"), "sean@example.test")
    assert sean.post(f"/inbox/{reply.id}/lift", data={"confirm": "LIFT"}).status_code == 403
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")
    assert "lift suppression" in maaz.get("/inbox").text
    assert maaz.post(f"/inbox/{reply.id}/lift", data={"confirm": ""}).status_code == 400
    resp = maaz.post(f"/inbox/{reply.id}/lift", data={"confirm": "LIFT"}, follow_redirects=False)
    assert resp.status_code == 303
    session.expire_all()
    assert session.execute(auto).first() is None
    event = session.execute(select(AuditEvent).where(AuditEvent.action == "lift_suppression")).scalar_one()
    assert event.user_email == "maaz@example.test"
    assert session.get(Lead, lead.id).sequence_state == "stopped"  # a human answers them

    # A suppression a person added is never lifted from here.
    session.add(SuppressionEntry(email=lead.email, reason="removal request", added_by="maaz@example.test"))
    session.commit()
    assert maaz.post(f"/inbox/{reply.id}/lift", data={"confirm": "LIFT"}).status_code == 409
    session.expire_all()
    assert session.execute(auto).scalar_one().added_by == "maaz@example.test"


def test_outbox_shows_a_stopped_or_paused_lead_as_blocked_and_release_refuses(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)
    ensure_account("maaz@example.test")
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")

    lead.sequence_state, lead.next_step_due_at = "paused", h.utcnow() + timedelta(days=3)
    session.commit()
    assert "paused (out of office)" in maaz.get("/outbox").text
    assert maaz.post(f"/outbox/{lead.id}/send").status_code == 409
    session.expire_all()
    assert session.get(Lead, lead.id).status is LeadStatus.APPROVED  # waits, not held

    lead = session.get(Lead, lead.id)
    lead.sequence_state = "stopped"
    session.commit()
    assert "sequence stopped since approval" in maaz.get("/outbox").text
    assert maaz.post(f"/outbox/{lead.id}/send").status_code == 409
    session.expire_all()
    assert session.get(Lead, lead.id).status is LeadStatus.HELD
    assert session.execute(select(Job).where(Job.kind == "send")).first() is None
