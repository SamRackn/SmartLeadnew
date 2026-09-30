"""Drafting: turn qualifying signals into one grounded email.

Pipeline step 4. Two rules govern everything here.

**1. Grounded only in retrieved research plus the lead's `notes`.** Not "mostly
grounded". The failure mode this product exists to fix is plausible-sounding
embellishment -- copy that reads beautifully and asserts something nobody ever
scraped. So the Phase 1 composer quotes fact content VERBATIM and writes nothing
around it but fixed scaffolding.

**2. No draft at all when there is no angle.** `score` decides that; this module
honours it by returning a `DraftOutcome` with no draft and setting `no_angle`.
It never falls back to a generic opener.

--------------------------------------------------------------------------
THE PHASE 2 SEAM  (read this before swapping in the Claude call)
--------------------------------------------------------------------------
Phase 1 must run with no API key, so the composer is a deterministic template.
Phase 2 replaces it with a Claude structured-output call. The seam is:

    DraftRequest   ->  Composer  ->  DraftContent
    (what the                        (subject, body_text, signals_used)
     model may see)

* `DraftRequest` is the ONLY input a composer gets. It carries the lead fields,
  the qualifying signals and the notes -- and nothing else. That is the grounding
  rule expressed as a type: a composer cannot cite what it was never handed.
* `DraftContent` is the exact structured shape the Claude call will return. It
  is a pydantic model precisely so it can be handed to the API as the output
  schema unchanged. Phase 2 adds no field and removes none.
* `compose_template` is the Phase 1 implementation. Phase 2 adds
  `compose_claude(request) -> DraftContent` next to it and passes it as the
  `composer=` argument (or changes `default_composer()`). Nothing else moves.

What is deliberately OUTSIDE the composer, and must stay outside:

* **The CAN-SPAM footer.** `render_body()` appends the physical address and the
  unsubscribe link after the composer returns. A language model that is having a
  bad day can omit an instruction; it cannot omit a string concatenation. The
  legally-required part of the email is therefore not in the model's hands, and
  a missing `SENDER_POSTAL_ADDRESS` raises `MissingSenderAddress` rather than
  quietly shipping a footerless draft.
* **Verification.** `verify` re-derives grounding from the cache; it never takes
  the composer's word for what it cited. A model asserting "signals_used: [x]"
  is a claim, not evidence.
* **The no-angle decision.** Made by `score` before a composer is ever called,
  so there is no path where a model is asked to write something from nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, ConfigDict, Field

from mazz.config import get_settings
from mazz.integrations.base import setting
from mazz.models import Campaign, Draft, Lead, LeadStatus, VerificationStatus
from mazz.pipeline.score import (
    ScoredFact,
    ScoreResult,
    ScoringConfig,
    SignalTier,
    score_lead,
)
from mazz.pipeline.outreach import OfferIncomplete
from mazz.schemas import Fact, FactKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.pipeline.qualify import QualificationResult

    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

__all__ = [
    "DraftContent",
    "DraftRequest",
    "DraftOutcome",
    "Composer",
    "MissingSenderAddress",
    "PLACEHOLDER_ADDRESS_MARKERS",
    "placeholder_marker_in",
    "FOOTER_SEPARATOR",
    "MAX_QUOTE_CHARS",
    "MAX_SIGNALS",
    "compose_template",
    "default_composer",
    "render_body",
    "render_footer",
    "RELEVANCE_LINE",
    "postal_address_for",
    "unresolved_placeholders",
    "unsubscribe_base_url",
    "split_body",
    "unsubscribe_token",
    "unsubscribe_url",
    "build_request",
    "draft_for_lead",
    "outreach_for_lead",
    "persist_draft",
    "handle_draft",
]


# --------------------------------------------------------------------------
# Compliance footer
# --------------------------------------------------------------------------

#: Standard signature delimiter. The verifier splits on the LAST occurrence, so
#: the footer is separable from the message even if the copy contains a dash.
FOOTER_SEPARATOR = "\n\n-- \n"

DEFAULT_UNSUBSCRIBE_BASE = "https://app.mazzsmartlead.example"


class MissingSenderAddress(RuntimeError):
    """`SENDER_POSTAL_ADDRESS` is unset or a placeholder, so no compliant email
    can be rendered.

    Loud on purpose. CAN-SPAM requires a valid physical postal address in every
    commercial email; a draft without one is not a slightly worse draft, it is
    an email that must not be sent. Raising here stops the pipeline at the point
    a human can fix it (one env var) instead of producing a queue of drafts that
    are individually approvable and collectively a violation.
    """


#: Markers of an address nobody has actually set. A stand-in value is WORSE than
#: an empty one and must fail the same way: empty fails loudly and visibly here,
#: while "PLACEHOLDER - Mazz Ltd, 12 Rue Centrale, 75002 Paris, France" — the
#: value the first live run put in the footer of all four drafts — renders a
#: footer that looks compliant to a reviewer, to an auditor skimming for "footer
#: present", and to any test that asserts one. A fabricated postal address on a
#: commercial email is a compliance failure wearing the costume of a passing
#: check.
#:
#: Matched case-insensitively as a whole word so a real street can still contain
#: the letters: "12 Example Road" is refused, "36 Examplethorpe Lane" is not.
PLACEHOLDER_ADDRESS_MARKERS: tuple[str, ...] = (
    "placeholder",
    "changeme",
    "change me",
    "todo",
    "tbd",
    "fixme",
    "your address here",
    "your company address",
    "street address here",
    "xxx",
    "n/a",
    "example address",
    "123 main st",
    "123 main street",
    "sample address",
    "dummy",
    "fake",
    "unset",
    "not set",
    "lorem ipsum",
    "lorem",
    "example",
    # The house number every sample address uses. It refuses a real
    # "123 Elm St" too; that is the fail-closed direction, and the fix is one
    # confirmed edit on the workspace, not a code change.
    "123",
)

_PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(re.escape(marker) for marker in PLACEHOLDER_ADDRESS_MARKERS)
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def placeholder_marker_in(address: str) -> str | None:
    """The placeholder marker in this address, or None. Public so the review
    app and the auditor can ask the same question this module asks."""

    match = _PLACEHOLDER_RE.search(address or "")
    return match.group(0) if match else None


def unsubscribe_token(lead: Lead | str) -> str:
    """Deterministic, unguessable one-click unsubscribe token for a lead.

    HMAC of the lead's email under `SESSION_SECRET`. Deterministic so the same
    lead always gets the same link (re-drafting must not orphan a link already
    in someone's inbox) and so Phase 1 output is reproducible; keyed so the
    token cannot be derived from the address by a third party.

    The route that consumes it belongs to the review app, not to this module.
    """

    email = lead if isinstance(lead, str) else (lead.email or "")
    secret = (get_settings().session_secret or "").encode("utf-8")
    return hmac.new(secret, email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def unsubscribe_base_url() -> str:
    """Where `/u/<token>` is served. ``UNSUBSCRIBE_BASE_URL`` (the public
    unsubscribe host), else ``APP_BASE_URL``, else a placeholder that the live
    send path refuses (see `send.check_live_send_ready`)."""

    base = setting("UNSUBSCRIBE_BASE_URL") or setting("APP_BASE_URL", DEFAULT_UNSUBSCRIBE_BASE)
    return str(base).rstrip("/")


def unsubscribe_url(lead: Lead | str) -> str:
    return f"{unsubscribe_base_url()}/u/{unsubscribe_token(lead)}"


#: The client's own line (launch decision 2026-09-29). In the footer so every
#: step of every sequence carries it and no template can drop it.
RELEVANCE_LINE = "If this isn't relevant, just let me know and I won't follow up."


def postal_address_for(lead: Lead) -> str:
    """The confirmed physical address this lead's email must carry, or raise.

    A routed lead uses its WORKSPACE's address, and only once a named person
    has confirmed it. An unrouted lead (no campaign, so no workspace) falls
    back to ``SENDER_POSTAL_ADDRESS``: that path cannot reach the cold-path
    drafter, and the live send path refuses unrouted leads outright.
    """

    workspace = lead.campaign.workspace if lead.campaign is not None else None
    if workspace is not None:
        address = (workspace.postal_address or "").strip()
        where = f"workspace {workspace.slug!r} postal address"
        if address and not workspace.postal_address_confirmed_at:
            raise MissingSenderAddress(
                f"{where} has not been confirmed. A named person has to confirm "
                "it (Workspaces > edit > confirm address) before any email "
                "carries it."
            )
    else:
        address = (get_settings().sender_postal_address or "").strip()
        where = "SENDER_POSTAL_ADDRESS"
    if not address:
        raise MissingSenderAddress(
            f"{where} is not set. Every outbound email needs a physical mailing "
            "address (CAN-SPAM); refusing to render a draft without one."
        )
    address = " ".join(address.split())
    marker = placeholder_marker_in(address)
    if marker:
        raise MissingSenderAddress(
            f"{where} looks like a placeholder ({marker!r} in {address!r}), so it "
            "is refused exactly as an empty value is. A stand-in address is worse "
            "than none: it renders a footer that passes a glance and a 'footer "
            "present' check while the email carries a mailing address that is "
            "not the sender's."
        )
    return address


def render_footer(lead: Lead) -> str:
    """The CAN-SPAM block: address, the client's opt-out line, one-click unsubscribe.

    Raises `MissingSenderAddress` when the address is missing, unconfirmed or
    a placeholder.
    """

    return (
        f"{postal_address_for(lead)}\n{RELEVANCE_LINE}\n"
        f"Unsubscribe (one click): {unsubscribe_url(lead)}"
    )


_PLACEHOLDER_TOKEN = re.compile(r"\{[^{}\n]{0,40}\}|\[\[[^\]\n]*\]\]")


def unresolved_placeholders(subject: str | None, body: str | None) -> list[str]:
    """Merge fields that never got filled, plus an empty subject.

    `{first_name}`, `{day}`, `{{x}}`, `[[x]]` left in copy mean a template
    rendered with a hole in it. Checked at approve AND at send: one is the
    human gate, the other is the last moment anything can still stop it.
    """

    problems = []
    if not (subject or "").strip():
        problems.append("empty subject")
    for text in (subject or "", body or ""):
        problems += [m.group(0) for m in _PLACEHOLDER_TOKEN.finditer(text)]
    return problems


def render_body(message: str, lead: Lead) -> str:
    """Attach the compliance footer to composed message text.

    Called AFTER the composer, never by it. See the Phase 2 seam note at the top
    of this module: the footer is the one part of the email that no model gets a
    chance to forget.
    """

    return f"{message.rstrip()}{FOOTER_SEPARATOR}{render_footer(lead)}"


def split_body(body: str) -> tuple[str, str | None]:
    """Split a rendered body back into (message, footer). Footer is None if absent."""

    if FOOTER_SEPARATOR not in body:
        return body, None
    message, _, footer = body.rpartition(FOOTER_SEPARATOR)
    return message, footer


# --------------------------------------------------------------------------
# The structured contract
# --------------------------------------------------------------------------


class DraftContent(BaseModel):
    """EXACTLY what a composer returns — Phase 1 template or Phase 2 Claude.

    This is the structured-output schema. `body_text` is the message WITHOUT the
    compliance footer, because the footer is appended outside the composer by
    `render_body`; a model is never asked to reproduce a legal string verbatim.

    `signals_used` is the composer's own account of which facts the copy rests
    on. The verifier treats it as a claim to be checked, not as proof.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="Subject line. Grounded, no invented specifics.")
    body_text: str = Field(description="Message body WITHOUT the compliance footer.")
    signals_used: list[str] = Field(
        default_factory=list,
        description="Fact ids the copy rests on. Must be ids from the request.",
    )


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """Everything a composer is allowed to see, and nothing else.

    The grounding rule as a type. Note what is absent: the full fact list (only
    QUALIFYING signals are passed), any web access, any other lead. A composer
    cannot invent a fact it was never given, and cannot cite a fact that did not
    clear the threshold.
    """

    lead_id: int | None
    first_name: str
    full_name: str
    company_name: str | None
    job_title: str | None
    notes: str | None
    signals: tuple[ScoredFact, ...]

    def fact_ids(self) -> list[str]:
        return [s.fact.id for s in self.signals]


