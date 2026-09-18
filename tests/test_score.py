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
from mazz.models import Campaign, Lead, LeadStatus, ResearchCache, Workspace
from mazz.pipeline.queue import JobContext, enqueue_job
from mazz.pipeline.score import (
    DEFAULT_SCORING,
    UNCONFIRMED_DATED_CONTENT_KIND,
    UNCONFIRMED_JOB_POSTING_KIND,
    RoleFit,
    ScoringConfig,
    SignalTier,
    classify_role_fit,
    config_for_lead,
    facts_for_lead,
    handle_score,
    published_at_of,
    recency_multiplier,
    score_fact,
    score_facts,
    score_lead,
    scoring_config_from_json,
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


# ==========================================================================
# Unconfirmed provenance: the two sentinel kinds from ingest
# ==========================================================================
#
# `integrations.company` refuses to admit a low-confidence careers-page or
# dated-content candidate at its nominal kind and emits a sentinel instead.
# These tests pin down both halves of the deal: it stays non-qualifying (the
# safety property, which held before the scorer knew the kind existed), and the
# reviewer is told what actually happened (the readability fix).


def test_an_unconfirmed_job_candidate_can_never_qualify_on_its_own():
    """A misidentified filter control must not manufacture an angle.

    This is the guardrail-gap case from CLAUDE.md seen from the scoring side:
    verification cannot catch a false fact, because a fake req is exactly as
    traceable as a real one once it is cached. So the scorer refuses it here.
    """

    unconfirmed = make_fact(
        UNCONFIRMED_JOB_POSTING_KIND,
        "Revenue Operations — see all departments",
        published=NOW.date().isoformat(),
    )
    result = score_facts([unconfirmed], now=NOW)
    scored = result.scored[0]

    assert scored.tier is SignalTier.WEAK
    assert scored.score < DEFAULT_SCORING.threshold
    assert result.has_angle is False
    assert result.lead_status is LeadStatus.NO_ANGLE
    # ...even though its text would score STRONG at the confirmed kind.
    confirmed = make_fact(
        FactKind.JOB_POSTING.value,
        "Revenue Operations — see all departments",
        published=NOW.date().isoformat(),
    )
    assert score_facts([confirmed], now=NOW).has_angle is True


def test_an_unconfirmed_dated_item_can_never_qualify_on_its_own():
    """One tier down, same rule. A fresh blog_post clears 40.0 alone; a block
    we could not confirm is dated content must not."""

    unconfirmed = make_fact(
        UNCONFIRMED_DATED_CONTENT_KIND,
        "Shipped: lane-level benchmark reporting.",
        published=NOW.date().isoformat(),
    )
    result = score_facts([unconfirmed], now=NOW)

    assert result.scored[0].tier is SignalTier.WEAK
    assert result.has_angle is False
    assert score_facts(
        [make_fact(FactKind.BLOG_POST.value, "Shipped: lane-level benchmark reporting.", published=NOW.date().isoformat())],
        now=NOW,
    ).has_angle is True


@pytest.mark.parametrize(
    "kind, expected_phrase",
    [
        (UNCONFIRMED_JOB_POSTING_KIND, "could not confirm is a real req"),
        (UNCONFIRMED_DATED_CONTENT_KIND, "date we could not confirm"),
    ],
)
def test_the_reviewer_is_told_we_could_not_confirm_it_not_that_we_are_out_of_date(
    kind, expected_phrase
):
    """`reason` is what the review UI renders. "unknown fact kind" reads as a
    stale scorer; the truth is that we looked and could not stand behind it."""

    fact = Fact(
        id=f"{kind}:readability",
        kind=kind,
        content="Marketing — open roles",
        source_url="https://example.test/careers",
        retrieved_at=NOW,
        metadata={"published_at": None, "identification_method": "href_keyword"},
    )
    scored = score_fact(fact, now=NOW, config=DEFAULT_SCORING)

    assert expected_phrase in scored.reason
    assert "unknown fact kind" not in scored.reason
    assert "href_keyword" in scored.reason, "say HOW it was identified, not just that it failed"
    assert scored.tier is SignalTier.WEAK


def test_a_medium_confidence_identification_is_admitted_and_says_so():
    """Admitted at full weight, and labelled. Nobody has measured how much less
    often a title-vocabulary match is a real req than an ATS-host match, so
    there is no multiplier here to justify — only a note for the reviewer."""

    fact = Fact(
        id="job_posting:medium",
        kind=FactKind.JOB_POSTING.value,
        content="Revenue Operations Manager - own the go-to-market data stack.",
        source_url="https://example.test/careers/revops",
        retrieved_at=NOW,
        metadata={"published_at": NOW.date().isoformat(), "identification_method": "title_signal", "confidence": "medium"},
    )
    scored = score_fact(fact, now=NOW, config=DEFAULT_SCORING)

    assert scored.tier is SignalTier.STRONG
    assert scored.score == DEFAULT_SCORING.job_posting_supported
    assert "medium-confidence" in scored.reason
    assert "title_signal" in scored.reason


def test_the_sentinel_kind_strings_still_match_the_producer():
    """The strings are mirrored, not imported — the scorer reads the cache and
    must not depend on which adapter filled it. This is the guard against that
    mirror drifting: a rename in ingest fails here instead of silently
    reverting those facts to the generic unknown-kind message."""

    from mazz.integrations.company import (
        UNCONFIRMED_DATED_CONTENT_KIND as PRODUCED_DATED,
        UNCONFIRMED_JOB_POSTING_KIND as PRODUCED_JOB,
    )

    assert UNCONFIRMED_JOB_POSTING_KIND == PRODUCED_JOB
    assert UNCONFIRMED_DATED_CONTENT_KIND == PRODUCED_DATED


# ==========================================================================
# The per-campaign scoring config
# ==========================================================================
#
# `tests/test_workspaces.py::_as_scoring_config` is the contract these satisfy;
# it was written before this loader existed. Everything it asserts is asserted
# here too, against `scoring_config_from_json` itself.

AGENCY_CONFIG = {
    "threshold": 45.0,
    "supported_role_terms": ["paid media", "performance marketing", "head of growth"],
    "off_target_role_terms": ["welder", "line cook"],
}


def test_a_stored_config_rebuilds_with_frozensets_not_lists():
    """JSON has no frozenset, and `classify_role_fit` iterates the field."""

    config = scoring_config_from_json(AGENCY_CONFIG)

    assert isinstance(config.supported_role_terms, frozenset)
    assert isinstance(config.off_target_role_terms, frozenset)
    assert "paid media" in config.supported_role_terms
    assert config.threshold == 45.0


def test_a_stored_config_is_a_partial_override_not_a_replacement():
    """A row naming only `threshold` keeps every shipped weight. Otherwise
    setting one knob would silently reset the other twenty."""

    config = scoring_config_from_json({"threshold": 55.0})

    assert config.threshold == 55.0
    assert config.job_posting_supported == DEFAULT_SCORING.job_posting_supported
    assert config.funding == DEFAULT_SCORING.funding
    assert config.supported_role_terms == DEFAULT_SCORING.supported_role_terms


def test_an_empty_config_means_the_shipped_defaults():
    assert scoring_config_from_json({}) == DEFAULT_SCORING


def test_guardrail_2_has_the_last_word_over_a_stored_config():
    """Funding alone can never clear the threshold, whoever wrote the row.
    `ScoringConfig.__post_init__` is not caught here — it fails the job."""

    with pytest.raises(ValueError, match="funding must score below the threshold"):
        scoring_config_from_json({"threshold": 10.0, "funding": 60.0})


def test_an_unknown_knob_is_refused_rather_than_ignored():
    """A typo'd key would otherwise read as a setting that is applied and does
    nothing — for a client whose whole engagement is that vocabulary."""

    with pytest.raises(ValueError, match="unknown scoring knobs"):
        scoring_config_from_json({"treshold": 45.0})


def test_a_config_that_is_not_an_object_is_refused():
    """The database has a CHECK for this. This is the same refusal one layer
    up, because falling back to the SaaS defaults is the failure to avoid."""

    with pytest.raises(TypeError):
        scoring_config_from_json([1, 2])  # type: ignore[arg-type]


def test_role_terms_must_be_a_list_not_one_string():
    """`frozenset("paid media")` is a set of eleven characters, and "a" matches
    almost every req ever written. Refuse it rather than score on it."""

    with pytest.raises(ValueError, match="array of role terms"):
        scoring_config_from_json({"supported_role_terms": "paid media"})


def test_an_empty_or_non_string_role_term_is_refused():
    with pytest.raises(ValueError, match="non-empty string"):
        scoring_config_from_json({"supported_role_terms": ["paid media", ""]})
    with pytest.raises(ValueError, match="non-empty string"):
        scoring_config_from_json({"supported_role_terms": ["paid media", 42]})


def test_role_terms_are_normalised_so_a_client_capital_still_matches():
    """Terms are matched against lower-cased content, so "Paid Media" stored by
    a client would otherwise never fire — a config that reads as applied and
    silently is not."""

    config = scoring_config_from_json({"supported_role_terms": ["  Paid Media  "]})

    assert config.supported_role_terms == frozenset({"paid media"})
    fit, matched = classify_role_fit("Hiring a Paid Media Manager", config)
    assert fit is RoleFit.SUPPORTED
    assert matched == "paid media"


# --------------------------------------------------------------------------
# Loading it from the lead's campaign
# --------------------------------------------------------------------------


def _campaign(session, *, scoring_config: dict, slug: str = "acme-digital") -> Campaign:
    workspace = Workspace(slug=slug, name=slug.replace("-", " ").title())
    session.add(workspace)
    session.flush()
    campaign = Campaign(
        workspace_id=workspace.id,
        name="Q4 push",
        segment="agency-smb",
        scoring_config=scoring_config,
    )
    session.add(campaign)
    session.flush()
    return campaign


def _lead_with_facts(session, facts, *, email: str, campaign: Campaign | None = None) -> Lead:
    domain = email.split("@", 1)[1]
    session.add(
        ResearchCache(
            company_domain=domain,
            company_name="Test Co",
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=NOW,
        )
    )
    lead = Lead(
        full_name="Test Person",
        email=email,
        company_name="Test Co",
        company_domain=domain,
        status=LeadStatus.RESEARCHING,
        campaign_id=campaign.id if campaign else None,
    )
    session.add(lead)
    session.flush()
    return lead


#: A req the SHIPPED vocabulary calls off-target and an agency's vocabulary
#: calls the whole reason to reach out. This one fact is the entire argument
#: for per-campaign config.
RECRUITER_REQ = "Hiring a Recruiter to place candidates with our agency clients."


def test_a_lead_with_no_campaign_scores_exactly_as_before(session):
    """Every lead in the database predates campaigns. NULL is a steady state."""

    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat())],
        email="nocampaign@no-campaign.example",
    )

    assert lead.campaign_id is None
    assert config_for_lead(session, lead) == DEFAULT_SCORING
    assert score_lead(session, lead, now=NOW).has_angle is False


