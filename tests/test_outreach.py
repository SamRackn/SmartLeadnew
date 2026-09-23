"""Email 1: the client's own message, and the claims it is allowed to make.

The reference is a real email out of the client's account:

    Hey Louis,

    Quick question — is DAME Legal currently looking to bring in more clients?

    We help law firms generate qualified client opportunities through search
    marketing. One recent client saw 50+ potential clients in a single month.

    Worth sending over a little more info?

These tests hold two things. That the pipeline reproduces that message. And
that every claim in it — the vertical noun, the statistic — traces to something
a person can be shown, with the draft failing closed when it does not.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from mazz.db import get_engine
from mazz.config import reload_settings
from mazz.models import Campaign, Lead, ProofPoint, ResearchCache, Workspace
from mazz.pipeline.draft import outreach_for_lead, persist_draft
from mazz.pipeline.outreach import (
    Offer,
    OfferCarriesUnattributedNumber,
    OfferIncomplete,
    compose_outreach,
    offer_for_campaign,
    select_proof_point,
)
from mazz.pipeline.qualify import qualify_lead
from mazz.pipeline.verify import verify_draft
from mazz.schemas import Fact, FactKind, LeadStatus, VerificationStatus, stable_fact_id

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
DOMAIN = "damelegal.com"

LAW_TERMS = ["law firm", "attorneys at law", "attorney", "practice areas", "criminal defense"]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv(
        "SENDER_POSTAL_ADDRESS", "Search Site Marketing, 12 Main St, Austin TX 78701"
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-unsubscribe-tokens")
    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def session():
    connection = get_engine().connect()
    transaction = connection.begin()
    db = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


def fact(kind: str, content: str, path: str = "") -> Fact:
    url = f"https://{DOMAIN}/{path}"
    return Fact(
        id=stable_fact_id(kind, url, content),
        kind=kind,
        content=content,
        source_url=url,
        retrieved_at=NOW,
    )


def seed(session, *, vertical="law firms", offer_line=None, proof=True, facts=None):
    workspace = Workspace(name="Search Site Marketing", slug=f"ssm-{datetime.now().timestamp()}")
    session.add(workspace)
    session.flush()
    campaign = Campaign(
        workspace_id=workspace.id,
        name="SearchSiteMarketing",
        segment="law",
        vertical=vertical,
        offer_line=offer_line
        if offer_line is not None
        else "generate qualified client opportunities through search marketing",
        qualification_config={"vertical_terms": LAW_TERMS},
    )
    session.add(campaign)
    session.flush()
    if proof:
        session.add(
            ProofPoint(
                campaign_id=campaign.id,
                claim_text="One recent client saw 50+ potential clients in a single month.",
                client_name="Hartley & Boyd LLP",
                occurred_on=date(2026, 7, 1),
                source_note="GA4 + call tracking export, July 2026",
            )
        )
    if facts is None:
        facts = [
            fact(FactKind.HOMEPAGE.value, "DAME Legal - Attorneys at Law serving Nassau County"),
            fact(FactKind.SERVICES_PAGE.value, "Practice Areas: Criminal Defense, DWI", "practice-areas"),
            fact(FactKind.CONTACT_PAGE.value, "Free consultation. Contact us today.", "contact"),
        ]
    session.add(
        ResearchCache(
            company_domain=DOMAIN,
            company_name="DAME Legal",
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=NOW,
        )
    )
    lead = Lead(
        full_name="Louis Gerber",
        email="louis@damelegal.com",
        company_name="DAME Legal",
        job_title="Partner",
        company_domain=DOMAIN,
        campaign_id=campaign.id,
        status=LeadStatus.RESEARCHING,
    )
    session.add(lead)
    session.flush()
    return lead, campaign


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


def test_the_pipeline_reproduces_the_clients_own_email(session):
    lead, _ = seed(session)

    outcome = outreach_for_lead(session, lead)

    assert outcome.content.subject == "Quick Question"
    assert outcome.content.body_text == (
        "Hey Louis,\n\n"
        "Quick question — is DAME Legal currently looking to bring in more clients?\n\n"
        "We help law firms generate qualified client opportunities through search "
        "marketing. One recent client saw 50+ potential clients in a single month.\n\n"
        "Worth sending over a little more info?"
    )


def test_the_footer_is_appended_outside_the_composer(session):
    """Guardrail 6: no composer, model or template, may omit the footer."""

    lead, _ = seed(session)

    outcome = outreach_for_lead(session, lead)

    assert "Search Site Marketing, 12 Main St" in outcome.body
    assert "Unsubscribe (one click)" in outcome.body
    assert "Unsubscribe" not in outcome.content.body_text


def test_drafting_the_same_lead_twice_produces_identical_copy(session):
    lead, _ = seed(session)

    first = outreach_for_lead(session, lead)
    second = outreach_for_lead(session, lead)

    assert first.content.body_text == second.content.body_text
    assert first.content.signals_used == second.content.signals_used


def test_email_one_costs_no_model_call(monkeypatch, session):
    """The whole cost case for the cutover, as a test."""

    import builtins

    lead, _ = seed(session)
    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.split(".")[0] == "anthropic":
            raise AssertionError("email 1 reached for the Claude client")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert outreach_for_lead(session, lead).content is not None


# --------------------------------------------------------------------------
# The claims, and their evidence
# --------------------------------------------------------------------------


def test_every_claim_traces_and_the_draft_verifies(session):
    lead, _ = seed(session)

    draft = persist_draft(session, lead, outreach_for_lead(session, lead))
    result = verify_draft(session, lead, draft)

    assert result.passed, result.notes
    # The statistic is attributed to a named client, not floating in the copy.
    assert "Hartley & Boyd LLP" in result.notes


def test_a_retired_proof_point_stops_verifying_the_draft_that_cites_it(session):
    """`active` is not cosmetic. A statistic the client pulls today must stop
    passing verification today, which is why proof points are re-resolved at
    verification rather than baked into the draft row."""

    lead, campaign = seed(session)
    draft = persist_draft(session, lead, outreach_for_lead(session, lead))
    assert verify_draft(session, lead, draft).passed

    proof = select_proof_point(session, campaign.id)
    proof.active = False
    session.flush()

    result = verify_draft(session, lead, draft)

    assert not result.passed
    assert "resolves to nothing" in result.notes


def test_a_proof_point_from_another_campaign_does_not_resolve(session):
    """One agency-client's copy must never stand on another one's results."""

    lead, _ = seed(session)
    other_ws = Workspace(name="Other Agency", slug=f"other-{datetime.now().timestamp()}")
    session.add(other_ws)
    session.flush()
    other = Campaign(workspace_id=other_ws.id, name="Other", vertical="dental practices")
    session.add(other)
    session.flush()
    stolen = ProofPoint(
        campaign_id=other.id,
        claim_text="One recent client saw 900 new patients in a week.",
        client_name="Someone Else Entirely",
    )
    session.add(stolen)
    session.flush()

    outcome = outreach_for_lead(session, lead)
    draft = persist_draft(session, lead, outcome)
    draft.signals_used = [*outcome.content.signals_used[:-1], f"proof_point:{stolen.id}"]
    draft.body = draft.body.replace(
        "One recent client saw 50+ potential clients in a single month.",
        "One recent client saw 900 new patients in a week.",
    )
    session.flush()

    result = verify_draft(session, lead, draft)

    assert not result.passed
    assert "resolves to nothing" in result.notes


def test_the_vertical_noun_fails_when_the_cache_stops_supporting_it(session):
    """The one claim email 1 makes about the recipient, re-derived at
    verification rather than trusted from the drafting step."""

    lead, _ = seed(session)
    draft = persist_draft(session, lead, outreach_for_lead(session, lead))
    assert verify_draft(session, lead, draft).passed

    # The research is re-fetched and this company turns out to be something
    # else entirely. The already-written draft must stop verifying.
    row = session.get(ResearchCache, DOMAIN)
    row.facts = [
        fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental - family dentistry").model_dump(
            mode="json"
        )
    ]
    session.flush()

    result = verify_draft(session, lead, draft)

    assert not result.passed
    assert "qualification no longer confirms" in result.notes


def test_a_number_in_the_offer_line_is_not_licensed_by_it(session):
    """An operator typing a statistic into the offer line would be inventing a
    claim with no attribution and no date.

    It would also be invisible: the offer sentence reads as sender voice, so
    the verifier does not hold its number to the prospect's evidence — which is
    right for a sentence about our own service and exactly why the number must
    not be there. Proof points are the one field allowed to carry a digit into
    the copy, and they require a named client."""

    lead, _ = seed(
        session,
        offer_line="get 400 new cases a year through search marketing",
        proof=False,
    )

    with pytest.raises(OfferCarriesUnattributedNumber, match="proof point"):
        outreach_for_lead(session, lead)


# --------------------------------------------------------------------------
# Refusing to write
# --------------------------------------------------------------------------


def test_an_unqualified_lead_gets_no_draft_row_at_all(session):
    lead, _ = seed(
        session,
        facts=[fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental - family dentistry")],
    )

    outcome = outreach_for_lead(session, lead)
    draft = persist_draft(session, lead, outcome)

    assert draft is None
    assert outcome.content is None
    assert lead.status is LeadStatus.NO_ANGLE


def test_a_campaign_with_no_vertical_raises_rather_than_quietly_no_angling(session):
    """A missing vertical is an unfinished setup, not a lead outcome. Hiding it
    inside the `no_angle` count is how it would stay unfinished."""

    lead, campaign = seed(session)
    campaign.vertical = None
    session.flush()

    with pytest.raises(OfferIncomplete, match="vertical"):
        outreach_for_lead(session, lead)


def test_a_campaign_with_no_proof_point_still_writes_the_offer_sentence(session):
    """One fewer claim is the safe direction to fail in."""

    lead, _ = seed(session, proof=False)

    outcome = outreach_for_lead(session, lead)

    assert "We help law firms generate qualified client opportunities" in outcome.content.body_text
    assert not any(c.startswith("proof_point:") for c in outcome.content.signals_used)
    assert verify_draft(session, lead, persist_draft(session, lead, outcome)).passed


def test_the_composer_refuses_to_write_for_an_unqualified_lead(session):
    """Structural. The decision not to write belongs to qualify, and a composer
    that would render an email anyway is one refactor from being called."""

    lead, _ = seed(
        session, facts=[fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental")]
    )
    from mazz.pipeline.outreach import build_outreach_request

    request = build_outreach_request(
        lead, Offer(vertical="law firms", offer_line="do things"), qualify_lead(session, lead)
    )

    with pytest.raises(ValueError, match="not qualified"):
        compose_outreach(request)


# --------------------------------------------------------------------------
# Proof point selection
# --------------------------------------------------------------------------


def test_the_freshest_active_proof_point_is_chosen_deterministically(session):
    lead, campaign = seed(session)
    session.add_all(
        [
            ProofPoint(
                campaign_id=campaign.id,
                claim_text="An older result.",
                client_name="Old Client",
                occurred_on=date(2024, 1, 1),
            ),
            ProofPoint(
                campaign_id=campaign.id,
                claim_text="One recent client booked 12 consultations in a week.",
                client_name="Newest Client",
                occurred_on=date(2026, 9, 1),
            ),
            ProofPoint(
                campaign_id=campaign.id,
                claim_text="An undated result.",
                client_name="Undated Client",
            ),
        ]
    )
    session.flush()

    offer = offer_for_campaign(session, campaign)

    # Freshest wins; an undated claim is not evidence of recency and sorts last.
    assert offer.proof_claim == "One recent client booked 12 consultations in a week."


def test_a_draft_starts_unverified_and_the_lead_is_not_ready(session):
    """Nothing reaches `ready` on the drafter's say-so."""

    lead, _ = seed(session)

    draft = persist_draft(session, lead, outreach_for_lead(session, lead))

    assert draft.verification_status is VerificationStatus.PENDING
    assert lead.status is LeadStatus.RESEARCHING


