"""SQLAlchemy 2.x models — the whole persistence layer.

The tables, per CLAUDE.md's data model:

  workspaces        one per agency-client ClientRise runs outbound for
  campaigns         a workspace's engagement + its scoring configuration
  leads             the incoming record + pipeline status
  research_cache    scraped facts, keyed by COMPANY DOMAIN (not by person)
  drafts            generated copy + the fact ids it rests on
  suppression_list  never-contact list, read on every ingest
  jobs              table-as-queue (no Redis/Celery at ~25 leads/day)
  draft_batches     one row per Claude Batch API submission + what it cost

Cascade semantics (delete-by-person, GDPR/CCPA):
  drafts.lead_id and jobs.lead_id are ON DELETE CASCADE, so deleting a lead
  row removes its drafts and queued work in one statement. research_cache is
  COMPANY-scoped and shared between colleagues, so it is deliberately NOT
  cascaded — the privacy module prunes individual person-specific facts out of
  the JSONB list by fact id / subject_email. See mazz.schemas.Fact.

  leads.campaign_id points the other way (child -> parent), so it can neither
  block nor survive a person deletion: dropping the lead drops the reference.
  It is ON DELETE SET NULL so that retiring a campaign never deletes people,
  and never cascades into their drafts and jobs.

Tenancy boundary:
  workspaces scope CAMPAIGNS, not RESEARCH. research_cache stays keyed by
  company domain alone and is deliberately shared across workspaces — two
  agency-clients targeting the same employer must still cost one research
  operation. That dedup is the largest cost lever in the system; do not add a
  workspace_id to research_cache.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Predictable constraint/index names so Alembic autogenerate can always name
# (and therefore drop) what it created.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------
# Enums — native Postgres types, storing the lowercase *values*.
# --------------------------------------------------------------------------


class LeadStatus(str, enum.Enum):
    RESEARCHING = "researching"
    READY = "ready"
    NO_ANGLE = "no_angle"
    APPROVED = "approved"
    SENT = "sent"
    HELD = "held"


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """Native Postgres enum storing the member values, not the member names."""

    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        create_type=True,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


lead_status_enum = _pg_enum(LeadStatus, "lead_status")
verification_status_enum = _pg_enum(VerificationStatus, "verification_status")
job_status_enum = _pg_enum(JobStatus, "job_status")


def _utcnow_column(**kwargs) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


# --------------------------------------------------------------------------
# workspaces
# --------------------------------------------------------------------------


class Workspace(Base):
    """One agency-client that ClientRise runs outbound on behalf of.

    The client's own Smartlead templates say the operating model is per-client:
    "Change the highlighted segments for clients target industry", and a booking
    email offering "our Founder, CLIENT NAME" (docs/CLIENT-REPLY-TEMPLATES.md).
    So the prospect ICP — and therefore the scoring vocabulary — varies per
    engagement. This table is the thing a campaign, and through it a scoring
    config, belongs to. ClientRise's own outbound is just another row.

    Deliberately minimal: identity and a display name. There is no auth, no user
    membership and no tenant-isolation middleware here, and adding the table
    does not create any — the review app is single-user session auth today, and
    real multi-tenancy is a separate piece of work (CLAUDE.md work-order item 5).
    Nothing in the pipeline filters by workspace yet; do not assume it does.
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Human-stable handle (``acme-digital``). Unique, and the identifier a CSV
    #: import or CLI names a workspace by, so that renaming the display name
    #: never breaks a reference and a numeric id never has to be typed.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Campaign.name",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workspace {self.id} {self.slug}>"


# --------------------------------------------------------------------------
# campaigns
# --------------------------------------------------------------------------


