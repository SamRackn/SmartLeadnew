"""Migration-readiness audit (GML-*): Gmail OAuth, wire format, reply sync,
classification, legacy adoption.

Every test asserts the CORRECT business behaviour. Tests marked CONTROL pass
today and pin behaviour that was checked and found sound; the rest FAIL while
the named defect exists. Gmail is faked; nothing touches the network.
"""

from __future__ import annotations

import base64
import email
import email.policy
import types
import urllib.error
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.ingest import smartlead as S
from mazz.integrations import google_oauth
from mazz.integrations.email_sender import GmailSender, OutgoingMessage, build_mime
from mazz.integrations.gmail_reader import parse_gmail_message
from mazz.models import Lead, LeadStatus, Mailbox, MailboxStatus, Reply, SentMessage, SuppressionEntry
from mazz.pipeline import replies as R
from mazz.pipeline.queue import JobDeferred, PermanentJobError
from mazz.pipeline.send import send_lead, unresolved_send_attempt
from mazz.web.app import create_app
from test_replies import Call, Err, FakeGmail, followup_draft, gmsg, inbound, sent_lead
from test_sequence import TUE, drafted_and_approved, mailbox, run_jobs, ssm_campaign
from test_smartlead_cutover import LAST, SentSearch, row
from test_smartlead_cutover import lead as cutover_lead

