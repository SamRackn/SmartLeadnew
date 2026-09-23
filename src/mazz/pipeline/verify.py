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
  malformed input, a judge that would not answer: all failures. There is no
  "could not check, assuming fine".
* There is no warning level. A softened control is one a tired reviewer clicks
  past at 4pm on a Friday, which is the same as not having one.
* The reason is written to `drafts.verification_notes` — the column exists for
  exactly this — so the reviewer and the auditor see WHICH claim failed, not a
  red dot.

--------------------------------------------------------------------------
WHAT CHANGED IN W1, AND WHY PRECISION IS A SAFETY PROPERTY
--------------------------------------------------------------------------
The Phase 1 verifier was token-level: every significant word in the copy had to
appear in the cited evidence, with a small hand-written vocabulary as the only
escape. That was sound while `compose_template` quoted facts verbatim — nothing
it wrote could fail.

`compose_claude` paraphrases. Run a real LLM draft through the token-level check
and it correctly catches the fabricated "40", and it ALSO flags `hiring`,
`scaled`, `reps`, `since` and `Series` — ordinary English restating a cited fact
in different words. If that shipped, nearly every draft would land `held`, the
review queue would be almost entirely false positives, and the rational human
response is to stop reading the warnings and approve on sight. That turns the
strongest compliance control in the system into a rubber stamp, which is worse
than not having it, because it still looks like it is working.

So this module checks CLAIMS, not tokens:

  quote     text in quotation marks must appear verbatim in the evidence
  number    every digit, canonicalised for magnitude ($12M == 12 million)
  date      ISO dates, month names, quarters, and relative phrases
  role      a job title asserted in a hiring construction, as a whole phrase
  entity    proper nouns and acronyms — names of companies, people, products
  term      any remaining word that is neither ordinary English nor evidence

**Numbers, names and dates stay exact-match.** Those are the claims that create
legal exposure, and nothing — not the lexicon, not an abbreviation, not a judge
— excuses one. What relaxes is the prose *around* them: paraphrase does not have
to be word-for-word. The general-English floor lives in `lexicon.py` with its
admission policy written down, so widening it is a visible, reviewable diff.

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

A fourth, narrower source is the `kind` of each cited fact (see
`lexicon.KIND_VOCABULARY`). The kind is set by the retrieval adapter and stored
in the cache alongside the content, so it is retrieved evidence too: "you're
hiring" over a cited `job_posting` restates the evidence even when the posting
never uses the word. The licensed sets are tiny and say only what the kind
means — `job_posting` licenses "hiring", never "growing".

Two rules sharpen it:

  * **A fact with no `source_url` is untraceable and fails closed.** That is the
    documented `schemas.Fact` contract and this module implements it as written.
    Notes-derived material is grounded through `lead.notes` directly, so nothing
    legitimate needs a URL-less fact to be citable.
  * **Digits have no allow-list.** Every number in a draft must appear in the
    evidence, full stop. Numbers are where hallucinated outbound gets expensive
    ("you've scaled to 40 support reps"), and a specific false number is the
    claim a recipient's lawyer notices.

--------------------------------------------------------------------------
THE OPTIONAL ENTAILMENT JUDGE
--------------------------------------------------------------------------
`VERIFY_JUDGE=claude` adds a second Claude call that reads the copy against the
cited evidence and looks for unsupported assertions. It is strictly ADDITIVE:

* It runs only after the deterministic checks have PASSED, so it can only ever
  turn a pass into a failure. It can never rescue a draft the exact-match rules
  rejected — a judge can be wrong, and those rules cannot.
* A judge that errors, refuses, or returns something unparseable FAILS the
  draft. "Could not check" is not a pass.
* It is off by default and never runs in the test suite.

--------------------------------------------------------------------------
WHAT THIS IS NOT
--------------------------------------------------------------------------
Honest limits, so nobody mistakes the control's reach:

* It proves a claim traces to retrieved evidence. It does NOT prove the evidence
  is true. A scraper that misreads a filter control as a job req puts a false
  fact in the cache, and a draft citing it passes verification correctly. That
  gap closes at cache-ingest, not here; it is recorded in CLAUDE.md.
* With a general-English floor, copy CAN assemble an unsupported inference out
  of ordinary words ("your team must be stretched"). It names nothing,
  quantifies nothing and dates nothing, so the deterministic layer has no
  handle on it. That is the grey area the judge exists for, and it is the
  honest cost of not holding every legitimate draft.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import select

from mazz.integrations.base import setting
from mazz.models import Draft, Lead, LeadStatus, VerificationStatus
from mazz.pipeline.draft import FOOTER_SEPARATOR, render_footer, split_body
from mazz.pipeline.lexicon import (
    ABBREVIATIONS,
    COGNITION_VERBS,
    COMMON_ENGLISH,
    FIRST_PERSON_MARKERS,
    KIND_VOCABULARY,
    MAGNITUDES,
    MONTHS,
    NUMBER_WORDS,
    OTHER_PARTY_MARKERS,
    PAST_RELATIVE_PHRASES,
    is_common,
    stem,
)
from mazz.pipeline.score import facts_for_lead
from mazz.schemas import Fact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.models import ProofPoint
    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

__all__ = [
    "SAFE_VOCABULARY",
    "Grounding",
    "sender_voice_spans",
    "Judge",
    "JudgeUnavailable",
    "JudgeVerdict",
    "UntraceableClaim",
    "VerificationResult",
    "build_grounding",
    "claude_judge",
    "default_judge",
    "extract_claims",
    "verify_content",
    "verify_draft",
    "cited_proof_points",
    "campaign_copy_for",
    "CampaignCopy",
    "apply_verification",
    "is_approvable",
    "latest_draft",
    "handle_verify",
]


#: Kept as a name for compatibility with anything that imported it. The
#: hand-listed Phase 1 vocabulary is now the general-English floor in
#: `lexicon.COMMON_ENGLISH`, which is where the admission policy is written
#: down. It still contains no digits and no business vocabulary.
SAFE_VOCABULARY: frozenset[str] = COMMON_ENGLISH


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*(?:-[A-Za-z][A-Za-z'’]*)*")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_WS = re.compile(r"\s+")

_MAGNITUDE_ALTS = "|".join(sorted(MAGNITUDES, key=len, reverse=True))
#: The currency symbol is captured so the reviewer is shown "$15M" rather than
#: "15M", and is ignored when canonicalising: the claim is the quantity.
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£])?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<suffix>%|percent\b|" + _MAGNITUDE_ALTS + r"\b)?",
    re.IGNORECASE,
)
_MONTH_ALTS = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(?P<month>" + _MONTH_ALTS + r")\.?\s+"
    r"(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*)?"
    r"(?P<year>\d{4})?\b",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>" + _MONTH_ALTS + r")\b",
    re.IGNORECASE,
)
_QUARTER_RE = re.compile(r"\bQ(?P<q>[1-4])(?:\s*,?\s*(?P<year>\d{4}))?\b")
_QUOTE_RE = re.compile(r"[\"“”]([^\"“”]{3,}?)[\"“”]")

