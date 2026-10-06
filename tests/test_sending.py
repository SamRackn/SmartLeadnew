"""Owned sending: the mailbox roster, the rotation, and the send step.

This replaced the Smartlead push, which means this file replaced the safety
properties `test_smartlead.py` used to assert. Those were about a client that
could not send at all ("no send/launch/trigger verb exists anywhere on it").
We send now, so the guarantees have to be different ones, and they are the
tests below:

* live sending is switched on by CONFIG, never by a credential appearing;
* an approved lead is re-checked against suppression at the moment of sending,
  not only at ingest;
* a mailbox obeys its daily cap and its spacing, and waiting is not a failure;
* a send that did not happen never leaves a lead looking `sent`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.integrations.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from mazz.integrations.email_sender import (
    GmailSender,
    OutgoingMessage,
    StubEmailSender,
    build_mime,
    get_email_sender,
)
from mazz.models import (
    Campaign,
    Draft,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    SentMessage,
    SuppressionEntry,
    VerificationStatus,
    Workspace,
)
from mazz.pipeline.draft import RELEVANCE_LINE, unsubscribe_url
from mazz.pipeline.mailbox import (
    MailboxUnavailable,
    NoMailboxConfigured,
    day_window,
    headroom,
    select_mailbox,
    sent_today,
)
from mazz.pipeline.queue import (
    default_handlers,
    JobDeferred,
    PermanentJobError,
    enqueue_job,
    process_next,
    reclaim_stale_jobs,
)
from mazz.pipeline.send import (
    definitely_not_sent,
    handle_send,
    send_lead,
    unresolved_send_attempt,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 1 Test Street, Testville")
    monkeypatch.setenv("EMAIL_SENDER", "stub")
    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


@pytest.fixture
def outbox(tmp_path):
    return tmp_path / "sent_outbox.jsonl"


@pytest.fixture
def sender(outbox):
    return StubEmailSender(outbox_path=outbox, clock=lambda: NOW)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_mailbox(session, **kwargs) -> Mailbox:
    defaults = {
        "email_address": "dana@outreach-northwind.com",
        "display_name": "Dana Whitfield",
        "credential_ref": "northwind-burner",
        "daily_cap": 30,
        "min_spacing_seconds": 0,
    }
    defaults.update(kwargs)
    mailbox = Mailbox(**defaults)
    session.add(mailbox)
    session.flush()
    return mailbox


def make_approved_lead(session, **kwargs):
    lead = h.make_lead(session, **kwargs)
    lead.status = LeadStatus.APPROVED
    draft = h.make_draft(
        session,
        lead,
        verification_status=VerificationStatus.PASSED,
        body="Saw the Revenue Operations Manager req.\n\n-- \nMazz Ltd, 1 Test "
        f"Street, Testville\n{RELEVANCE_LINE}\n"
        "Unsubscribe (one click): https://example.test/u/abc",
    )
    session.flush()
    return lead, draft


def record_send(session, mailbox, *, lead, draft, at: datetime) -> SentMessage:
    """A historical send, for cap accounting."""

    sent = SentMessage(
        lead_id=lead.id,
        draft_id=draft.id,
        mailbox_id=mailbox.id,
        from_address=mailbox.email_address,
        to_address=lead.email,
        subject="prior",
        body="prior",
        sent_at=at,
    )
    session.add(sent)
    session.flush()
    return sent


# ==========================================================================
# Switching live sending on is a decision, not a side effect
# ==========================================================================


def test_sender_defaults_to_the_stub():
    assert isinstance(get_email_sender(), StubEmailSender)


def test_a_credential_in_the_environment_does_not_switch_on_real_sending(monkeypatch):
    """The guardrail this file exists for.

    `draft_composer` has the same rule for spending money; this one is
    sharper, because the failure mode is not a bill, it is strangers receiving
    email nobody decided to send.
    """

    monkeypatch.setenv("GMAIL_SA_DEFAULT", '{"type": "service_account"}')
    monkeypatch.setenv("GMAIL_SA_NORTHWIND_BURNER", '{"type": "service_account"}')
    reload_settings()

    assert isinstance(get_email_sender(), StubEmailSender)


def test_email_sender_google_selects_the_live_adapter(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER", "google")
    reload_settings()
    assert isinstance(get_email_sender(), GmailSender)


def test_unknown_sender_name_is_refused(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER", "sendgrid")
    reload_settings()
    with pytest.raises(ValueError, match="unknown EMAIL_SENDER"):
        get_email_sender()


# ==========================================================================
# The message on the wire
# ==========================================================================


def _message(**kwargs) -> OutgoingMessage:
    defaults = {
        "from_address": "dana@outreach-northwind.com",
        "from_name": "Dana Whitfield",
        "to_address": "holden@acme.test",
        "to_name": "Holden Reyes",
        "subject": "Quick question about your RevOps hiring",
        "body": "Saw the req.\n\n-- \nMazz Ltd\nUnsubscribe: https://x.test/u/t",
    }
    defaults.update(kwargs)
    return OutgoingMessage(**defaults)


def test_mime_carries_the_identity_headers():
    mime = build_mime(_message(), now=NOW)
    assert mime["From"] == "Dana Whitfield <dana@outreach-northwind.com>"
    assert mime["To"] == "Holden Reyes <holden@acme.test>"
    assert mime["Subject"] == "Quick question about your RevOps hiring"
    # Message-ID is scoped to the sending domain so a reply threads against
    # something the mailbox's own provider recognises.
    assert mime["Message-ID"].endswith("@outreach-northwind.com>")
    assert "Mazz Ltd" in mime.get_content()


def test_mime_refuses_a_caller_supplied_from_header():
    """A display-name spoof would be a header override away."""

    with pytest.raises(ValueError, match="set by build_mime"):
        build_mime(_message(headers={"From": "ceo@bigcorp.test"}))


def test_list_unsubscribe_headers_are_set_on_a_real_send(session, sender, outbox):
    mailbox = make_mailbox(session)
    lead, _draft = make_approved_lead(session)

    send_lead(session, lead, sender=sender, now=NOW)

    line = outbox.read_text().strip()
    assert "List-Unsubscribe:" in line
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in line
    assert mailbox.email_address in line


def test_the_unsubscribe_header_and_the_visible_footer_link_agree(
    session, sender, outbox
):
    """One unsubscribe, two surfaces. They must be the same URL."""

    make_mailbox(session)
    lead, draft = make_approved_lead(session)

    send_lead(session, lead, sender=sender, now=NOW)

    mime = json.loads(outbox.read_text().strip())["mime"]
    header = re.search(r"List-Unsubscribe:\s*<([^>]+)>", mime, re.S).group(1)
    footer = re.search(r"Unsubscribe \(one click\):\s*(\S+)", draft.body).group(1)
    assert " ".join(header.split()) == footer


def test_the_unsubscribe_header_follows_the_body_across_a_secret_rotation(
    session, sender, outbox, monkeypatch
):
    """The token is an HMAC keyed on SESSION_SECRET, so re-deriving it at send
    time can disagree with the link already written into the approved body —
    a worker configured with a different secret, or any rotation between
    drafting and sending. The recipient would press the native unsubscribe
    button, hit a token the route cannot match, and keep receiving mail.

    Taking the link FROM the body makes them agree by construction."""

    make_mailbox(session)
    lead, draft = make_approved_lead(session)
    footer = re.search(r"Unsubscribe \(one click\):\s*(\S+)", draft.body).group(1)

    monkeypatch.setenv("SESSION_SECRET", "a-different-secret-entirely")
    reload_settings()
    assert unsubscribe_url(lead) != footer  # re-deriving now would diverge

    send_lead(session, lead, sender=sender, now=NOW)

    mime = json.loads(outbox.read_text().strip())["mime"]
    header = re.search(r"List-Unsubscribe:\s*<([^>]+)>", mime, re.S).group(1)
    assert " ".join(header.split()) == footer


def test_stub_sender_writes_the_real_mime_and_returns_a_receipt(sender, outbox):
    receipt = sender.send(_message(), credential_ref="northwind-burner")

    assert receipt.rfc_message_id.endswith("@outreach-northwind.com>")
    assert receipt.provider_message_id
    body = outbox.read_text()
    assert "Subject: Quick question about your RevOps hiring" in body
    assert "holden@acme.test" in body


# ==========================================================================
# Gmail adapter: the mapping and the error translation
# ==========================================================================


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.resp = _FakeResponse(status)


class _FakeGmail:
    """The three-call chain `service.users().messages().send(...).execute()`."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.sent_body = None

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, *, userId, body):
        assert userId == "me", "must send as the impersonated mailbox"
        self.sent_body = body
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


