"""Qualification: does the PREMISE of this email hold?

This is pipeline step 3, and it replaces signal scoring on the cold path.

--------------------------------------------------------------------------
WHY THE QUESTION CHANGED
--------------------------------------------------------------------------
The step this replaces ranked facts by "reason to reach out RIGHT NOW" — a job
req, a changelog entry, a fresh blog post. That is the right question when the
recipient is a software company that publishes. It is the wrong question here,
and the client's own sent mail is the evidence:

    Hey Louis,

    Quick question — is DAME Legal currently looking to bring in more clients?

    We help law firms generate qualified client opportunities through search
    marketing. One recent client saw 50+ potential clients in a single month.

    Worth sending over a little more info?

Three variable slots. Two of them (first name, company name) are already on the
lead row. The third is a single noun — "law firms" — and it is a *category*,
not an event. The one number in the message is about the SENDER's client, not
the recipient's business.

So the expensive half of the old step was producing facts this copy never used,
and the cheap half was refusing to write at all: a nine-person law firm has no
careers page, no changelog and no press feed, so nothing cleared the threshold
and every lead in a real list landed `no_angle` with no draft. A control that
refuses to write to the entire addressable market is not a strict control, it
is a broken one.

The question this module asks instead is the one the copy actually depends on:

  1. **Is this business the vertical the campaign sells to?**  The email says
     "We help law firms ..." to someone it is calling a law firm. That word has
     to be earned from the prospect's own site, or the message is addressed to
     a business that does not exist.
  2. **Does the presupposition hold?**  "Are you looking to bring in more
     clients" presumes they take client intake at all. Optional per campaign,
     because for some verticals it is a given and a second fetch is waste.
  3. **Is there a reason NOT to write?**  A parked domain, a national brand
     with an in-house team, an explicit "not accepting new clients".

--------------------------------------------------------------------------
WHAT THIS MODULE COSTS
--------------------------------------------------------------------------
Nothing. No model call, no tokens, no network. The campaign supplies the
vocabulary; this module matches it against text already in the domain-keyed
cache. AI is ~70% of cost per usable lead and this is the step that used to
decide whether to spend it — now the cold path spends none of it at all, and
the model budget is available for the leads that reply.

A `Classifier` seam is deliberately NOT introduced here. When deterministic
matching is not enough for some vertical, the thing to add is a classifier
whose verdict is written back to the cache as a fact, so it is paid for once
per domain and cited by id like any other evidence — not a per-lead call
wrapped around this function.

--------------------------------------------------------------------------
GUARDRAIL 2, RESTATED RATHER THAN RELAXED
--------------------------------------------------------------------------
"Weak signals mean no email" survives, with the gate moved to where the copy's
risk actually is. The failure the client is paying to escape is a message that
looks worked and is not — and for this copy that failure is not a missing news
item, it is telling a dental practice we help law firms.

So: a lead whose vertical is not confirmed from its own site is `no_angle` and
NO DRAFT IS WRITTEN. There is no generic fallback, no "helps businesses like
yours", no writing the email with the noun left vague. `DEFAULT_QUALIFICATION`
has an EMPTY vertical vocabulary, which qualifies nobody — a campaign that has
not been configured writes to no one, rather than to everyone.

What this module does NOT do: judge whether the finished copy is traceable.
That is `verify`, and it is a separate, harsher control that re-derives every
claim from the cache. Qualification is about whether to write; verification is
about what was written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, fields, replace
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from mazz.models import Campaign, Lead
from mazz.pipeline.facts import facts_for_lead
from mazz.schemas import QUALIFICATION_KINDS, Fact, LeadStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_QUALIFICATION",
    "DEFAULT_DISQUALIFIER_TERMS",
    "DEFAULT_INTAKE_TERMS",
    "Match",
    "QualificationConfig",
    "QualificationResult",
    "Verdict",
    "config_for_lead",
    "config_for_campaign",
    "handle_qualify",
    "qualification_config_from_json",
    "qualify_facts",
    "qualify_lead",
]

QUALIFY_JOB_KIND = "qualify"


class Verdict(str, Enum):
    """The three outcomes, and only two of them write an email.

    ``UNCONFIRMED`` and ``DISQUALIFIED`` both end as `no_angle` on the lead —
    the reviewer sees no draft either way — but they are kept apart because
    they mean opposite things operationally. A run full of ``UNCONFIRMED`` is
    a vocabulary that needs widening or a scrape that is failing. A run full
    of ``DISQUALIFIED`` is a list that was bought badly. Collapsing them would
    hide which of those is happening.
    """

    QUALIFIED = "qualified"
    UNCONFIRMED = "unconfirmed"
    DISQUALIFIED = "disqualified"


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Evidence that a business takes on new clients. Vertical-neutral on purpose:
#: these are the words every service business uses on a contact page, so the
#: shipped set is useful before a client has written a single term of their
#: own. Widening this is safe in a way that widening `vertical_terms` is not —
#: a false intake match sends a message to a real member of the vertical, while
#: a false vertical match sends the wrong message entirely.
DEFAULT_INTAKE_TERMS: frozenset[str] = frozenset(
    {
        "free consultation",
        "free case evaluation",
        "free quote",
        "book a consultation",
        "schedule a consultation",
        "request a consultation",
        "new clients",
        "new patients",
        "accepting new",
        "contact us today",
        "get in touch",
        "request a quote",
        "no obligation",
        "get started",
    }
)

#: Reasons not to write, shipped as a floor every campaign inherits. These are
#: statements a site makes about itself that make the ask wrong or the message
#: embarrassing, not judgements about fit.
DEFAULT_DISQUALIFIER_TERMS: frozenset[str] = frozenset(
    {
        "not accepting new clients",
        "not accepting new patients",
        "no longer accepting",
        "this domain is for sale",
        "domain for sale",
        "under construction",
        "coming soon",
        "permanently closed",
        "we have closed",
        "ceased operations",
    }
)


@dataclass(frozen=True, slots=True)
class QualificationConfig:
    """Per-campaign vocabulary. The whole of it; there is nothing global.

    Stored as ``campaigns.qualification_config``, a partial JSONB override with
    the same contract as the scoring config it replaces: ``{}`` means the
    shipped defaults, unknown keys are refused rather than ignored.

    ``vertical_terms`` deliberately has NO shipped default. It is the one field
    with no safe guess: every other knob widens or narrows a decision, and this
    one decides whether the word in "We help ___" is true. An empty set
    qualifies nobody, so a campaign nobody has configured sends nothing —
    which is the correct behaviour for a fail-closed control and the exact
    opposite of what a permissive default would do.
    """

    #: Terms that, appearing on the prospect's OWN pages, confirm they are the
    #: vertical this campaign sells to. Matched case-insensitively on word
    #: boundaries, so "law" does not match "flawless" and "legal" does not
    #: match "paralegal".
    vertical_terms: frozenset[str] = frozenset()

    #: The specific service line the offer targets, if the campaign is narrower
    #: than its vertical ("personal injury" inside "law firms"). Empty means the
    #: campaign addresses the whole vertical. Never required on its own — it is
    #: recorded as supporting evidence and surfaced to the reviewer.
    service_terms: frozenset[str] = frozenset()

    intake_terms: frozenset[str] = DEFAULT_INTAKE_TERMS
    disqualifier_terms: frozenset[str] = DEFAULT_DISQUALIFIER_TERMS

    #: Require evidence of client intake, not just the vertical.
    #:
    #: ON by default, and the default is not a preference. Email 1 opens "is
    #: <company> currently looking to bring in more clients?", and the verifier
    #: only licenses that register when a cited `contact_page` fact backs it
    #: (see `lexicon.KIND_VOCABULARY`). With this off, a lead can qualify, get
    #: a draft written, and then be held at verification for the evidence this
    #: gate would have asked for one step earlier — the same outcome, reached
    #: more expensively and logged as a quality failure instead of a research
    #: gap. Turn it off only for a campaign whose copy asks no such question.
    require_intake_signal: bool = True

    #: Whether a vertical has to be confirmed at all.
    #:
    #: Set from the CAMPAIGN, not from stored JSON (`config_for_lead` does it,
    #: and the loader refuses the key) — it is a fact about what the copy says,
    #: not a knob someone tunes. False when the campaign has opted into the
    #: generic phrase "businesses like yours", which asserts nothing about the
    #: recipient and so leaves the vertical gate nothing to check.
    #:
    #: It waives that gate and NOTHING else. The opening question still
    #: presupposes they take on new clients, and a site that disqualifies
    #: itself still stops the lead: the generic phrase changes what we claim,
    #: not whether it is worth writing to this company at all.
    vertical_required: bool = True

    #: How many DISTINCT vertical terms must appear before the vertical counts
    #: as confirmed. 1 is right for an unambiguous vocabulary ("attorneys at
    #: law"); raise it for one built from weaker words.
    min_vertical_matches: int = 1

    #: Blocked/unreachable-site fallback. Used ONLY when the cache holds no
    #: homepage/services/contact page at all, and only when BOTH match: a
    #: term in the lead's industry fields AND a term in its company name
    #: ("Legal Services" + "Smith Law PLLC"). Either set empty = no fallback,
    #: which is the default -- a lead we could not read stays unconfirmed.
    fallback_industry_terms: frozenset[str] = frozenset()
    fallback_name_terms: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.min_vertical_matches < 1:
            # Zero would mean "confirmed with no evidence", i.e. every domain
            # that resolves is a law firm. That is guardrail 2 turned off from
            # a JSON field, so it is refused here rather than validated in the
            # UI alone.
            raise ValueError(
                "qualification_config['min_vertical_matches'] must be at least 1; "
                f"got {self.min_vertical_matches}. A vertical confirmed by zero "
                "terms is a vertical asserted without evidence."
            )
        overlap = self.vertical_terms & self.disqualifier_terms
        if overlap:
            raise ValueError(
                "qualification_config: "
                f"{sorted(overlap)} appear in both vertical_terms and "
                "disqualifier_terms, so the same sentence would both confirm and "
                "refuse the lead. Disqualifiers win, making those vertical terms "
                "dead weight — say which you meant."
            )


DEFAULT_QUALIFICATION = QualificationConfig()


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_DATE_PREFIX = re.compile(r"^\[[^\]]*\]\s*")


def _normalize(text: str) -> str:
    """Lower-cased, whitespace-collapsed, leading date stamp removed.

    Matches the normalisation the adapters apply when they build fact content,
    so a term a client can see on the page is a term that matches.
    """

    folded = (text or "").replace("’", "'").replace("‘", "'")
    return _WS.sub(" ", _DATE_PREFIX.sub("", folded)).strip().lower()


@lru_cache(maxsize=256)
def _term_pattern(terms: frozenset[str]) -> re.Pattern[str] | None:
    """One alternation per term set, word-bounded, longest-first.

    Longest-first so "not accepting new clients" wins over "new clients" when
    both are present — otherwise a disqualifier reads as an intake signal,
    which is the single worst misread available here.

    Cached on the frozenset because a campaign's vocabulary is stable across a
    whole run and recompiling per lead is pure waste.
    """

    cleaned = sorted(
        {t for t in (_normalize(term) for term in terms) if t},
        key=lambda t: (-len(t), t),
    )
    if not cleaned:
        return None
    alts = "|".join(re.escape(t) for t in cleaned)
    # \b on both ends, but the terms are multi-word, so the boundary is around
    # the whole phrase: "legal" does not match inside "paralegal", and
    # "new clients" does not match "new clientside".
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Match:
    """One term found in one fact. The unit of evidence this module produces.

    Carries the fact id rather than a copy of the reasoning because everything
    downstream — the draft's ``signals_used``, verification, the review UI —
    addresses evidence by id. ``excerpt`` exists for the reviewer, who should
    be able to see WHY we called this a law firm without opening the site.
    """

    fact_id: str
    term: str
    excerpt: str

    @property
    def id(self) -> str:
        return self.fact_id


#: How much surrounding text to show the reviewer around a matched term.
EXCERPT_RADIUS = 60


def _excerpt(text: str, start: int, end: int, radius: int = EXCERPT_RADIUS) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    body = text[left:right].strip()
    return f"{'...' if left > 0 else ''}{body}{'...' if right < len(text) else ''}"


def _find(facts: Sequence[Fact], terms: frozenset[str]) -> tuple[Match, ...]:
    """Every distinct (fact, term) pair where a term appears in a fact.

    Distinct on the pair, not on the term: the same word on two pages is two
    pieces of evidence and the reviewer should see both. Distinct on the term
    within one fact, because a page repeating "attorney" twelve times has not
    said anything twelve times.
    """

    pattern = _term_pattern(terms)
    if pattern is None:
        return ()

    found: list[Match] = []
    for fact in facts:
        text = _normalize(fact.content)
        if not text:
            continue
        seen: set[str] = set()
        for m in pattern.finditer(text):
            term = m.group(0).lower()
            if term in seen:
                continue
            seen.add(term)
            found.append(
                Match(fact_id=fact.id, term=term, excerpt=_excerpt(text, m.start(), m.end()))
            )
    return tuple(found)


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """What we concluded, and every piece of evidence behind it.

    ``cited_ids`` is the contract with drafting and verification: the fact ids
    that justify calling this business the campaign's vertical. The template
    copy puts exactly these in ``signals_used``, and `verify` re-derives the
    vertical word from exactly these — so a draft can never assert a category
    that this module did not establish from the prospect's own pages.
    """

    verdict: Verdict
    vertical: tuple[Match, ...] = ()
    service: tuple[Match, ...] = ()
    intake: tuple[Match, ...] = ()
    disqualifiers: tuple[Match, ...] = ()
    rationale: str = ""

    @property
    def qualified(self) -> bool:
        return self.verdict is Verdict.QUALIFIED

    @property
    def lead_status(self) -> LeadStatus:
        """Anything but QUALIFIED is `no_angle`, and `no_angle` writes nothing.

        Not `held`: held means "we wrote something and could not stand behind
        it". Nothing was written here, and the reviewer should see the
        difference between a draft we are holding and a lead we declined.
        """

        return LeadStatus.READY if self.qualified else LeadStatus.NO_ANGLE

    @property
    def cited_ids(self) -> list[str]:
        """Fact ids the copy may rest on: the vertical evidence, plus the
        service-line and intake evidence that supports it. Disqualifier
        matches are never cited — a qualified lead has none by construction."""

        ordered: list[str] = []
        for match in (*self.vertical, *self.service, *self.intake):
            if match.fact_id not in ordered:
                ordered.append(match.fact_id)
        return ordered

    @property
    def terms(self) -> list[str]:
        """The distinct vertical terms that matched, for the reviewer."""

        return sorted({m.term for m in self.vertical})


def _lead_fallback(lead: Lead | None, config: QualificationConfig) -> QualificationResult | None:
    """Qualify from the lead row alone, when the site could not be read."""

    from mazz.pipeline.verify import LEAD_RECORD_CITATION

    if lead is None or not config.fallback_industry_terms or not config.fallback_name_terms:
        return None
    industry = _normalize(f"{lead.industry_primary or ''} | {lead.industry_specific or ''}")
    name = _normalize(lead.company_name or "")
    ind = _term_pattern(config.fallback_industry_terms).search(industry)
    nm = _term_pattern(config.fallback_name_terms).search(name)
    if not (ind and nm):
        return None
    evidence = (
        Match(LEAD_RECORD_CITATION, ind.group(0).lower(), industry),
        Match(LEAD_RECORD_CITATION, nm.group(0).lower(), name),
    )
    if config.require_intake_signal:
        return None  # nothing on a lead row can show intake
    return QualificationResult(
        verdict=Verdict.QUALIFIED,
        vertical=evidence,
        rationale=(
            "site blocked or unreachable; qualified from the lead record: industry "
            f"{ind.group(0)!r} and company name {nm.group(0)!r}"
        ),
    )


def qualify_facts(
    facts: Iterable[Fact],
    *,
    config: QualificationConfig,
    lead: Lead | None = None,
) -> QualificationResult:
    """Qualify one company's cached research against one campaign's vocabulary.

    Pure: no session, no clock, no network, no writes. Same facts plus same
    config always produce the same verdict, which is what makes a change to a
    client's vocabulary something you can evaluate over a past list instead of
    guessing at.

    Only `QUALIFICATION_KINDS` are read. That is the narrow part of the design
    and it is deliberate: a blog post that mentions a law firm, or a job
    posting for a legal assistant, is not the company saying what it is. The
    evidence for "you are a law firm" has to come off a page where they say so.
    """

    considered = [f for f in facts if f.kind in QUALIFICATION_KINDS]

    disqualifiers = _find(considered, config.disqualifier_terms)
    if disqualifiers:
        # Checked FIRST and short-circuits. A site saying "not accepting new
        # clients" will also say "clients" and "attorney" a dozen times, so a
        # disqualifier that only tie-broke against the vertical would lose
        # every time it mattered.
        terms = ", ".join(sorted({m.term for m in disqualifiers}))
        return QualificationResult(
            verdict=Verdict.DISQUALIFIED,
            disqualifiers=disqualifiers,
            rationale=f"disqualified by the prospect's own site: {terms}",
        )

    if not considered:
        fallback = _lead_fallback(lead, config)
        if fallback is not None:
            return fallback
        return QualificationResult(
            verdict=Verdict.UNCONFIRMED,
            rationale=(
                "no homepage, services or contact page in the cache: there is "
                "nothing the vertical could be confirmed from"
            ),
        )

    vertical = _find(considered, config.vertical_terms)
    # "practice area" and "practice areas" are one term, not two (QUAL-01).
    distinct_terms = {m.term.removesuffix("s") for m in vertical}
    if config.vertical_required and len(distinct_terms) < config.min_vertical_matches:
        if not config.vertical_terms:
            rationale = (
                "campaign has no vertical_terms configured, so no prospect can "
                "be confirmed as its vertical; nothing is written until it is set"
            )
        else:
            rationale = (
                f"vertical not confirmed: found {len(distinct_terms)} of the "
                f"{config.min_vertical_matches} distinct terms required on the "
                "prospect's own pages"
            )
        return QualificationResult(
            verdict=Verdict.UNCONFIRMED, vertical=vertical, rationale=rationale
        )

    service = _find(considered, config.service_terms)
    intake = _find(considered, config.intake_terms)

    if config.require_intake_signal and not intake:
        return QualificationResult(
            verdict=Verdict.UNCONFIRMED,
            vertical=vertical,
            service=service,
            rationale=(
                "vertical confirmed, but the campaign requires evidence of client "
                "intake and none was found: the opening question presupposes "
                "something we cannot show is true"
            ),
        )

    if config.vertical_required:
        parts = ["vertical confirmed by " + ", ".join(sorted(distinct_terms))]
    else:
        # Say what actually happened. "Qualified" here means the lead is worth
        # writing to, not that we established anything about what it is.
        parts = [
            "no vertical claimed (campaign uses the generic phrase), so none "
            "was confirmed"
        ]
    if service:
        parts.append("service line: " + ", ".join(sorted({m.term for m in service})))
    if intake:
        parts.append("intake: " + ", ".join(sorted({m.term for m in intake})))
    return QualificationResult(
        verdict=Verdict.QUALIFIED,
        vertical=vertical,
        service=service,
        intake=intake,
        rationale="; ".join(parts),
    )


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

#: Fields derived from the CAMPAIGN row rather than from stored JSON. Refused
#: in a stored config: a campaign whose copy says "law firms" while its config
#: says the vertical is not required would drift silently, and the drift would
#: be invisible in exactly the place it matters.
_DERIVED_FIELDS = frozenset({"vertical_required"})

_FROZENSET_FIELDS = (
    "vertical_terms",
    "service_terms",
    "intake_terms",
    "disqualifier_terms",
    "fallback_industry_terms",
    "fallback_name_terms",
)


def qualification_config_from_json(stored: Mapping[str, Any]) -> QualificationConfig:
    """Rebuild a stored `campaigns.qualification_config` into a config object.

    Same refusals as the scoring config loader it is modelled on, for the same
    reason: a client's broken config silently reverting to the shipped defaults
    would move that client's `no_angle` rate for reasons nobody can see. Here it
    is sharper — the shipped default qualifies NOBODY, so a config that fails to
    load and falls back quietly would stop a campaign dead while looking fine.

    Unknown keys are refused rather than ignored. A term list is refused unless
    it is a JSON array of non-empty strings: a bare string would otherwise be
    iterated character by character, and a campaign would be matching on "l",
    "a", "w".
    """

    if not isinstance(stored, Mapping):
        raise TypeError(
            "campaigns.qualification_config must be a JSON object, got "
            f"{type(stored).__name__}: a malformed config must fail loudly, not "
            "fall back to a vocabulary nobody chose"
        )
    if not stored:
        return DEFAULT_QUALIFICATION

    valid = {f.name for f in fields(QualificationConfig)} - _DERIVED_FIELDS
    unknown = sorted(set(stored) - valid)
    if unknown:
        raise ValueError(
            f"campaigns.qualification_config names unknown knobs {unknown}; valid "
            f"keys are {sorted(valid)}. Refusing to apply a config that is partly "
            "ignored — a typo'd knob would read as set and do nothing."
        )

    flag = stored.get("require_intake_signal", None)
    if flag is not None and not isinstance(flag, bool):
        raise ValueError(
            "campaigns.qualification_config['require_intake_signal'] must be "
            f"true, false or absent, got {flag!r}"
        )

    minimum = stored.get("min_vertical_matches", None)
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int)):
        raise ValueError(
            "campaigns.qualification_config['min_vertical_matches'] must be a "
            f"whole number, got {minimum!r}"
        )

    kwargs: dict[str, Any] = {
        name: getattr(DEFAULT_QUALIFICATION, name)
        for name in {f.name for f in fields(QualificationConfig)}
    }
    kwargs.update(stored)
    for key in _FROZENSET_FIELDS:
        kwargs[key] = _coerce_terms(key, kwargs[key])
    return QualificationConfig(**kwargs)


def _coerce_terms(key: str, value: Any) -> frozenset[str]:
    """A JSON array of non-empty strings, normalised. Anything else raises."""

    if isinstance(value, frozenset):
        return value
    if isinstance(value, str) or not isinstance(value, (list, tuple, set)):
        raise ValueError(
            f"campaigns.qualification_config[{key!r}] must be an array of terms, "
            f"got {type(value).__name__}. A bare string would be matched one "
            "character at a time."
        )
    terms: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"campaigns.qualification_config[{key!r}] contains a non-string "
                f"term {item!r}"
            )
        normalized = _normalize(item)
        if normalized:
            terms.add(normalized)
    return frozenset(terms)


def config_for_lead(session: "Session", lead: Lead) -> QualificationConfig:
    """The qualification config in force for this lead.

    A lead with no campaign gets `DEFAULT_QUALIFICATION`, which qualifies
    nobody. That is not a gap to fill in later — an unrouted lead has no
    vertical noun to put in "We help ___", so there is no email to write and
    `no_angle` is the honest answer.
    """

    campaign_id = getattr(lead, "campaign_id", None)
    if campaign_id is None:
        return DEFAULT_QUALIFICATION

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        # ON DELETE SET NULL makes this unreachable; if it races, a stale id is
        # the problem, not the config, and the default writes nothing.
        log.warning(
            "lead %s references campaign %s, which does not exist; qualifying "
            "with the shipped defaults (which qualify nobody)",
            lead.id,
            campaign_id,
        )
        return DEFAULT_QUALIFICATION

    return config_for_campaign(campaign)


def config_for_campaign(campaign: Campaign) -> QualificationConfig:
    """The stored vocabulary, plus what the campaign's own copy implies.

    Split out so `verify` can rebuild the identical config from a campaign row
    without a lead in hand. If drafting and verification derived this
    separately they could disagree about whether a vertical was ever claimed,
    which is the one thing that must not happen between those two steps.
    """

    from mazz.pipeline.outreach import vertical_is_generic

    config = qualification_config_from_json(campaign.qualification_config or {})
    if vertical_is_generic(campaign):
        return replace(config, vertical_required=False)
    return config


def qualify_lead(
    session: "Session",
    lead: Lead,
    *,
    config: QualificationConfig | None = None,
) -> QualificationResult:
    """Qualify one lead against its campaign's vocabulary. Writes nothing.

    ``config=None`` — the normal path — resolves the lead's campaign. Pass one
    explicitly only to ask what a lead would look like under a vocabulary it
    does not have, which is what makes a vocabulary change reviewable before it
    is saved.
    """

    resolved = config if config is not None else config_for_lead(session, lead)
    return qualify_facts(
        facts_for_lead(session, lead), config=resolved, lead=fallback_lead(session, lead)
    )


def fallback_lead(session: "Session", lead: Lead) -> Lead | None:
    """The lead, if its record may stand in for its site; otherwise None.

    Only a site that REFUSED us (robots / 401 / 403) qualifies. Enrich records
    that refusal as ``ProviderBlocked`` on the company row, and a later
    successful fetch clears it. A site that answered with nothing usable (a
    404 homepage, a JS-only shell) is not blocked, so it earns no fallback
    (QUAL-03).
    """

    from mazz.models import ResearchCache

    cache = session.get(ResearchCache, lead.company_domain) if lead.company_domain else None
    blocked = cache is not None and (cache.last_error or "").startswith("ProviderBlocked")
    return lead if blocked else None


def handle_qualify(ctx: "JobContext") -> None:
    """Queue handler for ``jobs.kind == 'qualify'``.

    Qualified: the lead chains to ``draft``. Anything else: `no_angle`, and the
    chain STOPS here. No draft job is queued, because the correct output for
    this lead is nothing at all, plainly labelled — and a lead we could not
    confirm is a member of the vertical is precisely the lead a generic email
    would go to.
    """

    result = qualify_lead(ctx.session, ctx.lead)

    if not result.qualified:
        ctx.lead.status = LeadStatus.NO_ANGLE
        ctx.lead.skip_reason = (
            "disqualified" if result.verdict is Verdict.DISQUALIFIED else "vertical_unconfirmed"
        )
        ctx.session.flush()
        log.info(
            "lead %s -> no_angle (%s): %s",
            ctx.lead.id,
            result.verdict.value,
            result.rationale,
        )
        return

    log.info("lead %s qualified: %s", ctx.lead.id, result.rationale)
    ctx.enqueue_next("draft")