def test_an_empty_campaign_config_means_the_shipped_defaults(session):
    campaign = _campaign(session, scoring_config={}, slug="empty-config")
    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat())],
        email="empty@empty-config.example",
        campaign=campaign,
    )

    assert config_for_lead(session, lead) == DEFAULT_SCORING


def test_the_campaigns_vocabulary_decides_whether_the_lead_is_worth_an_email(session):
    """The whole point, end to end through the database.

    The same cached req is `no_angle` under the shipped SaaS vocabulary and a
    STRONG angle under the agency's. Nothing about the research changed — only
    whose ICP we are scoring against.
    """

    req = make_fact(FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat())

    plain = _lead_with_facts(session, [req], email="plain@staffing-co.example")
    assert score_lead(session, plain, now=NOW).has_angle is False

    campaign = _campaign(
        session,
        scoring_config={"supported_role_terms": ["recruiter", "talent partner"]},
        slug="staffing-agency",
    )
    targeted = _lead_with_facts(
        session, [req], email="targeted@staffing-agency.example", campaign=campaign
    )

    result = score_lead(session, targeted, now=NOW)
    assert result.has_angle is True
    assert result.top.tier is SignalTier.STRONG
    assert result.top.role_fit is RoleFit.SUPPORTED


def test_a_campaign_can_also_raise_the_bar(session):
    """It cuts both ways: a client who only wants job reqs can price a fresh
    blog post out of qualifying. Fewer drafts is a legitimate setting."""

    post = make_fact(
        FactKind.BLOG_POST.value, "Notes from our offsite.", published=NOW.date().isoformat()
    )

    assert score_facts([post], now=NOW).has_angle is True

    campaign = _campaign(session, scoring_config={"threshold": 50.0}, slug="reqs-only")
    lead = _lead_with_facts(session, [post], email="strict@reqs-only.example", campaign=campaign)

    result = score_lead(session, lead, now=NOW)
    assert result.has_angle is False
    assert result.threshold == 50.0


