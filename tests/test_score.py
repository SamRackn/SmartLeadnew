"""Signal scoring tests.

Keyed off `tests/fixtures/scenarios.json` rather than hard-coded fixture names:
that manifest is the integrations-engineer's statement of what each scenario is
FOR, and if it and this file ever disagree, one of us is wrong and the build
should say so.

The load-bearing assertion in here is the negative one. `weak_signal_only` must
reach `no_angle`, because the product's whole value proposition is that it
declines to write something when there is nothing to say.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from mazz.db import get_engine
from mazz.integrations.company import StubCompanyAdapter
from mazz.models import Lead, LeadStatus, ResearchCache
from mazz.pipeline.score import (
    DEFAULT_SCORING,
    RoleFit,
    ScoringConfig,
    SignalTier,
    classify_role_fit,
    facts_for_lead,
    published_at_of,
    recency_multiplier,
    score_facts,
    score_lead,
)
from mazz.schemas import Fact, FactKind

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = {s["name"]: s for s in json.loads((FIXTURES / "scenarios.json").read_text())["scenarios"]}

#: Fixed "now". Every fixture is dated around 2026-09-14, and scoring is
#: recency-driven, so a wall-clock `now` would make these tests rot.
NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def company_facts(domain: str) -> list[Fact]:
    return list(StubCompanyAdapter().fetch_company(domain).facts)


def make_fact(kind: str, content: str, *, published: str | None, url: str = "https://x.example/a") -> Fact:
    return Fact(
        id=f"{kind}:test{abs(hash((kind, content))) % 10**12:012d}",
        kind=kind,
        content=content,
        source_url=url,
        retrieved_at=NOW,
        metadata={"published_at": published},
    )


@pytest.fixture
def session():
    """A session in a transaction that is always rolled back.

    Leaves the dev database byte-identical afterwards, which matters because
    other agents are running against the same `mazz_dev`.
    """

    connection = get_engine().connect()
    transaction = connection.begin()
    db = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


# --------------------------------------------------------------------------
# The brief's ranking
# --------------------------------------------------------------------------


def test_strong_signal_scenario_has_an_angle_led_by_a_job_posting():
    scenario = SCENARIOS["strong_signal"]
    assert scenario["expected"]["has_job_posting"] is True

    result = score_facts(company_facts(scenario["domain"]), now=NOW)

    assert result.has_angle is True
    assert result.lead_status is LeadStatus.READY
    top = result.top
    assert top is not None
    assert top.fact.kind == FactKind.JOB_POSTING.value
    assert top.tier is SignalTier.STRONG
    assert top.role_fit is RoleFit.SUPPORTED
    # The RevOps req is the reason to call: budget approved, pain present-tense.
    assert "Revenue Operations Manager" in top.fact.content


def test_weak_signal_only_scenario_reaches_no_angle():
    """The one the client is paying for: funding alone buys nothing."""

    scenario = SCENARIOS["weak_signal_only"]
    assert scenario["expected"]["lead_status"] == "no_angle"

    facts = company_facts(scenario["domain"])
    assert any(f.kind == FactKind.FUNDING.value for f in facts), "fixture should carry funding news"

    result = score_facts(facts, now=NOW)

    assert result.has_angle is False
    assert result.qualifying == ()
    assert result.lead_status is LeadStatus.NO_ANGLE
    assert "no angle" in result.rationale
    # Every fact is still scored and kept — no_angle is a verdict, not amnesia.
    assert len(result.scored) == len(facts)


def test_funding_can_never_clear_the_threshold_however_fresh_or_large():
    huge_and_new = make_fact(
        FactKind.FUNDING.value,
        "Acme raises $450M Series D led by everyone you have ever heard of.",
        published=NOW.date().isoformat(),
    )
    result = score_facts([huge_and_new], now=NOW)

    scored = result.scored[0]
    assert scored.tier is SignalTier.WEAK
    assert scored.score < DEFAULT_SCORING.threshold
    assert result.has_angle is False


def test_scoring_config_refuses_a_threshold_funding_could_clear():
    """The one number that must not drift, guarded at construction time."""

    with pytest.raises(ValueError, match="funding must score below the threshold"):
        ScoringConfig(threshold=10.0)


def test_job_posting_beats_dated_content_beats_funding():
    job = make_fact(FactKind.JOB_POSTING.value, "Revenue Operations Manager - own the stack", published="2026-09-10")
    blog = make_fact(FactKind.BLOG_POST.value, "We changed how we onboard", published="2026-09-10")
    funding = make_fact(FactKind.FUNDING.value, "We raised a Series B", published="2026-09-10")

    result = score_facts([funding, blog, job], now=NOW)
    order = [s.fact.kind for s in result.scored]

    assert order == [FactKind.JOB_POSTING.value, FactKind.BLOG_POST.value, FactKind.FUNDING.value]


def test_off_target_job_posting_does_not_clear_on_its_own():
    """A backend engineer req is hiring. It is not a reason for THIS pitch."""

    req = make_fact(
        FactKind.JOB_POSTING.value,
        "Senior Backend Engineer - Go, Postgres, event-driven carrier integrations.",
        published="2026-09-10",
    )
    result = score_facts([req], now=NOW)

    assert result.scored[0].role_fit is RoleFit.OFF_TARGET
    assert result.scored[0].tier is SignalTier.WEAK
    assert result.has_angle is False


def test_role_fit_classification_prefers_the_supported_match():
    fit, term = classify_role_fit("Sales Engineer - partner with backend engineer teams")
    assert fit is RoleFit.SUPPORTED
    assert term == "sales engineer"


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------


def test_recent_content_clears_but_the_same_post_two_years_later_does_not():
    fresh = make_fact(FactKind.BLOG_POST.value, "Notes on our new pricing", published="2026-09-05")
    stale = make_fact(FactKind.BLOG_POST.value, "Notes on our old pricing", published="2024-09-05")

    fresh_result = score_facts([fresh], now=NOW)
    stale_result = score_facts([stale], now=NOW)

    assert fresh_result.has_angle is True
    assert stale_result.has_angle is False
    assert stale_result.scored[0].tier is SignalTier.MEDIUM  # still medium, just decayed


def test_undated_material_is_discounted_not_treated_as_fresh():
    """`retrieved_at` says when WE looked, never when THEY published."""

    undated = Fact(
        id="blog_post:undated",
        kind=FactKind.BLOG_POST.value,
        content="Our approach to logistics",
        source_url="https://x.example/blog",
        retrieved_at=NOW,  # scraped this second
        metadata={},
    )
    assert published_at_of(undated) is None

    scored = score_facts([undated], now=NOW).scored[0]
    assert scored.age_days is None
    assert scored.score < DEFAULT_SCORING.blog_post
    assert "undated" in scored.reason


def test_recency_multiplier_is_flat_inside_the_fresh_window_then_decays():
    inside, _ = recency_multiplier(NOW - timedelta(days=10), NOW)
    edge, _ = recency_multiplier(NOW - timedelta(days=30), NOW)
    outside, _ = recency_multiplier(NOW - timedelta(days=200), NOW)

    assert inside == 1.0
    assert edge == 1.0
    assert 0 < outside < 1.0
    assert outside >= DEFAULT_SCORING.decay_floor


def test_published_at_is_read_from_the_content_date_stamp_as_a_fallback():
    stamped = Fact(
        id="changelog:stamped",
        kind=FactKind.CHANGELOG.value,
        content="[2026-09-05] Shipped lane benchmarks",
        source_url="https://x.example/changelog",
        retrieved_at=NOW,
        metadata={},
    )
    assert published_at_of(stamped) == datetime(2026, 9, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Empty / unknown
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["legitimately_empty", "not_found"])
def test_scenarios_with_nothing_found_reach_no_angle(name):
    scenario = SCENARIOS[name]
    assert scenario["expected"]["lead_status"] == "no_angle"

    result = StubCompanyAdapter().fetch_company(scenario["domain"])
    assert result.found_nothing

    scored = score_facts(result.facts, now=NOW)
    assert scored.has_angle is False
    assert scored.lead_status is LeadStatus.NO_ANGLE
    assert "nothing to reach out about" in scored.rationale


def test_unknown_fact_kind_scores_weak_rather_than_crashing():
    exotic = make_fact("podcast_appearance", "Our CEO went on a podcast", published="2026-09-12")
    scored = score_facts([exotic], now=NOW).scored[0]

    assert scored.tier is SignalTier.WEAK
    assert scored.score < DEFAULT_SCORING.threshold
    assert "unknown fact kind" in scored.reason


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_scoring_is_deterministic_for_the_same_inputs():
    facts = company_facts("northwind-logistics.com")

    first = score_facts(facts, now=NOW)
    second = score_facts(list(reversed(facts)), now=NOW)

    assert [s.fact.id for s in first.scored] == [s.fact.id for s in second.scored]
    assert [s.score for s in first.scored] == [s.score for s in second.scored]
    assert first.rationale == second.rationale


# --------------------------------------------------------------------------
# Database glue
# --------------------------------------------------------------------------


def _seed(session, domain: str, *, email: str, company: str) -> Lead:
    facts = company_facts(domain)
    session.add(
        ResearchCache(
            company_domain=domain,
            company_name=company,
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=NOW,
        )
    )
    lead = Lead(
        full_name="Test Person",
        email=email,
        company_name=company,
        company_domain=domain,
        status=LeadStatus.RESEARCHING,
    )
    session.add(lead)
    session.flush()
    return lead


def test_score_lead_reads_facts_back_out_of_research_cache(session):
    lead = _seed(session, "northwind-logistics.com", email="score-strong@northwind-logistics.com", company="Northwind Logistics")

    loaded = facts_for_lead(session, lead)
    assert loaded, "facts must round-trip through research_cache JSONB"
    assert all(isinstance(f, Fact) for f in loaded)

    result = score_lead(session, lead, now=NOW)
    assert result.has_angle is True
    assert result.company_domain == "northwind-logistics.com"


def test_score_lead_on_the_weak_scenario_declines(session):
    lead = _seed(session, "paperkite.io", email="score-weak@paperkite.io", company="PaperKite")

    result = score_lead(session, lead, now=NOW)

    assert result.has_angle is False
    assert result.lead_status is LeadStatus.NO_ANGLE


def test_a_lead_with_no_cache_row_has_no_angle(session):
    lead = Lead(
        full_name="Nobody Cached",
        email="score-nocache@nowhere.example",
        company_domain="nowhere.example",
        status=LeadStatus.RESEARCHING,
    )
    session.add(lead)
    session.flush()

    result = score_lead(session, lead, now=NOW)

    assert facts_for_lead(session, lead) == []
    assert result.has_angle is False
