"""Replies: read the mailboxes, classify deterministically, apply the stop rules.

No model calls. A reply is classified by headers and keyword rules over the
sender's OWN words (quoted history and our footer stripped, otherwise our
"Unsubscribe" footer would classify every reply):

    bounce               a DSN. Not a reply: handled via `mailbox.record_bounce`
                         (suppress, stop the sequence, pause on 5.7.x / >3%).
    ooo                  auto-reply headers or subject. Pauses the sequence.
    unsubscribe_request  suppressed AT ONCE, and flagged for a person to confirm.
    negative             a short, clear "not interested". Stops the sequence.
    needs_review         everything else. A genuine reply: stops the sequence.

The system never decides "interested". That is a human tag (`ReplyTag`).
Every automatic action is audited as `system`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from mazz import audit
from mazz.integrations import gmail_reader as gr
from mazz.models import (
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    Reply,
    SentMessage,
    SuppressionEntry,
)
from mazz.pipeline.mailbox import record_bounce
from mazz.pipeline.sequence import FOLLOWUP_JOB_KIND, due_after, lead_timezone

log = logging.getLogger(__name__)

OOO = "ooo"
UNSUBSCRIBE = "unsubscribe_request"
NEGATIVE = "negative"
NEEDS_REVIEW = "needs_review"

#: A lead gets at most this many OOO pauses; after that an OOO is only recorded.
MAX_OOO_PAUSES = 2
DEFAULT_OOO_DAYS = 7
#: A "not interested" longer than this is a conversation, not a brush-off.
NEGATIVE_MAX_CHARS = 400
REPLY_BODY_LIMIT = 5000
#: Hard cap on text read before any pattern runs. A reply is a person's
#: sentences; megabytes of it are an attack on the worker (the cursor does not
#: advance on failure, so one slow message would be re-read every cycle).
MAX_REPLY_CHARS = 50_000
MAX_DSN_CHARS = 200_000

# --------------------------------------------------------------------------
# Reading the sender's own words
# --------------------------------------------------------------------------

# Single-line bounded (Gmail wraps "On ... <addr>" / "wrote:" at most once): no
# pattern here may scan past a bounded window per line, because a stranger
# controls this text.
_WROTE = re.compile(r"^On\b[^\n]{0,300}(?:\n[^\n]{0,100})?\bwrote:[ \t]*$", re.M)
_ORIGINAL = re.compile(r"^-{2,}\s*(?:Original Message|Forwarded message)", re.M | re.I)
_OUTLOOK_HEADER = re.compile(r"^From:.*\n(?:Sent|Date):", re.M)
_OUR_FOOTER = re.compile(r"^(?:If this isn't relevant|Unsubscribe \(one click\))", re.M | re.I)


def strip_quoted(text: str) -> str:
    """The sender's own text: quoted history and our footer removed."""

    text = (text or "")[:MAX_REPLY_CHARS].replace("\r\n", "\n")
    for rx in (_WROTE, _ORIGINAL, _OUTLOOK_HEADER, _OUR_FOOTER):
        m = rx.search(text)
        if m:
            text = text[: m.start()]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

_AUTO_SUBJECT = re.compile(
    r"^\s*(?:(?:re|fw|fwd):\s*)*(?:(?:automatic|auto)[ -]?repl(?:y|ies)|autoreply|"
    r"out[ -]of[ -](?:the[ -])?office|ooo\b|away from (?:the )?office|vacation (?:reply|response))",
    re.I,
)
_UNSUB = re.compile(
    r"\b(?:unsubscribe|remove me|remove my (?:e-?mail|address)|stop (?:e-?mailing|contacting|"
    r"sending|messaging)|take me off|do not (?:e-?mail|contact)|don'?t (?:e-?mail|contact)|opt[ -]?out)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:not interested|no,? thanks|no,? thank you|not a fit|not looking|not at this time|"
    r"no interest|we(?:'re| are) (?:all set|good)|not for us|not needed|we(?:'re| are) covered|"
    r"already have (?:a|an|someone))\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Classification:
    kind: str
    needs_confirmation: bool = False


def is_auto_reply(headers: dict[str, str], subject: str) -> bool:
    auto = headers.get("auto-submitted", "").strip().lower()
    return bool(
        (auto and auto != "no")
        or "x-autoreply" in headers
        or "x-autorespond" in headers
        or headers.get("precedence", "").strip().lower() in ("auto_reply", "auto-reply")
        or _AUTO_SUBJECT.search(subject or "")
    )


def classify(headers: dict[str, str], subject: str, own_text: str) -> Classification:
    # An opt-out outranks the auto-reply headers: "remove me" sent from a
    # client that stamps Auto-Submitted is still a person asking to stop.
    if _UNSUB.search(own_text[:600]):
        return Classification(UNSUBSCRIBE, needs_confirmation=True)
    if is_auto_reply(headers, subject):
        return Classification(OOO)
    if len(own_text) <= NEGATIVE_MAX_CHARS and _NEGATIVE.search(own_text):
        return Classification(NEGATIVE)
    return Classification(NEEDS_REVIEW)


# --------------------------------------------------------------------------
# Out-of-office return dates
# --------------------------------------------------------------------------

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")]
    )
    for m in names
}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TRIGGER = re.compile(
    r"\b(?:return(?:ing|s)?|back(?: in the office| at work| in office)?|until|through|"
    r"available again)\b(?: on)?",
    re.I,
)
_MD = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I)
_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\b\.?(?:,?\s+(\d{{4}}))?", re.I)
_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _make(year: int | None, month: int, day: int, received_on: date) -> date | None:
    try:
        if year is not None:
            return date(year + 2000 if year < 100 else year, month, day)
        d = date(received_on.year, month, day)
        return d if d >= received_on - timedelta(days=1) else date(received_on.year + 1, month, day)
    except ValueError:
        return None