def test_a_broken_campaign_config_fails_the_job_rather_than_reverting(session):
    """Fail closed. A silent fallback to the shipped SaaS vocabulary would move
    this client's `no_angle` rate for reasons nobody can see — and `no_angle`
    is the number the product is judged on."""

    campaign = _campaign(
        session, scoring_config={"threshold": 10.0, "funding": 60.0}, slug="broken-config"
    )
    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.FUNDING.value, "Acme raises $30M.", published=NOW.date().isoformat())],
        email="broken@broken-config.example",
        campaign=campaign,
    )

    with pytest.raises(ValueError, match="funding must score below the threshold"):
        config_for_lead(session, lead)
    with pytest.raises(ValueError, match="funding must score below the threshold"):
        score_lead(session, lead, now=NOW)


def test_an_explicit_config_still_wins_over_the_campaigns(session):
    """The `config=` argument is for asking "what would this look like under X".
    It must not be what the pipeline passes — see `draft.draft_for_lead`."""

    campaign = _campaign(
        session, scoring_config={"supported_role_terms": ["recruiter"]}, slug="explicit-override"
    )
    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat())],
        email="explicit@explicit-override.example",
        campaign=campaign,
    )

    assert score_lead(session, lead, now=NOW).has_angle is True
    assert score_lead(session, lead, now=NOW, config=DEFAULT_SCORING).has_angle is False