KEY = Fernet.generate_key().decode()
HTTPS = "https://outreach.getsearchsitemarketing.com"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {
        "SESSION_SECRET": "test-session-secret",
        "OAUTH_TOKEN_KEY": KEY,
        "GOOGLE_OAUTH_CLIENT_ID": "client-id.apps.example",
        "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
        "APP_BASE_URL": "https://app.example.test",
        "UNSUBSCRIBE_BASE_URL": HTTPS,
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


class WireService:
    """A Gmail service that records `users.messages.send` bodies. Given an
    address it can also be read (no new mail), as a live follow-up does first."""

    def __init__(self, address=None):
        self.bodies: list[dict] = []
        self.address = address

    def users(self):
        return self

    def getProfile(self, userId):
        if self.address is None:
            return Call(RuntimeError("this fake cannot be read"))
        return Call({"emailAddress": self.address, "historyId": "2"})

    def history(self):
        return types.SimpleNamespace(list=lambda **kw: Call({"historyId": "2", "history": []}))

    def messages(self):
        return self

    def send(self, userId, body):
        self.bodies.append(body)
        return Call({"id": f"g{len(self.bodies)}", "threadId": body.get("threadId") or "T-1"})

    def parsed(self, n=-1):
        return email.message_from_bytes(
            base64.urlsafe_b64decode(self.bodies[n]["raw"]), policy=email.policy.default
        )


def live_sender(svc):
    return GmailSender(service_factory=lambda **kw: svc)


# --------------------------------------------------------------------------
# GML: wire format (CONTROL)
# --------------------------------------------------------------------------


def test_control_wire_format_headers_threading_and_footer(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign, full_name="Zoe Dame", first_name="Zoe")
    box = session.execute(select(Mailbox)).scalar_one()
    box.display_name = "Séan Gough"
    session.commit()
    svc = WireService()
    send_lead(session, lead, sender=live_sender(svc), now=TUE)
    session.commit()

    first = svc.parsed(0)
    assert "threadId" not in svc.bodies[0]
    assert first["List-Unsubscribe"].startswith("<https://") and first["List-Unsubscribe"].endswith(">")
    assert "mailto" not in first["List-Unsubscribe"]
    assert first["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert first["Message-ID"].endswith("@getsearchsitemarketing.com>")
    assert "Séan" in first["From"]
    assert not first.is_multipart()  # text only: the footer is in the one body there is
    text = first.get_content()
    assert "Unsubscribe (one click): https://" in text and "Worth sending over a little more info?" in text

    # Follow-up: same thread, replies to and lists the first Message-ID.
    sent = session.execute(select(SentMessage)).scalar_one()
    box = session.execute(select(Mailbox)).scalar_one()
    box.gmail_history_id = "1"  # a live follow-up reads its mailbox first (GML-1)
    svc.address = box.email_address
    box.last_sent_at = TUE  # the send receipts carry the real clock; keep spacing out of the way
    from mazz.models import Job

    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    job.run_after = h.utcnow() - timedelta(minutes=1)
    run_jobs(session)
    lead = session.get(Lead, lead.id)
    lead.status = LeadStatus.APPROVED
    session.commit()
    send_lead(session, lead, sender=live_sender(svc), now=TUE + timedelta(days=3))
    follow = svc.parsed(1)
    assert svc.bodies[1]["threadId"] == "T-1" == sent.provider_thread_id
    assert follow["In-Reply-To"] == first["Message-ID"] == sent.rfc_message_id
    assert follow["References"] == first["Message-ID"]
    assert follow["Subject"] == "Re: Quick Question"


def test_control_crlf_in_a_header_value_is_refused_not_injected():
    msg = OutgoingMessage(from_address="a@b.example", from_name="A", to_address="x@y.example",
                          to_name="Eve\r\nBcc: evil@z.example", subject="Hi", body="b")
    with pytest.raises(ValueError):
        build_mime(msg)


# --------------------------------------------------------------------------
# GML-SEND-1: a name that cannot be rendered leaves a phantom "may have been sent"
# --------------------------------------------------------------------------


def test_a_name_that_cannot_be_rendered_leaves_no_phantom_send_attempt(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign, full_name="Louis\r\nBcc: evil@x.example")
    svc = WireService()
    with pytest.raises(Exception):
        send_lead(session, lead, sender=live_sender(svc), now=TUE)
    session.rollback()
    session.expire_all()
    lead = session.get(Lead, lead.id)
    assert svc.bodies == []  # nothing left, and nothing injected
    # Nothing was sent, so the lead must not be parked as "may have been sent":
    assert unresolved_send_attempt(session, lead) is None


# --------------------------------------------------------------------------
# GML-SYNC: reply sync
# --------------------------------------------------------------------------


class Gmail404(FakeGmail):
    """A message listed by history that Gmail then reports as gone (404)."""

    def messages(self):
        return type("M", (), {
            "get": lambda _s, userId, id, format: Call(self.msgs[id] if id in self.msgs else Err(404)),
            "list": lambda _s, **kw: Call({"messages": []}),
        })()


def test_one_vanished_message_does_not_wedge_the_whole_mailbox(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    box.gmail_history_id = "100"
    good = gmsg("good", sent.provider_thread_id, lead.email, "Re: Quick Question", "Tell me more",
                ts=TUE + timedelta(hours=1))
    svc = Gmail404(box.email_address, head="130", messages=[good], history={
        "historyId": "130",
        "history": [{"messagesAdded": [{"message": {"id": "gone"}}, {"message": {"id": "good"}}]}]})
    rep = R.sync_mailbox(session, box, svc, now=TUE + timedelta(days=1))  # raises today: HttpError 404
    assert rep.replies == 1 and box.gmail_history_id == "130"


class Flaky(FakeGmail):
    """messages.get for `bad` fails with a 5xx while `broken` is set."""

    broken = True

    def messages(self):
        def get(userId, id, format):
            if id == "bad" and self.broken:
                return Call(Err(503))
            return Call(self.msgs[id])

        return type("M", (), {"get": staticmethod(get), "list": staticmethod(lambda **kw: Call({"messages": []}))})()


def test_control_an_interrupted_sync_never_advances_the_cursor_and_replays_cleanly(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    box.gmail_history_id = "100"
    box.oauth_refresh_token_enc = google_oauth.encrypt_token("rt")
    session.commit()
    mk = lambda mid, body: gmsg(mid, sent.provider_thread_id, lead.email, "Re: Quick Question", body,  # noqa: E731
                                ts=TUE + timedelta(hours=1))
    svc = Flaky(box.email_address, head="130", messages=[mk("first", "Tell me more"), mk("bad", "Also pricing")],
                history={"historyId": "130", "history": [{"messagesAdded": [
                    {"message": {"id": "first"}}, {"message": {"id": "bad"}}]}]})
    R.sync_all(service_for=lambda a: svc)
    session.expire_all()
    box = session.execute(select(Mailbox)).scalar_one()
    assert box.gmail_history_id == "100"  # not advanced
    assert session.execute(select(Reply)).first() is None  # and the half-batch rolled back
    svc.broken = False
    R.sync_all(service_for=lambda a: svc)
    session.expire_all()
    box = session.execute(select(Mailbox)).scalar_one()
    assert box.gmail_history_id == "130"
    assert len(session.execute(select(Reply)).scalars().all()) == 2


class Paged(FakeGmail):
    def history(self):
        def _list(**kw):
            self.calls.append(("history", kw))
            return Call(self.history_result[kw.get("pageToken")])

        return type("H", (), {"list": staticmethod(_list)})()


def test_control_history_paging_follows_next_page_token_and_keeps_the_start_id(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    box.gmail_history_id = "100"
    mk = lambda mid: gmsg(mid, sent.provider_thread_id, lead.email, "Re: x", mid, ts=TUE + timedelta(hours=1))  # noqa: E731
    svc = Paged(box.email_address, head="300", messages=[mk("a"), mk("b")], history={
        None: {"historyId": "200", "nextPageToken": "p2",
               "history": [{"messagesAdded": [{"message": {"id": "a"}}]}]},
        "p2": {"historyId": "300", "history": [{"messagesAdded": [{"message": {"id": "b"}}]}]}})
    rep = R.sync_mailbox(session, box, svc, now=TUE + timedelta(days=1))
    assert rep.replies == 2 and box.gmail_history_id == "300"
    assert [c[1]["startHistoryId"] for c in svc.calls] == ["100", "100"]


def test_the_fallback_scan_covers_threads_whose_last_send_is_older_than_14_days(session, tmp_path):
    """History expired (or first sync after cutover): a reply on a thread we last
    wrote to 20 days ago is lost, because the scan only walks 14 days of sends."""

    lead, box, sent = sent_lead(session, tmp_path)
    box.gmail_history_id = "1"
    now = sent.sent_at + timedelta(days=20)
    late = gmsg("late", sent.provider_thread_id, lead.email, "Re: Quick Question", "Tell me more",
                ts=now - timedelta(days=1))
    svc = FakeGmail(box.email_address, head="900", history=Err(404), threads={sent.provider_thread_id: [late]})
    rep = R.sync_mailbox(session, box, svc, now=now)
    assert rep.scanned and rep.replies == 1


def test_a_follow_up_is_not_sent_while_the_mailbox_has_not_been_read_recently(session, tmp_path):
    """Reply sync is the only thing that stops a sequence on a reply. If it has
    not run for days (5xx loop, poison message, worker down), a live follow-up
    still leaves for someone who may already have answered. Now the follow-up
    reads its own mailbox first, and a mailbox it cannot read defers it."""

    lead, box, sent = followup_draft(session, tmp_path)  # step 1 went out via the stub
    lead.status = LeadStatus.APPROVED
    box.last_synced_at = TUE - timedelta(days=10)
    box.last_sent_at = TUE  # receipts carry the real clock; do not let spacing defer by accident
    session.commit()
    svc = WireService()
    with pytest.raises((JobDeferred, PermanentJobError)):
        send_lead(session, lead, sender=live_sender(svc), now=TUE + timedelta(days=3))
    assert svc.bodies == []


# --------------------------------------------------------------------------
# GML-CLASS: classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "Please don't stop emailing me, this is exactly what we need.",
    "Sounds interesting, call me Thursday.\n--\nJane Doe | Acme Law\nTo unsubscribe from Acme's newsletter click here",
])
def test_an_interested_reply_is_not_read_as_an_opt_out(body):
    assert R.classify({}, "Re: Quick Question", R.strip_quoted(body)).kind == R.NEEDS_REVIEW


def test_an_opt_out_from_a_colleague_suppresses_the_person_who_asked(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    inbound(session, box, sent, lead, "Please remove me from your list.", frm="partner@damelegal.com")
    asked = session.execute(
        select(SuppressionEntry.id).where(SuppressionEntry.email == "partner@damelegal.com")
    ).first()
    assert asked is not None


def test_a_soft_bounce_is_not_made_hard_by_an_ip_address_that_looks_like_a_status(session, tmp_path):
    lead, box, sent = sent_lead(session, tmp_path)
    dsn = parse_gmail_message(gmsg(
        "dsn-ip", "t", "mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)",
        f"Remote-MTA: dns; mx1.example.net (5.9.149.12)\n\n452 4.2.2 mailbox temporarily full for {lead.email}\n"
        f"Message-ID: {sent.rfc_message_id}\n",
    ))
    rep = R.SyncReport()
    R.process_message(session, box, dsn, rep)
    assert rep.bounces == 0 and sent.bounced_at is None
    assert session.execute(select(SuppressionEntry).where(SuppressionEntry.email == lead.email)).first() is None


def test_an_out_of_office_date_already_in_the_past_is_not_used_to_schedule():
    assert R.parse_return_date("I am away until 14 Oct 2025.", date(2026, 10, 6)) is None


# --------------------------------------------------------------------------
# GML-OAUTH
# --------------------------------------------------------------------------


def test_the_callback_survives_the_token_endpoint_being_unreachable(session, monkeypatch):
    box = Mailbox(email_address="sean.gough@getsearchsitemarketing.com", display_name="Sean Gough",
                  credential_ref="m1", oauth_state="STATE9", oauth_state_at=h.utcnow())
    session.add(box)
    session.commit()

    def down(*a, **kw):
        raise urllib.error.URLError("timed out")

    # The network layer, not exchange_code: the fix lives in `_post_form`.
    monkeypatch.setattr(google_oauth.urllib.request, "urlopen", down)
    client = TestClient(create_app(), base_url="https://testserver", raise_server_exceptions=False)
    resp = client.get("/oauth/google/callback", params={"code": "abc", "state": "STATE9"})
    assert resp.status_code == 400  # a clean "not connected" page, not a 500


def test_an_undecryptable_token_is_a_config_fault_not_a_disabled_mailbox(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)
    box = session.execute(select(Mailbox)).scalar_one()
    box.oauth_refresh_token_enc = "not-a-fernet-token"  # as after an OAUTH_TOKEN_KEY change
    session.commit()
    with pytest.raises(Exception):
        send_lead(session, lead, sender=GmailSender(), now=TUE)
    session.expire_all()
    box = session.execute(select(Mailbox)).scalar_one()
    # A wrong key is the same fault for every mailbox; marking each one dead
    # (one per attempted send) turns a config fix into a roster rebuild.
    assert box.status is MailboxStatus.ACTIVE


# --------------------------------------------------------------------------
# GML-ADOPT: legacy adoption
# --------------------------------------------------------------------------


def test_adoption_trusts_gmail_over_the_export_for_how_many_steps_went_out(session):
    """Export says 1 sent; the Gmail thread shows Smartlead sent FU1 too."""

    campaign = ssm_campaign(session)
    mailbox(session)
    box = session.execute(select(Mailbox)).scalar_one()
    box.oauth_refresh_token_enc = "encrypted-elsewhere"
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    s1 = gmsg("s1", "t1", box.email_address, "Quick Question", "Hey One",
              headers={"Message-ID": "<s1@sl>"}, ts=LAST)
    s2 = gmsg("s2", "t1", box.email_address, "Re: Quick Question", "Following up",
              headers={"Message-ID": "<s2@sl>"}, ts=LAST + timedelta(days=3))
    svc = SentSearch(box.email_address, {"one@one-law.com": ["s1", "s2"]},
                     messages=[s1, s2], threads={"t1": [s1, s2]})
    S.adopt(session, service_for=lambda a: svc, now=TUE)
    one = cutover_lead(session, "one@one-law.com")
    assert one.sequence_step == 2  # else FU1 is sent to him a second time


def test_adoption_does_not_hold_a_lead_because_a_mailbox_was_skipped(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    a = session.execute(select(Mailbox)).scalar_one()
    a.oauth_refresh_token_enc = "encrypted-elsewhere"
    session.add(Mailbox(email_address="b@getsearchsitemarketing.com", display_name="B", credential_ref="oauth",
                        status=MailboxStatus.NEEDS_RECONNECT))  # its thread lives here; not searched
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    svc = SentSearch(a.email_address, {}, messages=[], threads={})
    S.adopt(session, service_for=lambda addr: svc, now=TUE)
    one = cutover_lead(session, "one@one-law.com")
    # Held means "never retried": adopt only selects status=sent. A skipped mailbox
    # is "unknown", not "not found".
    assert one.status is not LeadStatus.HELD


# --------------------------------------------------------------------------
# GML-SEND-2: 403 overloading
# --------------------------------------------------------------------------


class Http403(Exception):
    def __init__(self, reason, text):
        super().__init__(
            f'<HttpError 403 when requesting https://gmail.googleapis.com/gmail/v1/users/me/messages/send '
            f'returned "{text}". Details: "[{{\'message\': \'{text}\', \'domain\': \'usageLimits\', '
            f'\'reason\': \'{reason}\'}}]">'
        )
        self.resp = type("R", (), {"status": 403})()


@pytest.mark.parametrize("reason,text", [
    ("userRateLimitExceeded", "User-rate limit exceeded. Retry after 2026-10-15T19:00:00.000Z"),
    ("dailyLimitExceeded", "Daily Limit Exceeded"),
])
def test_a_403_rate_or_daily_limit_is_retryable_not_a_dead_credential(reason, text):
    from mazz.integrations.base import ProviderRateLimited

    msg = OutgoingMessage(from_address="a@b.example", from_name="A", to_address="x@y.example",
                          subject="s", body="b")
    err = GmailSender()._translate(Http403(reason, text), msg)
    assert isinstance(err, ProviderRateLimited), type(err).__name__


# --------------------------------------------------------------------------
# GML-1: a live follow-up reads its own mailbox first
# --------------------------------------------------------------------------


class ReadAndWire(WireService):
    """WireService whose mailbox has new mail: history lists `msgs`."""

    def __init__(self, address, msgs):
        super().__init__(address)
        self.read = FakeGmail(address, head="700", messages=msgs, history={
            "historyId": "700",
            "history": [{"messagesAdded": [{"message": {"id": m["id"]}}]} for m in msgs],
        })

    def getProfile(self, userId):
        return self.read.getProfile(userId)

    def history(self):
        return self.read.history()

    def get(self, userId, id, format):
        return self.read.messages().get(userId=userId, id=id, format=format)


def _released_followup(session, tmp_path, msgs_for):
    lead, box, sent = followup_draft(session, tmp_path)
    lead.status = LeadStatus.APPROVED
    box.gmail_history_id = "600"
    box.last_synced_at = TUE + timedelta(days=3)  # "fresh": a clock gate would wave it through
    box.last_sent_at = TUE
    session.commit()
    return lead, ReadAndWire(box.email_address, msgs_for(lead, sent))


def test_a_reply_since_the_last_sync_stops_the_follow_up_before_the_wire(session, tmp_path):
    lead, svc = _released_followup(session, tmp_path, lambda lead, sent: [
        gmsg("late", sent.provider_thread_id, lead.email, "Re: Quick Question",
             "Yes, call me Thursday.", ts=TUE + timedelta(days=3))])
    with pytest.raises(PermanentJobError, match="read just now"):
        send_lead(session, lead, sender=live_sender(svc), now=TUE + timedelta(days=3, minutes=5))
    assert svc.bodies == []
    session.expire_all()
    assert session.get(Lead, lead.id).sequence_state == "stopped"
    assert session.execute(select(Reply)).scalar_one().lead_id == lead.id


def test_a_quiet_thread_lets_the_released_follow_up_go(session, tmp_path):
    lead, svc = _released_followup(session, tmp_path, lambda lead, sent: [])
    send_lead(session, lead, sender=live_sender(svc), now=TUE + timedelta(days=3, minutes=5))
    assert len(svc.bodies) == 1
