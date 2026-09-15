"""The pipeline: the table-as-queue worker and the enrichment stage.

``queue`` claims work with ``SELECT ... FOR UPDATE SKIP LOCKED`` so two workers
can never double-run a lead; ``enrich`` checks the domain-keyed
``research_cache`` before it ever calls a provider, so colleagues at one
employer cost one lookup between them.
"""

from mazz.pipeline.enrich import EnrichOutcome, enrich_lead, handle_enrich
from mazz.pipeline.queue import (
    DEFAULT_HANDLERS,
    JobContext,
    PermanentJobError,
    RetrySettings,
    UnknownJobKind,
    WorkerStats,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    process_next,
    reclaim_stale_jobs,
    run_worker,
)

__all__ = [
    "DEFAULT_HANDLERS",
    "EnrichOutcome",
    "JobContext",
    "PermanentJobError",
    "RetrySettings",
    "UnknownJobKind",
    "WorkerStats",
    "claim_job",
    "complete_job",
    "enqueue_job",
    "enrich_lead",
    "fail_job",
    "handle_enrich",
    "process_next",
    "reclaim_stale_jobs",
    "run_worker",
]
