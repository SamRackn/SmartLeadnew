"""Signal scoring: rank retrieved facts by "reason to reach out RIGHT NOW".

This is pipeline step 3. It answers one question per company, and it is the only
step allowed to answer it: *is there an angle at all?*

The ranking is the brief's, not a general-purpose relevance model:

  STRONG  A job posting for a role the client's product replaces or supports.
          The best signal there is. A req means budget was approved and the pain
          is present-tense, this quarter, with a named owner. Nothing else in the
          corpus carries that.

  MEDIUM  A recently dated blog post, changelog entry or press item. A real
          reason to reach out while it is still fresh, which is why recency is
          most of the score here: the same post is a good opener in week one and
          an embarrassment in year two. Old material decays toward the floor.

  WEAK    Funding news. The brief is explicit and unusually blunt about this:
          every competitor's tool already leads with funding, so opening with it
          is the opposite of differentiation. It is scored low enough that
          funding can NEVER clear the threshold on its own, no matter how fresh
          or how large the round. It is kept (not dropped) because it is fine as
          supporting colour once a real angle exists.

If nothing clears the threshold the lead is `no_angle` and NO DRAFT IS WRITTEN.
That is a feature and it is the specific failure the client is paying to escape:
his complaint about the incumbent is that it always produces something, so every
lead looks worked and none of them are. An honest empty beats filler, so the
scorer refuses to lower its own bar just because a lead would otherwise end the
day with nothing to show. ``no_angle`` is surfaced plainly to the reviewer.

What this module does NOT do: judge whether a claim is *verifiable*. That is
`verify`, and it is a separate, harsher control. Scoring is about interest;
verification is about truth.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from mazz.models import Lead, LeadStatus, ResearchCache
from mazz.schemas import Fact, FactKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

__all__ = [
    "SignalTier",
    "RoleFit",
    "ScoringConfig",
    "DEFAULT_SCORING",
    "ScoredFact",
    "ScoreResult",
    "classify_role_fit",
    "published_at_of",
    "recency_multiplier",
    "score_fact",
    "score_facts",
    "facts_for_domain",
    "facts_for_lead",
    "parse_facts",
    "score_lead",
    "handle_score",
]


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------


class SignalTier(str, Enum):
    """Why a fact scored what it scored, in the brief's own vocabulary.

    The tier is *semantic* (what kind of evidence this is); the numeric score
    additionally folds in recency. Keep both: the reviewer wants to see "strong
    signal, slightly stale", not a bare 61.4.
    """

    STRONG = "strong"
    """Job posting for a role the product replaces or supports."""

    MEDIUM = "medium"
    """Recently dated blog / changelog / press."""

    WEAK = "weak"
    """Funding news, homepage boilerplate, off-target reqs, unknown kinds."""


class RoleFit(str, Enum):
    """Whether a job posting is a role the client's product touches."""

    SUPPORTED = "supported"
    """Revenue/GTM/customer-facing: exactly what the product replaces or supports."""

    OFF_TARGET = "off_target"
    """A backend engineer req is hiring, but it is not OUR reason to call."""

    UNCLASSIFIED = "unclassified"
    """Recognisably a req, role unclear. Still a hiring signal, scored lower."""


# Terms that mark a req as a role the client's B2B SaaS revenue product either
# replaces (manual RevOps/SDR work) or supports (the team that would use it).
# Deliberately a plain vocabulary rather than a model: it is inspectable by the
# client, and he is the one who knows his ICP. Override via ScoringConfig.
SUPPORTED_ROLE_TERMS: frozenset[str] = frozenset(
    {
        "revenue operations",
        "revops",
        "rev ops",
        "sales operations",
        "sales ops",
        "sales development",
        "sdr",
        "bdr",
        "business development",
        "account executive",
        "account manager",
        "demand generation",
        "demand gen",
        "growth marketing",
        "marketing operations",
        "marketing ops",
        "lifecycle marketing",
        "outbound",
        "go-to-market",
        "gtm",
        "customer success",
        "customer support",
        "support specialist",
        "sales engineer",
        "solutions engineer",
        "partnerships",
        "crm",
        "pipeline generation",
    }
)

