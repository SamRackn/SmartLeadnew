"""SQLAlchemy 2.x models — the whole persistence layer.

Five tables, per the brief's data model:

  leads             the incoming record + pipeline status
  research_cache    scraped facts, keyed by COMPANY DOMAIN (not by person)
  drafts            generated copy + the fact ids it rests on
  suppression_list  never-contact list, read on every ingest
  jobs              table-as-queue (no Redis/Celery at ~25 leads/day)

Cascade semantics (delete-by-person, GDPR/CCPA):
  drafts.lead_id and jobs.lead_id are ON DELETE CASCADE, so deleting a lead
  row removes its drafts and queued work in one statement. research_cache is
  COMPANY-scoped and shared between colleagues, so it is deliberately NOT
  cascaded — the privacy module prunes individual person-specific facts out of
  the JSONB list by fact id / subject_email. See mazz.schemas.Fact.
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
    String,
    Text,
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
    "Draft",
    "Job",
    "JobStatus",
    "Lead",
    "LeadStatus",
    "ResearchCache",
    "SuppressionEntry",
    "VerificationStatus",
]