# ==========================================================================
# The opt-in generic vertical
# ==========================================================================
#
# "We help businesses like yours ..." for a campaign that has no vertical noun
# and has explicitly asked for it. The phrase is not a claim -- it asserts
# nothing about the recipient that could be false -- so what these tests pin
# down is the boundary: it waives the VERTICAL gate and nothing else, and it
# never happens by accident.


def test_a_blank_vertical_without_the_flag_still_refuses(session):
    """The default is unchanged: a blank vertical is a setup gap, loudly."""

    lead, campaign = seed(session)
    campaign.vertical = None
    session.flush()

    with pytest.raises(OfferIncomplete, match="generic-vertical fallback"):
        outreach_for_lead(session, lead)


def test_the_flag_sends_the_generic_phrase(session):
    lead, campaign = seed(session)
    campaign.vertical = None
    campaign.generic_vertical_fallback = True
    session.flush()

    outcome = outreach_for_lead(session, lead)

    assert (
        "We help businesses like yours generate qualified client opportunities"
        in outcome.content.body_text
    )
    # The rest of the message is untouched.
    assert outcome.content.body_text.startswith("Hey Louis,")
    assert "is DAME Legal currently looking to bring in more clients?" in outcome.content.body_text


def test_a_real_vertical_always_wins_over_the_flag(session):
    """The flag is a fallback, not a mode. A campaign that has both keeps the
    specific noun, which is the thing that makes the email work."""

    lead, campaign = seed(session)
    campaign.generic_vertical_fallback = True
    session.flush()

    outcome = outreach_for_lead(session, lead)

    assert "We help law firms" in outcome.content.body_text
    assert "businesses like yours" not in outcome.content.body_text