# Terms that mark a req as somebody else's sales opportunity. A company hiring a
# backend engineer is hiring; it is not a reason for THIS pitch, and pretending
# it is would be the generic filler the brief bans.
OFF_TARGET_ROLE_TERMS: frozenset[str] = frozenset(
    {
        "backend engineer",
        "back-end engineer",
        "frontend engineer",
        "front-end engineer",
        "software engineer",
        "mobile engineer",
        "ios engineer",
        "android engineer",
        "data engineer",
        "platform engineer",
        "infrastructure engineer",
        "devops",
        "site reliability",
        "security engineer",
        "machine learning engineer",
        "qa engineer",
        "product designer",
        "ux designer",
        "controller",
        "accountant",
        "bookkeeper",
        "recruiter",
        "paralegal",
        "warehouse",
        "driver",
        "nurse",
        "technician",
    }
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Every number the scorer uses, in one inspectable place.

    These are judgement calls, not physics, and the client will want to move
    them once he sees real leads. They live here (a) so moving one is a one-line
    change with a test, and (b) so the ONE number that must not move casually --
    ``funding`` staying under ``threshold`` -- is visible next to the threshold
    rather than buried in a branch. ``__post_init__`` refuses a config where
    funding alone could clear.
    """

    threshold: float = 40.0

    # --- base weights by kind ------------------------------------------
    job_posting_supported: float = 75.0
    job_posting_unclassified: float = 55.0
    job_posting_off_target: float = 20.0
    changelog: float = 45.0
    blog_post: float = 42.0
    press: float = 40.0
    funding: float = 12.0
    careers_page: float = 12.0
    linkedin_profile: float = 10.0
    homepage: float = 8.0
    lead_notes: float = 10.0
    unknown_kind: float = 5.0

    # --- recency -------------------------------------------------------
    #: Anything published inside this window is "now"; no decay at all.
    fresh_days: int = 30
    #: Past the fresh window, value halves every this-many days.
    half_life_days: float = 60.0
    #: Decay floor. Old material is worth little, never negative.
    decay_floor: float = 0.10
    #: A fact with no publication date gets this multiplier. We do not know it
    #: is fresh, and `retrieved_at` only says when WE looked -- treating scrape
    #: time as publication time would make a 2019 page look like this morning.
    undated_multiplier: float = 0.55

    supported_role_terms: frozenset[str] = SUPPORTED_ROLE_TERMS
    off_target_role_terms: frozenset[str] = OFF_TARGET_ROLE_TERMS

    def __post_init__(self) -> None:
        if self.funding >= self.threshold:
            raise ValueError(
                "funding must score below the threshold: the brief is explicit "
                "that funding news alone is not a reason to reach out "
                f"(funding={self.funding}, threshold={self.threshold})"
            )
        if self.job_posting_supported < self.threshold:
            raise ValueError(
                "a supported-role job posting is the strongest signal there is; "
                "it must be able to clear the threshold"
            )


DEFAULT_SCORING = ScoringConfig()


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoredFact:
    """One fact with its score, tier and a human-readable reason.

    ``reason`` exists for the review UI: the client clicks a lead and must see
    not just the draft but *why these signals*. A number alone is not an
    explanation.
    """

    fact: Fact
    score: float
    tier: SignalTier
    reason: str
    role_fit: RoleFit | None = None
    age_days: int | None = None

    @property
    def id(self) -> str:
        return self.fact.id

    # NOTE: there is deliberately no `ScoredFact.clears_threshold`. A fact does
    # not know the threshold it was judged against; `ScoreResult.qualifying`
    # owns that comparison so one result set can never mix two thresholds.


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """The verdict for one company's fact list."""

    scored: tuple[ScoredFact, ...]
    threshold: float
    company_domain: str | None = None
    #: Facts at or above the threshold, best first. The drafter uses only these.
    qualifying: tuple[ScoredFact, ...] = ()
    rationale: str = ""

    @property
    def has_angle(self) -> bool:
        """The whole point of this module. False -> `no_angle`, no draft."""
        return bool(self.qualifying)

    @property
    def top(self) -> ScoredFact | None:
        return self.qualifying[0] if self.qualifying else None

    @property
    def lead_status(self) -> LeadStatus:
        return LeadStatus.READY if self.has_angle else LeadStatus.NO_ANGLE

    def ids(self) -> list[str]:
        return [s.fact.id for s in self.qualifying]


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------

_DATE_IN_CONTENT = re.compile(r"^\[(\d{4}-\d{2}-\d{2})[^\]]*\]")


def published_at_of(fact: Fact) -> datetime | None:
    """When the SOURCE says it was published, or None if it does not say.

    Order of trust: adapter metadata (``published_at`` for company facts,
    ``posted_at`` for LinkedIn ones), then the ``[YYYY-MM-DD]`` prefix the
    company adapter stamps onto dated content. ``retrieved_at`` is deliberately
    NOT a fallback -- it is when we scraped, not when they published, and
    conflating them makes every stale page look brand new.
    """

    meta: Mapping[str, Any] = fact.metadata or {}
    for key in ("published_at", "posted_at", "date"):
        raw = meta.get(key)
        if not raw:
            continue
        parsed = _coerce_datetime(raw)
        if parsed is not None:
            return parsed

    match = _DATE_IN_CONTENT.match(fact.content or "")
    if match:
        return _coerce_datetime(match.group(1))
    return None


def _coerce_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency_multiplier(
    published: datetime | None,
    now: datetime,
    config: ScoringConfig = DEFAULT_SCORING,
) -> tuple[float, int | None]:
    """Return (multiplier, age_in_days). Fresh is 1.0; old decays to the floor.

    Future-dated material (a req posted with tomorrow's date, clock skew) is
    treated as fresh rather than rejected -- it is a data quirk, not a lie.
    """

    if published is None:
        return config.undated_multiplier, None

    age_days = int((now - published).total_seconds() // 86400)
    if age_days <= config.fresh_days:
        return 1.0, max(age_days, 0)

    halvings = (age_days - config.fresh_days) / config.half_life_days
    return max(0.5**halvings, config.decay_floor), age_days


# --------------------------------------------------------------------------
# Role fit
# --------------------------------------------------------------------------


def classify_role_fit(
    content: str, config: ScoringConfig = DEFAULT_SCORING
) -> tuple[RoleFit, str | None]:
    """Is this req a role our product replaces or supports?

    Supported wins ties on purpose: "Sales Engineer" contains "engineer", and a
    req that mentions both an off-target and a supported role is still a room we
    belong in.
    """

    text = (content or "").lower()
    for term in sorted(config.supported_role_terms, key=len, reverse=True):
        if term in text:
            return RoleFit.SUPPORTED, term
    for term in sorted(config.off_target_role_terms, key=len, reverse=True):
        if term in text:
            return RoleFit.OFF_TARGET, term
    return RoleFit.UNCLASSIFIED, None


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

_MEDIUM_KINDS = {
    FactKind.BLOG_POST.value,
    FactKind.CHANGELOG.value,
    FactKind.PRESS.value,
}


def score_fact(
    fact: Fact,
    *,
    now: datetime,
    config: ScoringConfig = DEFAULT_SCORING,
) -> ScoredFact:
    """Score one fact. Pure, deterministic given ``now``."""

    kind = (fact.kind or "").strip().lower()
    published = published_at_of(fact)
    multiplier, age_days = recency_multiplier(published, now, config)
    role_fit: RoleFit | None = None

    if kind == FactKind.JOB_POSTING.value:
        role_fit, matched = classify_role_fit(fact.content, config)
        if role_fit is RoleFit.SUPPORTED:
            base, tier = config.job_posting_supported, SignalTier.STRONG
            why = f"job posting for a role we support ({matched!r})"
        elif role_fit is RoleFit.OFF_TARGET:
            base, tier = config.job_posting_off_target, SignalTier.WEAK
            why = f"job posting, but off-target for this pitch ({matched!r})"
        else:
            base, tier = config.job_posting_unclassified, SignalTier.STRONG
            why = "job posting, role unclassified but hiring is present-tense pain"
    elif kind in _MEDIUM_KINDS:
        base = {
            FactKind.BLOG_POST.value: config.blog_post,
            FactKind.CHANGELOG.value: config.changelog,
            FactKind.PRESS.value: config.press,
        }[kind]
        tier = SignalTier.MEDIUM
        why = f"dated {kind.replace('_', ' ')}"
    elif kind == FactKind.FUNDING.value:
        base, tier = config.funding, SignalTier.WEAK
        why = "funding news — every competitor's tool already opens with this"
    elif kind == FactKind.CAREERS_PAGE.value:
        base, tier = config.careers_page, SignalTier.WEAK
        why = "careers page itself, not a specific req"
    elif kind == FactKind.LINKEDIN_PROFILE.value:
        base, tier = config.linkedin_profile, SignalTier.WEAK
        why = "profile background — context, not a reason to reach out now"
    elif kind == FactKind.HOMEPAGE.value:
        base, tier = config.homepage, SignalTier.WEAK
        why = "homepage positioning — true of them every day of the year"
    elif kind == FactKind.LEAD_NOTES.value:
        base, tier = config.lead_notes, SignalTier.WEAK
        why = "lead notes — grounding material, not a timing signal"
    else:
        base, tier = config.unknown_kind, SignalTier.WEAK
        why = f"unknown fact kind {kind!r}; scored weak until the scorer learns it"

    score = round(base * multiplier, 2)

    if age_days is None:
        why += "; undated, so discounted"
    elif multiplier < 1.0:
        why += f"; {age_days}d old, decayed"
    elif age_days is not None:
        why += f"; {age_days}d old"

    return ScoredFact(
        fact=fact,
        score=score,
        tier=tier,
        reason=why,
        role_fit=role_fit,
        age_days=age_days,
    )


def _sort_key(scored: ScoredFact) -> tuple:
    # Deterministic total order: score desc, tier, then FRESHER first (two reqs
    # posted the same month score identically; the newer one is the better
    # opener), then fact id as the final tiebreak. Fact ids are stable, so two
    # runs over one cache row produce byte-identical ordering -- which is what
    # makes Phase 1 drafting reproducible.
    tier_rank = {SignalTier.STRONG: 0, SignalTier.MEDIUM: 1, SignalTier.WEAK: 2}
    age = scored.age_days if scored.age_days is not None else 10**6
    return (-scored.score, tier_rank[scored.tier], age, scored.fact.id)


def score_facts(
    facts: Iterable[Fact],
    *,
    now: datetime | None = None,
    config: ScoringConfig = DEFAULT_SCORING,
    company_domain: str | None = None,
) -> ScoreResult:
    """Score and rank a company's facts. The `no_angle` decision lives here.

    Pure and deterministic given ``now``: no DB, no clock unless you omit it.
    """

    now = now or datetime.now(timezone.utc)
    scored = tuple(sorted((score_fact(f, now=now, config=config) for f in facts), key=_sort_key))

    # A WEAK fact can never qualify however fresh it is. That is the funding
    # rule made structural rather than arithmetic: even if somebody raises the
    # funding weight in config one day, tier still gates it.
    qualifying = tuple(
        s for s in scored if s.tier is not SignalTier.WEAK and s.score >= config.threshold
    )

    if qualifying:
        top = qualifying[0]
        rationale = (
            f"{len(qualifying)} of {len(scored)} facts cleared {config.threshold:g}; "
            f"best: {top.tier.value} ({top.score:g}) — {top.reason}"
        )
    elif not scored:
        rationale = "no facts cached for this company — nothing to reach out about"
    else:
        best = scored[0]
        rationale = (
            f"no angle: best of {len(scored)} facts scored {best.score:g}, under "
            f"{config.threshold:g} — {best.reason}"
        )

    return ScoreResult(
        scored=scored,
        threshold=config.threshold,
        company_domain=company_domain,
        qualifying=qualifying,
        rationale=rationale,
    )


# --------------------------------------------------------------------------
# Database glue
# --------------------------------------------------------------------------


def facts_for_domain(session: "Session", company_domain: str | None) -> list[Fact]:
    """Load and parse the cached facts for one domain.

    A row whose JSON does not parse is skipped with a warning rather than
    crashing the lead: one malformed fact must not take out the others. Nothing
    is invented to replace it -- fewer facts simply means a weaker angle, which
    is the honest outcome.
    """

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
    """Cached facts for this lead's employer. research_cache is domain-keyed."""

    return facts_for_domain(session, lead.company_domain)


def score_lead(
    session: "Session",
    lead: Lead,
    *,
    now: datetime | None = None,
    config: ScoringConfig = DEFAULT_SCORING,
) -> ScoreResult:
    """Score one lead's cached research. Does not write anything."""

    facts = facts_for_lead(session, lead)
    return score_facts(facts, now=now, config=config, company_domain=lead.company_domain)


def handle_score(ctx: "JobContext") -> None:
    """Queue handler for ``jobs.kind == 'score'``.

    On an angle: mark the lead READY-for-drafting and chain to ``draft``.
    On no angle: set ``no_angle`` and STOP. No draft job is queued, because the
    correct output for this lead is nothing at all, plainly labelled.
    """

    result = score_lead(ctx.session, ctx.lead)

    if not result.has_angle:
        ctx.lead.status = LeadStatus.NO_ANGLE
        ctx.session.flush()
        log.info("lead %s -> no_angle: %s", ctx.lead.id, result.rationale)
        return

    log.info("lead %s has an angle: %s", ctx.lead.id, result.rationale)
    ctx.enqueue_next("draft")
