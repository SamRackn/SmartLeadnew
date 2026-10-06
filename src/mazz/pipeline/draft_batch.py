"""Bulk drafting over the Claude Batch API — the half-price path.

`draft.handle_draft` drafts one lead per job, synchronously. That is correct
and it is what the review app's "redraft" button needs. It is the wrong way to
run the pipeline, because it pays list price for work that nobody is waiting
on: a draft sits in a queue until a human opens the review app, so the 24-hour
worst case of the Batch API costs the client nothing and saves 50% of every
token.

--------------------------------------------------------------------------
THE THREE COST LEVERS, AND WHERE EACH ONE LIVES
--------------------------------------------------------------------------
1. **Don't call the model.** `score` decides `no_angle` before drafting, so a
   lead with no angle costs zero tokens. `build_batch` resolves those inline
   and never puts them in the batch. Already true; stated here because it is
   the largest single saving and it is easy to accidentally undo.
2. **Half price.** This module. One `batches.create` instead of N calls.
3. **~90% off the shared prefix.** `compose_claude._system_blocks`, plus the
   prewarm below — see `submit_batch` for why the prewarm is not optional.

--------------------------------------------------------------------------
HOW IT FITS THE QUEUE
--------------------------------------------------------------------------
Drafting is the one stage that cannot be a `JobHandler`, because a handler
processes one job and returns, and a batch is N jobs suspended across a wall
of latency. So this runs beside the worker, not inside it:

    run_once()
      -> claim_draft_jobs      N pending `draft` jobs, FOR UPDATE SKIP LOCKED
      -> build_batch           score each; no_angle resolved free; rest -> requests
      -> submit_batch          prewarm the cache, then one batches.create
      -> (later) poll_batch    on `ended`: persist drafts, chain to verify

Claimed jobs stay `running` for the life of the batch. `poll_batch` refreshes
their `locked_at` on every poll so that `reclaim_stale_jobs` -- whose default
patience is 15 minutes, far shorter than a batch -- does not decide the worker
died and hand the same leads to a second drafting run.

Nothing here can send an email, and nothing here can approve one. A batch
result becomes a `drafts` row with `verification_status=pending`, exactly as
the synchronous path produces, and `verify` still has to pass before a human
is even shown the lead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from sqlalchemy import select

from mazz.models import DraftBatch, Job, JobStatus, Lead, LeadStatus
from mazz.pipeline.compose_claude import (
    NO_ANGLE_SENTINEL,
    ComposerRefused,
    Usage,
    build_params,
    get_client,
    parse_message,
    prewarm_cache,
    price_usd,
    usage_of,
)
from mazz.pipeline.draft import (
    DraftOutcome,
    build_request,
    persist_draft,
    render_body,
)
from mazz.pipeline.queue import (
    default_handlers,
    complete_job,
    enqueue_job,
    fail_job,
    utcnow,
)
from mazz.pipeline.score import ScoringConfig, score_lead

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import Anthropic
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

__all__ = [
    "BatchPlan",
    "DRAFT_JOB_KIND",
    "MAX_BATCH_SIZE",
    "RunReport",
    "build_batch",
    "claim_draft_jobs",
    "custom_id_for",
    "job_id_from_custom_id",
    "poll_batch",
    "poll_open_batches",
    "run_once",
    "submit_batch",
]

DRAFT_JOB_KIND = "draft"

#: The API's own ceiling is 100,000 requests / 256MB. This is a working default
#: sized to the client's volume, not to the limit: a smaller batch drains sooner
#: and a failure costs less to redo. Raise it before raising throughput.
MAX_BATCH_SIZE = 500


def custom_id_for(job: Job) -> str:
    """Batch `custom_id` for a job.

    Results come back in ARBITRARY order, so they must be keyed by `custom_id`
    and never by position. Encoding the jobs row id means no join table: the
    result identifies its own unit of work.
    """

    return f"job-{job.id}"


def job_id_from_custom_id(custom_id: str) -> int | None:
    prefix, _, raw = custom_id.partition("-")
    if prefix != "job" or not raw.isdigit():
        return None
    return int(raw)


# --------------------------------------------------------------------------
# Claim
# --------------------------------------------------------------------------


def claim_draft_jobs(
    session: "Session", *, limit: int = MAX_BATCH_SIZE, now: datetime | None = None
) -> list[Job]:
    """Claim up to `limit` due `draft` jobs. Same locking as `claim_job`.

    ``FOR UPDATE SKIP LOCKED`` so two drafting runs cannot batch the same lead
    twice. Commit promptly -- the rows stay locked until you do.
    """

    now = now or utcnow()
    jobs = list(
        session.execute(
            select(Job)
            .where(
                Job.kind == DRAFT_JOB_KIND,
                Job.status == JobStatus.PENDING,
                Job.run_after <= now,
            )
            .order_by(Job.run_after, Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.status = JobStatus.RUNNING
        job.locked_at = now
    session.flush()
    return jobs


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass(slots=True)
class BatchPlan:
    """What a drafting run intends to send, and what it already settled."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    #: Leads resolved to `no_angle` without an API call. The cheapest outcome
    #: there is; counted so a run can report how many leads cost nothing.
    no_angle: int = 0

    def __bool__(self) -> bool:
        return bool(self.requests)


