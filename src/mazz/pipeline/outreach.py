"""Email 1: the offer, the proof point, and the template that renders them.

--------------------------------------------------------------------------
WHAT THIS MODULE IS
--------------------------------------------------------------------------
The client's own sent mail, as a function. This is the message being
reproduced, verbatim from his account:

    Subject: Quick Question

    Hey Louis,

    Quick question — is DAME Legal currently looking to bring in more clients?

    We help law firms generate qualified client opportunities through search
    marketing. One recent client saw 50+ potential clients in a single month.

    Worth sending over a little more info?

Five slots, and it is worth being precise about where each one comes from,
because the whole design of the pipeline follows from it:

    first_name    "Louis"                  the lead row
    company_name  "DAME Legal"             the lead row
    vertical      "law firms"              the CAMPAIGN, confirmed against the
                                           prospect's own site by `qualify`
    offer_line    "generate qualified ..." the CAMPAIGN, the client's words
    claim_text    "One recent client ..."  a `proof_points` row

Nothing else varies. There is no sentence in this email that a model needs to
write, and so there is no model in this path: no tokens, no batch, no cache, no
latency, nothing to hold when a generation comes back wrong. AI was ~70% of
cost per usable lead and email 1 now costs none of it.

--------------------------------------------------------------------------
WHY A FIXED TEMPLATE IS THE STRICTER OPTION, NOT THE LAZY ONE
--------------------------------------------------------------------------
A generated email is checked by re-deriving every claim out of free prose,
which is what the verifier's lexicon exists to do and why it is hard. This copy
makes exactly two assertions about the recipient:

  1. that the company exists under the name we address it by — the lead row;
  2. that it belongs to `vertical` — and that word is only ever printed
     because `qualify` found it on the prospect's own pages, whose fact ids
     travel with the draft in ``signals_used``.

and exactly one about the sender — `claim_text`, copied character for
character out of a `proof_points` row that carries who, when and where the
evidence lives. The number is never recomputed, never rounded, never
paraphrased, because nothing here is capable of doing any of those things.

The scaffolding sentences make no claim about the recipient at all. "Are you
looking to bring in more clients" is a QUESTION — it asserts nothing, which is
why it is safe to send to a qualified lead and why it carries the
personalisation without carrying risk.

--------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT HERE
--------------------------------------------------------------------------
* **No signature.** The mailbox that sends decides the name on the message,
  and it is chosen at send time by health and cap headroom — hours after this
  runs. The footer (address + one-click unsubscribe, guardrail 6) is appended
  by `draft.render_body` outside any composer.
* **No per-campaign body template, yet.** The structure above is fixed and the
  client's control is over its content — the vertical, the offer line, the
  proof points. Moving the sentences themselves into a database column is a
  real next step, and it needs the verifier to treat an operator-written
  sentence as an untrusted claim rather than as scaffolding. Until then a
  fixed structure is the honest place to stop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from mazz.models import Campaign, Lead, ProofPoint
from mazz.pipeline.qualify import QualificationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from mazz.pipeline.draft import DraftContent

log = logging.getLogger(__name__)

__all__ = [
    "PROOF_CITATION_PREFIX",
    "SUBJECT",
    "Offer",
    "OfferCarriesUnattributedNumber",
    "OfferIncomplete",
    "OutreachRequest",
    "build_outreach_request",
    "GENERIC_VERTICAL",
    "compose_outreach",
    "effective_vertical",
    "vertical_is_generic",
    "offer_for_campaign",
    "proof_citation_id",
    "proof_point_id_from_citation",
    "select_proof_point",
]


class OfferCarriesUnattributedNumber(ValueError):
    """The offer line contains a digit, and digits need a source.

    "We help law firms get 400 new cases a year" reads as sender voice, so the
    verifier — correctly — treats the whole sentence as a statement about us
    and does not hold its number to the prospect's evidence. That is the right
    rule for a sentence describing our own service, and it is exactly why this
    field may not carry a statistic: the number would be the strongest claim in
    the message and the only one with nothing behind it.

    Statistics go in `proof_points`, which is the one place in this system a
    digit may enter the copy from, and which requires a named client and a
    place the evidence lives. Refused here AND at the campaign form, because a
    rule enforced only in the UI is a rule a fixture or a migration walks past.
    """


class OfferIncomplete(ValueError):
    """The campaign cannot say what it sells, so no email can be written.

    Raised rather than resolved to `no_angle`, and the difference matters. A
    `no_angle` lead is a business outcome the reviewer reads as "there was
    nothing to say to this person". A campaign missing its vertical noun is an
    unfinished setup, and quietly marking a whole list `no_angle` would hide
    that behind a number that looks like ordinary attrition.
    """


#: The vertical phrase for a campaign that has none, when it has explicitly
#: opted in (``campaigns.generic_vertical_fallback``).
#:
#: It is worth being clear about what this phrase is and is not. It is not a
#: claim: "businesses like yours" asserts nothing about the recipient that
#: could be false, so it carries none of the legal exposure a wrong vertical
#: would. What it costs is the thing that makes the copy work at all -- "We
#: help law firms" is specific enough that a partner at a law firm reads the
#: next sentence, and this is what every competitor's template already says.
#:
#: So it is a deliberate trade an operator makes per campaign, never a default
#: and never the silent consequence of a blank form field.
GENERIC_VERTICAL = "businesses like yours"

#: Subject line. The client's own — and note it is exactly what the retired
#: Claude prompt forbade ("No subject-line gimmicks. No 'Quick question'").
#: That rule was written for event-led copy where the subject carried the
#: specific thing we had found. This copy's opening question IS the message,
#: and the client's sent mail is the specification now.
SUBJECT = "Quick Question"

#: Citation scheme for a claim that comes from the sender's own evidence rather
#: than from the research cache. `verify` resolves these against `proof_points`
#: exactly as it resolves fact ids against the cache — the sender's statistic
#: is a claim like any other and does not get to travel uncited.
PROOF_CITATION_PREFIX = "proof_point:"

_WS = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")


def proof_citation_id(proof_point_id: int) -> str:
    return f"{PROOF_CITATION_PREFIX}{proof_point_id}"


def proof_point_id_from_citation(citation: str) -> int | None:
    """The row id inside a proof-point citation, or None if it is not one."""

    if not citation.startswith(PROOF_CITATION_PREFIX):
        return None
    raw = citation[len(PROOF_CITATION_PREFIX) :]
    try:
        return int(raw)
    except ValueError:
        return None


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def effective_vertical(campaign: Campaign) -> str | None:
    """The noun this campaign's copy will actually print, or None if it cannot.

    One function so the composer and the verifier can never disagree about what
    the email says. `verify.campaign_copy_for` calls this too: if the two
    derived the phrase separately, a draft could be written with one and
    checked against the other.
    """

    vertical = _clean(campaign.vertical)
    if vertical:
        return vertical
    if campaign.generic_vertical_fallback:
        return GENERIC_VERTICAL
    return None


def vertical_is_generic(campaign: Campaign) -> bool:
    """True when the copy will use the generic phrase rather than a real noun.

    Drives two things: qualification waives the vertical gate (there is no
    vertical being claimed), and the review UI says so plainly instead of
    reporting a confirmation that never happened.
    """

    return not _clean(campaign.vertical) and bool(campaign.generic_vertical_fallback)


@dataclass(frozen=True, slots=True)
class Offer:
    """What the campaign sells, and the one piece of evidence it may cite.

    Assembled from the campaign row plus at most one `proof_points` row. Frozen
    and plain so that rendering can be tested without a database, and so that
    "what did this email claim" is answerable from the object that produced it.
    """

    vertical: str
    offer_line: str
    proof_claim: str | None = None
    proof_point_id: int | None = None
    #: True when `vertical` is the generic phrase rather than a real noun.
    vertical_is_generic: bool = False

    def citations(self) -> list[str]:
        return [] if self.proof_point_id is None else [proof_citation_id(self.proof_point_id)]


def select_proof_point(session: "Session", campaign_id: int) -> ProofPoint | None:
    """The proof point this campaign leads with: the most recent active one.

    Deterministic, and deliberately not random. Two reasons. A rotation would
    make "what did we send this person" depend on when the job ran, which is
    the kind of thing you only discover you needed on the day a recipient asks.
    And the freshest claim is the one the copy's own words commit to: "one
    RECENT client". Rows with no date sort last rather than first — an undated
    claim is not evidence of recency.
    """

    stmt = (
        select(ProofPoint)
        .where(ProofPoint.campaign_id == campaign_id, ProofPoint.active.is_(True))
        .order_by(ProofPoint.occurred_on.desc().nullslast(), ProofPoint.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def offer_for_campaign(session: "Session", campaign: Campaign) -> Offer:
    """Build the offer for a campaign, refusing an incomplete one.

    Both required fields are refusals rather than defaults, because both have
    a plausible-looking fallback that would be a lie. "businesses like yours"
    for a missing vertical is the generic filler this product exists to
    delete; an empty offer line would send "We help law firms ." to a stranger.
    """

    vertical = effective_vertical(campaign)
    is_generic = vertical_is_generic(campaign)
    offer_line = _clean(campaign.offer_line)
    missing = [
        name
        for name, value in (("vertical", vertical), ("offer_line", offer_line))
        if not value
    ]
    if missing:
        hint = ""
        if "vertical" in missing:
            hint = (
                " (or turn on the generic-vertical fallback if this offer really "
                "is vertical-agnostic)"
            )
        raise OfferIncomplete(
            f"campaign {campaign.id} ({campaign.name!r}) has no {' and no '.join(missing)}, "
            f"so there is no sentence to write{hint}. Set it on the campaign before "
            "running leads through it — this is a setup gap, not a lead outcome."
        )

    if _DIGIT.search(campaign.signature or ""):
        raise OfferCarriesUnattributedNumber(
            f"campaign {campaign.id} ({campaign.name!r}) has a number in its "
            "signature. The verifier admits the signature as sender copy, so it "
            "may not carry a figure."
        )

    if _DIGIT.search(offer_line):
        raise OfferCarriesUnattributedNumber(
            f"campaign {campaign.id} ({campaign.name!r}) has a number in its offer "
            f"line: {offer_line!r}. Describe what you do here and put the result "
            "in a proof point, where it gets a client name and a date."
        )

    proof = select_proof_point(session, campaign.id)
    if proof is None:
        # Not an error. The offer sentence stands on its own, and an email with
        # one fewer claim in it is the safe direction to fail in.
        log.info(
            "campaign %s has no active proof point; email 1 will carry the offer "
            "sentence without a result claim",
            campaign.id,
        )
        return Offer(
            vertical=vertical, offer_line=offer_line, vertical_is_generic=is_generic
        )

    return Offer(
        vertical=vertical,
        offer_line=offer_line,
        proof_claim=_clean(proof.claim_text),
        proof_point_id=proof.id,
        vertical_is_generic=is_generic,
    )


@dataclass(frozen=True, slots=True)
class OutreachRequest:
    """Everything email 1 is allowed to see. Compare `draft.DraftRequest`.

    Note what is absent: the facts themselves. The composer does not read the
    prospect's pages — `qualify` did that, reached a verdict, and passes on the
    ids of the evidence behind it. The copy cannot quote a page it was never
    given, which is how a template stays a template.
    """

    lead_id: int | None
    first_name: str
    company_name: str
    offer: Offer
    qualification: QualificationResult

    def citations(self) -> list[str]:
        """Every id this email's claims rest on: the vertical evidence from the
        prospect's own site, then the sender's proof point."""

        return [*self.qualification.cited_ids, *self.offer.citations()]


def build_outreach_request(
    lead: Lead, offer: Offer, qualification: QualificationResult
) -> OutreachRequest:
    company = _clean(lead.company_name)
    if not company:
        raise OfferIncomplete(
            f"lead {lead.id} has no company name, and the opening question is "
            "addressed to one. A lead this incomplete should not have reached "
            "drafting — check ingest validation."
        )
    return OutreachRequest(
        lead_id=lead.id,
        first_name=_first_name(lead.full_name),
        company_name=company,
        offer=offer,
        qualification=qualification,
    )


def _first_name(full_name: str | None) -> str:
    parts = _clean(full_name).split()
    return parts[0] if parts else "there"


def compose_from_step(step, request: OutreachRequest, lead: Lead, campaign: Campaign) -> "DraftContent":
    """Email 1 from the campaign's step-1 template (the client's exact copy).

    The template is operator-written, so it is NOT grounding: the verifier
    checks the rendered body word by word. Only the vertical, offer line and
    signature fields carry the campaign's admitted sender copy.
    """

    from mazz.pipeline.draft import DraftContent
    from mazz.pipeline.sequence import render_template

    if not request.qualification.qualified:
        raise ValueError("compose_from_step called for a lead that is not qualified")
    return DraftContent(
        subject=(step.subject or SUBJECT).strip(),
        body_text=render_template(
            step.body_template, lead, campaign, vertical=request.offer.vertical
        ),
        signals_used=request.citations(),
    )


def compose_outreach(request: OutreachRequest) -> "DraftContent":
    """Render email 1. Pure, deterministic, no network, no model, no clock.

    Every sentence is either fixed scaffolding that asserts nothing about the
    recipient, or a value the client entered, or a name off the lead row. There
    is no third category — which is the same property the old template composer
    had, arrived at from the opposite direction: that one quoted the prospect's
    pages verbatim, this one does not mention them at all.
    """

    from mazz.pipeline.draft import DraftContent

    if not request.qualification.qualified:
        # Structural, not defensive. `qualify` owns the decision not to write,
        # and a composer that would render an email for an unqualified lead is
        # one refactor away from being called on one.
        raise ValueError(
            "compose_outreach called for a lead that is not qualified "
            f"({request.qualification.verdict.value}); the decision not to write "
            "belongs to qualify, and an email must never be forced from it"
        )

    offer = request.offer
    help_sentence = f"We help {offer.vertical} {offer.offer_line}"
    if not help_sentence.endswith("."):
        help_sentence += "."
    if offer.proof_claim:
        claim = offer.proof_claim
        if not claim.endswith((".", "!", "?")):
            claim += "."
        help_sentence = f"{help_sentence} {claim}"

    body = "\n\n".join(
        [
            f"Hey {request.first_name},",
            f"Quick question — is {request.company_name} currently looking to "
            "bring in more clients?",
            help_sentence,
            "Worth sending over a little more info?",
        ]
    )

    return DraftContent(
        subject=SUBJECT,
        body_text=body,
        signals_used=request.citations(),
    )
