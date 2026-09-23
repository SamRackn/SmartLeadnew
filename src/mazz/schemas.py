"""Pydantic mirrors of the persistence layer.

Most of this file is ordinary row-shaped DTOs. One thing in it is a contract:
``Fact``. Read that section before writing an enrichment adapter, a scorer, a
drafter, or the verifier.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mazz.models import JobStatus, LeadStatus, VerificationStatus

__all__ = [
    "Fact",
    "FactKind",
    "QUALIFICATION_KINDS",
    "stable_fact_id",
    "DraftCreate",
    "DraftRead",
    "JobRead",
    "JobStatus",
    "LeadCreate",
    "LeadRead",
    "LeadStatus",
    "ResearchCacheRead",
    "ResearchCacheWrite",
    "SuppressionEntryCreate",
    "SuppressionEntryRead",
    "VerificationStatus",
]


# ==========================================================================
# THE FACT CONTRACT
# ==========================================================================
#
# A Fact is one retrieved piece of evidence about a company (or, for LinkedIn
# lookups, about a person at that company). Facts are the ONLY thing a draft
# may rest on, besides the lead's own `notes` field.
#
# Facts live as a JSON list in research_cache.facts, keyed by company domain.
# They are produced by the integrations layer, ranked by the scorer, cited by
# the drafter (in drafts.signals_used), and re-resolved by the verifier.
#
# The five contract fields — `id`, `kind`, `content`, `source_url`,
# `retrieved_at` — are the shape every producer must emit and every consumer
# may rely on.
#
#   id            STABLE identifier, unique within the domain's fact list.
#                 "Stable" means: re-running enrichment over unchanged source
#                 material MUST produce the same id, because drafts store
#                 these ids and the verifier resolves them later. Do NOT use
#                 a uuid4 or a list position. Use `stable_fact_id()` below,
#                 or any deterministic function of the source material.
#
#   kind          What sort of evidence this is. See FactKind for the shared
#                 vocabulary; the field is typed `str` so a producer can add
#                 a kind without a schema change, but anything outside
#                 FactKind scores as unknown/weak until the scorer learns it.
#
#   content       The human-readable snippet, verbatim from the source. This
#                 is the text a claim in the draft must trace back to. Do not
#                 paraphrase or summarize here — verification compares against
#                 it, and a summary is already an unverifiable claim.
#
#   source_url    Where `content` came from. May be None ONLY for facts that
#                 have no URL (e.g. derived from the lead's notes). The
#                 verifier treats a fact with no source_url as untraceable:
#                 it fails closed. Scraped facts must always carry one.
#
#   retrieved_at  When it was fetched. Recency is most of the scoring signal —
#                 a job posting from last week beats a blog post from 2019.
#                 Timezone-aware UTC.
#
# Two optional fields, additive and safe to ignore:
#
#   subject_email GDPR/CCPA hook. Set it when a fact is about a specific
#                 PERSON rather than the company (a LinkedIn profile fetch,
#                 say). research_cache is company-scoped and therefore NOT
#                 cascade-deleted with a lead, so delete-by-person prunes the
#                 facts list by this field. A person-specific fact emitted
#                 without it will survive a deletion request — that is a
#                 compliance bug, so set it.
#
#   metadata      Free-form producer extras (job title, post date, headcount
#                 delta...). Never load-bearing for verification.
#
# ==========================================================================


class FactKind(str, Enum):
    """Shared vocabulary for ``Fact.kind``.

    Two generations of kinds live here, and the split is the point.

    QUALIFICATION kinds are the pages where a business describes itself: the
    homepage, a services/practice-area page, a contact page. They are what
    `pipeline.qualify` reads to answer "does the premise of this email hold?"
    -- is this the vertical the campaign sells to, do they take on new clients
    -- and they are the only kinds the shipped template copy rests on. Two or
    three fetches per domain, cached, instead of a crawl.

    EVENT kinds answer "what happened at this company recently?" -- the job
    postings, changelogs and funding rounds the pipeline was originally built
    around. They are retained because the cache holds them and because a reply
    is worth deep research, but nothing on the cold path scores them any more:
    the ICP is small professional-services firms with no careers page, no
    changelog and no press feed, and a ranking that demands one of those
    refuses to write to almost every real lead.
    """

    # --- qualification pages --------------------------------------------
    #
    # Named for the SOURCE, never for the interpretation. `research_cache` is
    # keyed by company domain and deliberately shared across workspaces, so a
    # fact in it must mean the same thing to every campaign that reads it.
    # "This page says 'Practice Areas'" is shared; "this is a law firm, which
    # is the vertical MY campaign sells to" is `pipeline.qualify`'s per-campaign
    # reading of that same text, and is never written back to the cache.

    #: A services / practice-areas / "what we do" page. Where a business says
    #: what it is in its own words -- the evidence the vertical rests on.
    SERVICES_PAGE = "services_page"
    #: A contact / intake page. Where evidence of taking new clients lives:
    #: an intake form, a "free consultation", a "new clients welcome" line.
    CONTACT_PAGE = "contact_page"

    # --- event (deep research; not scored on the cold path) -------------
    JOB_POSTING = "job_posting"
    BLOG_POST = "blog_post"
    CHANGELOG = "changelog"
    PRESS = "press"
    FUNDING = "funding"

    # --- context --------------------------------------------------------
    HOMEPAGE = "homepage"
    CAREERS_PAGE = "careers_page"
    LINKEDIN_PROFILE = "linkedin_profile"
    LEAD_NOTES = "lead_notes"


#: The kinds `pipeline.qualify` reads. A fact outside this set cannot confirm a
#: vertical, and so cannot be cited by the shipped template copy. Deliberately
#: narrow: these are the pages where a business describes ITSELF. A blog post
#: mentioning a law firm is not evidence that this company is one.
QUALIFICATION_KINDS: frozenset[str] = frozenset(
    {
        FactKind.HOMEPAGE.value,
        FactKind.SERVICES_PAGE.value,
        FactKind.CONTACT_PAGE.value,
    }
)


def stable_fact_id(kind: str, source_url: str | None, content: str) -> str:
    """Deterministic fact id: same source material -> same id, always.

    Producers may use any scheme they like as long as it has that property;
    this is the default so that two adapters do not invent two.
    """

    digest = hashlib.sha256(
        "\x1f".join([kind, source_url or "", content.strip()]).encode("utf-8")
    ).hexdigest()
    return f"{kind}:{digest[:16]}"


class Fact(BaseModel):
    """One retrieved piece of evidence. See THE FACT CONTRACT above."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str = Field(description="Stable id, cited by drafts. Never positional.")
    kind: str = Field(description="FactKind value, or a new producer-defined kind.")
    content: str = Field(description="Verbatim snippet the draft must trace to.")
    source_url: str | None = Field(
        default=None,
        description="Origin URL. None only for non-URL facts; untraceable to the verifier.",
    )
    retrieved_at: datetime = Field(description="Fetch time, timezone-aware UTC.")

    subject_email: str | None = Field(
        default=None,
        description="Set when the fact is about a person; drives delete-by-person pruning.",
    )
    metadata: dict = Field(
        default_factory=dict, description="Producer extras. Never load-bearing."
    )

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        content: str,
        source_url: str | None = None,
        retrieved_at: datetime,
        subject_email: str | None = None,
        metadata: dict | None = None,
    ) -> "Fact":
        """Construct a Fact with a stable, derived id."""

        return cls(
            id=stable_fact_id(kind, source_url, content),
            kind=kind,
            content=content,
            source_url=source_url,
            retrieved_at=retrieved_at,
            subject_email=subject_email,
            metadata=metadata or {},
        )