def build_batch(
    session: "Session",
    jobs: list[Job],
    *,
    now: datetime | None = None,
    config: ScoringConfig | None = None,
) -> BatchPlan:
    """Score each claimed lead; batch only the ones with something to say.

    The no-angle branch is settled here, synchronously and for free, exactly as
    `draft_for_lead` would settle it. It must stay that way: a `no_angle` lead
    that goes into the batch pays for a model call whose only correct answer is
    "do not write this email".

    ``config=None`` means each lead is scored against ITS OWN campaign config,
    resolved per lead by `score.score_lead`. That is not a default worth
    changing: a batch is claimed off one queue and routinely mixes leads from
    several campaigns, so a single config for the whole batch would score one
    agency-client's prospects against another's ICP — and this is the step that
    decides which of them get an email at all. Pass one explicitly only to
    score a whole batch against a hypothetical.
    """

    plan = BatchPlan()
    for job in jobs:
        lead = session.get(Lead, job.lead_id)
        if lead is None:  # cascade-deleted mid-flight; nothing to draft
            complete_job(session, job)
            continue

        result = score_lead(session, lead, now=now, config=config)
        if not result.has_angle:
            log.info("lead %s: no angle, not batched — %s", lead.id, result.rationale)
            persist_draft(
                session,
                lead,
                DraftOutcome(
                    lead_status=LeadStatus.NO_ANGLE,
                    score_result=result,
                    reason=result.rationale,
                ),
            )
            complete_job(session, job)
            plan.no_angle += 1
            continue

        params = build_params(build_request(lead, result))
        plan.requests.append({"custom_id": custom_id_for(job), "params": params})
        plan.jobs.append(job)

    session.flush()
    return plan


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------


def _batch_request(item: dict[str, Any]) -> Any:
    """One `{custom_id, params}` entry, in the SDK's own types where available.

    `Request` and `MessageCreateParamsNonStreaming` are TypedDicts, so a plain
    dict is the identical runtime object and serialises the same. Preferring
    the real constructors means the SDK's type checking applies where it can;
    falling back means the batch path stays importable and testable on a
    machine that has not installed `anthropic` -- which is every machine
    running the Phase 1 template composer.
    """

    try:
        from anthropic.types.message_create_params import (
            MessageCreateParamsNonStreaming,
        )
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:  # pragma: no cover - exercised when the SDK is absent
        return {"custom_id": item["custom_id"], "params": item["params"]}

    return Request(
        custom_id=item["custom_id"],
        params=MessageCreateParamsNonStreaming(**item["params"]),
    )


def submit_batch(
    session: "Session",
    plan: BatchPlan,
    *,
    client: "Anthropic" | None = None,
    prewarm: bool = True,
) -> DraftBatch | None:
    """Prewarm the cache, then send the batch. Returns the tracking row.

    **The prewarm is load-bearing, not a nicety.** A cache entry only becomes
    readable once the request writing it has started responding. A batch is a
    fan-out of identical-prefix requests submitted at once, so without a
    prewarm a large share of them miss the cache AND pay the write premium --
    strictly worse than not caching at all. One `max_tokens=0` call up front
    turns every one of those misses into a read. It is the difference between
    caching working and caching being a surcharge.
    """

    if not plan:
        return None

    client = client or get_client()
    if prewarm:
        prewarm_cache(client)

    batch = client.messages.batches.create(
        requests=[_batch_request(item) for item in plan.requests]
    )

    row = DraftBatch(
        batch_id=batch.id,
        model=str(plan.requests[0]["params"]["model"]),
        status=batch.processing_status,
        job_ids=[job.id for job in plan.jobs],
        request_count=len(plan.requests),
    )
    session.add(row)
    session.flush()
    log.info(
        "submitted draft batch %s: %d requests (%d leads cost nothing — no angle)",
        batch.id,
        row.request_count,
        plan.no_angle,
    )
    return row


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RunReport:
    """What one drafting run did, in the terms the cost model cares about."""

    batch_id: str | None = None
    submitted: int = 0
    no_angle: int = 0
    drafted: int = 0
    failed: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    @property
    def cost_per_draft(self) -> float:
        return self.cost_usd / self.drafted if self.drafted else 0.0


