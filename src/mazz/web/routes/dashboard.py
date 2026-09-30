"""Read-only performance dashboard: what a usable lead is costing us.

CLAUDE.md's primary constraint is COST PER USABLE LEAD, and AI research +
personalization is 57-70% of that cost at every scale. This page exists to
answer "what is a usable lead costing us right now, and where is that money
going" before anything else — it is not a generic admin panel, and it never
grows into one.

Two rules enforced here, the same ones ``leads.py`` enforces on the send path:

* **No send or approve affordance, anywhere.** This module contains zero
  ``POST`` routes. It reads ``leads``, ``drafts``, ``draft_batches``,
  ``research_cache`` and ``jobs``, and writes nothing at all — not even a
  reclaim of a stale job, which stays a deliberate operator action. A
  dashboard is exactly where a convenient "approve all" would get added
  later — it does not get added here.
* **Never print an optimistic number on a zero denominator.** Cost per
  usable lead with zero usable leads is undefined, not ``$0.00`` — see
  ``scripts/verifier_live_eval.py``'s ``cost_per_usable_lead`` for the
  incident that made this a hard rule. Every ratio computed below is
  ``None`` (rendered as an em dash / explanatory text) when its denominator
  is zero, and a panel whose underlying table has no rows at all is omitted
  rather than shown with fabricated zeroes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.models import (
    Draft,
    DraftBatch,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    ResearchCache,
    SentMessage,
    VerificationStatus,
)
from mazz.pipeline.mailbox import day_window, headroom, sent_today
from mazz.pipeline.queue import DEFAULT_STALE_AFTER
from mazz.web.auth import current_user, require_operator
from mazz.web.routes.leads import ALL_STATUSES

# Operator only: Sean's account sees leads, review, outbox and inbox for his
# workspace, never the roster or other workspaces' setup.
router = APIRouter(dependencies=[Depends(require_operator)])

@dataclass(frozen=True, slots=True)
class MailboxHealth:
    """One mailbox's capacity and health, for the sending panel."""

    mailbox: Mailbox
    sent_today: int
    headroom: int

    @property
    def is_capped(self) -> bool:
        return self.headroom <= 0


#: How many recent batches to show. This is a review aid, not an export —
#: the full history lives in the table, forever, per the model's own docstring.
_RECENT_BATCH_LIMIT = 10

#: How many failed jobs to list. Same reasoning: enough to see the pattern,
#: few enough that the page stays a summary.
_RECENT_FAILURE_LIMIT = 10

#: `jobs.last_error` holds up to 4000 chars, which is a stack trace, not a
#: dashboard cell. Truncated for display only — the full text is in the row.
_ERROR_EXCERPT_CHARS = 160


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@dataclass(frozen=True, slots=True)
class _BatchRow:
    batch_id: str
    model: str
    request_count: int
    cost_usd: float | None
    status: str
    duration_display: str


@dataclass(frozen=True, slots=True)
class _JobFailureRow:
    job_id: int
    lead_id: int
    kind: str
    attempts: int
    age_display: str
    error_excerpt: str


def _elapsed_display(seconds: float) -> str:
    """Coarse, human duration: two units at most, and never a zero unit.

    "41m", not "41m 0s" — the trailing zero reads as precision this number
    does not have, and these cells are scanned, not studied.
    """

    if seconds < 0:
        # Clock skew or bad data — say so rather than print a negative duration.
        return "—"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def _duration_display(submitted_at: datetime, ended_at: datetime | None) -> str:
    if ended_at is None:
        return "in progress"
    return _elapsed_display((ended_at - submitted_at).total_seconds())


def _age_display(since: datetime, now: datetime) -> str:
    return _elapsed_display((now - since).total_seconds())


