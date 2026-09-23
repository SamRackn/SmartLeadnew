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
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from mazz.ingest.csv_import import SuppressionChecker, SuppressionUnavailable
from mazz.integrations.base import ProviderAuthError, ProviderError
from mazz.integrations.email_sender import (
    EmailSender,
    OutgoingMessage,
    get_email_sender,
)
from mazz.models import Draft, Lead, LeadStatus, Mailbox, MailboxStatus, SentMessage
from mazz.pipeline.draft import unsubscribe_url
from mazz.pipeline.mailbox import (
    MailboxUnavailable,
    NoMailboxConfigured,
    select_mailbox,
)
from mazz.pipeline.queue import JobContext, JobDeferred, PermanentJobError
from mazz.pipeline.verify import is_approvable, latest_draft

log = logging.getLogger(__name__)

__all__ = ["SEND_JOB_KIND", "handle_send", "send_lead"]

#: ``jobs.kind`` for this step. The review app enqueues it on approve.
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


def _outgoing(lead: Lead, draft: Draft, mailbox: Mailbox) -> OutgoingMessage:
    """Build the message. The body is used verbatim -- footer already attached."""

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
        },
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
    sender = sender or get_email_sender()

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
    if not is_approvable(draft):
        raise PermanentJobError(
            f"draft {draft.id} is not approvable "
            f"(verification_status={draft.verification_status.value}); it must "
            "have been re-drafted or rejected after approval. Refusing to send."
        )

    # -- suppression, again, now ------------------------------------------
    try:
        match = SuppressionChecker(session).check(lead.email, lead.company_domain)
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

    # -- pick a mailbox ----------------------------------------------------
    try:
        mailbox = select_mailbox(session, campaign_id=lead.campaign_id, now=now)
    except MailboxUnavailable as exc:
        # Waiting on a cap or on spacing. Not a failure — come back later.
        raise JobDeferred(str(exc), retry_at=exc.retry_at) from exc
    except NoMailboxConfigured as exc:
        raise PermanentJobError(str(exc)) from exc

    message = _outgoing(lead, draft, mailbox)

    # -- send --------------------------------------------------------------
    try:
        receipt = sender.send(message, credential_ref=mailbox.credential_ref)
    except ProviderError as exc:
        _record_mailbox_failure(session, mailbox, exc)
        raise

    # -- book it -----------------------------------------------------------
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

    log.info(
        "sent lead %s from %s (message %s)",
        lead.id,
        mailbox.email_address,
        receipt.provider_message_id or receipt.rfc_message_id,
    )
    return sent


def _record_mailbox_failure(session, mailbox: Mailbox, exc: ProviderError) -> None:
    """Mark the mailbox's health, and disable it if the credential is dead.

    A rate limit or a 503 says nothing about the mailbox; a 401 says the
    delegation is broken, and every subsequent send from it will fail the same
    way. Leaving it ``active`` would hand it the next lead, and the one after,
    until five jobs have burned their attempts against one bad credential.
    """

    mailbox.consecutive_failures = (mailbox.consecutive_failures or 0) + 1
    mailbox.last_error = f"{exc.__class__.__name__}: {exc}"[:4000]
    if isinstance(exc, ProviderAuthError):
        mailbox.status = MailboxStatus.DISABLED
        log.error(
            "mailbox %s disabled: authentication failed (%s)",
            mailbox.email_address,
            exc,
        )
    session.flush()


def handle_send(ctx: JobContext) -> None:
    """Queue handler for ``kind='send'``. Nothing chains after it."""

    send_lead(ctx.session, ctx.lead)