def _gmail(result=None, error=None) -> tuple[GmailSender, _FakeGmail]:
    fake = _FakeGmail(result=result, error=error)
    return GmailSender(service_factory=lambda **kw: fake), fake


def test_gmail_maps_the_provider_ids_onto_the_receipt():
    sender, fake = _gmail(result={"id": "18f2a", "threadId": "18f29"})

    receipt = sender.send(_message(), credential_ref="northwind-burner")

    assert receipt.provider_message_id == "18f2a"
    assert receipt.provider_thread_id == "18f29"
    assert receipt.provider == "google"
    assert "raw" in fake.sent_body


def test_gmail_accepting_without_an_id_is_a_bad_response():
    """"Accepted" with nothing to point at is not proof anything was sent."""

    sender, _ = _gmail(result={})
    with pytest.raises(ProviderBadResponse):
        sender.send(_message(), credential_ref="northwind-burner")


@pytest.mark.parametrize(
    "status, message, expected, not_sent",
    [
        (401, "invalid credentials", ProviderAuthError, True),
        (403, "forbidden", ProviderAuthError, True),
        (403, "rateLimitExceeded", ProviderRateLimited, True),
        (429, "too many", ProviderRateLimited, True),
        (503, "backend error", ProviderUnavailable, False),
        (400, "malformed", ProviderBadResponse, True),
    ],
)
def test_gmail_error_translation(status, message, expected, not_sent):
    """Retryability is the thing this mapping gets right or expensively wrong.

    403 is overloaded: a quota refusal and a broken delegation arrive with the
    same status, and only one of them is worth waiting out.
    """

    sender, _ = _gmail(error=_FakeHttpError(status, message))
    with pytest.raises(expected) as exc:
        sender.send(_message(), credential_ref="northwind-burner")

    # The second question the send step asks of the same error: can it rule
    # out that Gmail delivered the message anyway? Only a refusal can.
    assert definitely_not_sent(exc.value) is not_sent


