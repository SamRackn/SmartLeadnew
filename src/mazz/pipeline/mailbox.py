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
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mazz.models import Mailbox, MailboxStatus, SentMessage

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


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar day ``now`` falls in.

    UTC rather than each mailbox's local business day: a cap is a volume
    control, not a schedule, and a single unambiguous boundary is one both this
    code and a human reading `sent_messages` can agree on. Send-time-of-day
    shaping is a separate concern and does not belong in the cap.
    """

    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def sent_today(session: Session, mailbox: Mailbox, *, now: datetime) -> int:
    """How many messages this mailbox has actually sent in the current UTC day.

    Counted from `sent_messages`, never from a counter on the mailbox row: a
    crash between the send and the bookkeeping must not hand a mailbox a fresh
    budget, and the only record that cannot drift is the one written when the
    message actually left.
    """

    start, end = day_window(now)
    return int(
        session.execute(
            select(func.count(SentMessage.id)).where(
                SentMessage.mailbox_id == mailbox.id,
                SentMessage.sent_at >= start,
                SentMessage.sent_at < end,
            )
        ).scalar_one()
    )


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
    """Active mailboxes that may serve this campaign. Pinned wins if present."""

    active = (
        session.execute(
            select(Mailbox)
            .where(Mailbox.status == MailboxStatus.ACTIVE)
            .order_by(Mailbox.id)
        )
        .scalars()
        .all()
    )
    if campaign_id is not None:
        pinned = [m for m in active if m.campaign_id == campaign_id]
        if pinned:
            return pinned
    return [m for m in active if m.campaign_id is None]


def select_mailbox(
    session: Session,
    *,
    campaign_id: int | None = None,
    now: datetime | None = None,
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
            # Capped for the day: free again at the next UTC midnight.
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

    ready.sort(
        key=lambda c: (
            c.mailbox.consecutive_failures,
            c.sent_today,
            c.mailbox.last_sent_at or datetime.min.replace(tzinfo=timezone.utc),
            c.mailbox.id,
        )
    )
    return ready[0].mailbox