def test_handle_score_honours_the_campaign_config(session):
    """The wiring this task exists to fix: `handle_score` used to call
    `score_lead(session, lead)` and silently take DEFAULT_SCORING, so a
    campaign's vocabulary never reached the pipeline at all."""

    campaign = _campaign(
        session, scoring_config={"supported_role_terms": ["recruiter"]}, slug="queued-agency"
    )
    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat())],
        email="queued@queued-agency.example",
        campaign=campaign,
    )
    job = enqueue_job(session, lead.id, "score")[0]

    handle_score(JobContext(session=session, job=job, lead=lead, handlers={}))

    assert lead.status is not LeadStatus.NO_ANGLE, (
        "the shipped vocabulary calls this req off-target; the campaign's does not, "
        "and the campaign's is the one that governs"
    )


def test_handle_score_still_reaches_no_angle_for_a_lead_with_no_campaign(session):
    """The negative half of the same wiring: nothing about this change may make
    a weak lead look worked."""

    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.FUNDING.value, "Acme raises $30M.", published=NOW.date().isoformat())],
        email="weak@no-campaign-weak.example",
    )
    job = enqueue_job(session, lead.id, "score")[0]

    handle_score(JobContext(session=session, job=job, lead=lead, handlers={}))

    assert lead.status is LeadStatus.NO_ANGLE


# ==========================================================================
# A narrowed vocabulary has to actually narrow
# ==========================================================================
#
# The bug these cover: `classify_role_fit` returns UNCLASSIFIED for a req that
# matches neither list, and UNCLASSIFIED used to score 55.0 at STRONG
# unconditionally — fifteen points clear of the 40.0 default threshold. So a
# campaign that narrowed `supported_role_terms` to its client's ICP still
# qualified nearly every req at every company, at a slightly lower score. The
# config read as applied and barely moved a verdict, which is the one thing the
# per-campaign decision (docs/DECISIONS.md) exists to buy.
#
# The end-to-end test in tests/test_routing.py did not catch it because its
# campaign specifies `off_target_role_terms` AND raises the threshold to 45 —
# the fully-specified, favourable case. A partially-specified config is the
# common one, and it is the one asserted here.

#: Matches NEITHER vocabulary: not in the shipped SaaS list, not in a staffing
#: agency's. It is `UNCLASSIFIED` under both, which is what isolates the rule
#: under test from any change in `classify_role_fit`.
UNCLASSIFIED_REQ = "Hiring a Paid Media Manager to own our ecommerce accounts."

#: The probe's own req. Under the SHIPPED vocabulary this is SUPPORTED
#: ("revenue operations"); under a staffing agency's it matches nothing. It is
#: the case that made the bug visible, kept verbatim at the bottom of this file.
PROBE_REQ = "Hiring a Revenue Operations Manager"

#: Names nothing but the client's ICP. No off-target list, no threshold — the
#: config an operator actually writes on day one.
NARROWED_ONLY = {"supported_role_terms": ["recruiter", "talent acquisition"]}