def test_gmail_transport_failure_is_retryable():
    sender, _ = _gmail(error=OSError("connection reset"))
    with pytest.raises(ProviderUnavailable) as exc:
        sender.send(_message(), credential_ref="northwind-burner")

    # A reset connection can follow a delivered message.
    assert definitely_not_sent(exc.value) is False


def test_gmail_accepting_without_an_id_may_have_sent():
    sender, _ = _gmail(result={})
    with pytest.raises(ProviderBadResponse) as exc:
        sender.send(_message(), credential_ref="northwind-burner")

    assert definitely_not_sent(exc.value) is False


# ==========================================================================
# Selection: caps, spacing, rotation, pinning
# ==========================================================================


def test_rotation_prefers_the_mailbox_that_has_sent_least_today(session):
    busy = make_mailbox(session, email_address="busy@a.test")
    idle = make_mailbox(session, email_address="idle@a.test")
    lead, draft = make_approved_lead(session)
    record_send(session, busy, lead=lead, draft=draft, at=NOW - timedelta(hours=1))

    assert select_mailbox(session, now=NOW).id == idle.id


def test_rotation_breaks_a_tie_on_who_sent_longest_ago(session):
    recent = make_mailbox(
        session, email_address="recent@a.test", last_sent_at=NOW - timedelta(hours=1)
    )
    older = make_mailbox(
        session, email_address="older@a.test", last_sent_at=NOW - timedelta(hours=9)
    )
    assert recent.id < older.id  # so id order cannot be what decides it
    assert select_mailbox(session, now=NOW).id == older.id


