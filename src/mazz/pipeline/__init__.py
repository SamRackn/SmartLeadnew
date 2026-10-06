"""The pipeline: the table-as-queue worker, enrichment, and campaign routing.

``queue`` claims work with ``SELECT ... FOR UPDATE SKIP LOCKED`` so two workers
can never double-run a lead; ``enrich`` checks the domain-keyed
``research_cache`` before it ever calls a provider, so colleagues at one
employer cost one lookup between them; ``routing`` decides which campaign — and
therefore which scoring config — a lead belongs to.
"""

from mazz.pipeline.enrich import EnrichOutcome, enrich_lead, handle_enrich
from mazz.pipeline.routing import (
    DEFAULT_ROUTING_POLICY,
    PinnedRoutingPolicy,
    RoutingDecision,
    RoutingPolicy,
    SystemRoutingPolicy,
    assign_campaign,
)
from mazz.pipeline.queue import (
    default_handlers,
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
    "default_handlers",
    "DEFAULT_ROUTING_POLICY",
    "EnrichOutcome",
    "JobContext",
    "PermanentJobError",
    "PinnedRoutingPolicy",
    "RetrySettings",
    "RoutingDecision",
    "RoutingPolicy",
    "SystemRoutingPolicy",
    "UnknownJobKind",
    "WorkerStats",
    "assign_campaign",
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