def test_a_narrowed_vocabulary_rejects_a_req_outside_it(session):
    """Direction one. The req the operator did NOT ask for is now `no_angle`.

    Same fact, same freshness, same 40.0 threshold: the only thing that changed
    is that this campaign stated its ICP.
    """

    req = make_fact(
        FactKind.JOB_POSTING.value, UNCLASSIFIED_REQ, published=NOW.date().isoformat()
    )

    # Shipped defaults: unrecognised req, still hiring, still STRONG. Unchanged.
    shipped = score_facts([req], now=NOW)
    assert shipped.scored[0].tier is SignalTier.STRONG
    assert shipped.scored[0].score == DEFAULT_SCORING.job_posting_unclassified
    assert shipped.has_angle is True

    campaign = _campaign(session, scoring_config=NARROWED_ONLY, slug="narrowed-only")
    lead = _lead_with_facts(
        session, [req], email="rev@narrowed-only.example", campaign=campaign
    )

    result = score_lead(session, lead, now=NOW)

    assert result.has_angle is False, (
        "a campaign whose ICP is recruiting roles must not be handed a RevOps req "
        "just because neither vocabulary named it"
    )
    assert result.scored[0].tier is SignalTier.WEAK
    assert result.scored[0].role_fit is RoleFit.UNCLASSIFIED
    assert result.scored[0].score == DEFAULT_SCORING.job_posting_off_target
    assert "vocabulary" in result.scored[0].reason


def test_a_narrowed_vocabulary_still_accepts_the_req_it_named(session):
    """Direction two, and the half that stops this becoming "reject everything".

    The same campaign, the same threshold, the req it exists to find: STRONG.
    """

    req = make_fact(
        FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat()
    )

    campaign = _campaign(session, scoring_config=NARROWED_ONLY, slug="narrowed-accepts")
    lead = _lead_with_facts(
        session, [req], email="rec@narrowed-accepts.example", campaign=campaign
    )

    result = score_lead(session, lead, now=NOW)

    assert result.has_angle is True
    assert result.top.tier is SignalTier.STRONG
    assert result.top.role_fit is RoleFit.SUPPORTED


def test_the_campaign_vocabulary_is_scored_at_the_full_supported_weight(session):
    """The narrowed vocabulary buys the FULL supported weight, not a discount.

    `scripts/seed_demo.py` section 10 shows this with a scraped, undated req and
    prints 41.25, which invites the reading that routing costs the req some of
    its weight. It does not. The only term between 75.0 and 41.25 is the undated
    recency discount, and an unrouted lead pays exactly the same one — asserted
    here as arithmetic so a change to either number fails the build instead of
    silently moving what the demo claims, or what a client's `no_angle` rate is.
    """

    dated = make_fact(
        FactKind.JOB_POSTING.value, RECRUITER_REQ, published=NOW.date().isoformat()
    )
    undated = Fact(
        id="job_posting:undated-recruiter",
        kind=FactKind.JOB_POSTING.value,
        content=RECRUITER_REQ,
        source_url="https://x.example/careers/recruiter",
        retrieved_at=NOW,  # scraped this second; NOT a publication date
        metadata={},
    )
    assert published_at_of(undated) is None

    config = scoring_config_from_json(NARROWED_ONLY)

    # Under the campaign's vocabulary: full shipped weight, undiscounted.
    assert score_fact(dated, now=NOW, config=config).score == (
        DEFAULT_SCORING.job_posting_supported
    )

    # The demo's number, decomposed: base x undated, nothing else.
    scored = score_fact(undated, now=NOW, config=config)
    assert scored.tier is SignalTier.STRONG
    assert scored.role_fit is RoleFit.SUPPORTED
    assert scored.score == round(
        DEFAULT_SCORING.job_posting_supported * DEFAULT_SCORING.undated_multiplier, 2
    ) == 41.25

    # ...and the undated discount is not something routing introduced: the same
    # fact under the shipped vocabulary pays it too, off a smaller base.
    unrouted = score_fact(undated, now=NOW)
    assert unrouted.role_fit is RoleFit.OFF_TARGET  # "recruiter" is off-target there
    assert unrouted.score == round(
        DEFAULT_SCORING.job_posting_off_target * DEFAULT_SCORING.undated_multiplier, 2
    )


def test_an_unrouted_lead_is_untouched_by_the_narrowing_rule(session):
    """`campaign_id IS NULL` is a steady state, not a migration to finish, and
    a lot of committed behaviour rests on it. An unrecognised req is still
    55.0 at STRONG for those leads, exactly as before."""

    lead = _lead_with_facts(
        session,
        [make_fact(FactKind.JOB_POSTING.value, UNCLASSIFIED_REQ, published=NOW.date().isoformat())],
        email="nobody@unrouted-unclassified.example",
    )

    assert lead.campaign_id is None
    result = score_lead(session, lead, now=NOW)

    assert DEFAULT_SCORING.treats_unclassified_as_off_target is False
    assert result.has_angle is True
    assert result.top.tier is SignalTier.STRONG
    assert result.top.score == 55.0