def test_a_failing_mailbox_goes_to_the_back_of_the_rotation(session):
    sick = make_mailbox(session, email_address="sick@a.test", consecutive_failures=2)
    healthy = make_mailbox(session, email_address="healthy@a.test")
    assert sick.id < healthy.id
    assert select_mailbox(session, now=NOW).id == healthy.id


def test_a_capped_mailbox_is_not_a_candidate(session):
    capped = make_mailbox(session, email_address="capped@a.test", daily_cap=1)
    spare = make_mailbox(session, email_address="spare@a.test")
    lead, draft = make_approved_lead(session)
    record_send(session, capped, lead=lead, draft=draft, at=NOW - timedelta(minutes=5))

    assert headroom(session, capped, now=NOW) == 0
    assert select_mailbox(session, now=NOW).id == spare.id


def test_all_capped_defers_until_the_next_utc_day(session):
    mailbox = make_mailbox(session, daily_cap=1)
    lead, draft = make_approved_lead(session)
    record_send(session, mailbox, lead=lead, draft=draft, at=NOW - timedelta(minutes=5))

    with pytest.raises(MailboxUnavailable) as exc:
        select_mailbox(session, now=NOW)
    assert exc.value.retry_at == datetime(2026, 9, 22, tzinfo=timezone.utc)


def test_yesterdays_sends_do_not_count_against_todays_cap(session):
    mailbox = make_mailbox(session, daily_cap=1)
    lead, draft = make_approved_lead(session)
    record_send(session, mailbox, lead=lead, draft=draft, at=NOW - timedelta(days=1))

    assert sent_today(session, mailbox, now=NOW) == 0
    assert select_mailbox(session, now=NOW).id == mailbox.id


def test_spacing_defers_rather_than_failing(session):
    make_mailbox(
        session, min_spacing_seconds=300, last_sent_at=NOW - timedelta(seconds=60)
    )

    with pytest.raises(MailboxUnavailable) as exc:
        select_mailbox(session, now=NOW)
    assert exc.value.retry_at == NOW + timedelta(seconds=240)


def test_paused_and_disabled_mailboxes_are_never_selected(session):
    make_mailbox(session, email_address="paused@a.test", status=MailboxStatus.PAUSED)
    make_mailbox(
        session, email_address="disabled@a.test", status=MailboxStatus.DISABLED
    )

    with pytest.raises(NoMailboxConfigured):
        select_mailbox(session, now=NOW)


def test_no_mailbox_at_all_is_permanent_not_a_wait(session):
    with pytest.raises(NoMailboxConfigured):
        select_mailbox(session, now=NOW)


def _campaign(session, name="A", segment="logistics") -> Campaign:
    workspace = Workspace(slug=f"ws-{name.lower()}", name=f"WS {name}")
    session.add(workspace)
    session.flush()
    campaign = Campaign(workspace_id=workspace.id, name=name, segment=segment)
    session.add(campaign)
    session.flush()
    return campaign


def test_a_pinned_mailbox_owns_its_campaign(session):
    campaign = _campaign(session)
    make_mailbox(session, email_address="general@a.test")
    pinned = make_mailbox(
        session, email_address="pinned@a.test", campaign_id=campaign.id
    )

    assert select_mailbox(session, campaign_id=campaign.id, now=NOW).id == pinned.id


def test_a_pinned_mailbox_never_serves_another_campaign(session):
    campaign = _campaign(session)
    other = _campaign(session, name="B", segment="staffing")
    make_mailbox(session, email_address="pinned@a.test", campaign_id=campaign.id)

    # Nothing is in the general rotation, and the pin belongs to someone else.
    with pytest.raises(NoMailboxConfigured):
        select_mailbox(session, campaign_id=other.id, now=NOW)


# ==========================================================================
# The send step
# ==========================================================================


def test_send_records_the_message_and_marks_the_lead_sent(session, sender, outbox):
    mailbox = make_mailbox(session)
    lead, draft = make_approved_lead(session)

    sent = send_lead(session, lead, sender=sender, now=NOW)

    assert lead.status is LeadStatus.SENT
    assert sent.mailbox_id == mailbox.id
    assert sent.to_address == lead.email
    assert sent.from_address == mailbox.email_address
    assert sent.body == draft.body
    assert sent.rfc_message_id
    assert mailbox.last_sent_at == NOW
    assert len(outbox.read_text().strip().splitlines()) == 1


