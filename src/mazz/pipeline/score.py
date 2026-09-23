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
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from mazz.models import Campaign, Lead, LeadStatus, ResearchCache
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
    "UNCONFIRMED_DATED_CONTENT_KIND",
    "UNCONFIRMED_JOB_POSTING_KIND",
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
    "config_for_lead",
    "scoring_config_from_json",
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
    """Funding news, homepage boilerplate, reqs outside the campaign's
    vocabulary, unknown kinds. WEAK can never qualify, however fresh."""


class RoleFit(str, Enum):
    """Whether a job posting is a role the client's product touches.

    Note what these mean and do not mean. ``classify_role_fit`` reports which
    vocabulary a req matched; it does NOT decide the tier. ``UNCLASSIFIED``
    means only "matched neither list", and what that is *worth* depends on
    whose vocabulary was in force -- see ``ScoringConfig.treats_unclassified_as_off_target``.
    """

    SUPPORTED = "supported"
    """Revenue/GTM/customer-facing: exactly what the product replaces or supports."""

    OFF_TARGET = "off_target"
    """A backend engineer req is hiring, but it is not OUR reason to call."""

    UNCLASSIFIED = "unclassified"
    """Recognisably a req, matched neither vocabulary. Never the top tier when
    the campaign named its own vocabulary."""


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

    #: What a req that matches NEITHER vocabulary is worth. See
    #: `treats_unclassified_as_off_target` below for the whole argument; the
    #: short version is that ``None`` (the default) derives the answer from
    #: whether this config names its own supported vocabulary, and setting it
    #: explicitly overrides that derivation in either direction.
    unclassified_is_off_target: bool | None = None

    @property
    def vocabulary_is_customised(self) -> bool:
        """Did whoever configured this campaign state their own ICP vocabulary?

        Compared against the shipped constant rather than tracked as a flag, so
        it cannot drift out of sync with the field it describes: a campaign that
        stores the shipped list verbatim has not customised anything, and is
        treated as not having customised anything.
        """

        return self.supported_role_terms != SUPPORTED_ROLE_TERMS

    @property
    def treats_unclassified_as_off_target(self) -> bool:
        """Does a req outside the vocabulary fall out of contention entirely?

        ``UNCLASSIFIED`` quietly changed meaning when the config became
        per-campaign. Under ONE global vocabulary it meant *"we do not recognise
        this role"* — a rare, genuinely ambiguous req — and 55.0 at STRONG was
        defensible: we could not name the role, but hiring is present-tense pain
        and somebody at that company is under it right now.

        Under a per-campaign vocabulary it means *"not in THIS client's
        vocabulary"*, which is an enormously broader and far less interesting
        set: every req at every company that is not the handful of roles this
        engagement is about. Keeping 55.0/STRONG there means an operator who
        writes a tight vocabulary for a new agency-client still gets very nearly
        the same leads, at a slightly lower score — a config that reads as
        applied and barely moves a verdict, which is the precise failure this
        module refuses everywhere else.

        So: when a campaign names its own vocabulary, that naming is itself the
        evidence. The operator has said which roles mean "reach out"; a req that
        is none of them is off-target for this engagement, scored at the
        off-target weight and WEAK — where tier, not arithmetic, keeps it out of
        a draft. The reviewer still sees it, labelled as outside the vocabulary
        rather than as unrecognised, because those are different findings.

        When the shipped vocabulary is in force (which is every lead with
        ``campaign_id IS NULL``, and every campaign that did not narrow it) this
        is False and the 55.0/STRONG reading is unchanged, byte for byte.

        A client who genuinely wants any hiring signal at all can say so with
        ``unclassified_is_off_target: false`` alongside their vocabulary. That
        is a decision worth writing down rather than inheriting by accident.
        """

        if self.unclassified_is_off_target is not None:
            return self.unclassified_is_off_target
        return self.vocabulary_is_customised

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

