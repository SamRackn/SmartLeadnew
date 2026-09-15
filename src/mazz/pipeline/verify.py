"""Verification: prove every claim in a draft, or hold it.

Pipeline step 5. The brief says this in as many words, and it is worth repeating
at the top of the file because it changes how the code is allowed to behave:

    "Verification step is a compliance control, not just a quality one —
     hallucinated claims in outbound B2B email are a real risk."

A quality score is advisory. A compliance control is not. So:

* Failure means `verification_status=failed` and lead status `held`. Both, always.
* A held draft is NEVER auto-approvable. `is_approvable()` returns False for
  anything that is not PASSED, and PASSED only means "a human may now look at
  it" — the brief's no-auto-send rule still stands above this.
* It FAILS CLOSED. Unresolvable citation, missing cache row, absent footer,
  malformed input: all failures. There is no "could not check, assuming fine".
* There is no warning level. A softened control is one a tired reviewer clicks
  past at 4pm on a Friday, which is the same as not having one.
* The reason is written to `drafts.verification_notes` — the column exists for
  exactly this — so the reviewer and the auditor see WHICH claim failed, not a
  red dot.

--------------------------------------------------------------------------
HOW A CLAIM IS CHECKED
--------------------------------------------------------------------------
Grounding corpus, built per draft, from three sources and no others:

  1. The cached facts whose ids the draft cites — re-resolved from
     `research_cache` by id. Not the ids the composer *says* it used as
     evidence-in-themselves: the ids are a claim, the cache row is the evidence.
  2. The lead's `notes` field.
  3. The lead's own identity record (name, company, title, location, industry),
     which the client supplied himself.

Then every token in subject + message body must be one of:

  * a token present in that corpus, or
  * a member of `SAFE_VOCABULARY` — function words and fixed scaffolding that
    assert nothing about the recipient.

Two rules sharpen it:

  * **A fact with no `source_url` is untraceable and fails closed.** That is the
    documented `schemas.Fact` contract and this module implements it as written.
    Notes-derived material is grounded through `lead.notes` directly, so nothing
    legitimate needs a URL-less fact to be citable.
  * **Digits have no allow-list.** Every number in a draft must appear in the
    evidence, full stop. `SAFE_VOCABULARY` cannot excuse one. Numbers are where
    hallucinated outbound gets expensive ("you've scaled to 40 support reps"),
    and a specific false number is the claim a recipient's lawyer notices.

--------------------------------------------------------------------------
WHAT THIS IS NOT
--------------------------------------------------------------------------
Honest limits, so nobody mistakes the control's reach:

* It checks *specifics* — numbers, names, titles, products, metrics. It does not
  and cannot judge whether a grounded sentence draws a fair inference from its
  evidence. A token-grounding check is a floor, not a proof of truthfulness.
* `SAFE_VOCABULARY` is generic English plus the fixed template scaffolding. A
  sentence built only from those words passes without evidence, because it
  asserts nothing checkable. Adding a content word to that set would punch a
  hole in the control, so the set is small, listed in full below, and tested.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from sqlalchemy import select

from mazz.models import Draft, Lead, LeadStatus, VerificationStatus
from mazz.pipeline.draft import FOOTER_SEPARATOR, render_footer, split_body
from mazz.pipeline.score import facts_for_lead
from mazz.schemas import Fact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

__all__ = [
    "SAFE_VOCABULARY",
    "Grounding",
    "UntraceableClaim",
    "VerificationResult",
    "build_grounding",
    "extract_claims",
    "verify_content",
    "verify_draft",
    "apply_verification",
    "is_approvable",
    "latest_draft",
    "handle_verify",
]


# --------------------------------------------------------------------------
# Tokenizing
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _normalize_word(token: str) -> str:
    return token.lower().strip("-'’")


def _normalize_number(token: str) -> str:
    text = token.replace(",", "")
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _word_forms(word: str) -> set[str]:
    """A word and its trivial singular/plural variants.

    Quoted evidence says "2 openings" and the copy may say "opening". Folding
    those together avoids failing a draft over an -s while costing the control
    nothing: no hallucinated specific is ever one -s away from real evidence.
    """

    forms = {word}
    if len(word) > 3:
        if word.endswith("ies"):
            forms.add(word[:-3] + "y")
        if word.endswith("es"):
            forms.add(word[:-2])
        if word.endswith("s"):
            forms.add(word[:-1])
        forms.add(word + "s")
    if word.endswith("'s") or word.endswith("’s"):
        forms.add(word[:-2])
    return forms


def _corpus_tokens(text: str) -> tuple[set[str], set[str]]:
    """(words, numbers) drawn from evidence.

    Evidence is read generously: hyphenated and dotted strings contribute both
    the whole token and its parts, so a source_url of
    ``.../careers/revenue-operations-manager`` grounds the words in it. The
    draft side is read literally. The asymmetry is deliberate and one-way —
    it can only ever recognise MORE evidence, never excuse more claims.
    """

    words: set[str] = set()
    numbers: set[str] = set()

    for raw in _WORD_RE.findall(text):
        word = _normalize_word(raw)
        if word:
            words |= _word_forms(word)
    for raw in _NUMBER_RE.findall(text):
        numbers.add(_normalize_number(raw))

    for piece in _SPLIT_RE.split(text):
        if not piece:
            continue
        if piece.isdigit():
            numbers.add(_normalize_number(piece))
        else:
            word = _normalize_word(piece)
            if word and _WORD_RE.fullmatch(piece):
                words |= _word_forms(word)
    return words, numbers


# --------------------------------------------------------------------------
# Safe vocabulary
# --------------------------------------------------------------------------
#
# Words that assert nothing about the recipient: function words, and the fixed
# scaffolding the Phase 1 template emits. Listed in full, in one place, so that
# widening the control's blind spot requires a visible diff.
#
# It contains NO content words — nothing about headcount, growth, funding,
# hiring, tooling, results or metrics. Those must always come from evidence.
# It contains no digits at all; see `extract_claims`.

_FUNCTION_WORDS = """
a about above after again against all also am an and any are aren't as at
be because been before being below between both but by
can can't cannot could couldn't
did didn't do does doesn't doing don't down during
each either
few for from further
had hadn't has hasn't have haven't having he her here hers herself him himself
his how however
i i'd i'll i'm i've if in into is isn't it it's its itself
just
let's
me more most much must mustn't my myself
no nor not
of off on once one only or other ought our ours ourselves out over own
same shan't she should shouldn't so some such
than that that's the their theirs them themselves then there there's these they
this those through to too
under until up
very
was wasn't we were weren't what what's when where which while who whom why with
will won't would wouldn't
you you'd you'll you're you've your yours yourself yourselves
"""

#: Generic verbs and courtesies with no specific content. Nothing here names a
#: thing, a quantity or an outcome, so nothing here can carry a false claim on
#: its own. Kept separate from the function words so the line between "grammar"
#: and "judgement call" stays visible.
_GENERIC_WORDS = """
ask asked asking
get getting give given go going gone
happy glad hope
keep know
look looking
mean means meant
make makes making
say said see seen send sending sent
take takes
tell thank thanks regards
think
want wanted
"""

#: Fixed scaffolding from `draft.compose_template`. Every word here is either a
#: greeting, a piece of sentence glue, a description of the EVIDENCE TYPE being
#: cited (which `Fact.kind` already carries), or part of the sender-side call to
#: action. Nothing here describes the recipient's business.
_TEMPLATE_WORDS = """
hi hello there
saw seeing note quick latest update
opening openings role roles
list site notes
live current priority right now
show teams team spot short call deck
worth reply either way leave
minute minutes
"""

SAFE_VOCABULARY: frozenset[str] = frozenset(
    w for w in (_FUNCTION_WORDS + _GENERIC_WORDS + _TEMPLATE_WORDS).split() if w
)


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grounding:
    """The evidence a draft is allowed to rest on, plus what went wrong building it."""

    words: frozenset[str]
    numbers: frozenset[str]
    #: fact id -> short description, for verification_notes and the review UI.
    resolved: Mapping[str, str] = field(default_factory=dict)
    #: Structural failures found while assembling evidence. Non-empty => FAIL,
    #: regardless of what the copy says.
    problems: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


def _lead_identity_text(lead: Lead) -> str:
    """Fields the client supplied himself. Legitimate grounding for the copy."""

    parts = [
        lead.full_name,
        lead.company_name,
        lead.job_title,
        lead.location,
        lead.industry_primary,
        lead.industry_specific,
        lead.website,
        lead.company_domain,
        lead.email,
    ]
    return " ".join(p for p in parts if p)


def build_grounding(
    lead: Lead,
    cited_ids: Sequence[str],
    available_facts: Iterable[Fact],
) -> Grounding:
    """Resolve citations into an evidence corpus, failing closed on every gap."""

    by_id: dict[str, Fact] = {f.id: f for f in available_facts}
    words: set[str] = set()
    numbers: set[str] = set()
    resolved: dict[str, str] = {}
    problems: list[str] = []
    sources: list[str] = []

    if not cited_ids:
        problems.append(
            "draft cites no facts at all: there is nothing for its claims to "
            "rest on"
        )

    seen: set[str] = set()
    for fact_id in cited_ids:
        if fact_id in seen:
            continue
        seen.add(fact_id)

        fact = by_id.get(fact_id)
        if fact is None:
            problems.append(
                f"cited fact id {fact_id!r} is not in research_cache for "
                f"{lead.company_domain!r} — the citation resolves to nothing"
            )
            continue
        if not fact.source_url:
            # schemas.Fact says this explicitly, and we implement it as written:
            # a fact with no source_url cannot be shown to a recipient's counsel
            # as the thing we relied on, so it is untraceable by definition.
            problems.append(
                f"cited fact {fact_id!r} has no source_url, so it is "
                "untraceable; a draft may not rest on it"
            )
            continue

        fw, fn = _corpus_tokens(f"{fact.content} {fact.source_url} {fact.kind}")
        words |= fw
        numbers |= fn
        resolved[fact_id] = f"{fact.kind} <{fact.source_url}>"
        sources.append(f"fact {fact_id} ({fact.kind}) <{fact.source_url}>")

    if lead.notes and lead.notes.strip():
        nw, nn = _corpus_tokens(lead.notes)
        words |= nw
        numbers |= nn
        sources.append(f"lead {lead.id} notes field")

    iw, inum = _corpus_tokens(_lead_identity_text(lead))
    words |= iw
    numbers |= inum
    sources.append(f"lead {lead.id} identity record")

    return Grounding(
        words=frozenset(words),
        numbers=frozenset(numbers),
        resolved=resolved,
        problems=tuple(problems),
        sources=tuple(sources),
    )


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UntraceableClaim:
    """One token in the copy that no evidence supports."""

    token: str
    kind: str  # "number" | "word"
    where: str  # "subject" | "body"
    context: str

    def __str__(self) -> str:
        return f"{self.kind} {self.token!r} in {self.where} (…{self.context}…)"


def _context_for(text: str, match: re.Match[str], width: int = 44) -> str:
    start = max(match.start() - width, 0)
    end = min(match.end() + width, len(text))
    return " ".join(text[start:end].split())


def extract_claims(text: str, where: str, grounding: Grounding) -> list[UntraceableClaim]:
    """Every token in ``text`` that evidence does not support.

    Numbers first, and without mercy: `SAFE_VOCABULARY` does not apply to them.
    """

    claims: list[UntraceableClaim] = []

    for match in _NUMBER_RE.finditer(text):
        token = _normalize_number(match.group(0))
        if token in grounding.numbers:
            continue
        claims.append(
            UntraceableClaim(
                token=match.group(0),
                kind="number",
                where=where,
                context=_context_for(text, match),
            )
        )

    for match in _WORD_RE.finditer(text):
        word = _normalize_word(match.group(0))
        if not word or word in SAFE_VOCABULARY:
            continue
        if grounding.words & _word_forms(word):
            continue
        claims.append(
            UntraceableClaim(
                token=match.group(0),
                kind="word",
                where=where,
                context=_context_for(text, match),
            )
        )

    return claims


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    notes: str
    untraceable: tuple[UntraceableClaim, ...] = ()
    problems: tuple[str, ...] = ()
    resolved_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def lead_status(self) -> LeadStatus:
        """Failure is `held`. Not `ready`, not `no_angle`, not a warning."""

        return LeadStatus.READY if self.passed else LeadStatus.HELD


def _fail(
    reason_lines: Sequence[str],
    *,
    untraceable: Sequence[UntraceableClaim] = (),
    problems: Sequence[str] = (),
    resolved: Sequence[str] = (),
) -> VerificationResult:
    body = "\n".join(f"- {line}" for line in reason_lines)
    return VerificationResult(
        status=VerificationStatus.FAILED,
        notes=f"FAILED — this draft is held and cannot be approved.\n{body}",
        untraceable=tuple(untraceable),
        problems=tuple(problems),
        resolved_ids=tuple(resolved),
    )


# --------------------------------------------------------------------------
# Verifying
# --------------------------------------------------------------------------


def verify_content(
    *,
    lead: Lead,
    subject: str,
    body: str,
    signals_used: Sequence[str],
    available_facts: Iterable[Fact],
) -> VerificationResult:
    """Verify rendered copy against the cache. Pure: no DB writes, no clock.

    ``body`` is the FULL rendered body including the compliance footer, because
    a draft missing its footer is also a compliance failure and this is the
    control that catches it.
    """

    facts = list(available_facts)
    grounding = build_grounding(lead, list(signals_used), facts)

    message, footer = split_body(body or "")
    problems = list(grounding.problems)

    # --- CAN-SPAM footer ------------------------------------------------
    if footer is None:
        problems.append(
            "body carries no compliance footer (physical mailing address + "
            "one-click unsubscribe); it must never be sent"
        )
    else:
        try:
            expected = render_footer(lead)
        except Exception as exc:  # MissingSenderAddress, or anything else
            problems.append(f"cannot confirm the compliance footer: {exc}")
        else:
            if footer.strip() != expected.strip():
                problems.append(
                    "compliance footer does not match the configured sender "
                    "address / unsubscribe link for this lead"
                )

    # --- claims ---------------------------------------------------------
    untraceable = extract_claims(subject or "", "subject", grounding)
    untraceable += extract_claims(message, "body", grounding)

    if problems or untraceable:
        lines: list[str] = []
        lines.extend(problems)
        for claim in untraceable:
            lines.append(
                f"untraceable {claim.kind} {claim.token!r} in the {claim.where}: "
                f"no cited fact and no lead note contains it — …{claim.context}…"
            )
        if grounding.resolved:
            lines.append(
                "resolved evidence: "
                + "; ".join(f"{k} -> {v}" for k, v in sorted(grounding.resolved.items()))
            )
        return _fail(
            lines,
            untraceable=untraceable,
            problems=problems,
            resolved=sorted(grounding.resolved),
        )

    detail = "; ".join(f"{k} -> {v}" for k, v in sorted(grounding.resolved.items()))
    notes = (
        "PASSED — every claim traces to cited evidence or the lead's notes.\n"
        f"- evidence: {detail or '(none)'}\n"
        f"- grounding sources: {', '.join(grounding.sources)}\n"
        "- compliance footer present and matches configuration."
    )
    return VerificationResult(
        status=VerificationStatus.PASSED,
        notes=notes,
        resolved_ids=tuple(sorted(grounding.resolved)),
    )


def verify_draft(
    session: "Session",
    lead: Lead,
    draft: Draft,
    *,
    available_facts: Iterable[Fact] | None = None,
) -> VerificationResult:
    """Verify a persisted draft against this lead's cached research."""

    facts = list(available_facts) if available_facts is not None else facts_for_lead(session, lead)
    return verify_content(
        lead=lead,
        subject=draft.subject or "",
        body=draft.body or "",
        signals_used=list(draft.signals_used or []),
        available_facts=facts,
    )