def poll_batch(
    session: "Session",
    row: DraftBatch,
    *,
    client: "Anthropic" | None = None,
    handlers: Mapping[str, Any] | None = None,
) -> RunReport:
    """Check a submitted batch; if it has ended, land every result.

    Safe to call repeatedly. While the batch is still processing this only
    refreshes the claimed jobs' `locked_at`, which is what keeps
    `reclaim_stale_jobs` from concluding the worker died -- a batch outlives
    the 15-minute default many times over.
    """

    client = client or get_client()
    handlers = default_handlers() if handlers is None else handlers
    report = RunReport(batch_id=row.batch_id, submitted=row.request_count)

    batch = client.messages.batches.retrieve(row.batch_id)
    row.status = batch.processing_status

    if batch.processing_status != "ended":
        now = utcnow()
        for job in _jobs_of(session, row):
            job.locked_at = now
        session.flush()
        log.info("draft batch %s still %s", row.batch_id, batch.processing_status)
        return report

    jobs_by_id = {job.id: job for job in _jobs_of(session, row)}
    seen: set[int] = set()

    for result in client.messages.batches.results(row.batch_id):
        job_id = job_id_from_custom_id(result.custom_id)
        job = jobs_by_id.get(job_id) if job_id is not None else None
        if job is None:
            log.warning(
                "batch %s returned unknown custom_id %r", row.batch_id, result.custom_id
            )
            continue
        seen.add(job.id)
        _land_result(session, row, job, result, report, handlers)

    # A claimed job with no result at all: the batch expired mid-flight, or the
    # request never made it. Retryable — it falls into the next batch.
    for job_id, job in jobs_by_id.items():
        if job_id not in seen:
            report.failed += 1
            fail_job(session, job, f"no result in batch {row.batch_id}")

    row.succeeded_count = report.drafted
    row.errored_count = report.failed
    row.usage = _usage_dict(report.usage)
    row.cost_usd = report.cost_usd
    row.ended_at = utcnow()

    _check_cache_worked(row, report)
    session.flush()

    log.info(
        "draft batch %s ended: %d drafted, %d failed, %.0f%% of prompt tokens "
        "cached, $%.4f total (~$%.5f per draft)",
        row.batch_id,
        report.drafted,
        report.failed,
        report.usage.cache_hit_rate * 100,
        report.cost_usd,
        report.cost_per_draft,
    )
    return report


def _jobs_of(session: "Session", row: DraftBatch) -> list[Job]:
    ids = list(row.job_ids or [])
    if not ids:
        return []
    return list(session.execute(select(Job).where(Job.id.in_(ids))).scalars().all())