Composer = Callable[[DraftRequest], DraftContent]


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """Result of running the drafting stage for one lead.

    ``content is None`` means no angle. That is a successful, meaningful result
    -- not an error and not a retry -- and it maps to `no_angle`.

    Exactly one of ``qualification`` and ``score_result`` is set, and which one
    says which path produced this outcome. ``qualification`` is the cold path
    every lead takes; ``score_result`` belongs to the retired event-research
    stack, kept importable while it is removed. Both are optional here rather
    than one required field because a caller that needs the verdict should have
    to say which kind of verdict it means.
    """

    lead_status: LeadStatus
    score_result: ScoreResult | None = None
    content: DraftContent | None = None
    body: str | None = None
    reason: str = ""
    qualification: "QualificationResult | None" = None

    @property
    def drafted(self) -> bool:
        return self.content is not None


# --------------------------------------------------------------------------
# Phase 1 composer
# --------------------------------------------------------------------------

#: How many signals the copy may lean on. Three quotes is already a long cold
#: email; the rest stay visible in the review UI as supporting context.
MAX_SIGNALS = 3

#: Quotes are truncated at a word boundary. Truncation keeps every token
#: verbatim, so a shortened quote stays traceable.
MAX_QUOTE_CHARS = 180

_DATE_PREFIX = re.compile(r"^\[[^\]]*\]\s*")
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", _DATE_PREFIX.sub("", text or "")).strip()