def test_send_refuses_a_lead_that_is_not_approved(session, sender):
    make_mailbox(session)
    lead, _ = make_approved_lead(session)
    lead.status = LeadStatus.READY  # never approved by a human
    session.flush()

    with pytest.raises(Exception, match="not approved"):
        send_lead(session, lead, sender=sender, now=NOW)
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_send_refuses_a_draft_that_no_longer_passes_verification(session, sender):
    """Approved at 9am, re-drafted at 11am, worker runs at noon."""

    make_mailbox(session)
    lead, draft = make_approved_lead(session)
    draft.verification_status = VerificationStatus.FAILED
    session.flush()

    with pytest.raises(Exception, match="not approvable"):
        send_lead(session, lead, sender=sender, now=NOW)
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_someone_suppressed_after_approval_is_held_not_sent(session, sender):
    """The ingest gate ran before this person unsubscribed. This one did not."""

    make_mailbox(session)
    lead, _ = make_approved_lead(session)
    session.add(SuppressionEntry(email=lead.email, reason="unsubscribed"))
    session.flush()

    with pytest.raises(Exception, match="suppressed"):
        send_lead(session, lead, sender=sender, now=NOW)

    assert lead.status is LeadStatus.HELD
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_an_unreadable_suppression_list_holds_the_lead(session, sender, monkeypatch):
    """Fails closed, exactly as ingest does: unreadable and 'on the list' look
    identical from here, and only one of the two mistakes is recoverable."""

    import mazz.pipeline.send as send_module
    from mazz.ingest.csv_import import SuppressionUnavailable

    class _Broken:
        def __init__(self, session):
            pass

        def check(self, email, company_domain, **_):
            raise SuppressionUnavailable("relation suppression_list does not exist")

        def check_lead(self, lead):
            raise SuppressionUnavailable("relation suppression_list does not exist")

    make_mailbox(session)
    lead, _ = make_approved_lead(session)
    monkeypatch.setattr(send_module, "SuppressionChecker", _Broken)

    with pytest.raises(Exception, match="could not be read"):
        send_lead(session, lead, sender=sender, now=NOW)

    assert lead.status is LeadStatus.HELD
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_a_capped_roster_defers_the_send(session, sender):
    mailbox = make_mailbox(session, daily_cap=1)
    lead, draft = make_approved_lead(session)
    other, other_draft = make_approved_lead(session, email="earlier@elsewhere.example")
    record_send(session, mailbox, lead=other, draft=other_draft, at=NOW - timedelta(minutes=5))

    with pytest.raises(JobDeferred) as exc:
        send_lead(session, lead, sender=sender, now=NOW)

    assert exc.value.retry_at == datetime(2026, 9, 22, tzinfo=timezone.utc)
    # Still approved, still queued — nothing was lost and nothing was sent.
    assert lead.status is LeadStatus.APPROVED


class _FailingSender(StubEmailSender):
    def __init__(self, exc, **kwargs):
        super().__init__(**kwargs)
        self._exc = exc

    def send(self, message, *, credential_ref):
        raise self._exc


def test_an_auth_failure_disables_the_mailbox(session, outbox):
    """One dead credential must not eat five leads' retry budgets."""

    mailbox = make_mailbox(session)
    lead, _ = make_approved_lead(session)
    sender = _FailingSender(
        ProviderAuthError("delegation revoked"), outbox_path=outbox
    )

    with pytest.raises(ProviderAuthError):
        send_lead(session, lead, sender=sender, now=NOW)

    assert mailbox.status is MailboxStatus.DISABLED
    assert mailbox.consecutive_failures == 1
    assert "delegation revoked" in mailbox.last_error
    assert lead.status is LeadStatus.APPROVED  # not sent, and not lost
    assert session.execute(select(SentMessage)).scalars().all() == []