# --------------------------------------------------------------------------
# Unconfirmed-provenance sentinels
# --------------------------------------------------------------------------
#
# The ingest layer tags every careers-page and dated-content candidate with how
# it was identified, and refuses to admit a low-confidence one at its nominal
# kind — it emits one of these sentinels instead. That closes the guardrail gap
# in CLAUDE.md from the producing side: verification cannot catch a false fact,
# because a misidentified filter control is exactly as *traceable* as a real
# req once it is in the cache.
#
# These already failed closed here with no code at all: an unrecognised kind
# lands on `unknown_kind` (5.0) at WEAK, and WEAK can never qualify however
# fresh it is. Naming them explicitly changes NOTHING about that arithmetic.
# What it changes is what the reviewer reads. "unknown fact kind
# 'job_posting_unconfirmed'; scored weak until the scorer learns it" says the
# scorer is out of date. It is not: the pipeline looked, found something
# job-shaped, and could not confirm it was a real req. That is a finding, and
# the reviewer should see it as one.
#
# Mirrored as strings rather than imported from `mazz.integrations.company`.
# The scorer reads whatever is in `research_cache` and must not depend on which
# adapter produced it — any future provider can emit these. `test_score.py`
# asserts the strings still match the producer's constants, so a rename over
# there fails the build here rather than silently reverting these facts to the
# generic unknown-kind message.
UNCONFIRMED_JOB_POSTING_KIND = "job_posting_unconfirmed"
UNCONFIRMED_DATED_CONTENT_KIND = "dated_content_unconfirmed"

_UNCONFIRMED_KINDS = {
    UNCONFIRMED_JOB_POSTING_KIND: (
        "job candidate we could not confirm is a real req"
    ),
    UNCONFIRMED_DATED_CONTENT_KIND: (
        "dated item whose date we could not confirm"
    ),
}


def _provenance_suffix(fact: Fact, *, only: bool = False) -> str:
    """How a demoted candidate was identified, in the reviewer's own line.

    The ingest layer records `identification_method` on anything it had to
    judge. Surfacing it means "we could not confirm it" comes with the reason,
    which is the difference between a reviewer trusting the verdict and
    wondering whether the scorer is broken.
    """

    method = (fact.metadata or {}).get("identification_method")
    if not method:
        return ""
    return f" (identified {'only ' if only else ''}by {str(method)!r})"


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
        elif config.treats_unclassified_as_off_target:
            # This campaign named its own vocabulary, so "matched none of it" is
            # the operator's own verdict, not our ignorance. WEAK is what makes a
            # narrowed vocabulary actually narrow — the tier gates it structurally,
            # the same way funding is gated, so no weight anybody stores can let it
            # back in.
            base, tier = config.job_posting_off_target, SignalTier.WEAK
            why = (
                "job posting, but no role in this campaign's vocabulary of "
                f"{len(config.supported_role_terms)} — outside this client's ICP"
            )
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
    elif kind in _UNCONFIRMED_KINDS:
        # Deliberately still WEAK at the unknown-kind weight. The weight is not
        # what keeps this out of a draft — the tier is, structurally — so there
        # is no knob here to misconfigure. It is not dropped either: the
        # reviewer sees it in the signal list, correctly labelled as something
        # we found and could not stand behind.
        base, tier = config.unknown_kind, SignalTier.WEAK
        why = (
            f"{_UNCONFIRMED_KINDS[kind]}"
            f"{_provenance_suffix(fact, only=True)}; not a reason to reach out until it is"
            " confirmed"
        )
    else:
        base, tier = config.unknown_kind, SignalTier.WEAK
        why = f"unknown fact kind {kind!r}; scored weak until the scorer learns it"

    # A medium-confidence identification is ADMITTED at its nominal kind and
    # scores exactly like a high-confidence one. That is deliberate and it is
    # the honest position: nobody has measured how much less often a
    # title-vocabulary match is a real req than an ATS-host match, and inventing
    # a multiplier would put a made-up number on the largest lever in the
    # system. It is said out loud in the reason instead, where a reviewer who
    # has seen a few of them can judge, and where a future measurement has
    # something to compare against.
    metadata = fact.metadata or {}
    if str(metadata.get("confidence") or "").lower() == "medium":
        why += (
            "; medium-confidence identification "
            f"({str(metadata.get('identification_method'))!r})"
        )

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