def _land_result(
    session: "Session",
    row: DraftBatch,
    job: Job,
    result: Any,
    report: RunReport,
    handlers: Mapping[str, Any],
) -> None:
    """Turn one batch result into a drafts row, or into a failed job.

    Every failure path here is a `fail_job`, never a silent skip. A lead that
    drops out of the pipeline without a row and without an error is a lead the
    client believes was researched and found wanting.
    """

    kind = result.result.type

    if kind == "errored":
        error = result.result.error
        permanent = getattr(error, "type", None) == "invalid_request"
        report.failed += 1
        fail_job(session, job, f"batch error: {error}", permanent=permanent)
        return

    if kind in ("expired", "canceled"):
        report.failed += 1
        fail_job(session, job, f"batch request {kind}")
        return

    if kind != "succeeded":  # pragma: no cover - future result type
        report.failed += 1
        fail_job(session, job, f"unknown batch result type {kind!r}")
        return

    message = result.result.message
    usage = usage_of(message)
    report.usage = report.usage + usage
    report.cost_usd += price_usd(usage, model=row.model, batch=True)

    lead = session.get(Lead, job.lead_id)
    if lead is None:  # deleted between submit and collect
        complete_job(session, job)
        return

    try:
        content = parse_message(message)
    except ComposerRefused as exc:
        # NOT no_angle. See ComposerRefused. Held for a retry, and the queue's
        # give-up path puts the lead in `held` for a human rather than dropping
        # it. Note the Batches API rejects the server-side `fallbacks`
        # parameter, so there is no automatic second model to try here.
        report.failed += 1
        fail_job(session, job, exc)
        return
    except Exception as exc:  # truncated, or invalid against the schema
        report.failed += 1
        fail_job(session, job, exc)
        return

    # The model's own "these facts support nothing specific" answer. `score`
    # normally catches this first; when it does not, the answer is still no
    # email, never a generic one.
    if content.body_text.strip() == NO_ANGLE_SENTINEL:
        log.info("lead %s: model found no angle in qualifying facts", lead.id)
        result_score = score_lead(session, lead)
        persist_draft(
            session,
            lead,
            DraftOutcome(
                lead_status=LeadStatus.NO_ANGLE,
                score_result=result_score,
                reason="model found no specific angle in the qualifying facts",
            ),
        )
        report.no_angle += 1
        complete_job(session, job)
        return

    # A composer may only cite what it was given. Same check the synchronous
    # path makes in `draft_for_lead` — a model's promise, checked.
    offered = set(score_lead(session, lead).ids())
    stray = [fid for fid in content.signals_used if fid not in offered]
    if stray:
        report.failed += 1
        fail_job(
            session,
            job,
            f"composer cited fact ids that were not offered to it: {stray}",
            permanent=True,
        )
        return

    outcome = DraftOutcome(
        lead_status=LeadStatus.RESEARCHING,
        score_result=score_lead(session, lead),
        content=content,
        body=render_body(content.body_text, lead),
    )
    persist_draft(session, lead, outcome)
    report.drafted += 1
    complete_job(session, job)

    # Not READY. Verification still decides that — a batch result is no more
    # trusted than a synchronous one.
    if "verify" in handlers:
        enqueue_job(session, lead.id, "verify")


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
    }


def _check_cache_worked(row: DraftBatch, report: RunReport) -> None:
    """Record it loudly when prompt caching did nothing.

    This is the failure the guardrails call out by name: caching that silently
    stops working produces no error and no wrong output, only a bigger bill,
    and it stays broken for months because nothing looks wrong. The usage
    fields are the only ground truth, so a batch that read nothing from cache
    says so on its own row.

    Not an exception: the batch succeeded and the drafts are good. This is a
    cost defect, and it belongs in the record, not in a traceback.
    """

    if row.request_count < 2 or report.usage.cache_read_input_tokens > 0:
        return
    row.last_error = (
        "prompt caching did no work: cache_read_input_tokens == 0 across "
        f"{row.request_count} requests. Every lead paid full price for the "
        "system prompt. Check that the prefix is byte-identical (no date, "
        "name or flag in SYSTEM_PROMPT), that the prewarm ran, and that the "
        "prefix clears the model's minimum cacheable length."
    )
    log.error("draft batch %s: %s", row.batch_id, row.last_error)


def poll_open_batches(
    session: "Session",
    *,
    client: "Anthropic" | None = None,
    handlers: Mapping[str, Any] | None = None,
) -> list[RunReport]:
    """Poll every batch that has not ended. The cron entry point."""

    rows = (
        session.execute(select(DraftBatch).where(DraftBatch.ended_at.is_(None)))
        .scalars()
        .all()
    )
    return [poll_batch(session, row, client=client, handlers=handlers) for row in rows]


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


def run_once(
    session: "Session",
    *,
    limit: int = MAX_BATCH_SIZE,
    client: "Anthropic" | None = None,
    now: datetime | None = None,
) -> RunReport:
    """Claim, score, and submit one batch. Does not wait for it.

    Collection is a separate call (`poll_open_batches`) because a batch can
    take up to 24 hours and nothing should hold a database session open for
    that. Run this on whatever cadence suits the volume; run the poller more
    often.
    """

    from mazz.config import get_settings

    if get_settings().draft_composer != "claude":
        # The template cold path (qualify gate, company cap) runs in the queue
        # worker. This runner scores instead of qualifying, so it must not
        # drain `draft` jobs meant for a template campaign.
        raise RuntimeError(
            "draft_batch.run_once is the DRAFT_COMPOSER=claude path; it is off "
            "while DRAFT_COMPOSER is template"
        )
    jobs = claim_draft_jobs(session, limit=limit, now=now)
    if not jobs:
        return RunReport()

    plan = build_batch(session, jobs, now=now)
    report = RunReport(no_angle=plan.no_angle, submitted=len(plan.requests))
    if not plan:
        log.info("nothing to draft: all %d claimed leads had no angle", plan.no_angle)
        return report

    row = submit_batch(session, plan, client=client)
    report.batch_id = row.batch_id if row else None
    return report