def _error_excerpt(last_error: str | None) -> str:
    """One line of the failure, enough to tell failures apart at a glance."""

    if not last_error:
        # A failed job with no recorded error is itself worth noticing.
        return "(no error recorded)"
    collapsed = " ".join(last_error.split())
    if len(collapsed) <= _ERROR_EXCERPT_CHARS:
        return collapsed
    return collapsed[: _ERROR_EXCERPT_CHARS - 1] + "…"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)

    # ----------------------------------------------------------------
    # Pipeline: leads by status. Always rendered, even at zero — an empty
    # database legitimately has zero leads in every status, which is a real
    # fact, not a fabricated ratio.
    # ----------------------------------------------------------------
    status_counts: dict[LeadStatus, int] = {s: 0 for s in ALL_STATUSES}
    for status, count in session.execute(
        select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
    ).all():
        status_counts[status] = count
    total_leads = sum(status_counts.values())
    no_angle_share = (
        status_counts[LeadStatus.NO_ANGLE] / total_leads * 100 if total_leads else None
    )

    # ----------------------------------------------------------------
    # Verification: drafts by verification_status. Hold rate excludes
    # `pending` from its denominator, the same way scripts/verifier_live_eval.py's
    # `hold_rate` excludes cases that never reached a verdict.
    # ----------------------------------------------------------------
    verification_counts: dict[VerificationStatus, int] = {v: 0 for v in VerificationStatus}
    for v_status, count in session.execute(
        select(Draft.verification_status, func.count(Draft.id)).group_by(
            Draft.verification_status
        )
    ).all():
        verification_counts[v_status] = count
    total_drafts = sum(verification_counts.values())
    verified_drafts = (
        verification_counts[VerificationStatus.PASSED]
        + verification_counts[VerificationStatus.FAILED]
    )
    hold_rate = (
        verification_counts[VerificationStatus.FAILED] / verified_drafts
        if verified_drafts
        else None
    )

    # ----------------------------------------------------------------
    # Cost per usable lead. Omitted entirely if no batch has ever been
    # submitted — there is no measured spend to report yet, and $0.00 would
    # read as "outbound is free", which is the opposite of true.
    # ----------------------------------------------------------------
    batch_count = session.execute(select(func.count(DraftBatch.id))).scalar_one()
    show_cost_panel = batch_count > 0
    total_cost_usd: float | None = None
    usable_leads = 0
    cost_per_usable_lead: float | None = None
    if show_cost_panel:
        total_cost_usd = float(
            session.execute(
                select(func.coalesce(func.sum(DraftBatch.cost_usd), 0))
            ).scalar_one()
        )
        # "A usable lead is one with a draft that passed verification."
        usable_leads = verification_counts[VerificationStatus.PASSED]
        cost_per_usable_lead = (
            total_cost_usd / usable_leads if usable_leads else None
        )

    # ----------------------------------------------------------------
    # Cache effectiveness: two different caches, reported separately.
    # ----------------------------------------------------------------

    # Research cache: dedup across colleagues at the same employer. Shown
    # only once there are leads to dedup at all.
    show_research_cache_panel = total_leads > 0
    research_cache_rows = 0
    leads_per_cache_row: float | None = None
    if show_research_cache_panel:
        research_cache_rows = session.execute(
            select(func.count(ResearchCache.company_domain))
        ).scalar_one()
        leads_per_cache_row = (
            total_leads / research_cache_rows if research_cache_rows else None
        )

    # Prompt cache: cache_read_input_tokens vs cache_creation_input_tokens,
    # summed across every batch's `usage` JSONB. Shown only once a batch has
    # run — before that there is no token usage to sum.
    show_prompt_cache_panel = batch_count > 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    prompt_cache_hit_rate: float | None = None
    if show_prompt_cache_panel:
        for usage in session.execute(select(DraftBatch.usage)).scalars().all():
            usage = usage or {}
            cache_read_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
            cache_creation_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        prompt_cache_total = cache_read_tokens + cache_creation_tokens
        prompt_cache_hit_rate = (
            cache_read_tokens / prompt_cache_total if prompt_cache_total else None
        )

    # ----------------------------------------------------------------
    # Recent batches. Omitted entirely if none have ever been submitted.
    # ----------------------------------------------------------------
    recent_batches: list[_BatchRow] = []
    if show_cost_panel:
        rows = session.execute(
            select(DraftBatch).order_by(DraftBatch.submitted_at.desc()).limit(_RECENT_BATCH_LIMIT)
        ).scalars().all()
        recent_batches = [
            _BatchRow(
                batch_id=b.batch_id,
                model=b.model,
                request_count=b.request_count,
                cost_usd=float(b.cost_usd) if b.cost_usd is not None else None,
                status=b.status,
                duration_display=_duration_display(b.submitted_at, b.ended_at),
            )
            for b in rows
        ]

    # ----------------------------------------------------------------
    # Queue health. PHASE2.md W6 asks for an alarm on jobs exhausting
    # `max_attempts`; this is that alarm's display half. A `failed` job means
    # the retry budget is spent or the error was permanent — `fail_job` has
    # already parked its lead in `held`, so the lead is not lost, but nothing
    # will pick the work back up on its own. Omitted entirely when no job has
    # ever been enqueued.
    # ----------------------------------------------------------------
    now = datetime.now(timezone.utc)

    job_counts: dict[JobStatus, int] = {s: 0 for s in JobStatus}
    for j_status, count in session.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    ).all():
        job_counts[j_status] = count
    total_jobs = sum(job_counts.values())
    show_queue_panel = total_jobs > 0

    settled_jobs = job_counts[JobStatus.DONE] + job_counts[JobStatus.FAILED]
    # Denominator is settled work only. Counting pending/running jobs as
    # successes would make the rate drift down simply because the queue is
    # busy, which is the flattering direction — exactly what this page does not do.
    job_failure_rate = (
        job_counts[JobStatus.FAILED] / settled_jobs if settled_jobs else None
    )

    # Stale `running` jobs: claimed, past the staleness cutoff, and still not
    # reported back. `reclaim_stale_jobs` is deliberately manual (a slow job
    # looks identical to a dead worker), so this count is the only thing that
    # tells the operator to run it.
    stale_cutoff = now - DEFAULT_STALE_AFTER
    stale_running_jobs = session.execute(
        select(func.count(Job.id)).where(
            Job.status == JobStatus.RUNNING,
            Job.locked_at.isnot(None),
            Job.locked_at < stale_cutoff,
        )
    ).scalar_one()
    stale_after_display = f"{int(DEFAULT_STALE_AFTER.total_seconds() // 60)}m"

    # Backlog: the oldest job that is claimable *right now*. Matches the
    # worker's own claim query (pending AND run_after <= now), so a job merely
    # sitting out its backoff does not read as a stuck queue.
    oldest_claimable_at = session.execute(
        select(func.min(Job.created_at)).where(
            Job.status == JobStatus.PENDING,
            Job.run_after <= now,
        )
    ).scalar_one()
    oldest_claimable_age = (
        _age_display(oldest_claimable_at, now) if oldest_claimable_at else None
    )

    recent_failures: list[_JobFailureRow] = []
    if job_counts[JobStatus.FAILED]:
        failed_rows = (
            session.execute(
                select(Job)
                .where(Job.status == JobStatus.FAILED)
                .order_by(Job.updated_at.desc())
                .limit(_RECENT_FAILURE_LIMIT)
            )
            .scalars()
            .all()
        )
        recent_failures = [
            _JobFailureRow(
                job_id=j.id,
                lead_id=j.lead_id,
                kind=j.kind,
                attempts=j.attempts,
                age_display=_age_display(j.updated_at, now),
                error_excerpt=_error_excerpt(j.last_error),
            )
            for j in failed_rows
        ]

    # ----------------------------------------------------------------
    # Sending. Absent from this page until we owned the rail, which made
    # it the one question the client could not answer here: how much has
    # actually left, from which mailbox, and what is left in the tank.
    #
    # sent_today is counted from sent_messages rows, never from a counter
    # on the mailbox, for the same reason pipeline.mailbox counts it that
    # way: a crash between sending and bookkeeping must not silently
    # hand a mailbox extra capacity.
    # ----------------------------------------------------------------
    day_start, _day_end = day_window(now)

    total_sent = session.execute(
        select(func.count(SentMessage.id))
    ).scalar_one()
    sent_today_total = session.execute(
        select(func.count(SentMessage.id)).where(SentMessage.sent_at >= day_start)
    ).scalar_one()

    mailboxes = (
        session.execute(select(Mailbox).order_by(Mailbox.email_address)).scalars().all()
    )
    mailbox_health = [
        MailboxHealth(
            mailbox=mailbox,
            sent_today=sent_today(session, mailbox, now=now),
            headroom=headroom(session, mailbox, now=now),
        )
        for mailbox in mailboxes
    ]
    active_health = [
        row for row in mailbox_health if row.mailbox.status is MailboxStatus.ACTIVE
    ]

    #: Capacity that exists TODAY, from mailboxes that can actually serve.
    #: Paused and disabled mailboxes contribute nothing -- counting them
    #: would advertise a send rate the roster cannot deliver.
    remaining_capacity_today = sum(row.headroom for row in active_health)
    daily_capacity = sum(row.mailbox.daily_cap for row in active_health)

    approved_waiting = status_counts.get(LeadStatus.APPROVED, 0)

    #: Days of backlog at the current roster's pace. None, not zero, when
    #: there is no capacity: "0 days" reads like "nothing is waiting", and
    #: an empty or fully paused roster means the opposite.
    if daily_capacity > 0:
        backlog_days = approved_waiting / daily_capacity
    else:
        backlog_days = None

    show_sending_panel = bool(mailboxes) or total_sent > 0

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "show_sending_panel": show_sending_panel,
            "total_sent": total_sent,
            "sent_today_total": sent_today_total,
            "mailbox_health": mailbox_health,
            "active_mailboxes": len(active_health),
            "total_mailboxes": len(mailbox_health),
            "daily_capacity": daily_capacity,
            "remaining_capacity_today": remaining_capacity_today,
            "approved_waiting": approved_waiting,
            "backlog_days": backlog_days,
            "all_statuses": ALL_STATUSES,
            "status_counts": status_counts,
            "total_leads": total_leads,
            "no_angle_share": no_angle_share,
            "verification_counts": verification_counts,
            "total_drafts": total_drafts,
            "hold_rate": hold_rate,
            "show_cost_panel": show_cost_panel,
            "total_cost_usd": total_cost_usd,
            "usable_leads": usable_leads,
            "cost_per_usable_lead": cost_per_usable_lead,
            "show_research_cache_panel": show_research_cache_panel,
            "research_cache_rows": research_cache_rows,
            "leads_per_cache_row": leads_per_cache_row,
            "show_prompt_cache_panel": show_prompt_cache_panel,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "prompt_cache_hit_rate": prompt_cache_hit_rate,
            "recent_batches": recent_batches,
            "show_queue_panel": show_queue_panel,
            "all_job_statuses": list(JobStatus),
            "job_counts": job_counts,
            "total_jobs": total_jobs,
            "job_failure_rate": job_failure_rate,
            "stale_running_jobs": stale_running_jobs,
            "stale_after_display": stale_after_display,
            "oldest_claimable_age": oldest_claimable_age,
            "recent_failures": recent_failures,
        },
    )