# Cache reading moved to `pipeline.facts` — it was never scoring, and `qualify`
# and `verify` both need it without importing this module. Re-exported here so
# existing callers and tests keep working while the event stack is removed.
from mazz.pipeline.facts import (  # noqa: E402
    facts_for_domain,
    facts_for_lead,
    parse_facts,
)


# --------------------------------------------------------------------------
# Per-campaign configuration
# --------------------------------------------------------------------------
#
# ClientRise runs outbound on behalf of each agency-client at that client's own
# target industry, so the ICP is per engagement (docs/DECISIONS.md, and the
# client's own templates: "Change the highlighted segments for clients target
# industry"). The vocabulary in SUPPORTED_ROLE_TERMS above is written for a B2B
# SaaS seller, which ClientRise is not — and that vocabulary decides which leads
# clear the threshold and which are settled `no_angle`. One global constant
# cannot serve two engagements, so the config is loaded from the lead's campaign.

#: `ScoringConfig` fields that are sets in Python and arrays in JSON.
_FROZENSET_FIELDS = ("supported_role_terms", "off_target_role_terms")


def scoring_config_from_json(stored: Mapping[str, Any]) -> ScoringConfig:
    """Rebuild a stored `campaigns.scoring_config` into a `ScoringConfig`.

    A PARTIAL override, spread over the shipped defaults: a row naming only
    ``threshold`` keeps every shipped weight, and ``{}`` means the defaults
    unchanged. That is what makes the column safe to add a knob to later.

    Three things it refuses to do, all for the same reason — a client's broken
    config silently reverting to the SaaS vocabulary would move that client's
    `no_angle` rate for reasons nobody can see, and `no_angle` is the number the
    whole product is judged on:

    * it does not swallow an unknown key (a typo'd knob would otherwise be a
      setting that reads as applied and is not);
    * it does not swallow a non-object (the database has a CHECK for this, and
      this is the same refusal one layer up);
    * it does not catch `ScoringConfig.__post_init__`. Guardrail 2 — funding
      alone can never clear the threshold — gets the last word over anything
      anybody stored, and a config that breaks it fails the job.

    Role terms are matched case-insensitively against lower-cased fact content,
    so they are normalised here rather than quietly never matching: a client who
    writes "Paid Media" means the same thing as "paid media".
    """

    if not isinstance(stored, Mapping):
        raise TypeError(
            f"campaigns.scoring_config must be a JSON object, got "
            f"{type(stored).__name__}: a malformed config must fail loudly, not "
            "fall back to the shipped defaults"
        )
    if not stored:
        return DEFAULT_SCORING

    valid = {f.name for f in fields(ScoringConfig)}
    unknown = sorted(set(stored) - valid)
    if unknown:
        raise ValueError(
            f"campaigns.scoring_config names unknown scoring knobs {unknown}; "
            f"valid keys are {sorted(valid)}. Refusing to apply a config that is "
            "partly ignored — a typo'd knob would read as set and do nothing."
        )

    _refuse_dead_unclassified_weight(stored)

    flag = stored.get("unclassified_is_off_target", None)
    if flag is not None and not isinstance(flag, bool):
        # JSON's "false" is truthy in Python, and this flag decides whether a
        # whole class of reqs can reach a draft. Refuse rather than guess.
        raise ValueError(
            "campaigns.scoring_config['unclassified_is_off_target'] must be true, "
            f"false or absent, got {flag!r}"
        )

    kwargs: dict[str, Any] = {name: getattr(DEFAULT_SCORING, name) for name in valid}
    kwargs.update(stored)
    for key in _FROZENSET_FIELDS:
        kwargs[key] = _coerce_role_terms(key, kwargs[key])
    return ScoringConfig(**kwargs)