#: Words that end a noun phrase. Used to trim a captured job title.
_PHRASE_BOUNDARY: frozenset[str] = frozenset(
    """
    to who that which and or so for in at on with from after before while as by
    because since but if when where their your our its his her my
    """.split()
)

_ROLE_TRIGGERS = re.compile(
    r"\b(?:hiring|hire|hires|hired|recruiting|recruit|backfilling|"
    r"looking\s+for|bringing\s+on|opened|posted|advertising)\s+"
    r"(?:a|an|another|the|your|\d+|two|three|four|five|six|seven|eight|nine|ten)?\s*",
    re.IGNORECASE,
)
_ROLE_POSSESSIVE = re.compile(
    r"\b(?:your|the|this|that)\s+(?P<title>(?:[A-Za-z][\w'’\-]*\s+){0,5}?)"
    r"(?:opening|openings|role|roles|req|reqs|requisition|position|positions|"
    r"posting|postings|search|vacancy|hire|listing)\b",
    re.IGNORECASE,
)

#: Marker between evidence segments so a phrase cannot be matched across two
#: unrelated facts.
_SEGMENT_BREAK = "\x00"


def _normalize_text(text: str) -> str:
    """Lowercased, whitespace-collapsed, curly punctuation folded."""

    folded = (
        (text or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("—", " ")
        .replace("–", " ")
    )
    return _WS.sub(" ", folded).strip().lower()


def _canonical_number(raw: str, suffix: str | None) -> str:
    """One numeric claim, canonicalised so spelling is not a claim.

    "$12M", "12M" and "12 million" all become ``12000000``; "40" and "40M" do
    not become each other. Percentages keep their sign so "40%" never matches a
    bare "40".
    """

    text = raw.replace(",", "")
    try:
        value = float(text)
    except ValueError:  # pragma: no cover - regex guarantees a number
        return text
    key = (suffix or "").strip().lower()
    if key in ("%", "percent"):
        return f"{_plain(value)}%"
    if key in MAGNITUDES:
        value *= MAGNITUDES[key]
    return _plain(value)


def _plain(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _iter_numbers(text: str) -> Iterable[tuple[int, int, str]]:
    for match in _NUMBER_RE.finditer(text):
        yield (
            match.start(),
            match.end(),
            _canonical_number(match.group("num"), match.group("suffix")),
        )


def _iter_number_words(text: str) -> Iterable[tuple[int, int, str]]:
    """Spelled cardinals, with an optional magnitude word ("two million")."""

    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        if word not in NUMBER_WORDS:
            continue
        value = float(NUMBER_WORDS[word])
        end = match.end()
        tail = _WORD_RE.match(text, _skip_space(text, end))
        if tail and tail.group(0).lower() in MAGNITUDES:
            value *= MAGNITUDES[tail.group(0).lower()]
            end = tail.end()
        yield match.start(), end, _plain(value)


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _iter_dates(text: str) -> Iterable[tuple[int, int, tuple[int | None, int, int | None], str]]:
    """(start, end, (year, month, day), display) for every explicit date."""

    for match in _ISO_DATE_RE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        yield match.start(), match.end(), (year, month, day), match.group(0)
    for match in _MONTH_DAY_RE.finditer(text):
        month = MONTHS[match.group("month").lower()]
        day = int(match.group("day")) if match.group("day") else None
        year = int(match.group("year")) if match.group("year") else None
        yield match.start(), match.end(), (year, month, day), match.group(0)
    for match in _DAY_MONTH_RE.finditer(text):
        month = MONTHS[match.group("month").lower()]
        yield (
            match.start(),
            match.end(),
            (None, month, int(match.group("day"))),
            match.group(0),
        )


def _content_stream(text: str) -> list[str]:
    """Stems of the words that carry content, in order, for phrase matching.

    Ordinary English is dropped from BOTH sides so that "a Revenue Operations
    Manager" and "Revenue Operations Manager -" compare equal, while "Head of
    Revenue Operations" — a title nobody posted — does not.
    """

    out: list[str] = []
    for piece in re.split(r"([^A-Za-z0-9'’\-]+)", text):
        if piece == _SEGMENT_BREAK:
            out.append(_SEGMENT_BREAK)
            continue
        for match in _WORD_RE.finditer(piece):
            word = match.group(0)
            if is_common(word):
                continue
            key = stem(word)
            if key:
                out.append(key)
    return out


# --------------------------------------------------------------------------
# Speaker: whose sentence is this?
# --------------------------------------------------------------------------
#
# A claim that requires evidence is an assertion about the RECIPIENT'S company.
# The sender's own offers, questions, availability and framing are not claims
# about the recipient, and the entailment judge in this module has said so in
# its prompt since it was written: "Opinions, offers and questions from the
# sender about the sender's own business are not claims about the recipient.
# Ignore them." The deterministic layer did not know that, and the first live
# `compose_claude` run held 4 of 4 drafts, most of them on the sender's own
# prose: `entity "I'm"`, `number '15'` from "a 15-minute call", `word 'curious'`
# from "I'd be curious".
#
# So spans are classified, and the classification is deliberately hard to earn:
#
#   * A span is the sender's voice only if it POSITIVELY carries a first-person
#     marker. A span with no pronoun at all — "Headcount doubled." — is checked
#     in full, which is what keeps the labelled fabrication cases failing.
#   * Any second- or third-person marker disqualifies it. "I saw you scaled to
#     40 reps" is about the recipient, and every claim in it is checked.
#   * Any cognition or reporting verb disqualifies it, because what follows one
#     is a proposition about somebody else. "We think headcount doubled" has no
#     "you" in it and is still not sender voice.
#   * Naming the recipient's company or the recipient disqualifies it.
#
# Sentence first, then clauses within a sentence that is not wholly the
# sender's. Splitting can only ever SHRINK an exempt span (a clause carries
# fewer pronouns than its sentence), so the finer pass is the conservative one.
#
# What the exemption covers is also narrow: numbers, dates and ordinary prose.
# Quotes, proper nouns and job titles stay checked everywhere, in every span,
# because those are the claim types that create the legal exposure — a
# fabricated name or title does not become safe by being said in first person.

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

#: Clause boundaries. Coordinators and complementizers, because the proposition
#: after one ("I'd be curious HOW you're handling it") is a different speaker's
#: business from the clause before it.
_CLAUSE_SPLIT = re.compile(
    r"[;:]\s+"
    r"|\s+[—–]\s+"
    r"|,\s+(?=(?:and|but|so|or|yet|since|because|while|although|though|whereas|"
    r"which|where|when|who|if|unless)\b)"
    r"|\s+(?=(?:and|but|so|or|yet|since|because|while|although|though|whereas|"
    r"if|unless|how|what|whether|which|where|when|why|who|whom)\b)",
    re.IGNORECASE,
)
# "that" is deliberately NOT a boundary. It is a determiner ("on exactly that
# handoff") far more often than a complementizer, and splitting there cut the
# sender's own clause in half. The complementizer case is the one that matters
# for safety — "I think that headcount doubled" — and it is already closed from
# the other side: "think" is a cognition verb, so that span is never sender
# voice with or without the split.


def _segments(text: str, pattern: re.Pattern[str], start: int, end: int) -> list[tuple[int, int]]:
    """(start, end) of each piece of ``text[start:end]`` between separators."""

    out: list[tuple[int, int]] = []
    cursor = start
    for match in pattern.finditer(text, start, end):
        if match.start() > cursor:
            out.append((cursor, match.start()))
        cursor = max(match.end(), match.start())
    if cursor < end:
        out.append((cursor, end))
    return out


def _span_words(text: str, start: int, end: int) -> set[str]:
    return {
        match.group(0).lower().replace("’", "'")
        for match in _WORD_RE.finditer(text, start, end)
    }


def _is_sender_voice(words: set[str], prospect_terms: frozenset[str]) -> bool:
    """Does this span talk about the sender, and nobody else?"""

    if not words & FIRST_PERSON_MARKERS:
        return False
    if words & OTHER_PARTY_MARKERS:
        return False
    if words & COGNITION_VERBS:
        return False
    if prospect_terms and any(stem(w) in prospect_terms for w in words):
        return False
    return True


def sender_voice_spans(
    text: str, prospect_terms: frozenset[str] = frozenset()
) -> tuple[tuple[int, int], ...]:
    """Spans of ``text`` that assert nothing about the recipient.

    Public because it is the part of this control that is easiest to get wrong
    and so the part that most needs to be testable on its own.
    """

    text = text or ""
    spans: list[tuple[int, int]] = []
    for s_start, s_end in _segments(text, _SENTENCE_SPLIT, 0, len(text)):
        if _is_sender_voice(_span_words(text, s_start, s_end), prospect_terms):
            spans.append((s_start, s_end))
            continue
        for c_start, c_end in _segments(text, _CLAUSE_SPLIT, s_start, s_end):
            if _is_sender_voice(_span_words(text, c_start, c_end), prospect_terms):
                spans.append((c_start, c_end))
    return tuple(spans)


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
    #: Normalized evidence text, for quote and phrase containment.
    text: str = ""
    #: Content-word stems in source order, segment-separated.
    stream: tuple[str, ...] = ()
    #: (year, month, day) triples drawn from the evidence; year/day may be None.
    dates: frozenset[tuple[int | None, int, int | None]] = frozenset()
    #: Vocabulary licensed by the KINDS of the cited facts, stemmed.
    kind_words: frozenset[str] = frozenset()
    #: Stems that NAME the recipient or the recipient's company. A span that
    #: uses one is about them, however much first person surrounds it.
    prospect_terms: frozenset[str] = frozenset()
    #: The raw snippets shown to the entailment judge, when one is enabled.
    evidence: tuple[str, ...] = ()


def _prospect_terms(lead: Lead) -> frozenset[str]:
    """Stems that name the recipient or their company.

    Used only to DISQUALIFY a span from the sender-voice exemption, never to
    support a claim, so a generous reading of what counts as naming them is the
    safe direction here: the more of the copy that is treated as being about
    them, the more of it is checked.
    """

    terms: set[str] = set()
    for value in (lead.full_name, lead.company_name, lead.company_domain, lead.website):
        for piece in _SPLIT_RE.split(value or ""):
            if len(piece) < 3 or is_common(piece):
                continue
            key = stem(piece)
            if key:
                terms.add(key)
    return frozenset(terms)


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


def _corpus_tokens(text: str) -> tuple[set[str], set[str]]:
    """(word stems, canonical numbers) drawn from evidence.

    Evidence is read generously: hyphenated and dotted strings contribute both
    the whole token and its parts, so a source_url of
    ``.../careers/revenue-operations-manager`` grounds the words in it. The
    draft side is read literally. The asymmetry is deliberate and one-way — it
    can only ever recognise MORE evidence, never excuse more claims.
    """

    words: set[str] = set()
    numbers: set[str] = set()

    for match in _WORD_RE.finditer(text):
        key = stem(match.group(0))
        if key:
            words.add(key)
    for _, _, canonical in _iter_numbers(text):
        numbers.add(canonical)
    for _, _, canonical in _iter_number_words(text):
        numbers.add(canonical)

    for piece in _SPLIT_RE.split(text):
        if not piece:
            continue
        if piece.isdigit():
            numbers.add(_plain(float(piece)))
        else:
            key = stem(piece)
            if key:
                words.add(key)
    return words, numbers


@dataclass(frozen=True, slots=True)
class CampaignCopy:
    """The two sentences email 1 takes from the campaign, and their warrant.

    Email 1 is not free prose, so its claim surface is known exactly rather
    than inferred from the words. Two pieces of it come from the campaign row
    instead of from research, and they are verified on opposite principles:

    ``offer_line`` -- "fill empty lanes through search marketing" -- is a
    statement about the SENDER's own business, written by the client. It makes
    no assertion about the recipient, so it is admitted as declared sender copy
    with its provenance recorded. There is nothing for us to check: the client
    is the authority on what he sells.

    ``vertical`` -- "logistics companies" -- IS an assertion about the
    recipient, and the only one in the message. It is admitted ONLY when
    qualification, re-derived here from the cache, still confirms it from the
    prospect's own pages. That is why this carries the verdict rather than just
    the word.

    Before this record existed, the vertical noun was passing verification by
    accident: the sentence containing it read as sender voice ("We help ...")
    and was skipped wholesale, so the one claim in the email about the
    recipient's business was the one claim nothing checked. It also broke the
    other way — a vertical noun overlapping the prospect's own name ("Northwind
    Logistics", "logistics companies") stopped looking like sender voice and
    held the draft over ordinary words. Both of those are the same bug: a
    structural property of the copy being guessed at from its surface.
    """

    vertical: str
    offer_line: str
    qualified: bool
    rationale: str = ""
    #: True when `vertical` is the generic phrase ("businesses like yours")
    #: rather than a claim. Nothing about the checking changes -- the phrase
    #: still has to be accounted for, and the lead still has to have qualified
    #: on intake and disqualifiers -- but the reviewer is told plainly that no
    #: vertical was confirmed, instead of being shown a confirmation that never
    #: happened.
    is_generic: bool = False


def build_grounding(
    lead: Lead,
    cited_ids: Sequence[str],
    available_facts: Iterable[Fact],
    proof_points: Iterable["ProofPoint"] = (),
    campaign_copy: "CampaignCopy | None" = None,
) -> Grounding:
    """Resolve citations into an evidence corpus, failing closed on every gap.

    Two citation namespaces, because a draft rests on two kinds of evidence.

    A bare id is a fact in `research_cache`: something retrieved from the
    PROSPECT's site, resolved by id and re-read here rather than trusted from
    the draft row.

    A ``proof_point:<id>`` citation is the SENDER's own evidence — the one
    statistic in the client's copy is about his client, not the recipient's
    business. It gets a namespace rather than a free pass: an unattributed or
    retired number fails exactly as an unresolvable fact id does. What makes it
    traceable is not a public URL (there is none) but a row naming whose result
    it was and where the underlying report lives, which is what a recipient
    asking "says who?" is actually owed.
    """

    from mazz.pipeline.outreach import PROOF_CITATION_PREFIX, proof_citation_id

    by_id: dict[str, Fact] = {f.id: f for f in available_facts}
    proofs_by_citation = {proof_citation_id(p.id): p for p in proof_points}
    words: set[str] = set()
    numbers: set[str] = set()
    resolved: dict[str, str] = {}
    problems: list[str] = []
    sources: list[str] = []
    segments: list[str] = []
    dates: set[tuple[int | None, int, int | None]] = set()
    kind_words: set[str] = set()
    evidence: list[str] = []

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

        if fact_id.startswith(PROOF_CITATION_PREFIX):
            proof = proofs_by_citation.get(fact_id)
            if proof is None:
                problems.append(
                    f"cited proof point {fact_id!r} is not an active proof point "
                    "on this lead's campaign — the sender's own claim resolves "
                    "to nothing"
                )
                continue
            attribution = (proof.client_name or "").strip()
            if not attribution:
                # Unreachable through the UI and the CHECK constraint; kept
                # because an unattributed statistic is the exact shape of claim
                # the proof_points table exists to prevent, and a control that
                # only holds where it is convenient is not a control.
                problems.append(
                    f"cited proof point {fact_id!r} names no client, so its "
                    "claim is unattributable; a draft may not rest on it"
                )
                continue

            blob = proof.claim_text or ""
            pw, pn = _corpus_tokens(blob)
            words |= pw
            numbers |= pn
            segments.append(blob)
            for _, _, parsed, _ in _iter_dates(blob):
                dates.add(parsed)
            resolved[fact_id] = f"proof point <{attribution}>"
            sources.append(f"proof point {proof.id} (sender's own result, {attribution})")
            evidence.append(f"[{fact_id} | proof_point | {attribution}] {blob}")
            continue

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

        blob = f"{fact.content} {fact.source_url} {fact.kind}"
        fw, fn = _corpus_tokens(blob)
        words |= fw
        numbers |= fn
        segments.append(blob)
        for _, _, parsed, _ in _iter_dates(blob):
            dates.add(parsed)
        for licensed in KIND_VOCABULARY.get(fact.kind, frozenset()):
            kind_words.add(stem(licensed))
        resolved[fact_id] = f"{fact.kind} <{fact.source_url}>"
        sources.append(f"fact {fact_id} ({fact.kind}) <{fact.source_url}>")
        evidence.append(f"[{fact_id} | {fact.kind} | {fact.source_url}] {fact.content}")

    if lead.notes and lead.notes.strip():
        nw, nn = _corpus_tokens(lead.notes)
        words |= nw
        numbers |= nn
        segments.append(lead.notes)
        for _, _, parsed, _ in _iter_dates(lead.notes):
            dates.add(parsed)
        sources.append(f"lead {lead.id} notes field")
        evidence.append(f"[lead notes] {lead.notes.strip()}")

    if campaign_copy is not None:
        if not campaign_copy.qualified:
            # The draft calls this business something the cache no longer
            # supports. Facts are re-read here, so a cache that changed under a
            # drafted lead is caught at exactly this point — which is the
            # whole reason verification re-derives rather than trusting the
            # draft row.
            if campaign_copy.is_generic:
                # No vertical was claimed, so this is never a vertical failure
                # -- it is intake or a disqualifier, and saying "calls this
                # company 'businesses like yours'" would send a reviewer
                # looking for a claim that was never made.
                problems.append(
                    "this lead no longer qualifies, so the draft must not be "
                    f"sent: {campaign_copy.rationale}"
                )
            else:
                problems.append(
                    "the draft calls this company "
                    f"{campaign_copy.vertical!r}, and qualification no longer "
                    f"confirms that from its own pages: {campaign_copy.rationale}"
                )
        else:
            for text_, label in (
                (campaign_copy.offer_line, "campaign offer line (the sender's own words)"),
                (
                    campaign_copy.vertical,
                    "generic vertical (claims nothing about the recipient)"
                    if campaign_copy.is_generic
                    else f"vertical, {campaign_copy.rationale}",
                ),
            ):
                cleaned = (text_ or "").strip()
                if not cleaned:
                    continue
                cw, cn = _corpus_tokens(cleaned)
                words |= cw
                # Deliberately NOT `numbers |= cn`. An operator typing a
                # statistic into the offer line would otherwise have invented a
                # number with no attribution and no date behind it — that is
                # what `proof_points` is for, and it is the one field in this
                # system that may carry a digit into the copy.
                del cn
                segments.append(cleaned)
                sources.append(f"{label}: {cleaned!r}")
                evidence.append(f"[campaign copy | {label}] {cleaned}")

    identity = _lead_identity_text(lead)
    iw, inum = _corpus_tokens(identity)
    words |= iw
    numbers |= inum
    segments.append(identity)
    sources.append(f"lead {lead.id} identity record")
    evidence.append(f"[lead record] {identity}")

    joined = f" {_SEGMENT_BREAK} ".join(segments)
    return Grounding(
        words=frozenset(words),
        numbers=frozenset(numbers),
        resolved=resolved,
        problems=tuple(problems),
        sources=tuple(sources),
        text=_normalize_text(joined),
        stream=tuple(_content_stream(joined)),
        dates=frozenset(dates),
        kind_words=frozenset(w for w in kind_words if w),
        prospect_terms=_prospect_terms(lead),
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UntraceableClaim:
    """One claim in the copy that no evidence supports.

    ``kind`` is the claim type — "quote", "number", "date", "role", "entity" or
    "word" — and it is part of what the reviewer reads, so it says what sort of
    thing went unproven rather than just that something did.
    """

    token: str
    kind: str
    where: str  # "subject" | "body"
    context: str

    def __str__(self) -> str:
        return f"{self.kind} {self.token!r} in {self.where} (…{self.context}…)"


#: How long a meeting the sender is proposing. "a 20-minute call" is an offer,
#: not a measurement of the recipient's business, and it was two of the four
#: holds on the first live run. Kept deliberately tight: the duration must be
#: bound to a meeting noun, so "40 minutes a day of routing" — a claim about
#: their operation — is not covered and still needs evidence.
_MEETING_NOUNS = (
    r"(?:call|calls|chat|chats|conversation|conversations|meeting|meetings|"
    r"demo|intro|introduction|session|sync|zoom|coffee)"
)
_DURATION_WORDS = r"(?:minute|minutes|min|mins|hour|hours|hr|hrs)"
_MEETING_OFFER_RE = re.compile(
    r"\b(?:\d{1,3}|"
    + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    + r")\s*[-–]?\s*"
    + _DURATION_WORDS
    + r"\s+"
    + _MEETING_NOUNS
    + r"\b"
    r"|\b" + _MEETING_NOUNS + r"\s+(?:of\s+|for\s+)(?:\d{1,3}|"
    + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    + r")\s*[-–]?\s*"
    + _DURATION_WORDS + r"\b",
    re.IGNORECASE,
)


#: "a week or two", "a day or two" — an idiom for "approximately", not a count
#: of anything the recipient has. It reads as a number to the extractor ("two"),
#: and on the live run it only escaped because one cited req happened to say
#: "(2 openings)": the same sentence over different evidence would have held the
#: draft. Bound to time units so "a rep or two" is not covered.
_VAGUE_INTERVAL_RE = re.compile(
    r"\b(?:a|another|the\s+next)?\s*"
    r"(?:minute|hour|day|week|month|quarter|year)s?\s+or\s+(?:two|three)\b",
    re.IGNORECASE,
)


def _meeting_offer_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Spans where a quantity is the sender's scheduling, not a measurement."""

    text = text or ""
    return tuple(
        (match.start(), match.end())
        for pattern in (_MEETING_OFFER_RE, _VAGUE_INTERVAL_RE)
        for match in pattern.finditer(text)
    )


#: Derivational endings, stripped ONE WAY: from the draft's word, to look for
#: the evidence's word. `stem()` deliberately refuses to do this (over-stemming
#: collides two different words, and a collision there is a false negative), so
#: it is done here instead, where it cannot collide anything: the result is not
#: a comparison key, it is a lookup into evidence that is either present or not.
#:
#: "the escalation path" over a req that says "escalate integration problems" is
#: the copy restating cited evidence in a different part of speech — the exact
#: false positive W1 set out to remove, and one the first live run still
#: produced. The reverse — inventing support that is not in the evidence — is
#: impossible, because the base still has to BE in the evidence to match.
_DERIVATIONAL_SUFFIXES = ("ation", "ition", "ion", "ment", "ance", "ence")


def _derivational_bases(word: str) -> set[str]:
    lowered = word.lower().strip("-'’")
    bases: set[str] = set()
    for suffix in _DERIVATIONAL_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
            trunk = lowered[: -len(suffix)]
            bases.add(trunk)
            bases.add(trunk + "e")
    return bases


def _context_for(text: str, start: int, end: int, width: int = 44) -> str:
    left = max(start - width, 0)
    right = min(end + width, len(text))
    return " ".join(text[left:right].split())


class _Scanner:
    """Walks one piece of copy once, consuming the spans it has already judged."""

    def __init__(self, text: str, where: str, grounding: Grounding) -> None:
        self.text = text or ""
        self.where = where
        self.g = grounding
        self.claims: list[UntraceableClaim] = []
        self._consumed: list[tuple[int, int]] = []
        self._sender = sender_voice_spans(self.text, grounding.prospect_terms)
        self._offers = _meeting_offer_spans(self.text)

    # -- bookkeeping ----------------------------------------------------
    def consume(self, start: int, end: int) -> None:
        self._consumed.append((start, end))

    def is_consumed(self, start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in self._consumed)

    def in_sender_voice(self, start: int, end: int) -> bool:
        """Is this span the sender talking about the sender?

        Only numbers, dates and prose consult this. Quotes, proper nouns and
        job titles are checked in every span regardless.
        """

        return any(s <= start and end <= e for s, e in self._sender)

    def in_meeting_offer(self, start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in self._offers)

    def fail(self, token: str, kind: str, start: int, end: int) -> None:
        self.claims.append(
            UntraceableClaim(
                token=token,
                kind=kind,
                where=self.where,
                context=_context_for(self.text, start, end),
            )
        )

    # -- support tests --------------------------------------------------
    def supports_word(self, word: str) -> bool:
        """Is this word backed by evidence, a cited fact's kind, or an expansion?"""

        key = stem(word)
        if not key:
            return True
        if key in self.g.words or key in self.g.kind_words:
            return True
        expansion = ABBREVIATIONS.get(word.lower().strip("-'’"))
        if expansion and all(stem(p) in self.g.words for p in expansion):
            return True
        if any(stem(base) in self.g.words for base in _derivational_bases(word)):
            return True
        if "-" in word:
            # A hyphenated compound is supported when every part is either
            # ordinary English or evidence-backed: "tidy-up" is two common
            # words, "go-to-market" needs "market" to be in a cited fact, and
            # "Brightpath-Freight" is supported by neither and still fails.
            parts = [p for p in word.split("-") if p]
            if len(parts) > 1 and all(
                is_common(p) or self.supports_word(p) for p in parts
            ):
                return True
        return False

    def supports_phrase(self, stems: Sequence[str]) -> bool:
        """Does this run of content stems appear contiguously in the evidence?"""

        if not stems:
            return True
        window = len(stems)
        stream = self.g.stream
        target = tuple(stems)
        return any(
            stream[i : i + window] == target
            for i in range(0, max(len(stream) - window + 1, 0))
        )

    def supports_date(self, parsed: tuple[int | None, int, int | None]) -> bool:
        year, month, day = parsed
        for ey, em, ed in self.g.dates:
            if em != month:
                continue
            if year is not None and ey is not None and year != ey:
                continue
            if day is not None and ed is not None and day != ed:
                continue
            if year is not None and ey is None:
                continue
            if day is not None and ed is None:
                continue
            return True
        return False

    # -- passes ---------------------------------------------------------
    def scan_quotes(self) -> None:
        """Quoted text must be in the evidence verbatim. No paraphrase escape."""

        for match in _QUOTE_RE.finditer(self.text):
            inner = match.group(1).strip()
            trimmed = inner.rstrip(". ").strip()
            needle = _normalize_text(re.sub(r"^\[[^\]]*\]\s*", "", trimmed))
            self.consume(match.start(), match.end())
            if not needle:
                continue
            if needle in self.g.text:
                continue
            self.fail(inner, "quote", match.start(), match.end())

    def scan_dates(self) -> None:
        for start, end, parsed, display in _iter_dates(self.text):
            if self.is_consumed(start, end):
                continue
            self.consume(start, end)
            if self.in_sender_voice(start, end):
                continue
            if not self.supports_date(parsed):
                self.fail(display, "date", start, end)

        for match in _QUARTER_RE.finditer(self.text):
            if self.is_consumed(match.start(), match.end()):
                continue
            self.consume(match.start(), match.end())
            if self.in_sender_voice(match.start(), match.end()):
                continue
            if _normalize_text(match.group(0)) not in self.g.text:
                self.fail(match.group(0), "date", match.start(), match.end())

        lowered = self.text.lower()
        # Past and current periods only. A forward or present deictic ("next
        # week", "today") dates no event — see `lexicon.FORWARD_RELATIVE_PHRASES`.
        for phrase in PAST_RELATIVE_PHRASES:
            for match in re.finditer(r"\b" + re.escape(phrase) + r"\b", lowered):
                if self.is_consumed(match.start(), match.end()):
                    continue
                self.consume(match.start(), match.end())
                if self.in_sender_voice(match.start(), match.end()):
                    continue
                if phrase not in self.g.text:
                    self.fail(
                        self.text[match.start() : match.end()],
                        "date",
                        match.start(),
                        match.end(),
                    )

    def scan_numbers(self) -> None:
        """Digits and spelled cardinals. `COMMON_ENGLISH` does not reach here.

        There is still no allow-list of numbers, and there never will be: what
        is exempt is a SPAN, not a value. A quantity asserted about the
        recipient is checked whatever it is; a quantity in the sender's own
        sentence ("I spend most of my time…") or in the length of a meeting the
        sender is proposing ("a 20-minute call") is not a claim about the
        recipient's business at all.
        """

        for start, end, canonical in _iter_numbers(self.text):
            if self.is_consumed(start, end):
                continue
            self.consume(start, end)
            if self._number_is_not_a_claim(start, end):
                continue
            if canonical not in self.g.numbers:
                self.fail(self.text[start:end].strip(), "number", start, end)

        for start, end, canonical in _iter_number_words(self.text):
            if self.is_consumed(start, end):
                continue
            self.consume(start, end)
            if self._number_is_not_a_claim(start, end):
                continue
            if canonical not in self.g.numbers:
                self.fail(self.text[start:end].strip(), "number", start, end)

    def _number_is_not_a_claim(self, start: int, end: int) -> bool:
        return self.in_sender_voice(start, end) or self.in_meeting_offer(start, end)

    def scan_roles(self) -> None:
        """A job title asserted in the copy must be a title somebody posted.

        Checked as a whole phrase, not as loose words: "Revenue Operations
        Manager" is in the evidence and "Head of Revenue Operations" is not,
        even though every word of the second one is.
        """

        for match in _ROLE_TRIGGERS.finditer(self.text):
            # "hiring a Revenue Operations Manager": the title FOLLOWS the
            # trigger, so read forwards from it.
            self._check_title(match.end(), anchor="leading")
        for match in _ROLE_POSSESSIVE.finditer(self.text):
            title = match.group("title") or ""
            if title.strip():
                # "your Revenue Operations Manager posting": the title is what
                # sits against the head noun, so read BACKWARDS from it.
                self._check_title(match.start("title"), raw=title, anchor="trailing")

    def _check_title(
        self, start: int, raw: str | None = None, *, anchor: str = "leading"
    ) -> None:
        """Judge one candidate job title.

        ``anchor`` says which end of the captured span the title is attached to,
        and getting it wrong invents job titles out of ordinary prose. It did:
        "standing up that layer before the new hire lands" matched the
        possessive pattern as `that … hire`, and reading forwards from `that`
        reported `role 'layer'` — a common noun held as a fabricated job title
        on the first live run. The title in that construction is whatever sits
        immediately before "hire", which is "new" — ordinary English, no claim,
        nothing to check. Reading backwards costs nothing on a real one: "your
        Head of Revenue Operations opening" still yields the whole title.
        """

        if raw is None:
            base = _skip_space(self.text, start)
            raw, _ = self._take_phrase(start)
        else:
            base = start

        words = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(raw)]
        kept: list[tuple[str, int, int]] = []
        for word in reversed(words) if anchor == "trailing" else words:
            if word[0].lower() in _PHRASE_BOUNDARY:
                break
            kept.append(word)
        if anchor == "trailing":
            kept.reverse()
        if not kept:
            return

        stems: list[str] = []
        for word, _, _ in kept:
            if is_common(word):
                continue
            # One-way expansion, as everywhere else: "your RevOps req" is the
            # posted Revenue Operations title said shorter, and must be checked
            # against the words the posting actually used.
            expansion = ABBREVIATIONS.get(word.lower().strip("-'’"))
            for part in expansion or (word,):
                key = stem(part)
                if key and not is_common(part):
                    stems.append(key)
        if not stems:
            return

        span_start = base + kept[0][1]
        span_end = min(base + kept[-1][2], len(self.text))
        phrase = " ".join(word for word, _, _ in kept).strip()
        self.consume(span_start, span_end)
        if self.supports_phrase(stems):
            return
        self.fail(phrase, "role", span_start, span_end)

    def _take_phrase(self, start: int, limit: int = 6) -> tuple[str, int]:
        pos = _skip_space(self.text, start)
        end = pos
        taken = 0
        while taken < limit:
            match = _WORD_RE.match(self.text, _skip_space(self.text, end))
            if match is None:
                break
            if match.group(0).lower() in _PHRASE_BOUNDARY:
                break
            end = match.end()
            taken += 1
        return self.text[pos:end], end

    def scan_entities(self) -> None:
        """Proper nouns and acronyms: company, person, product and investor names."""

        for match in _WORD_RE.finditer(self.text):
            token = match.group(0)
            if self.is_consumed(match.start(), match.end()):
                continue
            # Ordinary English is not a proper noun, whatever its capitalisation
            # or its apostrophes. "I'm", "I'd" and "I'll" were reported as
            # untraceable ENTITIES on the first live run — the sender's own
            # contractions read as somebody's name — because this pass tested
            # shape before it tested vocabulary. A common word cannot name a
            # company, and one that reaches `scan_terms` instead is waved
            # through there anyway, so this skips nothing that was ever caught.
            if is_common(token):
                continue
            if not self._looks_like_entity(token, match.start()):
                continue
            self.consume(match.start(), match.end())
            if not self.supports_word(token):
                self.fail(token, "entity", match.start(), match.end())

    def _looks_like_entity(self, token: str, start: int) -> bool:
        bare = token.strip("-'’")
        if len(bare) < 2:
            return False
        if bare.isupper():  # CRM, SDR, VP — an acronym anywhere is a name
            return True
        if not bare[0].isupper():
            return False
        if any(c.isupper() for c in bare[1:]):  # PaperKite, RevOps
            return True
        # A capital at the start of a sentence is grammar, not a name. Such a
        # word is not waved through — it falls to `scan_terms`, where it is
        # checked like any other word — it is just not REPORTED as a proper
        # noun, because "Headcount doubled" is not a name and calling it one
        # would mislead the reviewer.
        return not self._is_sentence_initial(start)

    def _is_sentence_initial(self, start: int) -> bool:
        pos = start - 1
        while pos >= 0 and self.text[pos] in " \t\"'“”‘’([-—–*":
            pos -= 1
        return pos < 0 or self.text[pos] in ".!?:;\n"

    def scan_terms(self) -> None:
        """Whatever is left: ordinary English passes, specifics must be evidence.

        Skipped inside the sender's own spans. A word there describes the
        sender's practice, offer or question — "I work with support teams on
        exactly that handoff" — and requiring the recipient's research to
        contain the sender's vocabulary is not a test of anything.
        """

        for match in _WORD_RE.finditer(self.text):
            token = match.group(0)
            if self.is_consumed(match.start(), match.end()):
                continue
            if is_common(token):
                continue
            if self.in_sender_voice(match.start(), match.end()):
                continue
            if self.supports_word(token):
                continue
            self.fail(token, "word", match.start(), match.end())

    def run(self) -> list[UntraceableClaim]:
        # Order matters: the most specific claim type gets first refusal on a
        # span, so a reviewer sees "role 'Head of Revenue Operations'" rather
        # than three separate word complaints about the same phrase.
        self.scan_quotes()
        self.scan_dates()
        self.scan_numbers()
        self.scan_roles()
        self.scan_entities()
        self.scan_terms()
        return self.claims


def extract_claims(text: str, where: str, grounding: Grounding) -> list[UntraceableClaim]:
    """Every claim in ``text`` that the evidence does not support."""

    return _Scanner(text, where, grounding).run()


# --------------------------------------------------------------------------
# The optional entailment judge
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """A judge's answer. `entailed=False` fails the draft; True changes nothing."""

    entailed: bool
    unsupported: tuple[str, ...] = ()
    notes: str = ""


class JudgeUnavailable(RuntimeError):
    """The configured judge could not produce a verdict. Fails the draft.

    Not a degraded mode. If verification is configured to consult a judge and
    the judge cannot answer, the draft has not been fully checked, and an
    unchecked draft is held like any other.
    """


#: subject, message body (no footer), evidence snippets -> verdict.
Judge = Callable[[str, str, Sequence[str]], JudgeVerdict]


JUDGE_SYSTEM_PROMPT = """\
You are the last check before a cold outreach email is shown to a human
reviewer. You are given the EMAIL and the EVIDENCE it is allowed to rest on:
snippets a research pipeline retrieved about the recipient's company, plus the
sender's own notes and the sender's own record of who the recipient is.

Decide one thing: does every factual assertion in the email follow from that
evidence?

- A claim does not have to be worded like the evidence. Restatement, synonyms,
  tense and plural changes are all fine.
- A claim that adds information the evidence does not contain is NOT entailed,
  however plausible it sounds. "They are hiring a support specialist" does not
  entail "their support team is overwhelmed", "they are growing fast", or
  "they use Zendesk".
- Opinions, offers and questions from the sender about the sender's own
  business are not claims about the recipient. Ignore them.
- You are not judging writing quality, tone or persuasiveness. Only entailment.

Return an object with:
- `entailed`: true only if every assertion about the recipient follows from the
  evidence.
- `unsupported`: an array of short quotations from the email, one per
  unsupported assertion. Empty when `entailed` is true.
- `notes`: one sentence explaining the verdict.

If you are unsure whether an assertion is supported, treat it as unsupported.
A held draft costs a reviewer a minute; an invented claim about someone's
business costs the sender a great deal more.\
"""

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entailed": {"type": "boolean"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["entailed", "unsupported", "notes"],
    "additionalProperties": False,
}


def claude_judge(subject: str, message: str, evidence: Sequence[str]) -> JudgeVerdict:
    """Second-opinion entailment check. Additive only; never rescues a failure.

    Deliberately a small, cheap call: the deterministic layer has already run
    and this only ever sees drafts that passed it.
    """

    payload = json.dumps(
        {
            "email": {"subject": subject, "body": message},
            "evidence": list(evidence),
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    try:
        # Lazy, and inside the try: `anthropic` is an optional dependency, so a
        # missing package must become a held draft like any other unanswerable
        # check, never an import error at module load.
        from mazz.pipeline.compose_claude import get_client

        client = get_client()
        response = client.messages.create(
            model=str(setting("VERIFY_JUDGE_MODEL", "claude-opus-5")),
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": JUDGE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            messages=[{"role": "user", "content": payload}],
            output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        )
    except Exception as exc:
        raise JudgeUnavailable(f"entailment judge call failed: {exc}") from exc

    if getattr(response, "stop_reason", None) in ("refusal", "max_tokens"):
        raise JudgeUnavailable(
            f"entailment judge returned stop_reason={response.stop_reason!r}"
        )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise JudgeUnavailable("entailment judge returned no text block")
    try:
        data = json.loads(text)
        return JudgeVerdict(
            entailed=bool(data["entailed"]),
            unsupported=tuple(str(u) for u in data.get("unsupported", ())),
            notes=str(data.get("notes", "")),
        )
    except Exception as exc:
        raise JudgeUnavailable(f"entailment judge returned unusable output: {exc}") from exc


def default_judge() -> Judge | None:
    """The judge in force, from `VERIFY_JUDGE`. Off unless explicitly enabled.

    Like `draft.default_composer`, this does NOT switch on merely because an API
    key is present: a key in the environment is not consent to start spending.
    An unrecognised value raises rather than silently disabling the check.
    """

    choice = str(setting("VERIFY_JUDGE", "off") or "off").strip().lower()
    if choice in ("", "off", "none", "false", "0"):
        return None
    if choice == "claude":
        return claude_judge
    raise ValueError(
        f"VERIFY_JUDGE={choice!r} is not a known judge (expected 'off' or "
        "'claude'). Refusing to guess which check was meant."
    )


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
    proof_points: Iterable["ProofPoint"] = (),
    campaign_copy: "CampaignCopy | None" = None,
    judge: Judge | None = None,
) -> VerificationResult:
    """Verify rendered copy against the cache. Pure: no DB writes, no clock.

    ``body`` is the FULL rendered body including the compliance footer, because
    a draft missing its footer is also a compliance failure and this is the
    control that catches it.

    ``judge`` is the optional entailment second opinion. It is consulted only
    after every deterministic check has passed, so it can add a failure and can
    never remove one.
    """

    facts = list(available_facts)
    grounding = build_grounding(
        lead, list(signals_used), facts, list(proof_points), campaign_copy
    )

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
        return _fail(
            _failure_lines(problems, untraceable, grounding),
            untraceable=untraceable,
            problems=problems,
            resolved=sorted(grounding.resolved),
        )

    # --- optional entailment judge --------------------------------------
    # Runs last and only on a clean draft: it is a second opinion on the grey
    # area the exact-match rules cannot reach, not a replacement for them.
    if judge is not None:
        try:
            verdict = judge(subject or "", message, grounding.evidence)
        except Exception as exc:
            judge_problem = (
                f"entailment judge could not produce a verdict ({exc}); a draft "
                "that has not been fully checked is held, not passed"
            )
            return _fail(
                _failure_lines([judge_problem], (), grounding),
                problems=(judge_problem,),
                resolved=sorted(grounding.resolved),
            )
        if not verdict.entailed:
            lines = [
                "entailment judge: the copy asserts something the cited evidence "
                f"does not support — {verdict.notes or '(no detail given)'}"
            ]
            lines += [f"judge flagged: {u!r}" for u in verdict.unsupported]
            return _fail(
                _failure_lines(lines, (), grounding),
                problems=tuple(lines),
                resolved=sorted(grounding.resolved),
            )

    detail = "; ".join(f"{k} -> {v}" for k, v in sorted(grounding.resolved.items()))
    notes = (
        "PASSED — every claim traces to cited evidence or the lead's notes.\n"
        f"- evidence: {detail or '(none)'}\n"
        f"- grounding sources: {', '.join(grounding.sources)}\n"
        f"- entailment judge: {'consulted, no objection' if judge else 'not configured'}\n"
        "- compliance footer present and matches configuration."
    )
    return VerificationResult(
        status=VerificationStatus.PASSED,
        notes=notes,
        resolved_ids=tuple(sorted(grounding.resolved)),
    )


def _failure_lines(
    problems: Sequence[str],
    untraceable: Sequence[UntraceableClaim],
    grounding: Grounding,
) -> list[str]:
    lines: list[str] = list(problems)
    for claim in untraceable:
        lines.append(
            f"untraceable {claim.kind} {claim.token!r} in the {claim.where}: "
            f"no cited fact and no lead note supports it — …{claim.context}…"
        )
    if grounding.resolved:
        lines.append(
            "resolved evidence: "
            + "; ".join(f"{k} -> {v}" for k, v in sorted(grounding.resolved.items()))
        )
    return lines


def verify_draft(
    session: "Session",
    lead: Lead,
    draft: Draft,
    *,
    available_facts: Iterable[Fact] | None = None,
    judge: Judge | None = None,
) -> VerificationResult:
    """Verify a persisted draft against this lead's cached research.

    Both evidence corpora are re-read from the database here, never taken from
    the draft row: the research facts by id out of `research_cache`, and the
    cited proof points out of `proof_points`, filtered to the lead's own
    campaign and to rows that are still active. A statistic the client retired
    yesterday stops verifying today, which is the point of the `active` flag
    and the reason this resolution does not happen at draft time.
    """

    facts = list(available_facts) if available_facts is not None else facts_for_lead(session, lead)
    return verify_content(
        lead=lead,
        subject=draft.subject or "",
        body=draft.body or "",
        signals_used=list(draft.signals_used or []),
        available_facts=facts,
        proof_points=cited_proof_points(session, lead, draft.signals_used or []),
        campaign_copy=campaign_copy_for(session, lead, facts),
        judge=judge,
    )


def campaign_copy_for(
    session: "Session", lead: Lead, facts: Sequence[Fact]
) -> "CampaignCopy | None":
    """Re-derive the campaign's copy and its warrant for this lead.

    Returns None for a lead with no campaign, or a campaign with no vertical:
    there is no offer sentence to account for, so there is nothing to admit and
    nothing to refuse. A draft that nonetheless contains such a sentence simply
    fails on its words, which is the correct outcome.

    Qualification is re-run HERE against facts read fresh from the cache, not
    taken from the drafting step. A research row that changed, or a campaign
    vocabulary an operator narrowed since the draft was written, both land as a
    failed verification rather than as a message that silently stopped being
    true.
    """

    from mazz.models import Campaign
    from mazz.pipeline.outreach import effective_vertical, vertical_is_generic
    from mazz.pipeline.qualify import config_for_campaign, qualify_facts

    if lead.campaign_id is None:
        return None
    campaign = session.get(Campaign, lead.campaign_id)
    if campaign is None:
        return None

    vertical = effective_vertical(campaign)
    if not vertical:
        return None

    # The SAME config the composer's side derives, from the same function. If
    # these were built separately, a draft could be written believing no
    # vertical was claimed and checked believing one was.
    result = qualify_facts(facts, config=config_for_campaign(campaign))
    return CampaignCopy(
        vertical=vertical,
        offer_line=(campaign.offer_line or "").strip(),
        qualified=result.qualified,
        rationale=result.rationale,
        is_generic=vertical_is_generic(campaign),
    )


def cited_proof_points(
    session: "Session", lead: Lead, signals_used: Sequence[str]
) -> list["ProofPoint"]:
    """The active proof points on this lead's campaign that the draft cites.

    Scoped to the campaign on purpose. A citation that resolves to another
    client's proof point would be one agency-client's copy standing on another
    one's results, so a cross-campaign id is not found here and fails as
    unresolvable rather than quietly working.
    """

    from mazz.models import ProofPoint
    from mazz.pipeline.outreach import proof_point_id_from_citation

    wanted = {
        pid
        for pid in (proof_point_id_from_citation(c) for c in signals_used)
        if pid is not None
    }
    if not wanted or lead.campaign_id is None:
        return []

    stmt = select(ProofPoint).where(
        ProofPoint.id.in_(wanted),
        ProofPoint.campaign_id == lead.campaign_id,
        ProofPoint.active.is_(True),
    )
    return list(session.execute(stmt).scalars())


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

    result = verify_draft(ctx.session, ctx.lead, draft, judge=default_judge())
    apply_verification(ctx.session, ctx.lead, draft, result)