# ==========================================================================
# leads
# ==========================================================================


class LeadBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str
    email: str
    company_name: str | None = None
    website: str | None = None
    job_title: str | None = None
    employee_count: int | None = None
    revenue_range: str | None = None
    location: str | None = None
    industry_primary: str | None = None
    industry_specific: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None


class LeadCreate(LeadBase):
    """Inbound lead (CSV row or manual entry).

    ``company_domain`` is derived by the ingest layer from ``website`` (email
    domain as fallback) — it is accepted here but ingest is free to overwrite
    it. Re-ingesting an existing ``email`` updates that row; it never
    duplicates (``leads.email`` is UNIQUE).
    """

    company_domain: str | None = None


class LeadRead(LeadBase):
    id: int
    company_domain: str | None = None
    status: LeadStatus
    created_at: datetime
    updated_at: datetime


# ==========================================================================
# research_cache
# ==========================================================================


class ResearchCacheRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_domain: str
    company_name: str | None = None
    facts: list[Fact] = Field(default_factory=list)
    fetched_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ResearchCacheWrite(BaseModel):
    """What an enrichment run hands the persistence layer."""

    model_config = ConfigDict(from_attributes=True)

    company_domain: str
    company_name: str | None = None
    facts: list[Fact] = Field(default_factory=list)
    fetched_at: datetime | None = None
    last_error: str | None = None


# ==========================================================================
# drafts
# ==========================================================================


class DraftCreate(BaseModel):
    """The structured output shape of the drafter.

    ``signals_used`` holds Fact ids — the same strings as ``Fact.id``, not
    indexes and not the snippets themselves. The verifier resolves each one
    back against research_cache; an id it cannot resolve fails closed.
    """

    model_config = ConfigDict(from_attributes=True)

    lead_id: int
    subject: str
    body: str
    signals_used: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    verification_notes: str | None = None


class DraftRead(DraftCreate):
    id: int
    created_at: datetime


# ==========================================================================
# suppression_list
# ==========================================================================


class SuppressionEntryCreate(BaseModel):
    """At least one of email/domain must be set — mirrored by a DB CHECK."""

    model_config = ConfigDict(from_attributes=True)

    email: str | None = None
    domain: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _require_email_or_domain(self) -> "SuppressionEntryCreate":
        if not self.email and not self.domain:
            raise ValueError("suppression entry needs an email or a domain")
        return self


class SuppressionEntryRead(SuppressionEntryCreate):
    id: int
    added_at: datetime


# ==========================================================================
# jobs
# ==========================================================================


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    kind: str
    status: JobStatus
    attempts: int
    run_after: datetime
    last_error: str | None = None
    locked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