def _refuse_dead_unclassified_weight(stored: Mapping[str, Any]) -> None:
    """Refuse a config where ``job_posting_unclassified`` would do nothing.

    Naming a vocabulary makes an unmatched req off-target
    (``ScoringConfig.treats_unclassified_as_off_target``), which means the
    unclassified WEIGHT stops being consulted. A row that sets both is stating
    two incompatible things, and the one that loses would be a knob that reads
    as applied and is ignored — the same failure as a typo'd key, and refused
    the same way. The fix is one line either direction, and the error says so.
    """

    if "job_posting_unclassified" not in stored:
        return
    if "supported_role_terms" not in stored:
        return
    if "unclassified_is_off_target" in stored:
        return
    raise ValueError(
        "campaigns.scoring_config sets both 'supported_role_terms' and "
        "'job_posting_unclassified', which contradict: naming a vocabulary makes "
        "a req outside it off-target, so the unclassified weight is never read. "
        "Say which you mean with 'unclassified_is_off_target': true to keep the "
        "narrowing (and drop the dead weight), false to score any req this "
        "vocabulary does not match at 'job_posting_unclassified' as before."
    )


def _coerce_role_terms(key: str, value: Any) -> frozenset[str]:
    """JSON has no frozenset. Rebuild one, and refuse anything that is not terms."""

    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"campaigns.scoring_config[{key!r}] must be an array of role terms, "
            f"not a single string ({value!r}). One string would be read as a set "
            "of its characters and match nearly every req."
        )
    try:
        terms = list(value)
    except TypeError:
        raise ValueError(
            f"campaigns.scoring_config[{key!r}] must be an array of role terms, "
            f"got {type(value).__name__}"
        ) from None
    cleaned: set[str] = set()
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            raise ValueError(
                f"campaigns.scoring_config[{key!r}] contains {term!r}; every role "
                "term must be a non-empty string"
            )
        cleaned.add(term.strip().lower())
    return frozenset(cleaned)


def config_for_lead(session: "Session", lead: Lead) -> ScoringConfig:
    """The scoring config in force for this lead: its campaign's, or the defaults.

    ``leads.campaign_id`` is nullable and every lead in the database predates
    campaigns, so NULL is a steady state rather than a migration to finish — it
    means the shipped defaults, exactly as before.
    """

    campaign_id = getattr(lead, "campaign_id", None)
    if campaign_id is None:
        return DEFAULT_SCORING

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        # The FK is ON DELETE SET NULL, so this should be unreachable. If it
        # happens, the id is stale rather than the config being wrong, and the
        # shipped defaults are what a NULL would have meant anyway — but it is
        # worth a line in the log, because it means something raced.
        log.warning(
            "lead %s references campaign %s, which does not exist; scoring with "
            "the shipped defaults",
            lead.id,
            campaign_id,
        )
        return DEFAULT_SCORING

    return scoring_config_from_json(campaign.scoring_config or {})


def score_lead(
    session: "Session",
    lead: Lead,
    *,
    now: datetime | None = None,
    config: ScoringConfig | None = None,
) -> ScoreResult:
    """Score one lead's cached research. Does not write anything.

    ``config=None`` — the normal path — resolves the lead's campaign config.
    Pass one explicitly only to score a lead against a config it does not have
    (the review app's "what would this look like under X" has no caller yet).
    """

    resolved = config if config is not None else config_for_lead(session, lead)
    facts = facts_for_lead(session, lead)
    return score_facts(
        facts, now=now, config=resolved, company_domain=lead.company_domain
    )


def handle_score(ctx: "JobContext") -> None:
    """Queue handler for ``jobs.kind == 'score'``.

    On an angle: mark the lead READY-for-drafting and chain to ``draft``.
    On no angle: set ``no_angle`` and STOP. No draft job is queued, because the
    correct output for this lead is nothing at all, plainly labelled.

    Passes no config on purpose: `score_lead` resolves the lead's own campaign
    config. A config argument here would be one ICP applied to every client's
    prospects, which is the thing per-campaign scoring exists to prevent.
    """

    result = score_lead(ctx.session, ctx.lead)

    if not result.has_angle:
        ctx.lead.status = LeadStatus.NO_ANGLE
        ctx.session.flush()
        log.info("lead %s -> no_angle: %s", ctx.lead.id, result.rationale)
        return

    log.info("lead %s has an angle: %s", ctx.lead.id, result.rationale)
    ctx.enqueue_next("draft")