def test_a_transient_failure_marks_health_but_keeps_the_mailbox(session, outbox):
    """A 503 is not the mailbox's fault -- and not proof nothing left, either.

    Gmail's send is not idempotent, so a 5xx is treated as "may have been
    sent": held for a human, not retried into a second copy.
    """

    mailbox = make_mailbox(session)
    lead, draft = make_approved_lead(session)
    sender = _FailingSender(
        ProviderUnavailable("503", status_code=503), outbox_path=outbox
    )

    with pytest.raises(PermanentJobError, match="MAY ALREADY HAVE BEEN SENT"):
        send_lead(session, lead, sender=sender, now=NOW)

    assert mailbox.status is MailboxStatus.ACTIVE
    assert mailbox.consecutive_failures == 1
    assert draft.send_attempted_at == NOW
    assert draft.send_attempt_mailbox_id == mailbox.id


def test_a_success_resets_the_failure_counter(session, sender):
    mailbox = make_mailbox(session, consecutive_failures=2, last_error="503")
    lead, _ = make_approved_lead(session)

    send_lead(session, lead, sender=sender, now=NOW)

    assert mailbox.consecutive_failures == 0
    assert mailbox.last_error is None


# ==========================================================================
# Through the worker, and the guardrail that nothing else enqueues a send
# ==========================================================================


def test_the_pipeline_never_enqueues_a_send_itself(session):
    """Guardrail 4, structurally: the chain stops at verify.

    `send` is registered so the worker can run it, but only a human action
    creates one. If some future stage chains into it, this fails.
    """

    import inspect

    import mazz.pipeline.draft as draft_module
    import mazz.pipeline.enrich as enrich_module
    import mazz.pipeline.qualify as qualify_module
    import mazz.pipeline.score as score_module
    import mazz.pipeline.verify as verify_module

    for module in (
        enrich_module,
        qualify_module,
        score_module,
        draft_module,
        verify_module,
    ):
        source = inspect.getsource(module)
        assert 'enqueue_next("send")' not in source
        assert "enqueue_next(SEND_JOB_KIND)" not in source


def test_exactly_one_place_in_the_codebase_creates_a_send_job():
    """Guardrail 4's real shape, counted rather than asserted.

    Approving used to enqueue the send itself. Now approving records a
    judgement and `mazz.web.routes.outbox` releases it -- so there must be
    exactly ONE call site, and it must be that one. A second one appearing
    anywhere, in a route or a stage or a script, is the auto-sender arriving
    by increments, which is the only way it ever would.
    """

    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "mazz"
    call = re.compile(r"enqueue_job\s*\([^)]*SEND_JOB_KIND")

    creators = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if call.search(path.read_text(encoding="utf-8"))
    )

    assert creators == ["web/routes/outbox.py"], (
        "exactly one module may create a send job, and it is the outbox "
        f"release action. Found: {creators}"
    )


def test_approving_does_not_create_a_send_job():
    """The approve route must not be able to queue a send even by accident.

    It does not import the queue or the send-job kind at all, so restoring the
    old behaviour means restoring an import too -- a visible line in a diff
    rather than one word changed inside an existing call.
    """

    import inspect

    import mazz.web.routes.leads as leads_module

    source = inspect.getsource(leads_module)
    assert "enqueue_job" not in source.replace("enqueued", ""), (
        "leads.py must not enqueue anything; releasing a send belongs to "
        "mazz.web.routes.outbox"
    )
    assert "SEND_JOB_KIND" not in source


def test_worker_runs_a_queued_send_end_to_end(session, monkeypatch, outbox):
    import mazz.pipeline.send as send_module

    make_mailbox(session)
    lead, _ = make_approved_lead(session)
    enqueue_job(session, lead.id, "send")
    session.commit()

    monkeypatch.setattr(
        send_module,
        "get_email_sender",
        lambda: StubEmailSender(outbox_path=outbox, clock=lambda: NOW),
    )

    job = process_next(handlers={**default_handlers(), "send": handle_send})
    assert job is not None

    session.expire_all()
    refreshed = session.get(Lead, lead.id)
    assert refreshed.status is LeadStatus.SENT
    assert session.get(Job, job.id).status is JobStatus.DONE
    assert len(session.execute(select(SentMessage)).scalars().all()) == 1