def test_the_generic_phrase_verifies_without_confirming_a_vertical(session):
    """A business whose vertical we could never confirm still gets a verified
    draft -- because nothing was claimed about what it is."""

    lead, campaign = seed(
        session,
        facts=[
            fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental - family dentistry"),
            fact(FactKind.CONTACT_PAGE.value, "Free consultation, new patients welcome", "contact"),
        ],
    )
    campaign.vertical = None
    campaign.generic_vertical_fallback = True
    session.flush()

    draft = persist_draft(session, lead, outreach_for_lead(session, lead))
    result = verify_draft(session, lead, draft)

    assert result.passed, result.notes
    # And the reviewer is told plainly, rather than shown a confirmation that
    # never happened.
    assert "generic vertical (claims nothing about the recipient)" in result.notes


def test_the_flag_does_not_waive_the_intake_evidence(session):
    """The opening question still presupposes they take on new clients. The
    generic phrase changes what we CLAIM, not whether the question is fair."""

    lead, campaign = seed(
        session,
        facts=[fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental - family dentistry")],
    )
    campaign.vertical = None
    campaign.generic_vertical_fallback = True
    session.flush()

    outcome = outreach_for_lead(session, lead)

    assert outcome.content is None
    assert outcome.lead_status is LeadStatus.NO_ANGLE
    assert "intake" in outcome.reason


def test_the_flag_does_not_waive_a_disqualifier(session):
    """A parked or closed site is still a reason not to write, whatever we
    would have called them."""

    lead, campaign = seed(
        session,
        facts=[
            fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental. This domain is for sale."),
            fact(FactKind.CONTACT_PAGE.value, "Free consultation", "contact"),
        ],
    )
    campaign.vertical = None
    campaign.generic_vertical_fallback = True
    session.flush()

    outcome = outreach_for_lead(session, lead)

    assert outcome.content is None
    assert "domain is for sale" in outcome.reason


def test_a_generic_campaign_still_fails_when_the_lead_stops_qualifying(session):
    """Verification re-derives for the generic case too, and says why in terms
    of what was actually claimed -- never "the draft calls this company
    'businesses like yours'", which would send a reviewer hunting a claim
    nobody made."""

    lead, campaign = seed(session)
    campaign.vertical = None
    campaign.generic_vertical_fallback = True
    session.flush()
    draft = persist_draft(session, lead, outreach_for_lead(session, lead))
    assert verify_draft(session, lead, draft).passed

    row = session.get(ResearchCache, DOMAIN)
    row.facts = [
        fact(FactKind.HOMEPAGE.value, "DAME Legal. We are not accepting new clients.")
        .model_dump(mode="json")
    ]
    session.flush()

    result = verify_draft(session, lead, draft)

    assert not result.passed
    assert "no longer qualifies" in result.notes
    assert "calls this company" not in result.notes


def test_the_derived_vertical_flag_cannot_be_set_from_stored_json(session):
    """It is a fact about what the copy says, not a knob. A campaign whose copy
    names a vertical while its config says none is required would drift
    silently, exactly where it matters most."""

    from mazz.pipeline.qualify import qualification_config_from_json

    with pytest.raises(ValueError, match="unknown knobs"):
        qualification_config_from_json({"vertical_required": False})
