"""The queue: ``jobs`` is a plain Postgres table, and that is the whole design.

At ~25 leads/day the brief explicitly rules out Redis and Celery. A table plus
``SELECT ... FOR UPDATE SKIP LOCKED`` gives us the two properties that actually
matter -- exactly-one-worker-per-job, and durable state that survives a restart
-- with no second piece of infrastructure to run, monitor, or explain.

**Claiming.** ``claim_job`` runs

    SELECT ... FROM jobs
    WHERE status = 'pending' AND run_after <= now()
    ORDER BY run_after, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED

``FOR UPDATE`` takes a row lock; ``SKIP LOCKED`` makes a second worker step over
the locked row instead of blocking on it. Two workers therefore never claim the
same job -- not "rarely", never -- and neither one waits. ``ix_jobs_status_run_after``
serves the predicate. ``tests/test_queue.py`` proves it with two real concurrent
sessions rather than by assertion.

**Failing.** A failed job records ``last_error``, increments ``attempts``, and
pushes ``run_after`` out by exponential backoff. After ``max_attempts`` it is
marked ``failed`` and STOPS -- an infinite retry loop against a provider that
is never coming back is just a slow way to burn money and hide a problem. Some
failures skip the retries entirely: a malformed LinkedIn URL on the lead record
will fail identically at 2am, so it fails permanently and immediately, with a
message that says to fix the lead.

A permanently failed job puts its lead in ``held``, never ``no_angle``.
``no_angle`` is a finding ("we researched him and there is genuinely nothing to
say"); an error is an absence of a finding. Conflating them would quietly
hand the client a verdict we never actually reached.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mazz.db import session_scope
from mazz.models import Job, JobStatus, Lead, LeadStatus

__all__ = [
    "default_handlers",
    "JobContext",
    "JobDeferred",
    "JobHandler",
    "PermanentJobError",
    "RetrySettings",
    "UnknownJobKind",
    "WorkerStats",
    "claim_job",
    "complete_job",
    "defer_job",
    "enqueue_job",
    "fail_job",
    "process_next",
    "reclaim_stale_jobs",
    "run_worker",
    "utcnow",
]

log = logging.getLogger("mazz.pipeline.queue")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrySettings:
    """Bounded backoff. ``max_attempts`` is the bound that stops the loop.

    Delay before retry N is ``min(base * 2**(N-1), cap)`` -- 1m, 2m, 4m, 8m,
    then give up. Deliberately not jittered: there is one worker, not a
    thundering herd, and a deterministic sequence is one a test can assert.
    """

    max_attempts: int = 5
    base_delay_seconds: float = 60.0
    max_delay_seconds: float = 3600.0

    def delay_after(self, attempts: int) -> timedelta:
        """Backoff after ``attempts`` failures (1-based)."""

        exponent = max(attempts - 1, 0)
        seconds = min(self.base_delay_seconds * (2**exponent), self.max_delay_seconds)
        return timedelta(seconds=seconds)


DEFAULT_RETRY = RetrySettings()

#: How long a ``running`` job may sit untouched before we assume its worker
#: died. ``python -m mazz.ops worker`` sweeps with ``reclaim_stale_jobs`` once a
#: minute; the threshold is long enough that a job which is merely slow is
#: never yanked out from under a worker that is still busy with it.
DEFAULT_STALE_AFTER = timedelta(minutes=15)


class PermanentJobError(Exception):
    """Retrying cannot possibly help. Fail now, loudly, and stop.

    Bad data on the lead record, an unknown job kind, a mapping bug. Burning
    five attempts and forty minutes on these only delays the moment somebody
    reads the error.
    """


class UnknownJobKind(PermanentJobError):
    """No handler registered for ``jobs.kind``."""


class JobDeferred(Exception):
    """Not now, but not a failure either. Re-arm for ``retry_at``.

    Pacing needs a third outcome. A send waiting on a mailbox's daily cap has
    not gone wrong -- nothing is broken, the message simply must not leave for
    another six hours -- so treating it as a failure would spend one of the
    job's five attempts, and five capped days would kill work that was always
    going to succeed. ``attempts`` is therefore NOT incremented here.

    Use it only for conditions that resolve by waiting. A missing mailbox, a
    rejected draft or a bad credential are not deferrals: they need a person.
    """

    def __init__(self, message: str, *, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _is_permanent(exc: BaseException) -> bool:
    if isinstance(exc, PermanentJobError):
        return True
    # Integration errors advertise their own retryability; a 500 is worth
    # another go, a 401 or an unparseable payload is not.
    retryable = getattr(exc, "retryable", None)
    if retryable is False:
        return True
    # Bad input on the lead record: InvalidProfileURL / InvalidDomain are
    # ValueErrors, not ProviderErrors, precisely so they route to "fix the
    # lead" instead of "retry later".
    from mazz.integrations.company import InvalidDomain
    from mazz.integrations.linkedin import InvalidProfileURL

    return isinstance(exc, (InvalidDomain, InvalidProfileURL))


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------


#: First half of the two-int advisory key that serialises send enqueues per
#: lead (the second half is the lead id). Two-int keys never collide with the
#: single-bigint keys in send.py and ops.py.
SEND_ENQUEUE_LOCK_CLASS = 7101


def enqueue_job(
    session: Session,
    lead_id: int,
    kind: str,
    *,
    run_after: datetime | None = None,
    dedupe: bool = True,
) -> tuple[Job, bool]:
    """Queue one unit of work. Returns ``(job, newly_enqueued)``.

    With ``dedupe`` (the default) an outstanding ``pending``/``running`` job of
    the same kind for the same lead is reused rather than duplicated, so
    uploading the same spreadsheet twice does not research the same person
    twice. Ingest relies on this for its "enqueue exactly ONE jobs row".
    """

    if dedupe and kind == "send":
        # SELECT-then-INSERT races: two release clicks in flight at once would
        # both see no job (QA-02). Serialise them per lead until commit, so the
        # second one's SELECT sees the first one's row.
        # ponytail: send only. Each xact lock holds a shared lock-table slot
        # until commit, and an import enqueues thousands of jobs in one
        # transaction. A partial unique index covers every kind if that matters.
        session.execute(select(func.pg_advisory_xact_lock(SEND_ENQUEUE_LOCK_CLASS, lead_id)))
    if dedupe:
        existing = session.execute(
            select(Job)
            .where(
                Job.lead_id == lead_id,
                Job.kind == kind,
                Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            )
            .order_by(Job.id)
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    job = Job(
        lead_id=lead_id,
        kind=kind,
        status=JobStatus.PENDING,
        run_after=run_after or utcnow(),
    )
    session.add(job)
    session.flush()
    return job, True


# --------------------------------------------------------------------------
# Claim / complete / fail
# --------------------------------------------------------------------------


def claim_job(
    session: Session,
    *,
    kinds: Iterable[str] | None = None,
    now: datetime | None = None,
) -> Job | None:
    """Claim the next due job, or None.

    The row is locked with ``FOR UPDATE SKIP LOCKED`` and flipped to
    ``running`` inside the caller's transaction. Commit promptly: the lock is
    held until you do, and a handler should not run with a row lock open.
    """

    now = now or utcnow()
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds is not None:
        stmt = stmt.where(Job.kind.in_(list(kinds)))

    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.locked_at = now
    session.flush()
    return job


def complete_job(session: Session, job: Job) -> Job:
    """Mark a job done and release its lock marker."""

    job.status = JobStatus.DONE
    job.locked_at = None
    job.last_error = None
    session.flush()
    return job


def fail_job(
    session: Session,
    job: Job,
    error: BaseException | str,
    *,
    settings: RetrySettings = DEFAULT_RETRY,
    permanent: bool | None = None,
    hold_lead_on_giveup: bool = True,
    now: datetime | None = None,
) -> bool:
    """Record a failure. Returns True if the job has given up for good.

    Increments ``attempts``, stores ``last_error``, and either re-arms the job
    with a backed-off ``run_after`` or -- once ``max_attempts`` is spent, or the
    error is permanent -- marks it ``failed`` and stops.
    """

    now = now or utcnow()
    if permanent is None:
        permanent = isinstance(error, BaseException) and _is_permanent(error)

    message = (
        f"{error.__class__.__name__}: {error}"
        if isinstance(error, BaseException)
        else str(error)
    )

    job.attempts = (job.attempts or 0) + 1
    job.last_error = message[:4000]
    job.locked_at = None

    exhausted = job.attempts >= settings.max_attempts
    if permanent or exhausted:
        job.status = JobStatus.FAILED
        if permanent:
            job.last_error = f"[permanent, no retry] {job.last_error}"
        else:
            job.last_error = (
                f"[gave up after {job.attempts} attempts] {job.last_error}"
            )
        if hold_lead_on_giveup:
            _hold_lead(session, job.lead_id)
        session.flush()
        log.warning("job %s (%s) failed permanently: %s", job.id, job.kind, message)
        return True

    job.status = JobStatus.PENDING
    job.run_after = now + settings.delay_after(job.attempts)
    session.flush()
    log.info(
        "job %s (%s) attempt %s failed, retrying at %s: %s",
        job.id,
        job.kind,
        job.attempts,
        job.run_after,
        message,
    )
    return False


def _hold_lead(session: Session, lead_id: int) -> None:
    """Park a lead whose pipeline step gave up.

    ``held``, NOT ``no_angle``. We did not research him and find nothing; we
    failed to research him. Saying otherwise would invent a verdict.
    """

    lead = session.get(Lead, lead_id)
    if lead is None:
        return
    if lead.status in (LeadStatus.APPROVED, LeadStatus.SENT):
        return
    lead.status = LeadStatus.HELD


def defer_job(
    session: Session,
    job: Job,
    deferred: JobDeferred,
    *,
    now: datetime | None = None,
) -> Job:
    """Put a job back in the queue for later, without counting a failure.

    ``last_error`` is written even though nothing failed: it is the only field
    that can explain why an otherwise-healthy job is sitting with a
    ``run_after`` six hours out, and it is prefixed so a reader can tell it
    apart from a real error. The dashboard only surfaces ``last_error`` for
    FAILED jobs, so this never reads as a fault there.
    """

    now = now or utcnow()
    job.status = JobStatus.PENDING
    job.locked_at = None
    job.run_after = max(deferred.retry_at, now)
    job.last_error = f"[deferred] {deferred}"[:4000]
    session.flush()
    log.info(
        "job %s (%s) deferred until %s: %s", job.id, job.kind, job.run_after, deferred
    )
    return job


def reclaim_stale_jobs(
    session: Session,
    *,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    now: datetime | None = None,
) -> list[Job]:
    """Return jobs stuck in ``running`` past ``stale_after`` to ``pending``.

    The crashed-worker hook, swept by the service worker. A job that is merely
    slow looks exactly like one whose worker died, which is why the threshold
    is generous and why ``mazz.ops`` runs a single worker. Sends are safe to
    reclaim for a different reason: the send step commits an attempt marker
    before touching the wire, so a reclaimed send that may have left is held
    for a human rather than sent again.
    """

    now = now or utcnow()
    cutoff = now - stale_after
    jobs = (
        session.execute(
            select(Job).where(
                Job.status == JobStatus.RUNNING,
                Job.locked_at.isnot(None),
                Job.locked_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        held_since = job.locked_at
        job.status = JobStatus.PENDING
        job.locked_at = None
        job.last_error = (
            f"reclaimed: worker held this job since {held_since} "
            "and did not report back"
        )
        job.run_after = now
        log.warning("job %s (%s) reclaimed from a stale worker", job.id, job.kind)
    session.flush()
    return list(jobs)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


@dataclass(slots=True)
class JobContext:
    """What a handler is given.

    ``handlers`` is exposed so a step can chain to the next one *only if that
    step exists yet* -- in Phase 1 the scorer may not be registered, and
    enqueueing work nobody can run would just manufacture failed jobs.
    """

    session: Session
    job: Job
    lead: Lead
    handlers: Mapping[str, "JobHandler"]

    def has_handler(self, kind: str) -> bool:
        return kind in self.handlers

    def enqueue_next(self, kind: str, *, required: bool = False) -> Job | None:
        """Queue the following pipeline step, if something can run it."""

        if not required and not self.has_handler(kind):
            log.debug(
                "not chaining to %r for lead %s: no handler registered",
                kind,
                self.lead.id,
            )
            return None
        job, _ = enqueue_job(self.session, self.lead.id, kind)
        return job


JobHandler = Callable[[JobContext], None]


@functools.cache
def default_handlers() -> Mapping[str, JobHandler]:
    """The pipeline chain, in the order a lead moves through it.

    Each handler calls ``ctx.enqueue_next(...)`` for the following stage, so
    registering them is what makes ingest -> verify actually run end to end.
    A function, not a constant, so importing the queue does not drag in the
    adapters. Extend it by passing your own mapping to ``run_worker`` /
    ``process_next``, e.g. ``{**default_handlers(), "send": handle_send}``.

    **The chain stops at verify.** ``send`` is registered here so the worker can
    run it, but NOTHING enqueues it: a send job exists only because a human
    pressed approve in the review app, one lead at a time. Do not add
    ``ctx.enqueue_next("send")`` to `handle_verify` -- that single line would
    turn this into an auto-sender and break guardrail 4.
    """

    from mazz.pipeline.draft import handle_draft
    from mazz.pipeline.enrich import handle_enrich
    from mazz.pipeline.qualify import handle_qualify
    from mazz.pipeline.send import handle_send
    from mazz.pipeline.sequence import handle_followup
    from mazz.pipeline.verify import handle_verify

    return MappingProxyType({
        "enrich": handle_enrich,
        "qualify": handle_qualify,
        "draft": handle_draft,
        "verify": handle_verify,
        # Enqueued by the approve route only. See the docstring above.
        "send": handle_send,
        # Scheduled by a send at the next step's due date. Writes a draft and
        # chains to verify -- back to human review, never to send.
        "followup": handle_followup,
    })


# --------------------------------------------------------------------------
# The worker loop
# --------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerStats:
    claimed: int = 0
    done: int = 0
    retried: int = 0
    failed: int = 0
    deferred: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"claimed={self.claimed} done={self.done} "
            f"retried={self.retried} deferred={self.deferred} failed={self.failed}"
        )


SessionFactory = Callable[[], AbstractContextManager[Session]]


def process_next(
    *,
    handlers: Mapping[str, JobHandler] | None = None,
    kinds: Iterable[str] | None = None,
    settings: RetrySettings = DEFAULT_RETRY,
    session_factory: SessionFactory = session_scope,
    stats: WorkerStats | None = None,
) -> Job | None:
    """Claim and run at most one job. Returns the job, or None if idle.

    Three short transactions, on purpose:

    1. claim and commit -- the row lock is released immediately, so a slow
       provider call never blocks the other worker;
    2. run the handler and commit -- a handler that raises rolls back its own
       partial writes, so a half-written cache row cannot outlive a failure;
    3. record the failure -- in its own transaction, because transaction 2 is
       already rolled back and the error must be recorded regardless.
    """

    registry = default_handlers() if handlers is None else handlers
    stats = stats or WorkerStats()

    with session_factory() as session:
        job = claim_job(session, kinds=kinds)
        if job is None:
            return None
        job_id, job_kind, lead_id = job.id, job.kind, job.lead_id
        claimed = job

    stats.claimed += 1
    stats.kinds[job_kind] = stats.kinds.get(job_kind, 0) + 1

    try:
        with session_factory() as session:
            live = session.get(Job, job_id)
            if live is None:  # deleted under us (delete-by-person cascade)
                return claimed
            lead = session.get(Lead, lead_id)
            if lead is None:
                raise PermanentJobError(f"lead {lead_id} no longer exists")
            handler = registry.get(job_kind)
            if handler is None:
                raise UnknownJobKind(
                    f"no handler registered for job kind {job_kind!r} "
                    f"(registered: {sorted(registry)})"
                )
            handler(JobContext(session=session, job=live, lead=lead, handlers=registry))
            complete_job(session, live)
            stats.done += 1
            return live
    except JobDeferred as deferred:
        # Not an error: the handler asked to be run again later (a paced send
        # waiting on a mailbox's cap). No attempt is spent.
        with session_factory() as session:
            live = session.get(Job, job_id)
            if live is None:
                return claimed
            defer_job(session, live, deferred)
            stats.deferred += 1
            return live
    except Exception as exc:  # noqa: BLE001 - the queue is the top of the stack
        with session_factory() as session:
            live = session.get(Job, job_id)
            if live is None:
                return claimed
            gave_up = fail_job(session, live, exc, settings=settings)
            if gave_up:
                stats.failed += 1
            else:
                stats.retried += 1
            return live


def run_worker(
    *,
    handlers: Mapping[str, JobHandler] | None = None,
    kinds: Iterable[str] | None = None,
    settings: RetrySettings = DEFAULT_RETRY,
    session_factory: SessionFactory = session_scope,
    max_jobs: int | None = None,
    idle_sleep: float = 1.0,
    run_until_empty: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> WorkerStats:
    """Drain the queue.

    ``run_until_empty=True`` processes everything currently due and returns --
    what the seed script and the tests want. Leave it False for a long-running
    worker, which polls every ``idle_sleep`` seconds. Either way the loop is
    bounded by ``max_jobs`` and by ``should_stop`` so it can always be told to
    finish.
    """

    stats = WorkerStats()
    while True:
        if max_jobs is not None and stats.claimed >= max_jobs:
            return stats
        if should_stop is not None and should_stop():
            return stats

        job = process_next(
            handlers=handlers,
            kinds=kinds,
            settings=settings,
            session_factory=session_factory,
            stats=stats,
        )
        if job is not None:
            continue
        if run_until_empty:
            return stats
        time.sleep(idle_sleep)