def test_a_deferred_send_does_not_spend_an_attempt(session, monkeypatch, outbox):
    """Five capped days must not kill work that was always going to succeed."""

    import mazz.pipeline.send as send_module

    # handle_send() takes no clock -- JobContext fixes its signature -- so the
    # daily-cap window is the real UTC day. The prior send has to land inside
    # THAT day, not inside the frozen NOW this module uses elsewhere, or the
    # mailbox looks uncapped and the job sends instead of deferring. Anchoring
    # to the wall clock is what keeps this test from going red the next day.
    now = datetime.now(timezone.utc)
    prior_at = max(now - timedelta(minutes=5), day_window(now)[0])

    mailbox = make_mailbox(session, daily_cap=1)
    lead, draft = make_approved_lead(session)
    other, other_draft = make_approved_lead(session, email="earlier@elsewhere.example")
    record_send(session, mailbox, lead=other, draft=other_draft, at=prior_at)
    job, _ = enqueue_job(session, lead.id, "send")
    job_id = job.id
    session.commit()

    monkeypatch.setattr(
        send_module,
        "get_email_sender",
        lambda: StubEmailSender(outbox_path=outbox, clock=lambda: now),
    )

    process_next(handlers={**default_handlers(), "send": handle_send})

    session.expire_all()
    refreshed = session.get(Job, job_id)
    assert refreshed.status is JobStatus.PENDING
    assert refreshed.attempts == 0
    assert "[deferred]" in refreshed.last_error
    assert refreshed.run_after > datetime.now(timezone.utc)


# ==========================================================================
# At most once: a retry never re-sends what may already have left
# ==========================================================================
#
# These go through `process_next` on purpose. The send step used to hand the
# message to Gmail and book the result in ONE transaction, so anything failing
# after Gmail accepted it rolled back to "never sent" and the queue sent it
# again. Tests that called `send_lead` directly and inspected the objects in
# memory could not see that: the rollback is the bug.


class _CountingSender(StubEmailSender):
    """A stub that remembers every message it was handed."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent_to: list[str] = []

    def send(self, message, *, credential_ref):
        self.sent_to.append(message.to_address)
        return super().send(message, credential_ref=credential_ref)


def _use_sender(monkeypatch, sender):
    import mazz.pipeline.send as send_module

    monkeypatch.setattr(send_module, "get_email_sender", lambda: sender)


def _run_send():
    return process_next(handlers={**default_handlers(), "send": handle_send})


def test_a_failure_after_the_wire_is_never_retried_into_a_second_send(
    session, monkeypatch, outbox
):
    import mazz.pipeline.send as send_module

    make_mailbox(session)
    lead, draft = make_approved_lead(session)
    job, _ = enqueue_job(session, lead.id, "send")
    job_id, lead_id, draft_id = job.id, lead.id, draft.id
    session.commit()

    sender = _CountingSender(outbox_path=outbox)
    _use_sender(monkeypatch, sender)

    # Gmail accepts; then booking the result blows up, as a dropped database
    # connection or a killed worker would.
    def booking_fails(*_args, **_kwargs):
        raise RuntimeError("server closed the connection unexpectedly")

    real_book = send_module._book_sent
    monkeypatch.setattr(send_module, "_book_sent", booking_fails)
    _run_send()
    monkeypatch.setattr(send_module, "_book_sent", real_book)
    assert sender.sent_to == [lead.email]  # it DID leave, before the failure

    session.expire_all()
    job = session.get(Job, job_id)
    assert job.status is JobStatus.PENDING  # an ordinary, retryable failure
    job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()

    _run_send()

    session.expire_all()
    assert sender.sent_to == [lead.email], "the retry sent the email a second time"
    job = session.get(Job, job_id)
    assert job.status is JobStatus.FAILED
    assert "MAY ALREADY HAVE BEEN SENT" in job.last_error
    assert "dana@outreach-northwind.com" in job.last_error
    assert session.get(Lead, lead_id).status is LeadStatus.HELD
    assert unresolved_send_attempt(session, session.get(Lead, lead_id)).id == draft_id


def test_an_auth_failure_through_the_worker_stays_disabled(
    session, monkeypatch, outbox
):
    """The disable used to be rolled back with the failed job's transaction."""

    make_mailbox(session)
    lead, draft = make_approved_lead(session)
    enqueue_job(session, lead.id, "send")
    draft_id = draft.id
    session.commit()

    _use_sender(
        monkeypatch,
        _FailingSender(ProviderAuthError("delegation revoked"), outbox_path=outbox),
    )
    _run_send()

    session.expire_all()
    mailbox = session.execute(select(Mailbox)).scalars().one()
    assert mailbox.status is MailboxStatus.DISABLED
    assert mailbox.consecutive_failures == 1
    assert "delegation revoked" in mailbox.last_error
    # A refused credential proves nothing left, so no send is left dangling.
    assert session.get(Draft, draft_id).send_attempted_at is None