def _quote(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """Verbatim snippet, truncated at a word boundary. Never paraphrased."""

    cleaned = _clean(text)
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-—")
    return f"{cut}..."


def _role_phrase(fact: Fact) -> str:
    """The role title out of a job-posting fact, verbatim.

    Company-adapter reqs read "Revenue Operations Manager - Own the ...", so the
    text before the first dash is the title. If the shape is unfamiliar we fall
    back to the first few words rather than guessing a title: a wrong job title
    in the subject line is exactly the embellishment this product is against.
    """

    cleaned = _clean(fact.content)
    for sep in (" - ", " — ", " – ", ": "):
        if sep in cleaned:
            head = cleaned.split(sep, 1)[0].strip()
            if 2 <= len(head) <= 80:
                return head
    words = cleaned.split()
    return " ".join(words[:6])


def _first_name(full_name: str | None) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else "there"


def compose_template(request: DraftRequest) -> DraftContent:
    """PHASE 1 COMPOSER. Deterministic template copy, no API key, no network.

    Emits the same `DraftContent` shape the Claude call returns in Phase 2, so
    every consumer downstream (persistence, verification, the review UI) is
    already written against the final contract.

    Every sentence is either (a) fixed scaffolding drawn from a small fixed
    vocabulary, or (b) a verbatim quote from a qualifying fact, or (c) the
    lead's own notes. There is no third category, which is why the output
    verifies.
    """

    signals = request.signals[:MAX_SIGNALS]
    if not signals:
        raise ValueError(
            "compose_template called with no signals; the no-angle decision "
            "belongs to score, and a draft must never be forced from nothing"
        )

    lead_in = signals[0]
    company = (request.company_name or "").strip()
    used: list[str] = [lead_in.fact.id]

    # --- subject ------------------------------------------------------
    if lead_in.fact.kind == FactKind.JOB_POSTING.value:
        role = _role_phrase(lead_in.fact)
        subject = f"{company} — {role}".strip(" —") if company else role
        opener = f"I saw your {role} opening:"
    else:
        subject = f"{company} — a quick note on your latest update".strip(" —")
        if not company:
            subject = "A quick note on your latest update"
        opener = "I saw your latest update:"

    # --- body ---------------------------------------------------------
    lines: list[str] = [f"Hi {request.first_name},", ""]
    lines.append(f'{opener} "{_quote(lead_in.fact.content)}"')

    supporting = [s for s in signals[1:]]
    if supporting:
        lines.append("")
        lines.append("Also on my list from your site:")
        for s in supporting:
            lines.append(f'- "{_quote(s.fact.content)}"')
            used.append(s.fact.id)

    notes = (request.notes or "").strip()
    if notes:
        lines.append("")
        lines.append(f"From my notes: {_clean(notes)}")

    lines.append("")
    lines.append(_call_to_action(lead_in))
    lines.append("")
    lines.append("Worth a reply either way, and I will leave it here if not.")

    return DraftContent(
        subject=subject,
        body_text="\n".join(lines),
        signals_used=used,
    )


#: Fixed closing lines. Two properties are deliberate and must survive editing:
#:
#: 1. They make no claim about the recipient's company — they describe what WE
#:    want. Anything asserting something about THEM would have to be grounded,
#:    and scaffolding cannot be grounded.
#: 2. They contain no digits. `verify` holds every digit in a draft to evidence
#:    with no allow-list escape, because numbers are where hallucinated outbound
#:    gets legally interesting. "10 minutes" would have had to be an exception,
#:    and an exception in a fail-closed control is a hole. "A short call" costs
#:    us nothing and keeps the rule absolute.
_CTA_BY_TIER = {
    SignalTier.STRONG: (
        "If that is live for you right now, I can show you what we do for teams "
        "in the same spot — a short call, no deck."
    ),
    SignalTier.MEDIUM: (
        "If that is a current priority, I can show you what we do for teams in "
        "the same spot — a short call, no deck."
    ),
}


def _call_to_action(lead_signal: ScoredFact) -> str:
    return _CTA_BY_TIER.get(lead_signal.tier, _CTA_BY_TIER[SignalTier.MEDIUM])


def default_composer() -> Composer:
    """The composer in force.

    Dispatches on `DRAFT_COMPOSER`, and on nothing else. In particular it does
    NOT switch to Claude merely because `ANTHROPIC_API_KEY` is set: a key in
    the environment is not consent to start spending, and a developer with a
    key in their `.env` should not get a different pipeline from CI.

    `compose_claude` is imported lazily so that `anthropic` stays an optional
    dependency and the Phase 1 path keeps importing with nothing installed.

    Note this is the SYNCHRONOUS Claude path — one call per lead, list price.
    It is the right path for the review app's redraft button, where a person is
    waiting. The pipeline should draft through `draft_batch.run_once`, which
    sends the same request in bulk at half price. See that module.
    """

    if str(setting("DRAFT_COMPOSER", "template")).lower() != "claude":
        return compose_template

    from mazz.pipeline.compose_claude import compose_claude

    return compose_claude


# --------------------------------------------------------------------------
# Running the stage
# --------------------------------------------------------------------------


def build_request(lead: Lead, result: ScoreResult) -> DraftRequest:
    """Assemble the composer input from a lead and its scoring verdict.

    Only `result.qualifying` crosses over. Weak facts stay in the review UI as
    context and stay out of the copy.
    """

    return DraftRequest(
        lead_id=lead.id,
        first_name=_first_name(lead.full_name),
        full_name=(lead.full_name or "").strip(),
        company_name=lead.company_name,
        job_title=lead.job_title,
        notes=lead.notes,
        signals=tuple(result.qualifying),
    )


def draft_for_lead(
    session: "Session",
    lead: Lead,
    *,
    now: datetime | None = None,
    config: ScoringConfig | None = None,
    composer: Composer | None = None,
    score_result: ScoreResult | None = None,
) -> DraftOutcome:
    """Score, then draft — or decline to draft. Writes nothing to the database.

    Returns a `DraftOutcome`. When there is no angle, `content` is None and the
    status is `no_angle`: the caller must NOT write a draft row.

    ``config=None`` means "this lead's campaign config", resolved by
    `score.score_lead`. It must stay None-by-default rather than
    `DEFAULT_SCORING`: passing the shipped SaaS defaults explicitly would
    override the campaign's own vocabulary here and nowhere else, so the same
    lead would score one way at the scoring step and another way at the drafting
    step — and the second one is the one that decides whether an email exists.
    """

    result = score_result or score_lead(session, lead, now=now, config=config)

    if not result.has_angle:
        log.info("lead %s: declining to draft — %s", lead.id, result.rationale)
        return DraftOutcome(
            lead_status=LeadStatus.NO_ANGLE,
            score_result=result,
            reason=result.rationale,
        )

    compose = composer or default_composer()
    content = compose(build_request(lead, result))

    # A composer may only cite what it was given. In Phase 1 that is structurally
    # true; in Phase 2 it is a model's promise, so it is checked here as well as
    # in `verify` -- an id the request never contained means the composer went
    # somewhere it should not have, and that is a bug, not a soft warning.
    offered = set(result.ids())
    stray = [fid for fid in content.signals_used if fid not in offered]
    if stray:
        raise ValueError(
            f"composer cited fact ids that were not offered to it: {stray}. "
            "A draft may only rest on signals that cleared scoring."
        )

    body = render_body(content.body_text, lead)
    # Not READY. A freshly composed draft is unverified, and only `verify` can
    # promote a lead to `ready` — otherwise "ready" would mean "we wrote
    # something", which is the incumbent's definition, not ours.
    return DraftOutcome(
        lead_status=LeadStatus.RESEARCHING,
        score_result=result,
        content=content,
        body=body,
        reason=result.rationale,
    )


def persist_draft(session: "Session", lead: Lead, outcome: DraftOutcome) -> Draft | None:
    """Write the outcome. Returns the Draft row, or None for `no_angle`.

    The `no_angle` branch writes NO draft row. An empty-bodied placeholder row
    would show up in the review table as something to look at, which is exactly
    the busywork the brief is trying to delete.

    A persisted draft starts `verification_status=pending` and the lead stays in
    `researching` until `verify` has run. Nothing reaches `ready` on the
    drafter's say-so — passing verification is what makes a draft reviewable.
    """

    if not outcome.drafted:
        lead.status = LeadStatus.NO_ANGLE
        session.flush()
        return None

    assert outcome.content is not None and outcome.body is not None
    draft = Draft(
        lead_id=lead.id,
        subject=outcome.content.subject,
        body=outcome.body,
        signals_used=list(outcome.content.signals_used),
        verification_status=VerificationStatus.PENDING,
        verification_notes=None,
    )
    session.add(draft)
    lead.status = LeadStatus.RESEARCHING
    session.flush()
    return draft


def outreach_for_lead(
    session: "Session",
    lead: Lead,
    *,
    qualification: "QualificationResult | None" = None,
) -> DraftOutcome:
    """THE COLD PATH. Qualify, then render email 1. Writes nothing.

    No model, no tokens, no network — see `pipeline.outreach` for why the copy
    the client actually sends does not need one.

    ``qualification=None`` re-derives the verdict from this lead's campaign
    vocabulary. The `qualify` stage already reached one a moment ago and the
    result is deterministic over the same cache, so re-deriving is cheap and
    keeps drafting callable on its own (the review app's redraft button) without
    a scoring argument that could disagree with what the stage decided.

    An incomplete offer RAISES rather than returning `no_angle`. A campaign with
    no vertical noun is an unfinished setup, and hiding it inside the number
    that measures how many leads had nothing to say is how it would stay
    unfinished.
    """

    from mazz.pipeline.outreach import (
        build_outreach_request,
        compose_outreach,
        offer_for_campaign,
    )
    from mazz.pipeline.qualify import qualify_lead

    result = qualification if qualification is not None else qualify_lead(session, lead)

    if not result.qualified:
        log.info("lead %s: declining to draft — %s", lead.id, result.rationale)
        return DraftOutcome(
            lead_status=LeadStatus.NO_ANGLE,
            qualification=result,
            reason=result.rationale,
        )

    campaign = session.get(Campaign, lead.campaign_id) if lead.campaign_id else None
    if campaign is None:
        # Unreachable in practice: a lead with no campaign has no vocabulary,
        # so `qualify` cannot have qualified it. Refuse loudly anyway rather
        # than reach for a default offer — there is no such thing.
        raise OfferIncomplete(
            f"lead {lead.id} qualified with no campaign attached; there is no "
            "offer to write and no default that would not be a guess"
        )

    offer = offer_for_campaign(session, campaign)
    request = build_outreach_request(lead, offer, result)
    content = compose_outreach(request)

    # The same check the composer's own structure already guarantees, kept
    # because it is the contract rather than the implementation: a draft may
    # cite only the evidence its request carried.
    offered = set(request.citations())
    stray = [cid for cid in content.signals_used if cid not in offered]
    if stray:
        raise ValueError(
            f"composer cited ids that were not offered to it: {stray}"
        )

    return DraftOutcome(
        lead_status=LeadStatus.RESEARCHING,
        qualification=result,
        content=content,
        body=render_body(content.body_text, lead),
        reason=result.rationale,
    )


def handle_draft(ctx: "JobContext") -> None:
    """Queue handler for ``jobs.kind == 'draft'``.

    Chains to ``verify`` only when a draft was actually written. A `no_angle`
    lead has nothing to verify and is finished.
    """

    outcome = outreach_for_lead(ctx.session, ctx.lead)
    draft = persist_draft(ctx.session, ctx.lead, outcome)

    if draft is None:
        log.info("lead %s -> no_angle at draft stage: %s", ctx.lead.id, outcome.reason)
        return

    ctx.enqueue_next("verify")