def apply_verification(
    session: "Session",
    lead: Lead,
    draft: Draft,
    result: VerificationResult,
) -> VerificationResult:
    """Record the verdict. Failure holds the lead; there is no middle setting."""

    draft.verification_status = result.status
    draft.verification_notes = result.notes
    lead.status = result.lead_status
    session.flush()

    if not result.passed:
        log.warning(
            "draft %s for lead %s FAILED verification and is held: %s",
            draft.id,
            lead.id,
            result.notes.replace("\n", " | "),
        )
    return result


def is_approvable(draft: Draft) -> bool:
    """May a human be offered an Approve button for this draft?

    False for anything not PASSED — a failed or pending draft is never
    approvable, by a person or by anything else. True does NOT mean send: the
    brief's no-auto-send rule sits above this, and a human still has to click.
    """

    return draft.verification_status is VerificationStatus.PASSED


def latest_draft(session: "Session", lead: Lead) -> Draft | None:
    """The governing draft for a lead — the newest row."""

    return session.execute(
        select(Draft)
        .where(Draft.lead_id == lead.id)
        .order_by(Draft.created_at.desc(), Draft.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def handle_verify(ctx: "JobContext") -> None:
    """Queue handler for ``jobs.kind == 'verify'``.

    A lead with no draft row is not an error: `score` legitimately declines to
    draft. It is only an error if we were asked to verify something missing
    while the lead is not already `no_angle` — and even then we hold rather than
    guess, because a missing draft with a `verify` job queued means an earlier
    step did something we do not understand.
    """

    draft = latest_draft(ctx.session, ctx.lead)
    if draft is None:
        if ctx.lead.status is LeadStatus.NO_ANGLE:
            log.info("lead %s has no draft to verify (no_angle)", ctx.lead.id)
            return
        ctx.lead.status = LeadStatus.HELD
        ctx.session.flush()
        log.warning("lead %s: verify ran with no draft row; holding", ctx.lead.id)
        return

    result = verify_draft(ctx.session, ctx.lead, draft)
    apply_verification(ctx.session, ctx.lead, draft, result)