@pytest.mark.parametrize(
    "refusal",
    [
        ProviderRateLimited("429", status_code=429),
        ProviderBadResponse("400 malformed", status_code=400),
    ],
)
def test_a_clear_refusal_leaves_the_send_free_to_retry(session, outbox, refusal):
    make_mailbox(session)
    lead, draft = make_approved_lead(session)

    with pytest.raises(type(refusal)):
        send_lead(
            session, lead, sender=_FailingSender(refusal, outbox_path=outbox), now=NOW
        )

    assert draft.send_attempted_at is None
    assert unresolved_send_attempt(session, lead) is None

    sender = _CountingSender(outbox_path=outbox, clock=lambda: NOW)
    send_lead(session, lead, sender=sender, now=NOW)
    assert sender.sent_to == [lead.email]
    assert lead.status is LeadStatus.SENT


def test_a_send_reclaimed_from_a_crashed_worker_is_held_not_resent(
    session, monkeypatch, outbox
):
    """Reclaim is safe for sends only because the marker was committed first."""

    mailbox = make_mailbox(session)
    lead, draft = make_approved_lead(session)
    job, _ = enqueue_job(session, lead.id, "send")
    crashed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    # The worker marked the attempt, handed the message over, and died.
    draft.send_attempted_at = crashed_at
    draft.send_attempt_mailbox_id = mailbox.id
    job.status = JobStatus.RUNNING
    job.locked_at = crashed_at
    job_id, lead_id = job.id, lead.id
    session.commit()

    reclaimed = reclaim_stale_jobs(session)
    session.commit()
    assert [j.id for j in reclaimed] == [job_id]
    assert str(crashed_at) in reclaimed[0].last_error

    sender = _CountingSender(outbox_path=outbox)
    _use_sender(monkeypatch, sender)
    _run_send()

    session.expire_all()
    assert sender.sent_to == []
    assert session.get(Job, job_id).status is JobStatus.FAILED
    assert session.get(Lead, lead_id).status is LeadStatus.HELD


def test_a_redraft_does_not_launder_an_unresolved_attempt(session, outbox):
    """The stranger may have the FIRST draft; a fresh one must not go too."""

    mailbox = make_mailbox(session)
    lead, first = make_approved_lead(session)
    first.send_attempted_at = NOW - timedelta(hours=1)
    first.send_attempt_mailbox_id = mailbox.id
    h.make_draft(
        session,
        lead,
        verification_status=VerificationStatus.PASSED,
        body=first.body,
    )
    session.flush()

    sender = _CountingSender(outbox_path=outbox, clock=lambda: NOW)
    with pytest.raises(PermanentJobError, match="MAY ALREADY HAVE BEEN SENT"):
        send_lead(session, lead, sender=sender, now=NOW)
    assert sender.sent_to == []


# ==========================================================================
# Delete-by-person still reaches everything (guardrail 5)
# ==========================================================================


def test_deleting_a_lead_removes_what_we_sent_him(session, sender):
    make_mailbox(session)
    lead, _ = make_approved_lead(session)
    send_lead(session, lead, sender=sender, now=NOW)
    session.commit()

    assert len(session.execute(select(SentMessage)).scalars().all()) == 1

    session.delete(session.get(Lead, lead.id))
    session.commit()

    assert session.execute(select(SentMessage)).scalars().all() == []
    # The mailbox itself is untouched — it is not personal data.
    assert len(session.execute(select(Mailbox)).scalars().all()) == 1