class Campaign(Base):
    """An engagement inside a workspace, carrying its own scoring configuration.

    Why the config lives here rather than in code: ``pipeline.score`` ships one
    ``SUPPORTED_ROLE_TERMS`` vocabulary written for a B2B SaaS seller (revops,
    sdr, sales engineer, crm). ClientRise is not a SaaS, and it sells for many
    different agency-clients whose ICPs have nothing in common. That vocabulary
    decides which leads clear the threshold and which land ``no_angle``, so it is
    the single biggest lever on both cost per usable lead and on conversations
    missed — and it cannot be one global constant.

    ``scoring_config`` is a JSONB object whose keys are ``ScoringConfig`` field
    names. It is a PARTIAL override: ``{}`` (the default) means "use
    ``DEFAULT_SCORING`` unchanged", and a config naming only
    ``supported_role_terms`` changes only that. Stored as a plain object rather
    than a typed column set because the knobs are judgement calls the client will
    move, and adding one must not need a migration.

        {"threshold": 45.0,
         "supported_role_terms": ["paid media", "performance marketing"]}

    Two constraints on the shape, both enforced where they can be:
      * it must be a JSON OBJECT, not a list or a scalar — a CHECK does that in
        the database, because a malformed config silently falling back to the
        SaaS defaults is exactly the failure this table exists to prevent;
      * ``supported_role_terms`` / ``off_target_role_terms`` arrive as JSON
        ARRAYS and must be rebuilt into the frozensets ``ScoringConfig`` wants.
        That coercion belongs in ``pipeline.score`` (models must not import the
        pipeline), and ``ScoringConfig.__post_init__`` still gets the last word:
        a config where funding alone could clear the threshold is refused there,
        per guardrail 2, no matter who stored it.

    ``segment`` is the segment identifier from CLAUDE.md's routing seam
    (``lead -> RoutingPolicy.route(lead) -> (campaign, segment, mailbox)``).
    There is deliberately no mailbox column: ``mailboxes`` is work-order item 4
    and does not exist yet. One-campaign-per-mailbox stays reachable without a
    migration because the relation can be carried on the mailbox row when that
    table lands.
    """

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Routing segment this campaign serves. Free text, like ``jobs.kind``: the
    #: segmentation a client agrees on is theirs, not ours, and a new one must
    #: not need a migration.
    segment: Mapped[str | None] = mapped_column(String(64))

    #: Partial ``ScoringConfig`` override. ``{}`` == the shipped defaults.
    scoring_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped[Workspace] = relationship(back_populates="campaigns")
    leads: Mapped[list["Lead"]] = relationship(
        back_populates="campaign",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_campaigns_workspace_id_name"),
        CheckConstraint(
            "jsonb_typeof(scoring_config) = 'object'",
            name="scoring_config_is_object",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Campaign {self.id} ws={self.workspace_id} {self.name!r}>"


# --------------------------------------------------------------------------
# leads
# --------------------------------------------------------------------------


class Lead(Base):
    """A person the client intends to contact.

    The client already supplies every identity field, so there is no discovery
    step — this table is populated straight from his CSV/manual entry.
    """

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- identity, supplied by the client ------------------------------
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(String(255))
    #: Unique: re-ingesting the same person UPDATES, never duplicates.
    #: Ingest is responsible for lower-casing/trimming before it gets here.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    employee_count: Mapped[int | None] = mapped_column(Integer)
    revenue_range: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(255))
    industry_primary: Mapped[str | None] = mapped_column(String(128))
    industry_specific: Mapped[str | None] = mapped_column(String(128))
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    #: Free text from the client. Along with research_cache, one of only two
    #: legitimate grounding sources for a draft.
    notes: Mapped[str | None] = mapped_column(Text)

    # --- derived --------------------------------------------------------
    #: Derived from `website` (or the email domain as a fallback) by ingest.
    #: INDEXED because it is the join key to research_cache — colleagues at
    #: the same employer share one cache row.
    company_domain: Mapped[str | None] = mapped_column(String(253), index=True)

    # --- tenancy --------------------------------------------------------
    #: Which campaign (and through it, which workspace and scoring config) this
    #: lead belongs to.
    #:
    #: NULLABLE on purpose, and it stays nullable. Every lead already in the
    #: database predates campaigns and must keep flowing through the pipeline
    #: unchanged — an unassigned lead scores against DEFAULT_SCORING, exactly as
    #: it does today. Backfilling a "default campaign" would be a lie about which
    #: engagement those leads came from.
    #:
    #: ON DELETE SET NULL, chosen over CASCADE and RESTRICT:
    #:   * CASCADE would mean retiring a campaign deletes its prospects, and
    #:     (through leads' own cascades) their drafts and queued jobs. Deleting
    #:     people is delete-by-person's job, never a config change's side effect.
    #:   * RESTRICT would block a campaign delete for as long as any lead
    #:     referenced it.
    #: SET NULL keeps the lead valid and falls back to the shipped defaults,
    #: which is what the nullable column already means. Delete-by-person is
    #: untouched either way: the reference lives on the lead row, so deleting
    #: the lead deletes the reference, and `campaigns` holds no personal data
    #: for privacy.find_person_references to find.
    #: INDEXED: "all leads in this campaign" is the review app's and the router's
    #: hot query.
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    # --- pipeline -------------------------------------------------------
    status: Mapped[LeadStatus] = mapped_column(
        lead_status_enum,
        nullable=False,
        default=LeadStatus.RESEARCHING,
        server_default=LeadStatus.RESEARCHING.value,
        index=True,
    )

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    drafts: Mapped[list["Draft"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Draft.created_at.desc()",
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    #: No cascade: a lead is not owned by its campaign (see campaign_id).
    campaign: Mapped["Campaign | None"] = relationship(back_populates="leads")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Lead {self.id} {self.email} {self.status}>"


# --------------------------------------------------------------------------
# research_cache
# --------------------------------------------------------------------------


class ResearchCache(Base):
    """Scraped/enriched facts for one company, keyed by domain.

    The primary key is the company domain, NOT a person: multiple leads
    routinely share an employer and must share one research row.

    ``facts`` is a JSON list of Fact objects (see mazz.schemas.Fact) — each
    with a STABLE ``id``, because drafts cite those ids. Individual entries
    can be filtered out of the list (delete-by-person prunes facts whose
    ``subject_email`` matches) without dropping the row, which would destroy
    the colleagues' company-level research too.
    """

    __tablename__ = "research_cache"

    company_domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String(255))

    facts: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    #: When the enrichment adapters last ran successfully for this domain.
    #: Drives cache-freshness decisions in the pipeline.
    fetched_at: Mapped[datetime | None] = _utcnow_column()
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ResearchCache {self.company_domain} facts={len(self.facts or [])}>"


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------


class Draft(Base):
    """Generated email copy for a lead.

    A lead may accumulate several drafts over time; the newest row governs
    (see the (lead_id, created_at DESC) index).
    """

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    #: JSON list of the Fact ids this copy was built from. Every factual claim
    #: in `body` must trace back to one of these (or to the lead's `notes`).
    signals_used: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    verification_status: Mapped[VerificationStatus] = mapped_column(
        verification_status_enum,
        nullable=False,
        default=VerificationStatus.PENDING,
        server_default=VerificationStatus.PENDING.value,
    )
    #: Why verification failed, for the reviewer and the auditor. Free text.
    verification_notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )

    lead: Mapped[Lead] = relationship(back_populates="drafts")

    # "the latest draft governs" — this index serves the newest-per-lead lookup.
    __table_args__ = (
        Index("ix_drafts_lead_id_created_at", "lead_id", text("created_at DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Draft {self.id} lead={self.lead_id} {self.verification_status}>"


# --------------------------------------------------------------------------
# suppression_list
# --------------------------------------------------------------------------


class SuppressionEntry(Base):
    """Never-contact list. Read on every ingest, before a job row exists.

    An entry suppresses by exact email, by whole domain, or (when both are
    set) by the narrower of the two. At least one must be non-null — enforced
    in the database, not just in Python.
    """

    __tablename__ = "suppression_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    domain: Mapped[str | None] = mapped_column(String(253), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR domain IS NOT NULL",
            name="email_or_domain",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SuppressionEntry {self.email or self.domain}>"


# --------------------------------------------------------------------------
# draft_batches
# --------------------------------------------------------------------------


class DraftBatch(Base):
    """One submission to the Claude Batch API, and what it actually cost.

    Drafting runs in bulk because the Batch API is half price and nothing about
    a draft is latency-sensitive -- a lead waits for a human to approve it
    either way. That trade costs us the request/response symmetry the rest of
    the pipeline enjoys: a batch is submitted now and drains over minutes to
    (worst case) 24 hours, so the in-flight work needs a row.

    This table is also the ONLY place the pipeline records what AI actually
    costs. The cost-per-usable-lead figures in the gap analysis are projections
    from a spreadsheet; `usage` and `cost_usd` here are measured. Keep the rows
    -- the point is the time series, not the current batch.

    ``job_ids`` is the reconciliation list. Each request's ``custom_id`` encodes
    its jobs row id, so results map back without a join table; this list is how
    we notice a job whose result never arrived at all (expired, or dropped).
    """

    __tablename__ = "draft_batches"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Anthropic's batch id (``msgbatch_...``). Unique: one row per submission,
    #: and the poller looks batches up by it.
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    #: Which model drafted. Recorded per batch, not read from config at report
    #: time, so a later model switch does not silently rewrite history -- and so
    #: cost can be compared across a model change.
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Mirrors the API's ``processing_status``: in_progress / canceling / ended.
    #: Free text for the same reason ``jobs.kind`` is: the API may add a state.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="in_progress", index=True
    )

    job_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    succeeded_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    errored_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Token counts, shaped like `compose_claude.Usage`. The field that matters
    #: is `cache_read_input_tokens`: if it is ~0 across batches, prompt caching
    #: is silently broken and we are paying full price for the system prompt on
    #: every single lead.
    usage: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: Estimated list-price cost of this batch, in USD. Reporting only; nothing
    #: branches on it.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))

    submitted_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = _utcnow_column()
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DraftBatch {self.batch_id} {self.status} n={self.request_count}>"


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


class Job(Base):
    """Table-as-queue for the async pipeline steps.

    The brief rules out Redis/Celery at ~25 leads/day. The claim query is

        SELECT ... FROM jobs
        WHERE status = 'pending' AND run_after <= now()
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED LIMIT 1

    which is exactly what ix_jobs_status_run_after serves.

    ``kind`` is free text (enrich / score / draft / verify / push ...) so the
    pipeline can add steps without a migration.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum,
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Not eligible to be claimed before this instant. Backoff sets it forward.
    run_after: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    #: Set when a worker claims the row; cleared on completion. A stale
    #: locked_at is how a crashed worker's job gets reclaimed.
    locked_at: Mapped[datetime | None] = _utcnow_column()

    created_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = _utcnow_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lead: Mapped[Lead] = relationship(back_populates="jobs")

    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.id} {self.kind} {self.status} lead={self.lead_id}>"


__all__ = [
    "Base",
    "Campaign",
    "Draft",
    "DraftBatch",
    "Job",
    "JobStatus",
    "Lead",
    "LeadStatus",
    "ResearchCache",
    "SuppressionEntry",
    "VerificationStatus",
    "Workspace",
]