def test_a_campaign_that_narrows_only_the_off_target_list_keeps_the_old_reading(session):
    """The rule keys on the SUPPORTED vocabulary, because that is the one that
    states an ICP. A campaign that only adds off-target terms has not said what
    it sells to, so "we do not recognise this role" still means what it used to."""

    config = scoring_config_from_json({"off_target_role_terms": ["welder"]})

    assert config.vocabulary_is_customised is False
    assert config.treats_unclassified_as_off_target is False
    scored = score_fact(
        make_fact(FactKind.JOB_POSTING.value, UNCLASSIFIED_REQ, published=NOW.date().isoformat()),
        now=NOW,
        config=config,
    )
    assert scored.tier is SignalTier.STRONG


def test_a_campaign_storing_the_shipped_vocabulary_has_not_narrowed_anything():
    """Customisation is derived by comparison, not by a flag that can drift.
    A row echoing the shipped list back is the shipped list."""

    config = scoring_config_from_json(
        {"supported_role_terms": sorted(DEFAULT_SCORING.supported_role_terms)}
    )

    assert config.supported_role_terms == DEFAULT_SCORING.supported_role_terms
    assert config.treats_unclassified_as_off_target is False


def test_a_client_can_opt_back_into_taking_any_hiring_signal():
    """The escape hatch, written down rather than inherited. An agency-client
    who will work any req can say so — and it takes an explicit line."""

    config = scoring_config_from_json(
        {**NARROWED_ONLY, "unclassified_is_off_target": False}
    )

    assert config.vocabulary_is_customised is True
    assert config.treats_unclassified_as_off_target is False
    scored = score_fact(
        make_fact(FactKind.JOB_POSTING.value, UNCLASSIFIED_REQ, published=NOW.date().isoformat()),
        now=NOW,
        config=config,
    )
    assert scored.tier is SignalTier.STRONG
    assert scored.score == DEFAULT_SCORING.job_posting_unclassified


def test_a_vocabulary_plus_an_unclassified_weight_is_refused_as_contradictory():
    """Setting both states two incompatible things, and one of them would be a
    knob that reads as applied and is never consulted — the same failure as a
    typo'd key, refused the same way."""

    with pytest.raises(ValueError, match="never read"):
        scoring_config_from_json({**NARROWED_ONLY, "job_posting_unclassified": 70.0})

    # Saying which you mean resolves it, in either direction.
    loose = scoring_config_from_json(
        {**NARROWED_ONLY, "job_posting_unclassified": 70.0, "unclassified_is_off_target": False}
    )
    assert loose.job_posting_unclassified == 70.0
    tight = scoring_config_from_json(
        {**NARROWED_ONLY, "job_posting_unclassified": 70.0, "unclassified_is_off_target": True}
    )
    assert tight.treats_unclassified_as_off_target is True


def test_the_narrowing_flag_must_be_a_real_boolean():
    """JSON's "false" is truthy in Python, and this flag decides whether a whole
    class of reqs can reach a draft."""

    with pytest.raises(ValueError, match="must be true, false or absent"):
        scoring_config_from_json({"unclassified_is_off_target": "false"})


def test_funding_still_cannot_clear_under_a_narrowed_campaign_config():
    """Guardrail 2 is orthogonal to all of this and stays orthogonal."""

    with pytest.raises(ValueError, match="funding must score below the threshold"):
        scoring_config_from_json({**NARROWED_ONLY, "threshold": 10.0, "funding": 60.0})

    config = scoring_config_from_json({**NARROWED_ONLY, "funding": 39.0})
    fresh = make_fact(
        FactKind.FUNDING.value, "Acme raises $450M.", published=NOW.date().isoformat()
    )
    assert score_facts([fresh], now=NOW, config=config).has_angle is False


def test_narrowing_is_what_flips_the_verdict_not_the_threshold(session):
    """The probe's exact measurement, as a regression: the ONLY difference
    between these two configs is the vocabulary. Same threshold, same fact."""

    req = make_fact(
        FactKind.JOB_POSTING.value, PROBE_REQ, published=NOW.date().isoformat()
    )
    loose = scoring_config_from_json({})
    narrowed = scoring_config_from_json(NARROWED_ONLY)

    assert loose.threshold == narrowed.threshold == 40.0
    assert score_facts([req], now=NOW, config=loose).has_angle is True
    assert score_facts([req], now=NOW, config=narrowed).has_angle is False
