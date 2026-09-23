"""Enrichment: the research cache comes FIRST, the provider second.

``research_cache`` is keyed by company domain because the client's leads cluster
by employer -- he routinely works several people at one company. Company
research is about the company, so two colleagues must share exactly one cache
row and cost exactly one scrape between them. ``tests/test_queue.py`` asserts
the adapter's call count after processing both leads, and that assertion is the
whole justification for the domain key.

What is cached by domain and what is not
----------------------------------------
* **Company facts** (homepage, careers, blog, changelog, press) are keyed by
  domain. Lead #2 at the same employer is a cache HIT: zero provider calls.
* **LinkedIn facts** are about a PERSON. They live in the same domain-keyed row
  -- one row per company, as the model intends -- but they are tagged with
  ``subject_email`` and are fetched once per person, not once per domain.
  Skipping lead #2's profile would silently give him a weaker draft than his
  colleague's for no reason other than alphabetical order.

  That tag is also the delete-by-person hook: ``research_cache`` is company
  scoped and deliberately NOT cascade-deleted with a lead, so an untagged
  person-specific fact would survive a deletion request. ``enrich`` stamps
  ``subject_email`` itself rather than trusting the adapter to have done it.

An error is not an empty result
-------------------------------
A provider failure RAISES out of this module; it never returns "no facts".
"No facts" is a real finding that means ``no_angle`` downstream -- the client is
told there is genuinely nothing worth saying to this person. A swallowed
timeout returning ``[]`` would forge that finding and quietly shrink his
pipeline. So:

* ``FetchStatus.EMPTY`` / ``NOT_FOUND``  -> a successful lookup, cached, and a
  legitimate route to ``no_angle``;
* ``ProviderError``                      -> raised, the job retries with
  backoff, and the lead ends up ``held``, never ``no_angle``.

A cache row exists but has ``fetched_at IS NULL`` only if it has never been
successfully filled. Freshness is judged on ``fetched_at``, never on the row's
existence, so a failed attempt can never masquerade as a warm cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mazz.integrations.company import (
    CompanyAdapter,
    InvalidDomain,
    get_company_adapter,
    normalize_domain,
)
from mazz.integrations.linkedin import (
    InvalidProfileURL,
    LinkedInAdapter,
    get_linkedin_adapter,
)
from mazz.models import Lead, LeadStatus, ResearchCache
from mazz.schemas import Fact

__all__ = [
    "DEFAULT_MAX_AGE",
    "EnrichOutcome",
    "QUALIFY_JOB_KIND",
    "enrich_lead",
    "handle_enrich",
]

log = logging.getLogger("mazz.pipeline.enrich")

#: How long company research stays warm. Two weeks: long enough that a batch of
#: colleagues uploaded over a fortnight costs one scrape, short enough that a
#: job posting we cite is still open when the email lands.
DEFAULT_MAX_AGE = timedelta(days=14)

#: The step after enrichment. ``jobs.kind`` is free text, so this needs no
#: migration. ``handle_enrich`` chains to it only when a handler is registered
#: (see ``JobContext.enqueue_next``) -- in Phase 1 the scorer may not exist yet,
#: and queueing work nobody can run only manufactures failed jobs.
#: The stage enrichment chains to. Renamed from ``score`` with the cutover to
#: qualification: the question after enrichment is no longer "which of these
#: facts is the best reason to write" but "does the premise of the copy hold".
QUALIFY_JOB_KIND = "qualify"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class EnrichOutcome:
    """What one enrichment run did. Returned, logged, and asserted on."""

    company_domain: str
    cache_hit: bool
    company_fetched: bool
    person_fetched: bool
    provider_calls: int
    facts_total: int
    facts_added: int
    person_facts: int
    notes: list[str] = field(default_factory=list)

    @property
    def found_nothing(self) -> bool:
        """A SUCCESSFUL lookup that produced nothing.

        The only legitimate route to ``no_angle``. A provider error never gets
        here -- it raises.
        """

        return self.facts_total == 0

    def __str__(self) -> str:
        how = "cache hit" if self.cache_hit else "fetched"
        return (
            f"{self.company_domain}: {how}, {self.provider_calls} provider call(s), "
            f"{self.facts_total} fact(s) ({self.facts_added} new, "
            f"{self.person_facts} person-specific)"
        )


# --------------------------------------------------------------------------
# Cache plumbing
# --------------------------------------------------------------------------


def _as_facts(rows: Iterable[Any]) -> list[Fact]:
    """Rehydrate stored JSONB into Facts, skipping anything unreadable.

    A fact we cannot parse is dropped from the working set but left in the row:
    the drafter must not cite it, and silently deleting a citation an approved
    draft already rests on would be worse than ignoring it.
    """

    out: list[Fact] = []
    for row in rows or []:
        try:
            out.append(Fact.model_validate(row))
        except Exception:  # noqa: BLE001 - malformed legacy/foreign entry
            log.warning("skipping unparseable cached fact: %r", row)
    return out


def _dump(facts: Sequence[Fact]) -> list[dict[str, Any]]:
    """JSON-safe form for JSONB (``retrieved_at`` as an ISO-8601 string)."""

    return [f.model_dump(mode="json") for f in facts]


def _get_or_create_cache(session: Session, domain: str) -> tuple[ResearchCache, bool]:
    """Fetch the domain's cache row FOR UPDATE, creating it if absent.

    The row lock serializes two workers that happen to be enriching two
    colleagues at once; the SAVEPOINT around the insert turns the remaining
    create/create race into a re-select instead of a failed job.
    """

    stmt = (
        select(ResearchCache)
        .where(ResearchCache.company_domain == domain)
        .with_for_update()
    )
    cache = session.execute(stmt).scalar_one_or_none()
    if cache is not None:
        return cache, False

    try:
        with session.begin_nested():
            cache = ResearchCache(company_domain=domain, facts=[])
            session.add(cache)
            session.flush()
        return cache, True
    except IntegrityError:
        cache = session.execute(stmt).scalar_one()
        return cache, False


def _is_fresh(cache: ResearchCache, max_age: timedelta, now: datetime) -> bool:
    """Freshness is ``fetched_at``, never mere existence of the row."""

    if cache.fetched_at is None:
        return False
    fetched_at = cache.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (now - fetched_at) < max_age


def _merge(existing: Sequence[Fact], incoming: Sequence[Fact]) -> tuple[list[Fact], int]:
    """Union by stable fact id, existing first. Returns (merged, added)."""

    by_id = {f.id: f for f in existing}
    added = 0
    merged = list(existing)
    for fact in incoming:
        if fact.id in by_id:
            continue
        by_id[fact.id] = fact
        merged.append(fact)
        added += 1
    return merged, added


def _stamp_subject(facts: Sequence[Fact], email: str) -> list[Fact]:
    """Guarantee every person-specific fact carries its subject's email.

    The adapter is asked to set it, but this is a compliance control and
    compliance controls do not rely on somebody else remembering. An
    already-stamped fact is left exactly as it is, so its id stays stable.
    """

    out: list[Fact] = []
    for fact in facts:
        if fact.subject_email:
            out.append(fact)
        else:
            out.append(fact.model_copy(update={"subject_email": email}))
    return out


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------


def enrich_lead(
    session: Session,
    lead: Lead,
    *,
    company_adapter: CompanyAdapter | None = None,
    linkedin_adapter: LinkedInAdapter | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
    force: bool = False,
    now: datetime | None = None,
) -> EnrichOutcome:
    """Fill ``research_cache`` for this lead's company, then for the lead.

    Raises ``InvalidDomain`` if the lead has no usable domain (an ingest bug --
    ingest derives it) and lets every ``ProviderError`` propagate so the queue
    can retry it. It never returns an empty result to represent a failure.
    """

    now = now or _utcnow()
    domain = _lead_domain(lead)

    cache, created = _get_or_create_cache(session, domain)
    stored = _as_facts(cache.facts)
    fresh = (not force) and _is_fresh(cache, max_age, now)

    notes: list[str] = []
    provider_calls = 0
    company_fetched = False
    person_fetched = False
    incoming: list[Fact] = []

    # --- company research: domain-keyed, shared between colleagues ---------
    if fresh:
        notes.append(f"company research cached since {cache.fetched_at}")
    else:
        adapter = company_adapter or get_company_adapter()
        result = adapter.fetch_company(domain)  # ProviderError propagates
        provider_calls += 1
        company_fetched = True
        incoming.extend(result.facts)
        if result.found_nothing:
            # A real answer, not a failure: the site is reachable and has
            # nothing recently dated, or there is no site at all.
            notes.append(f"company lookup returned {result.status.value} (no facts)")

    # --- person research: keyed by the person, stored in the company row ---
    already_have_person = any(
        f.subject_email and f.subject_email.lower() == lead.email.lower()
        for f in stored
    )
    if already_have_person and not force:
        notes.append("profile facts for this person already cached")
    elif not lead.linkedin_url:
        notes.append("lead has no linkedin_url; company research only")
    else:
        adapter = linkedin_adapter or get_linkedin_adapter()
        try:
            result = adapter.fetch_profile(
                lead.linkedin_url, subject_email=lead.email
            )
        except InvalidProfileURL as exc:
            # Bad data on the lead record, not a provider outage. Retrying
            # cannot fix it and it must not throw away the company research we
            # just paid for, so it becomes a note the reviewer can act on.
            notes.append(f"linkedin_url unusable, skipped: {exc}")
        else:
            provider_calls += 1
            person_fetched = True
            incoming.extend(_stamp_subject(result.facts, lead.email))
            if result.found_nothing:
                notes.append(f"profile lookup returned {result.status.value}")

    merged, added = _merge(stored, incoming)

    cache.facts = _dump(merged)
    if lead.company_name and not cache.company_name:
        cache.company_name = lead.company_name
    if company_fetched or person_fetched:
        cache.fetched_at = now
        cache.last_error = None
    session.flush()

    outcome = EnrichOutcome(
        company_domain=domain,
        cache_hit=fresh,
        company_fetched=company_fetched,
        person_fetched=person_fetched,
        provider_calls=provider_calls,
        facts_total=len(merged),
        facts_added=added,
        person_facts=sum(1 for f in merged if f.subject_email),
        notes=notes,
    )
    log.info("enriched lead %s: %s (row %s)", lead.id, outcome, "new" if created else "existing")
    return outcome


def _lead_domain(lead: Lead) -> str:
    """The cache key, normalized identically to the adapter's lookup key."""

    for candidate in (lead.company_domain, lead.website, lead.email):
        if not candidate:
            continue
        try:
            return normalize_domain(candidate)
        except InvalidDomain:
            continue
    raise InvalidDomain(
        f"lead {lead.id} ({lead.email}) has no usable company domain; "
        "ingest should have derived one"
    )