def _date_in(window: str, received_on: date) -> date | None:
    for rx, order in ((_MD, "md"), (_DM, "dm"), (_NUM, "num")):
        m = rx.search(window)
        if not m:
            continue
        a, b, y = m.groups()
        year = int(y) if y else None
        if order == "md":
            month, day = _MONTHS[a.lower()], int(b)
        elif order == "dm":
            month, day = _MONTHS[b.lower()], int(a)
        else:
            month, day = int(a), int(b)
        d = _make(year, month, day, received_on)
        if d:
            return d
    return None


def parse_return_date(text: str, received_on: date) -> date | None:
    """The date an out-of-office says the person is back, or None.

    "returning on October 14", "back 10/14", "until 14 Oct 2026". A bare
    weekday ("back Monday") is not parsed: None means the 7-day default.
    """

    for m in _TRIGGER.finditer(text or ""):
        d = _date_in(text[m.end() : m.end() + 60], received_on)
        if d and d <= received_on + timedelta(days=180):
            return d
    return None


def resume_at(lead: Lead, received_at: datetime, return_date: date | None) -> datetime:
    """The next business day after the return date (UTC, 09:00 local); with no
    date, 7 days after the auto-reply, rolled to a business day."""

    tz = lead_timezone(lead)
    if return_date is None:
        return due_after(received_at, DEFAULT_OOO_DAYS, tz)
    day = return_date + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(9, 0), tzinfo=tz).astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Applying a reply
# --------------------------------------------------------------------------

_PENDING_REVIEW = (LeadStatus.READY, LeadStatus.APPROVED, LeadStatus.RESEARCHING)


def _stop(lead: Lead) -> None:
    """End the sequence. A follow-up still sitting in review/outbox is held:
    nothing may leave for someone who has answered."""

    lead.sequence_state = "stopped"
    lead.next_step_due_at = None
    if lead.status in _PENDING_REVIEW:
        lead.status = LeadStatus.HELD


#: `added_by` on a suppression this module made from a reply's wording. The
#: only kind the inbox's "lift" may remove: a keyword rule can be wrong.
AUTO_SUPPRESSED_BY = "system:reply-sync"


def _suppress(session, email: str, reason: str) -> None:
    from mazz.ingest.csv_import import email_sha256

    address = email.strip().lower()
    if session.execute(
        select(SuppressionEntry.id).where(
            SuppressionEntry.email == address, SuppressionEntry.workspace_id.is_(None)
        )
    ).first() is None:
        session.add(
            SuppressionEntry(
                email=address, email_sha256=email_sha256(address), reason=reason,
                added_by=AUTO_SUPPRESSED_BY,
            )
        )


