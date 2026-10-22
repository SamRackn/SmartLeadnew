"""Which mailbox sends the next approved email, and whether it may send now.

This is the rotation half of CLAUDE.md's routing triple (``lead ->
RoutingPolicy.route(lead) -> (campaign, segment, mailbox)``). The campaign arm
runs at ingest, in `routing.py`. The mailbox arm runs HERE, at send time, and
deliberately not at ingest: a mailbox chosen on Monday is a fact about Monday's
caps, health and rotation order, and the lead may not clear human review until
Thursday. Persisting that choice early would mean sending from a mailbox that
has since filled its day, gone into warmup, or lost its authentication.

Three rules, in the order they bite:

1. **A pinned mailbox owns its campaign.** ``mailboxes.campaign_id`` is
   CLAUDE.md's "one-campaign-per-mailbox must remain possible without a
   migration". If any mailbox is pinned to this lead's campaign, only those
   mailboxes serve it; otherwise the general (unpinned) rotation does. A pinned
   mailbox never leaks into another campaign's traffic.

2. **Caps and spacing are deliverability, not throughput knobs.** Five mailboxes
   on burner domains are the client's whole sending capacity. Emptying a day's
   cap in ninety seconds is the pattern that gets a domain filtered, so a
   mailbox with no headroom, or one that sent too recently, is not a candidate —
   the send waits. "Wait" is not "fail": the job re-arms for the moment the
   first mailbox frees up, and nothing is held or lost.

3. **No mailbox configured is an operator error, said out loud.** It raises
   permanently rather than retrying quietly forever, so the job lands in the
   dashboard's "Jobs that gave up" and the lead is held, visibly, instead of a
   queue that looks busy while nothing can ever leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from mazz.models import Draft, Lead, Mailbox, MailboxStatus, SentMessage, SuppressionEntry

__all__ = [
    "MailboxUnavailable",
    "NoMailboxConfigured",
    "day_window",
    "headroom",
    "next_eligible_at",
    "select_mailbox",
    "sent_today",
]


class MailboxUnavailable(Exception):
    """Every candidate mailbox is capped or still spacing. Try again later.

    ``retry_at`` is when the first one frees up, so the caller re-arms the job
    for that instant instead of spending one of its finite attempts on a
    condition that is not an error.
    """

    def __init__(self, message: str, *, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class NoMailboxConfigured(Exception):
    """No mailbox can EVER serve this lead. Permanent until someone acts."""


#: The cap's day. Every US send window (Mon-Fri 9-17 local, Hawaii included)
#: ends before Eastern midnight, so one Eastern day holds exactly one business
#: day of every lead's window. A UTC day split it: 16:00-17:00 Pacific in winter
#: is the next UTC day, so a mailbox could send its cap twice in one afternoon.
CAP_DAY_TZ = ZoneInfo("America/New_York")


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """The Eastern calendar day ``now`` falls in, as UTC instants.

    One fixed boundary for every mailbox: a cap is a volume control, and a
    single unambiguous day is one both this code and a human reading
    `sent_messages` can agree on. See `CAP_DAY_TZ` for why Eastern, not UTC.
    """

    day = now.astimezone(CAP_DAY_TZ).date()
    start = datetime.combine(day, time(0), tzinfo=CAP_DAY_TZ)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=CAP_DAY_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def sent_today(session: Session, mailbox: Mailbox, *, now: datetime) -> int:
    """How many messages this mailbox has sent, or may have sent, today.

    Counted from `sent_messages`, never from a counter on the mailbox row: a
    crash between the send and the bookkeeping must not hand a mailbox a fresh
    budget, and the only record that cannot drift is the one written when the
    message actually left. An attempt handed to the sender with no booked
    result yet (in flight, or ambiguous) counts too: it may be in an inbox.
    """

    start, end = day_window(now)
    booked = session.execute(
        select(func.count(SentMessage.id)).where(
            SentMessage.mailbox_id == mailbox.id,
            SentMessage.sent_at >= start,
            SentMessage.sent_at < end,
        )
    ).scalar_one()
    in_flight = session.execute(
        select(func.count(Draft.id)).where(
            Draft.send_attempt_mailbox_id == mailbox.id,
            Draft.send_attempted_at >= start,
            Draft.send_attempted_at < end,
            ~exists().where(SentMessage.draft_id == Draft.id),
        )
    ).scalar_one()
    return int(booked) + int(in_flight)


def headroom(session: Session, mailbox: Mailbox, *, now: datetime) -> int:
    """Messages this mailbox may still send today. Never negative."""

    return max(mailbox.daily_cap - sent_today(session, mailbox, now=now), 0)


def next_eligible_at(mailbox: Mailbox, *, now: datetime) -> datetime:
    """The earliest instant this mailbox may send again, ignoring its cap."""

    if mailbox.last_sent_at is None:
        return now
    last = mailbox.last_sent_at
    if last.tzinfo is None:  # defensive: a naive row from an older write
        last = last.replace(tzinfo=timezone.utc)
    return last + timedelta(seconds=mailbox.min_spacing_seconds)


@dataclass(frozen=True, slots=True)
class _Candidate:
    mailbox: Mailbox
    sent_today: int
    eligible_at: datetime


def _candidates(session: Session, *, campaign_id: int | None) -> list[Mailbox]:
    """Active mailboxes that may serve this campaign. Pinned wins if present.

    The pin is judged over every status, not just active ones: a campaign whose
    pinned mailboxes are all paused (a bounce auto-pause) waits for them, it
    does not spill onto whatever unpinned mailbox happens to be active.
    """

    rows = session.execute(select(Mailbox).order_by(Mailbox.id)).scalars().all()
    if campaign_id is not None and any(m.campaign_id == campaign_id for m in rows):
        return [m for m in rows if m.campaign_id == campaign_id and m.status == MailboxStatus.ACTIVE]
    return [m for m in rows if m.campaign_id is None and m.status == MailboxStatus.ACTIVE]


def select_mailbox(
    session: Session,
    *,
    campaign_id: int | None = None,
    now: datetime | None = None,
    only: int | None = None,
) -> Mailbox:
    """Pick the mailbox that should send the next message for this campaign.

    Ordering, most significant first:

    * fewest consecutive failures — a mailbox that just failed twice is not the
      one to hand the next prospect to;
    * fewest sent today — spreads volume across the roster rather than
      exhausting one mailbox before touching the next;
    * longest since its last send, then id — rotation, and a deterministic
      tie-break so a test can assert which mailbox got the message.

    Raises `MailboxUnavailable` (with ``retry_at``) when candidates exist but
    none may send yet, and `NoMailboxConfigured` when none can ever serve.
    """

    now = now or datetime.now(timezone.utc)
    if only is not None:
        # A follow-up leaves from the mailbox that owns the thread, or not at all.
        pool = [m for m in [session.get(Mailbox, only)] if m and m.status == MailboxStatus.ACTIVE]
        if not pool:
            raise NoMailboxConfigured(
                f"mailbox {only}, which sent the earlier emails in this thread, is "
                "not active. A follow-up must come from the same mailbox; "
                "re-activate it or stop this lead's sequence."
            )
    else:
        pool = _candidates(session, campaign_id=campaign_id)
    if not pool:
        raise NoMailboxConfigured(
            "no active mailbox serves "
            + (f"campaign {campaign_id}" if campaign_id else "the general rotation")
            + ". Add one on /mailboxes, or un-pause an existing one — nothing "
            "can be sent until a mailbox is available."
        )

    ready: list[_Candidate] = []
    waiting: list[datetime] = []
    for mailbox in pool:
        if headroom(session, mailbox, now=now) <= 0:
            # Capped for the day: free again at the next Eastern midnight.
            waiting.append(day_window(now)[1])
            continue
        eligible_at = next_eligible_at(mailbox, now=now)
        if eligible_at > now:
            waiting.append(eligible_at)
            continue
        ready.append(
            _Candidate(
                mailbox=mailbox,
                sent_today=sent_today(session, mailbox, now=now),
                eligible_at=eligible_at,
            )
        )

    if not ready:
        retry_at = min(waiting)
        raise MailboxUnavailable(
            f"every mailbox is capped or spacing; next free at {retry_at.isoformat()}",
            retry_at=retry_at,
        )

    # Alternate sending domains: whatever went out last, the other domain goes next.
    last = max(pool, key=lambda m: m.last_sent_at or datetime.min.replace(tzinfo=timezone.utc))
    last_domain = _domain(last) if last.last_sent_at else None
    ready.sort(
        key=lambda c: (
            c.mailbox.consecutive_failures,
            _domain(c.mailbox) == last_domain,
            c.sent_today,
            c.mailbox.last_sent_at or datetime.min.replace(tzinfo=timezone.utc),
            c.mailbox.id,
        )
    )
    return ready[0].mailbox


def _domain(mailbox: Mailbox) -> str:
    return mailbox.email_address.rpartition("@")[2].lower()


# --------------------------------------------------------------------------
# Bounce auto-pause
# --------------------------------------------------------------------------

#: Hard bounces are judged over the mailbox's last this-many sends...
BOUNCE_WINDOW = 50
#: ...and above this share of them the mailbox is paused. The denominator is
#: always the full window, so a new mailbox is not paused by one early bounce
#: (1/50 = 2%) but is by a second (4%).
BOUNCE_LIMIT = 0.03


def is_policy_rejection(code: str | None) -> bool:
    """5.7.x: the receiving side refused us on policy or reputation."""

    return bool(code) and code.strip().startswith("5.7.")


def pause_mailbox(mailbox: Mailbox, reason: str) -> None:
    if mailbox.status == MailboxStatus.ACTIVE:
        mailbox.status = MailboxStatus.PAUSED
    mailbox.last_error = f"auto-paused: {reason}"[:4000]


def bounce_rate(session: Session, mailbox: Mailbox) -> float:
    recent = (
        select(SentMessage.bounced_at)
        .where(SentMessage.mailbox_id == mailbox.id)
        .order_by(SentMessage.sent_at.desc())
        .limit(BOUNCE_WINDOW)
        .subquery()
    )
    bounced = session.execute(
        select(func.count()).select_from(recent).where(recent.c.bounced_at.is_not(None))
    ).scalar_one()
    return bounced / BOUNCE_WINDOW


def record_bounce(
    session: Session, sent: SentMessage, *, code: str | None, now: datetime | None = None
) -> None:
    """A hard bounce came back for ``sent``: suppress the address, stop the
    lead's sequence, and pause the mailbox on a 5.7.x or a rate above 3%.

    Called by reply sync (WP5) for a DSN it classifies as a hard bounce. A
    paused mailbox needs a person to look at it and un-pause it on /mailboxes.
    """

    now = now or datetime.now(timezone.utc)
    sent.bounced_at = sent.bounced_at or now
    sent.bounce_code = (code or "")[:16] or None

    address = sent.to_address.strip().lower()
    if session.execute(
        select(SuppressionEntry.id).where(
            SuppressionEntry.email == address, SuppressionEntry.workspace_id.is_(None)
        )
    ).first() is None:
        from mazz.ingest.csv_import import email_sha256

        session.add(SuppressionEntry(email=address, email_sha256=email_sha256(address),
                                     reason=f"hard bounce {code or ''}".strip(),
                                     added_by="system:bounce"))
    lead = session.get(Lead, sent.lead_id)
    if lead is not None and lead.sequence_state in ("active", "paused"):
        lead.sequence_state = "stopped"
        lead.next_step_due_at = None
    session.flush()

    mailbox = session.get(Mailbox, sent.mailbox_id) if sent.mailbox_id else None
    if mailbox is None:
        return
    if is_policy_rejection(code):
        pause_mailbox(mailbox, f"5.7.x rejection ({code}) for {address}")
    else:
        rate = bounce_rate(session, mailbox)
        if rate > BOUNCE_LIMIT:
            pause_mailbox(mailbox, f"hard-bounce rate {rate:.0%} over its last {BOUNCE_WINDOW} sends")
    session.flush()