# --------------------------------------------------------------------------
# Job handler
# --------------------------------------------------------------------------


def handle_enrich(ctx) -> None:
    """``jobs.kind == 'enrich'``.

    On a successful run that produced no facts at all, the lead becomes
    ``no_angle`` -- that is not a scoring threshold judgement, it is an
    absence of anything to score, and the brief is explicit that we surface it
    plainly rather than forcing a generic draft. Anything else stays
    ``researching`` and is handed to the scorer, which owns the threshold.

    A provider failure never reaches either branch: it propagates, the queue
    retries it, and an exhausted job leaves the lead ``held``.
    """

    from mazz.db import session_scope  # local: avoids an import cycle at module load

    domain = _lead_domain(ctx.lead)
    try:
        outcome = enrich_lead(ctx.session, ctx.lead)
    except Exception as exc:  # noqa: BLE001 - re-raised below
        # This transaction is doomed -- the queue is about to roll it back --
        # and it currently holds a FOR UPDATE lock on the company's cache row.
        # Releasing that lock first is not optional: a second connection would
        # otherwise block forever on a lock we ourselves are holding. The
        # durable record of the failure is jobs.last_error, written by the
        # queue; this is the company-level copy the review app can surface.
        ctx.session.rollback()
        _record_cache_error(session_scope, domain, exc)
        raise

    if outcome.found_nothing:
        ctx.lead.status = LeadStatus.NO_ANGLE
        ctx.session.flush()
        log.info(
            "lead %s -> no_angle: research succeeded and found nothing (%s)",
            ctx.lead.id,
            "; ".join(outcome.notes) or "no notes",
        )
        return

    ctx.enqueue_next(QUALIFY_JOB_KIND)


def _record_cache_error(session_scope, domain: str, exc: BaseException) -> None:
    """Note the failure against the company row, in its OWN transaction.

    The enrichment transaction is about to roll back, taking any write with it,
    so this uses a separate short session -- and the caller must have released
    its row lock first. ``fetched_at`` is deliberately left alone: a failed
    attempt must never look like a warm cache.
    """

    message = f"{exc.__class__.__name__}: {exc}"[:4000]
    try:
        with session_scope() as session:
            cache, _ = _get_or_create_cache(session, domain)
            cache.last_error = message
    except Exception:  # noqa: BLE001 - never mask the original failure
        log.exception("could not record research_cache error for %s", domain)