def apply_reply(
    session, mailbox: Mailbox, lead: Lead, msg: gr.ParsedMessage
) -> Reply | None:
    """Store, classify and act on one inbound message. None if already seen."""

    if session.execute(
        select(Reply.id).where(Reply.provider_message_id == msg.message_id)
    ).first():
        return None

    own = strip_quoted(msg.text)
    result = classify(msg.headers, msg.subject, own)
    return_date = (
        parse_return_date((own or msg.text)[:MAX_REPLY_CHARS], msg.received_at.date())
        if result.kind == OOO
        else None
    )
    prior_ooo = session.execute(
        select(Reply.id).where(Reply.lead_id == lead.id, Reply.classification == OOO)
    ).all()

    reply = Reply(
        lead_id=lead.id,
        mailbox_id=mailbox.id,
        provider_message_id=msg.message_id,
        provider_thread_id=msg.thread_id,
        from_address=msg.from_address,
        subject=msg.subject,
        body=own[:REPLY_BODY_LIMIT],
        received_at=msg.received_at,
        classification=result.kind,
        needs_confirmation=result.needs_confirmation,
        ooo_return_date=return_date,
    )
    session.add(reply)
    session.flush()

    detail: dict[str, Any] = {"reply_id": reply.id, "classification": result.kind}
    if result.kind == OOO:
        if lead.sequence_state == "active" and len(prior_ooo) < MAX_OOO_PAUSES:
            resume = resume_at(lead, msg.received_at, return_date)
            lead.sequence_state = "paused"
            lead.next_step_due_at = resume
            for job in session.execute(
                select(Job).where(
                    Job.lead_id == lead.id,
                    Job.kind == FOLLOWUP_JOB_KIND,
                    Job.status == JobStatus.PENDING,
                )
            ).scalars():
                job.run_after = resume
            detail["paused_until"] = resume.isoformat()
    else:
        _stop(lead)
        if result.kind == UNSUBSCRIBE:
            _suppress(session, lead.email, "unsubscribe request by reply")
            detail["suppressed"] = True

    audit.record(session, audit.SYSTEM, f"reply_{result.kind}", "lead", lead.id, **detail)
    session.flush()
    return reply


# --------------------------------------------------------------------------
# Bounces
# --------------------------------------------------------------------------

_STATUS = re.compile(r"\b([45]\.\d{1,3}\.\d{1,3})\b")
_NO_SUCH = re.compile(
    r"address not found|user unknown|no such user|does not exist|doesn'?t exist|unknown user",
    re.I,
)
BOUNCE_LOOKBACK = timedelta(days=45)


def is_bounce(msg: gr.ParsedMessage) -> bool:
    local = msg.from_address.partition("@")[0]
    ctype = msg.headers.get("content-type", "").lower()
    return (
        local in ("mailer-daemon", "postmaster")
        or ("multipart/report" in ctype and "delivery-status" in ctype)
        or "x-failed-recipients" in msg.headers
    )


def handle_bounce(session, mailbox: Mailbox, msg: gr.ParsedMessage) -> bool:
    """Match a DSN to the message it bounced and record a HARD bounce.
    True if one was recorded."""

    text = f"{msg.subject}\n{(msg.raw_text or msg.text)[:MAX_DSN_CHARS]}".lower()
    recent = session.execute(
        select(SentMessage)
        .where(
            SentMessage.mailbox_id == mailbox.id,
            SentMessage.sent_at >= msg.received_at - BOUNCE_LOOKBACK,
        )
        .order_by(SentMessage.sent_at.desc())
    ).scalars().all()

    sent = next(
        (s for s in recent if s.rfc_message_id and s.rfc_message_id.strip("<>").lower() in text),
        None,
    )
    if sent is None:
        failed = msg.headers.get("x-failed-recipients", "").lower()
        sent = next(
            (s for s in recent
             if (failed and s.to_address.lower() in failed) or s.to_address.lower() in text),
            None,
        )
    if sent is None or sent.bounced_at is not None:
        return False

    m = _STATUS.search(text)
    code = m.group(1) if m else ("5.1.1" if _NO_SUCH.search(text) else None)
    if code is None or not code.startswith("5.") or code == "5.2.2":
        log.info("bounce for %s is soft or unreadable (%s); not suppressing", sent.to_address, code)
        return False

    record_bounce(session, sent, code=code, now=msg.received_at)
    audit.record(session, audit.SYSTEM, "hard_bounce", "lead", sent.lead_id,
                 code=code, mailbox=mailbox.email_address)  # no address: audit outlives erasure
    return True


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SyncReport:
    replies: int = 0
    bounces: int = 0
    ignored: int = 0
    scanned: bool = False
    errors: list[str] = field(default_factory=list)


