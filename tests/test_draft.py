"""Drafting tests.

Three things are being pinned down here:

1. **The refusal.** `weak_signal_only` must produce NO DRAFT ROW. Not an empty
   one, not a generic one — none. Asserted against the real `drafts` table,
   because "we didn't write a draft" is only true if nothing is in there.
2. **Real citations.** `strong_signal`'s draft must cite fact ids that actually
   exist in `research_cache`, since those ids are what the verifier and the
   review UI resolve later.
3. **The Phase 2 seam.** The structured shape and the compliance footer must be
   exactly what they will be once a Claude call replaces the template, so that
   swapping the composer is a one-line change and cannot drop the footer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from mazz.config import get_settings, reload_settings
from mazz.db import get_engine
from mazz.integrations.company import StubCompanyAdapter
from mazz.models import Draft, Lead, LeadStatus, ResearchCache, VerificationStatus
from mazz.pipeline.draft import (
    FOOTER_SEPARATOR,
    DraftContent,
    MissingSenderAddress,
    build_request,
    compose_template,
    default_composer,
    draft_for_lead,
    persist_draft,
    render_body,
    render_footer,
    split_body,
    unsubscribe_token,
)
from mazz.pipeline.compose_claude import parse_message
from mazz.pipeline.score import score_lead
from mazz.schemas import Fact

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = {s["name"]: s for s in json.loads((FIXTURES / "scenarios.json").read_text())["scenarios"]}

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _StubTextBlock:
    text: str
    type: str = "text"


@dataclass
class _StubMessage:
    """The minimum `parse_message` reads: a text block and a stop_reason.

    Deliberately not the SDK's `Message`. These tests are about what the parse
    path does to the model's STRING, so building a real Message would only add
    required fields that have nothing to do with the assertion.
    """

    text: str
    stop_reason: str = "end_turn"
    stop_details: object = None

    @property
    def content(self) -> list:
        return [_StubTextBlock(self.text)]
TEST_ADDRESS = "Mazz Outreach LLC, 1100 W Fulton Market, Suite 300, Chicago IL 60607"


@pytest.fixture(autouse=True)
def compliance_env(monkeypatch):
    """A configured sender address — without one, rendering refuses (by design)."""

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", TEST_ADDRESS)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
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


def seed(session, domain: str, *, email: str, company: str, notes: str | None = None) -> Lead:
    facts = list(StubCompanyAdapter().fetch_company(domain).facts)
    session.add(
        ResearchCache(
            company_domain=domain,
            company_name=company,
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=NOW,
        )
    )
    lead = Lead(
        full_name="Dana Whitfield",
        email=email,
        company_name=company,
        job_title="VP Revenue",
        company_domain=domain,
        notes=notes,
        status=LeadStatus.RESEARCHING,
    )
    session.add(lead)
    session.flush()
    return lead


def cached_fact_ids(session, domain: str) -> set[str]:
    row = session.get(ResearchCache, domain)
    return {Fact.model_validate(f).id for f in (row.facts or [])}


def draft_count(session, lead_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(Draft).where(Draft.lead_id == lead_id)
    ).scalar_one()


# --------------------------------------------------------------------------
# 1. The refusal
# --------------------------------------------------------------------------


def test_weak_signal_only_writes_no_draft_row_at_all(session):
    scenario = SCENARIOS["weak_signal_only"]
    lead = seed(session, scenario["domain"], email="draft-weak@paperkite.io", company="PaperKite")

    outcome = draft_for_lead(session, lead, now=NOW)
    result = persist_draft(session, lead, outcome)

    assert outcome.drafted is False
    assert outcome.content is None
    assert result is None
    assert draft_count(session, lead.id) == 0, "funding-only leads must not be force-drafted"
    assert lead.status is LeadStatus.NO_ANGLE
    assert scenario["expected"]["lead_status"] == lead.status.value


def test_composing_with_no_signals_is_a_bug_not_a_fallback():
    """There is no code path that writes generic copy from nothing."""

    lead = Lead(id=1, full_name="Nobody", email="n@x.example", company_name="X")
    request = build_request(lead, score_lead.__wrapped__ if False else _empty_result())

    with pytest.raises(ValueError, match="no signals"):
        compose_template(request)


def _empty_result():
    from mazz.pipeline.score import score_facts

    return score_facts([], now=NOW)


# --------------------------------------------------------------------------
# 2. Real citations
# --------------------------------------------------------------------------


def test_strong_signal_draft_cites_fact_ids_present_in_research_cache(session):
    scenario = SCENARIOS["strong_signal"]
    domain = scenario["domain"]
    lead = seed(session, domain, email="draft-strong@northwind-logistics.com", company="Northwind Logistics")

    outcome = draft_for_lead(session, lead, now=NOW)
    draft = persist_draft(session, lead, outcome)

    assert draft is not None
    assert draft.signals_used, "a draft with no citations is not grounded"

    available = cached_fact_ids(session, domain)
    assert set(draft.signals_used) <= available
    # And they are the stable `kind:16hex` ids, not list positions.
    assert all(":" in fid for fid in draft.signals_used)
    assert any(fid.startswith("job_posting:") for fid in draft.signals_used)

    assert draft.verification_status is VerificationStatus.PENDING
    assert lead.status is LeadStatus.RESEARCHING, "only verification may mark a lead ready"


def test_the_copy_quotes_its_signals_verbatim(session):
    lead = seed(session, "northwind-logistics.com", email="draft-quote@northwind-logistics.com", company="Northwind Logistics")
    outcome = draft_for_lead(session, lead, now=NOW)

    assert outcome.content is not None
    assert "Revenue Operations Manager" in outcome.content.subject
    assert "Own the go-to-market data stack" in outcome.content.body_text


def test_the_lead_notes_field_is_legitimate_grounding(session):
    lead = seed(
        session,
        "northwind-logistics.com",
        email="draft-notes@northwind-logistics.com",
        company="Northwind Logistics",
        notes="Met at Manifest; their SDR handoff is a mess.",
    )
    outcome = draft_for_lead(session, lead, now=NOW)

    assert outcome.body is not None
    assert "Met at Manifest" in outcome.body


def test_a_composer_may_not_cite_a_fact_that_did_not_clear_scoring(session):
    """A Phase 2 model claiming a citation it was never handed is a hard error."""

    lead = seed(session, "northwind-logistics.com", email="draft-stray@northwind-logistics.com", company="Northwind Logistics")

    def lying_composer(request):
        return DraftContent(
            subject="Hello",
            body_text="Hi there,",
            signals_used=["job_posting:deadbeefdeadbeef"],
        )

    with pytest.raises(ValueError, match="not offered to it"):
        draft_for_lead(session, lead, now=NOW, composer=lying_composer)


# --------------------------------------------------------------------------
# 3. The Phase 2 seam
# --------------------------------------------------------------------------


def test_draft_content_is_exactly_the_structured_output_schema():
    """Phase 2 hands this model to Claude as the output schema, unchanged."""

    assert set(DraftContent.model_fields) == {"subject", "body_text", "signals_used"}
    assert DraftContent.model_config["extra"] == "forbid"

    # It round-trips through JSON, which is how a structured-output call returns.
    content = DraftContent(subject="s", body_text="b", signals_used=["job_posting:abc"])
    assert DraftContent.model_validate_json(content.model_dump_json()) == content


def test_swapping_the_composer_changes_nothing_around_it(session):
    """The Phase 2 substitution, rehearsed with a fake composer."""

    lead = seed(session, "northwind-logistics.com", email="draft-seam@northwind-logistics.com", company="Northwind Logistics")
    result = score_lead(session, lead, now=NOW)
    top_id = result.qualifying[0].fact.id

    def pretend_claude(request):
        assert request.signals, "a composer is only ever called when there is an angle"
        return DraftContent(
            subject="Northwind Logistics",
            body_text="Hi Dana,",
            signals_used=[top_id],
        )

    outcome = draft_for_lead(session, lead, now=NOW, composer=pretend_claude)
    draft = persist_draft(session, lead, outcome)

    assert draft is not None
    assert draft.signals_used == [top_id]
    # The footer is applied outside the composer, so a model cannot drop it.
    assert TEST_ADDRESS in draft.body
    assert "Unsubscribe" in draft.body


def test_default_composer_is_the_template_and_needs_no_api_key():
    assert default_composer() is compose_template
    assert get_settings().anthropic_api_key == ""


def test_the_composer_only_sees_qualifying_signals(session):
    """Grounding expressed as a type: it cannot cite what it was not handed."""

    lead = seed(session, "northwind-logistics.com", email="draft-request@northwind-logistics.com", company="Northwind Logistics")
    result = score_lead(session, lead, now=NOW)
    request = build_request(lead, result)

    assert len(result.scored) > len(result.qualifying), "fixture has weak facts too"
    assert set(request.fact_ids()) == set(result.ids())
    assert all(s.tier.value != "weak" for s in request.signals)


# --------------------------------------------------------------------------
# CAN-SPAM footer
# --------------------------------------------------------------------------


def test_every_draft_carries_the_address_and_a_one_click_unsubscribe(session):
    lead = seed(session, "northwind-logistics.com", email="draft-footer@northwind-logistics.com", company="Northwind Logistics")
    outcome = draft_for_lead(session, lead, now=NOW)

    assert outcome.body is not None
    message, footer = split_body(outcome.body)
    assert footer is not None
    assert TEST_ADDRESS in footer
    assert unsubscribe_token(lead) in footer
    assert "Unsubscribe (one click)" in footer
    # The footer is appended, not woven in: the message stands on its own.
    assert TEST_ADDRESS not in message


def test_an_unset_sender_address_fails_loudly_instead_of_shipping_a_footerless_draft(
    session, monkeypatch
):
    lead = seed(session, "northwind-logistics.com", email="draft-noaddr@northwind-logistics.com", company="Northwind Logistics")

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "")
    reload_settings()

    with pytest.raises(MissingSenderAddress):
        draft_for_lead(session, lead, now=NOW)

    assert draft_count(session, lead.id) == 0


@pytest.mark.parametrize(
    "address",
    [
        "PLACEHOLDER - Mazz Ltd, 12 Rue Centrale, 75002 Paris, France",
        "TODO set the real address",
        "123 Main St, Anytown",
        "Your Company Address, City",
        "CHANGEME",
        "XXX, XXX",
    ],
)
def test_a_placeholder_sender_address_is_refused_like_an_empty_one(
    session, monkeypatch, address
):
    """A fabricated CAN-SPAM address is worse than a missing one.

    The first live run rendered `PLACEHOLDER - Mazz Ltd, 12 Rue Centrale, 75002
    Paris, France` into all four footers, because the guard only asked whether
    the value was empty. That footer looks compliant to a reviewer, to an
    auditor skimming for a footer, and to every test that asserts one is
    present — while the email carries a mailing address that is not the
    sender's. So a stand-in fails closed exactly as an unset value does.
    """

    lead = seed(
        session,
        "northwind-logistics.com",
        email=f"draft-placeholder-{abs(hash(address)) % 9999}@northwind-logistics.com",
        company="Northwind Logistics",
    )

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", address)
    reload_settings()

    with pytest.raises(MissingSenderAddress) as raised:
        draft_for_lead(session, lead, now=NOW)

    assert "placeholder" in str(raised.value).lower()
    assert draft_count(session, lead.id) == 0


def test_a_real_address_that_merely_contains_the_letters_is_not_refused(session, monkeypatch):
    """The check is whole-word. Refusing a real street because it contains
    'example' inside a longer word would be a new way to fail closed on
    legitimate configuration."""

    monkeypatch.setenv(
        "SENDER_POSTAL_ADDRESS", "Mazz Ltd, 36 Examplethorpe Lane, Toddington, Bedfordshire"
    )
    reload_settings()

    lead = seed(
        session,
        "northwind-logistics.com",
        email="draft-realaddr@northwind-logistics.com",
        company="Northwind Logistics",
    )
    outcome = draft_for_lead(session, lead, now=NOW)

    assert "Examplethorpe" in outcome.body


def test_a_double_encoded_escape_is_decoded_before_it_becomes_an_email():
    """One live draft of four came back with the six literal characters
    `\\u2014` where an em dash belonged: the model escaped its own backslash and
    structured output validated it, because a string with a backslash in it is a
    perfectly good string.

    Repaired in the composer's parse path rather than in the verifier, because
    the copy is broken whatever verification says about it — a reviewer could
    approve `\\u2014` straight into someone's inbox. (It also produced a bogus
    `number '2014'` flag: the digits inside the escape read as a year.)
    """

    message = _StubMessage(
        json.dumps(
            {
                "subject": "Your Revenue Operations Manager posting",
                "body_text": "Marcus \\u2014 the routing work, or the reporting work?",
                "signals_used": ["job_posting:abc"],
            }
        )
    )

    content = parse_message(message)

    assert content.body_text == "Marcus — the routing work, or the reporting work?"
    assert "\\u" not in content.body_text
    assert "2014" not in content.body_text


def test_decoding_leaves_ordinary_copy_exactly_as_written():
    message = _StubMessage(
        json.dumps(
            {
                "subject": "Your Revenue Operations Manager posting",
                "body_text": "Marcus — two openings, 50% remote.\n\nWorth a call?",
                "signals_used": [],
            }
        )
    )

    assert parse_message(message).body_text == (
        "Marcus — two openings, 50% remote.\n\nWorth a call?"
    )


def test_the_unsubscribe_token_is_stable_and_not_the_raw_address(session):
    lead = seed(session, "northwind-logistics.com", email="draft-token@northwind-logistics.com", company="Northwind Logistics")

    token = unsubscribe_token(lead)
    assert token == unsubscribe_token(lead), "re-drafting must not orphan a live link"
    assert lead.email not in token
    assert len(token) == 32
    assert unsubscribe_token("someone-else@example.com") != token


def test_render_body_separates_message_from_footer_reversibly():
    lead = Lead(id=1, full_name="Dana Whitfield", email="d@x.example")
    body = render_body("Hi Dana,\n\nSomething.", lead)

    message, footer = split_body(body)
    assert message == "Hi Dana,\n\nSomething."
    assert footer == render_footer(lead)
    assert FOOTER_SEPARATOR in body


# --------------------------------------------------------------------------
# Determinism — Phase 1 must be reproducible
# --------------------------------------------------------------------------


def test_drafting_the_same_lead_twice_produces_identical_copy(session):
    lead = seed(
        session,
        "northwind-logistics.com",
        email="draft-determinism@northwind-logistics.com",
        company="Northwind Logistics",
        notes="Met at Manifest.",
    )

    first = draft_for_lead(session, lead, now=NOW)
    second = draft_for_lead(session, lead, now=NOW)

    assert first.content == second.content
    assert first.body == second.body
    assert first.content is not None
    assert first.content.signals_used == second.content.signals_used


def test_fact_ordering_in_the_cache_does_not_change_the_draft(session):
    """Fact ids are stable, so ranking — and therefore copy — is order-independent."""

    lead = seed(session, "northwind-logistics.com", email="draft-order@northwind-logistics.com", company="Northwind Logistics")
    first = draft_for_lead(session, lead, now=NOW)

    row = session.get(ResearchCache, "northwind-logistics.com")
    row.facts = list(reversed(row.facts))
    session.flush()

    second = draft_for_lead(session, lead, now=NOW)

    assert first.content == second.content
