"""Reading cached research out of `research_cache`. Nothing else.

Split out of `pipeline.score` because these three functions are not scoring:
they are how every stage after enrichment gets at the domain-keyed cache, and
`qualify` and `verify` both need them while neither should have to import the
retired event-scoring module to do it.

One rule runs through all of them. A fact that does not parse is SKIPPED with a
warning, never guessed at and never fatal: one malformed entry must not take
out the other facts for that company, and nothing is invented to replace it.
Fewer facts simply means less evidence, which is the honest outcome and shows
up downstream as a lead we decline to write to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import select

from mazz.models import Lead, ResearchCache
from mazz.schemas import Fact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

__all__ = ["facts_for_domain", "facts_for_lead", "parse_facts"]


def facts_for_domain(session: "Session", company_domain: str | None) -> list[Fact]:
    """Load and parse the cached facts for one domain."""

    if not company_domain:
        return []

    row = session.execute(
        select(ResearchCache).where(ResearchCache.company_domain == company_domain)
    ).scalar_one_or_none()
    if row is None:
        return []
    return parse_facts(row.facts or [], company_domain=company_domain)


def parse_facts(raw: Sequence[Any], *, company_domain: str | None = None) -> list[Fact]:
    """Coerce the JSONB fact list into `Fact` objects, skipping unparseable ones."""

    facts: list[Fact] = []
    for entry in raw:
        if isinstance(entry, Fact):
            facts.append(entry)
            continue
        try:
            facts.append(Fact.model_validate(entry))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "skipping unparseable cached fact for %s: %s", company_domain, exc
            )
    return facts


def facts_for_lead(session: "Session", lead: Lead) -> list[Fact]:
    """Cached facts for this lead's employer. research_cache is domain-keyed.

    Keyed by COMPANY DOMAIN, never by person: fifty prospects at one employer
    share one row and cost one research operation between them. That dedup is
    the largest cost lever in the system.
    """

    return facts_for_domain(session, lead.company_domain)