def process_message(session, mailbox: Mailbox, msg: gr.ParsedMessage, report: SyncReport) -> None:
    if msg.from_address == mailbox.email_address.strip().lower():
        report.ignored += 1
        return
    if is_bounce(msg):
        report.bounces += handle_bounce(session, mailbox, msg)
        return
    sent = (
        session.execute(
            select(SentMessage)
            .where(
                SentMessage.provider_thread_id == msg.thread_id,
                SentMessage.mailbox_id == mailbox.id,
            )
            .order_by(SentMessage.sent_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if msg.thread_id
        else None
    )
    lead = session.get(Lead, sent.lead_id) if sent else None
    if lead is None:
        report.ignored += 1
        return
    if apply_reply(session, mailbox, lead, msg) is not None:
        report.replies += 1


SCAN_DAYS = 14


def _scan(session, mailbox: Mailbox, service, now: datetime, report: SyncReport) -> None:
    """The fallback when there is no history cursor (first sync) or Gmail has
    expired it: walk the threads we started recently, plus recent DSNs."""

    report.scanned = True
    since = now - timedelta(days=SCAN_DAYS)
    threads = session.execute(
        select(SentMessage.provider_thread_id)
        .where(
            SentMessage.mailbox_id == mailbox.id,
            SentMessage.sent_at >= since,
            SentMessage.provider_thread_id.is_not(None),
        )
        .distinct()
    ).scalars().all()
    for thread_id in threads:
        for msg in gr.thread_messages(service, thread_id):
            process_message(session, mailbox, msg, report)
    listing = (
        service.users().messages()
        .list(userId="me", q=f"from:(mailer-daemon OR postmaster) newer_than:{SCAN_DAYS}d",
              maxResults=100)
        .execute()
    )
    for item in listing.get("messages") or []:
        process_message(session, mailbox, gr.get_message(service, item["id"]), report)


class WrongAccount(RuntimeError):
    """The token belongs to a different Google account than the mailbox."""


def sync_mailbox(session, mailbox: Mailbox, service, *, now: datetime | None = None) -> SyncReport:
    now = now or datetime.now(timezone.utc)
    report = SyncReport()
    prof = gr.profile(service)
    if str(prof.get("emailAddress", "")).lower() != mailbox.email_address.strip().lower():
        raise WrongAccount(
            f"token for {mailbox.email_address} reads as {prof.get('emailAddress')!r}"
        )
    head = str(prof["historyId"])

    if not mailbox.gmail_history_id:
        _scan(session, mailbox, service, now, report)
        cursor = head
    else:
        try:
            ids, cursor = gr.new_message_ids(service, mailbox.gmail_history_id)
        except gr.HistoryExpired:
            _scan(session, mailbox, service, now, report)
            cursor = head
        else:
            for mid in ids:
                process_message(session, mailbox, gr.get_message(service, mid), report)
    mailbox.gmail_history_id = cursor
    mailbox.last_synced_at = now
    session.flush()
    return report


def _flag_reconnect(mailbox_id: int, why: str) -> None:
    from mazz.db import session_scope

    with session_scope() as session:
        mailbox = session.get(Mailbox, mailbox_id)
        if mailbox is not None:
            mailbox.status = MailboxStatus.NEEDS_RECONNECT
            mailbox.last_error = why[:4000]
            log.error("mailbox %s needs reconnect: %s", mailbox.email_address, why)


def sync_all(
    *, service_for: Callable[[str], Any] | None = None, now: datetime | None = None
) -> dict[str, SyncReport]:
    """Sync every connected mailbox, each in its own transaction so one bad
    mailbox cannot roll back another's replies. A revoked grant flags the
    mailbox `needs_reconnect`; anything else is logged and retried next run."""

    from mazz.db import session_scope
    from mazz.integrations.email_sender import MailboxNeedsReconnect
    from mazz.integrations.google_oauth import is_revoked, service_for_mailbox

    service_for = service_for or service_for_mailbox
    with session_scope() as session:
        targets = [
            (m.id, m.email_address)
            for m in session.execute(
                select(Mailbox).where(
                    Mailbox.status.in_((MailboxStatus.ACTIVE, MailboxStatus.PAUSED)),
                    Mailbox.oauth_refresh_token_enc.is_not(None),
                )
            ).scalars()
        ]
    reports: dict[str, SyncReport] = {}
    for mailbox_id, address in targets:
        try:
            with session_scope() as session:
                mailbox = session.get(Mailbox, mailbox_id)
                reports[address] = sync_mailbox(session, mailbox, service_for(address), now=now)
        except (MailboxNeedsReconnect, WrongAccount) as exc:
            _flag_reconnect(mailbox_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - one mailbox must not stop the rest
            if is_revoked(exc):
                _flag_reconnect(mailbox_id, f"Google refused the refresh token: {exc}")
            else:
                log.exception("reply sync failed for %s", address)
    return reports


if __name__ == "__main__":  # python -m mazz.pipeline.replies
    logging.basicConfig(level=logging.INFO)
    for address, rep in sync_all().items():
        print(f"{address}: {rep.replies} replies, {rep.bounces} bounces, scanned={rep.scanned}")
