"""The send step: the last thing that happens to a lead, and the only
irreversible one.

Every other stage in this pipeline can be re-run. Research can be refetched, a
draft rewritten, a verdict recomputed. This one puts a message in a stranger's
inbox and there is no taking it back, so it re-checks, at the last possible
moment, everything an earlier stage already checked:

* **The human approval still stands.** The lead must be ``approved`` right now.
  Approval happened in the review app, possibly hours ago; if the copy was
  re-drafted or rejected since, this refuses. Guardrail 4 is not "a human
  approved something once", it is "a human approved *this*".
* **The draft still passes verification.** `is_approvable()` again, here, not
  because the route forgot but because the row could have changed under us.
* **The person is still not suppressed.** The ingest gate (guardrail 1) ran
  when the lead arrived. Somebody who unsubscribed yesterday was not on that
  list then and is now, and the moment of sending is the only moment that
  matters. It fails CLOSED, exactly as ingest does: an unreadable
  ``suppression_list`` holds the lead rather than sending into the dark.

None of these re-checks can be traded for throughput. They are each cheap --
one SELECT -- next to the cost of the thing they prevent.

**At most once.** Gmail's send is not idempotent, and the queue retries. So
the attempt is marked on the draft and COMMITTED before the message is handed
over, and cleared only when the sender's failure proves nothing left. A retry
that finds a marker with no booked ``sent_messages`` row -- the booking
failed, the worker died, Gmail timed out -- holds the lead and names the
mailbox to check, instead of emailing the same stranger a second time.
``python -m mazz.ops resolve-send`` records what the human found.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import exists, select

from mazz.ingest.csv_import import SuppressionChecker, SuppressionUnavailable
from mazz.integrations.base import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
)
from mazz.integrations.email_sender import (
    EmailSender,
    MailboxNeedsReconnect,
    OutgoingMessage,
    get_email_sender,
)
from mazz.models import Draft, Lead, LeadStatus, Mailbox, MailboxStatus, SentMessage
from mazz import audit
from mazz.config import get_settings
from mazz.integrations.base import find_repo_root, setting
from mazz.integrations.email_sender import STUB_PROVIDER, build_mime
from mazz.pipeline.draft import (
    RELEVANCE_LINE,
    MissingSenderAddress,
    postal_address_for,
    split_body,
    unresolved_placeholders,
    unsubscribe_base_url,
    unsubscribe_token,
    unsubscribe_url,
)
from mazz.pipeline.mailbox import (
    MailboxUnavailable,
    NoMailboxConfigured,
    select_mailbox,
)
from mazz.pipeline.queue import JobContext, JobDeferred, PermanentJobError
from mazz.pipeline.mailbox import pause_mailbox
from mazz.pipeline.sequence import lead_timezone, next_window_open, schedule_next_step
from mazz.pipeline.verify import is_approvable, latest_draft

log = logging.getLogger(__name__)

__all__ = [
    "SEND_JOB_KIND",
    "SendDisabled",
    "check_live_send_ready",
    "write_dry_run",
    "definitely_not_sent",
    "handle_send",
    "send_lead",
    "unresolved_send_attempt",
]

#: ``jobs.kind`` for this step. Only the outbox's Send action enqueues it.
SEND_JOB_KIND = "send"


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: The footer line `render_footer` writes. Parsed, not re-derived — see
#: `_unsubscribe_link`.
_FOOTER_UNSUBSCRIBE_RE = re.compile(r"Unsubscribe \(one click\):\s*(\S+)")


def _unsubscribe_link(lead: Lead, draft: Draft) -> str:
    """The one-click unsubscribe URL, taken FROM THE APPROVED BODY.

    Lifted out of the footer rather than recomputed, so the `List-Unsubscribe`
    header and the link the recipient can see are the same URL by construction
    rather than by coincidence. They can otherwise drift: the token is an HMAC
    keyed on ``SESSION_SECRET``, so a worker running with a different secret
    from the app that rendered the draft — or any secret rotation between
    drafting and sending — produces a header pointing somewhere the body does
    not. Someone would press the native unsubscribe button, hit a token the
    route cannot match, and keep receiving mail.

    Falls back to deriving it if the footer cannot be parsed, which should not
    happen (`render_body` always writes it and verification rejects a draft
    with no footer) but must not mean sending with no unsubscribe header.
    """

    match = _FOOTER_UNSUBSCRIBE_RE.search(draft.body or "")
    if match:
        return match.group(1)
    log.warning(
        "draft %s has no parseable unsubscribe footer; deriving the "
        "List-Unsubscribe header instead",
        draft.id,
    )
    return unsubscribe_url(lead)


def _outgoing(
    lead: Lead, draft: Draft, mailbox: Mailbox, thread: list[SentMessage] | None = None
) -> OutgoingMessage:
    """Build the message. The body is used verbatim -- footer already attached.

    ``thread`` is what was already sent to this lead, oldest first: a
    follow-up replies to the last of them and lists them all in References.
    """

    ids = [m.rfc_message_id for m in (thread or []) if m.rfc_message_id]
    threading = {"In-Reply-To": ids[-1], "References": " ".join(ids)} if ids else {}
    return OutgoingMessage(
        from_address=mailbox.email_address,
        from_name=mailbox.display_name,
        to_address=lead.email,
        to_name=lead.full_name,
        subject=draft.subject,
        body=draft.body,
        headers={
            "List-Unsubscribe": f"<{_unsubscribe_link(lead, draft)}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            **threading,
        },
        thread_id=next(
            (m.provider_thread_id for m in reversed(thread or []) if m.provider_thread_id), None
        ),
    )


def send_lead(
    session,
    lead: Lead,
    *,
    sender: EmailSender | None = None,
    now: datetime | None = None,
) -> SentMessage:
    """Send this lead's approved draft from one of the client's mailboxes.

    Returns the `SentMessage` audit row. Raises rather than returning anything
    falsey: a send that did not happen must never be mistakable for one that
    did, because the caller marks the lead ``sent`` on the strength of it.
    """

    now = now or _now()

    # Hold the lead's row from here until the attempt marker commits, and read
    # fresh state: a reply, bounce or unsubscribe committed by the reply sync
    # an instant ago must be seen, not the copy this session loaded earlier.
    # ponytail: the lock ends at the marker commit, so a reply landing in the
    # milliseconds before the wire still leaves; closing it would mean holding
    # a transaction across the Gmail call.
    session.flush()
    session.refresh(lead, ["status", "sequence_state", "next_step_due_at"], with_for_update=True)

    # -- the approval must still stand ------------------------------------
    if lead.status != LeadStatus.APPROVED:
        raise PermanentJobError(
            f"lead {lead.id} is {lead.status.value}, not approved — refusing to "
            "send. Only a human pressing approve puts a lead in this queue, and "
            "only an approval that is still current may leave."
        )

    draft = latest_draft(session, lead)
    if draft is None:
        raise PermanentJobError(f"lead {lead.id} has no draft to send")
    if session.execute(
        select(SentMessage.id).where(SentMessage.draft_id == draft.id)
    ).first() is not None:
        raise PermanentJobError(
            f"draft {draft.id} was already sent to lead {lead.id}; a draft goes "
            "out once. A follow-up is a new draft."
        )
    if not is_approvable(draft):
        raise PermanentJobError(
            f"draft {draft.id} is not approvable "
            f"(verification_status={draft.verification_status.value}); it must "
            "have been re-drafted or rejected after approval. Refusing to send."
        )

    # -- the sequence must still be running --------------------------------
    # A reply, bounce or unsubscribe can land between approve and send.
    if lead.sequence_state == "stopped":
        lead.status = LeadStatus.HELD
        session.flush()
        raise PermanentJobError(
            f"lead {lead.id}'s sequence was stopped (reply, bounce or unsubscribe) "
            "after this was approved; held, not sent."
        )
    if lead.sequence_state == "paused" and (lead.next_step_due_at or now) > now:
        raise JobDeferred(
            f"lead {lead.id} is paused (out of office) until {lead.next_step_due_at}",
            retry_at=lead.next_step_due_at,
        )

    # -- a previous attempt must be resolved -------------------------------
    # Lead-wide, not just this draft: a lead re-drafted after an ambiguous
    # send has a fresh draft with no marker, and the stranger may already
    # have the first one.
    unresolved = unresolved_send_attempt(session, lead)
    if unresolved is not None:
        attempted_from = (
            session.get(Mailbox, unresolved.send_attempt_mailbox_id)
            if unresolved.send_attempt_mailbox_id is not None
            else None
        )
        _hold_unresolved(session, lead)
        raise PermanentJobError(
            _may_have_been_sent(lead, unresolved, attempted_from)
            + " Refusing to try again."
        )

    # -- suppression, again, now ------------------------------------------
    try:
        match = SuppressionChecker(session).check_lead(lead)
    except SuppressionUnavailable as exc:
        # Fails closed, exactly as the ingest gate does.
        lead.status = LeadStatus.HELD
        session.flush()
        raise PermanentJobError(
            f"suppression_list could not be read at send time ({exc}); holding "
            f"lead {lead.id} rather than sending. An unreadable never-contact "
            "list and a list with this person on it look identical from here."
        ) from exc
    if match is not None:
        lead.status = LeadStatus.HELD
        session.flush()
        raise PermanentJobError(
            f"lead {lead.id} is suppressed ({match}) — approved earlier, "
            "suppressed since. Held, not sent."
        )

    # -- the copy must be whole, and its footer still true -----------------
    holes = unresolved_placeholders(draft.subject, draft.body)
    if holes:
        _hold(session, lead)
        raise PermanentJobError(
            f"draft {draft.id} has unfilled merge fields or no subject {holes}; "
            "a template rendered with a hole in it is never sent."
        )
    try:
        address = postal_address_for(lead)
    except MissingSenderAddress as exc:
        _hold(session, lead)
        raise PermanentJobError(f"lead {lead.id}: {exc}") from exc
    footer = split_body(draft.body)[1] or ""
    footer_lines = [line.strip() for line in footer.splitlines()]
    # Address and opt-out line compared exactly; the unsubscribe link only
    # for presence -- its token may legitimately differ after a secret
    # rotation, and the link in the body is the one that is sent (see
    # `_unsubscribe_link`).
    if (
        address not in footer_lines
        or RELEVANCE_LINE not in footer_lines
        or not _FOOTER_UNSUBSCRIBE_RE.search(footer)
    ):
        _hold(session, lead)
        raise PermanentJobError(
            f"draft {draft.id}'s footer does not carry the current confirmed "
            "sender address, the opt-out line and an unsubscribe link (the "
            "address changed since it was drafted, or the body was damaged). "
            "Held: re-draft it."
        )

    enabled = bool(get_settings().send_enabled)
    if enabled:
        sender = sender or get_email_sender()
        check_live_send_ready(lead, draft, sender)

    # -- the send window ----------------------------------------------------
    if get_settings().send_window:
        tz = lead_timezone(lead)
        opening = next_window_open(now, tz)
        if opening is not None:
            raise JobDeferred(
                f"outside lead {lead.id}'s send window (Mon-Fri 9-17 {tz.key}); "
                f"opens {opening.isoformat()}",
                retry_at=opening,
            )

    # -- pick a mailbox ----------------------------------------------------
    thread = session.execute(
        select(SentMessage)
        .where(SentMessage.lead_id == lead.id)
        .order_by(SentMessage.sent_at, SentMessage.id)
    ).scalars().all()
    if (draft.step or 1) == 1 and thread:
        # Email 1 exactly once, here too: the draft stage refuses it, but a
        # draft written before the lead was recorded as emailed (a Smartlead
        # import, a hand-resolved send) can still be approved.
        _hold(session, lead)
        raise PermanentJobError(
            f"lead {lead.id} has already been emailed ({len(thread)} message(s) on "
            "record); refusing to send Email 1 again. Held."
        )
    if (draft.step or 1) > 1 and not thread:
        raise PermanentJobError(
            f"draft {draft.id} is follow-up step {draft.step} but nothing was ever "
            f"sent to lead {lead.id}; refusing to send a follow-up with no thread"
        )
    if thread and not (thread[-1].provider_thread_id and thread[-1].rfc_message_id):
        # A Smartlead send whose Gmail thread was never adopted: an unthreaded
        # "Re: Quick Question" reads as spam and its replies would go unseen.
        _hold(session, lead)
        raise PermanentJobError(
            f"lead {lead.id}'s last email has no Gmail thread on record: a Smartlead "
            "send not yet adopted (`python -m mazz.ingest.smartlead adopt`), or a send "
            "resolved by hand with `ops resolve-send`, whose thread must be continued "
            "by hand from the mailbox. Held."
        )
    try:
        mailbox = select_mailbox(
            session,
            campaign_id=lead.campaign_id,
            now=now,
            only=thread[-1].mailbox_id if thread else None,
        )
    except MailboxUnavailable as exc:
        # Waiting on a cap or on spacing. Not a failure — come back later.
        raise JobDeferred(str(exc), retry_at=exc.retry_at) from exc
    except NoMailboxConfigured as exc:
        raise PermanentJobError(str(exc)) from exc

    message = _outgoing(lead, draft, mailbox, thread)

    # -- the kill switch ----------------------------------------------------
    # Before the attempt marker and before any sender exists: with sending
    # off, nothing is handed to anything that could put it on the wire.
    if not enabled:
        path = write_dry_run(message, lead, draft, now=now)
        raise SendDisabled(path)

    # -- mark the attempt, durably, BEFORE the wire -------------------------
    # Everything after `sender.send` returns can fail: the booking, the
    # commit, the process. Without a committed marker, each of those rolls
    # back to "never sent" and the queue sends the same email again.
    draft.send_attempted_at = now
    draft.send_attempt_mailbox_id = mailbox.id
    session.commit()

    # -- send --------------------------------------------------------------
    try:
        receipt = sender.send(message, credential_ref=mailbox.credential_ref)
    except ProviderError as exc:
        _record_mailbox_failure(session, mailbox, exc)
        not_sent = definitely_not_sent(exc)
        if not_sent:
            draft.send_attempted_at = None
            draft.send_attempt_mailbox_id = None
        else:
            lead.status = LeadStatus.HELD
        # Committed before raising: the job's own transaction is about to be
        # rolled back, and a mailbox disabled for a dead credential that
        # un-disables itself on rollback hands the next lead the same 401.
        session.commit()
        if not_sent:
            raise
        raise PermanentJobError(
            f"{_may_have_been_sent(lead, draft, mailbox)} The sender failed "
            f"ambiguously: {exc.__class__.__name__}: {exc}"
        ) from exc

    # -- book it -----------------------------------------------------------
    sent = _book_sent(session, lead, draft, mailbox, message, receipt)

    log.info(
        "sent lead %s from %s (message %s)",
        lead.id,
        mailbox.email_address,
        receipt.provider_message_id or receipt.rfc_message_id,
    )
    return sent


def _book_sent(
    session,
    lead: Lead,
    draft: Draft,
    mailbox: Mailbox,
    message: OutgoingMessage,
    receipt,
) -> SentMessage:
    """Record a message that has left. Runs only after the sender accepted it.

    Its own function so the seam between "Gmail has it" and "we know Gmail
    has it" is one named place -- the place the attempt marker exists for.
    """

    sent = SentMessage(
        lead_id=lead.id,
        draft_id=draft.id,
        mailbox_id=mailbox.id,
        from_address=message.from_address,
        to_address=message.to_address,
        subject=message.subject,
        body=message.body,
        rfc_message_id=receipt.rfc_message_id,
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
        sent_at=receipt.sent_at,
    )
    session.add(sent)

    mailbox.last_sent_at = receipt.sent_at
    mailbox.consecutive_failures = 0
    mailbox.last_error = None

    lead.status = LeadStatus.SENT
    session.flush()
    schedule_next_step(session, lead, draft, receipt.sent_at)
    session.flush()
    return sent


def definitely_not_sent(exc: ProviderError) -> bool:
    """True only when the sender's failure proves nothing left.

    Gmail's ``messages.send`` is not idempotent, so every other outcome is
    treated as "may have been sent". A refused credential, a rate limit and a
    4xx are the provider declining the request; a 5xx, a timeout, a reset
    connection or an acceptance with no message id can each follow a
    delivered message. Guessing wrong one way holds a lead for a human to
    clear in a minute; guessing wrong the other way emails a stranger twice.
    """

    if isinstance(exc, (ProviderAuthError, ProviderRateLimited)):
        return True
    status = exc.status_code
    return status is not None and 400 <= int(status) < 500


def unresolved_send_attempt(session, lead: Lead) -> Draft | None:
    """This lead's draft that was handed to the sender with no booked result."""

    return (
        session.execute(
            select(Draft)
            .where(
                Draft.lead_id == lead.id,
                Draft.send_attempted_at.isnot(None),
                ~exists().where(SentMessage.draft_id == Draft.id),
            )
            .order_by(Draft.send_attempted_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _hold_unresolved(session, lead: Lead) -> None:
    """Take a may-have-been-sent lead out of the outbox, durably.

    ``held`` and committed here rather than left to the queue: the queue's
    give-up path deliberately never holds an ``approved`` lead, and one left
    ``approved`` sits in the outbox looking ready to send when it may already
    be in the recipient's inbox.
    """

    lead.status = LeadStatus.HELD
    session.commit()


def _may_have_been_sent(lead: Lead, draft: Draft, mailbox: Mailbox | None) -> str:
    where = mailbox.email_address if mailbox is not None else "the sending mailbox"
    return (
        f"Lead {lead.id} ({lead.email}) MAY ALREADY HAVE BEEN SENT draft "
        f"{draft.id}: it was handed to the sender at {draft.send_attempted_at} "
        f"and no result was booked. Check the Sent folder of {where}, then "
        f"resolve it with `python -m mazz.ops resolve-send {lead.id} "
        "--sent` or `--not-sent`."
    )


def _record_mailbox_failure(session, mailbox: Mailbox, exc: ProviderError) -> None:
    """Mark the mailbox's health, and disable it if the credential is dead.

    A rate limit or a 503 says nothing about the mailbox; a 401 says the
    delegation is broken, and every subsequent send from it will fail the same
    way. Leaving it ``active`` would hand it the next lead, and the one after,
    until five jobs have burned their attempts against one bad credential.
    """

    mailbox.consecutive_failures = (mailbox.consecutive_failures or 0) + 1
    mailbox.last_error = f"{exc.__class__.__name__}: {exc}"[:4000]
    if re.search(r"\b5\.7\.\d+", str(exc)) and not isinstance(exc, ProviderAuthError):
        # Policy/reputation refusal at submission: stop using it until a
        # person has looked. Paused, not disabled -- the credential is fine.
        pause_mailbox(mailbox, f"5.7.x rejection at send: {exc}")
        log.error("mailbox %s auto-paused: %s", mailbox.email_address, exc)
    if isinstance(exc, MailboxNeedsReconnect):
        mailbox.status = MailboxStatus.NEEDS_RECONNECT
        log.error("mailbox %s needs reconnect: %s", mailbox.email_address, exc)
    elif isinstance(exc, ProviderAuthError):
        mailbox.status = MailboxStatus.DISABLED
        log.error(
            "mailbox %s disabled: authentication failed (%s)",
            mailbox.email_address,
            exc,
        )
    session.flush()


class SendDisabled(Exception):
    """``SEND_ENABLED`` is false: the message was rendered to disk, not sent.

    Not a failure and not a send. The lead stays ``approved`` and nothing is
    booked, so nothing downstream (caps, follow-up clocks, the dashboard)
    believes an email left.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"SEND_ENABLED is false; MIME written to {path}, nothing sent")
        self.path = path


def dry_run_dir() -> Path:
    return Path(setting("DRYRUN_DIR") or (find_repo_root() / "out" / "dryrun"))


def write_dry_run(message: OutgoingMessage, lead: Lead, draft: Draft, *, now: datetime) -> Path:
    """The exact MIME the live sender would transmit, as an .eml on disk."""

    directory = dry_run_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y%m%dT%H%M%S}-lead{lead.id}-draft{draft.id}.eml"
    path.write_bytes(build_mime(message, now=now).as_bytes())
    log.info("dry run: lead %s rendered to %s (SEND_ENABLED is false)", lead.id, path)
    return path


def check_live_send_ready(lead: Lead, draft: Draft, sender: EmailSender) -> None:
    """Refuse a LIVE send that would carry a footer we cannot stand behind.

    The stub writes to a local file and is exempt. A real provider needs the
    lead in a workspace (so its address is the confirmed one, not the env
    fallback) and an unsubscribe link on a real https host -- a link to the
    placeholder host is a dead unsubscribe, which is a compliance failure
    wearing a working footer.
    """

    if sender.provider == STUB_PROVIDER:
        return
    if lead.campaign is None:
        raise PermanentJobError(
            f"lead {lead.id} has no campaign, so no workspace and no confirmed "
            "postal address; live sending refuses unrouted leads."
        )
    link = _unsubscribe_link(lead, draft)
    if not link.endswith(f"/u/{unsubscribe_token(lead)}"):
        # Drafted under another key (UNSUBSCRIBE_SECRET set or changed since):
        # /u/ can no longer match it, so the recipient could not opt out.
        raise PermanentJobError(
            f"draft {draft.id}'s unsubscribe link no longer resolves under the current "
            "UNSUBSCRIBE_SECRET; re-draft it before sending live."
        )
    if not link.startswith("https://") or ".example" in link.split("/u/")[0]:
        raise PermanentJobError(
            f"draft {draft.id}'s unsubscribe link is {link!r}, which is not a live "
            "https unsubscribe host. Set UNSUBSCRIBE_BASE_URL (currently "
            f"{unsubscribe_base_url()!r}) and re-draft before sending live."
        )


def _hold(session, lead: Lead) -> None:
    """Park a lead the send step refused, durably (the job transaction is
    about to roll back)."""

    lead.status = LeadStatus.HELD
    session.commit()


def handle_send(ctx: JobContext) -> None:
    """Queue handler for ``kind='send'``. Nothing chains after it."""

    try:
        send_lead(ctx.session, ctx.lead)
    except SendDisabled as exc:
        audit.record(
            ctx.session, audit.SYSTEM, "dry_run_send", "lead", ctx.lead.id, path=str(exc.path)
        )
