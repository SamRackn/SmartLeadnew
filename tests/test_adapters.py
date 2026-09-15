"""Enrichment adapter tests: LinkedIn (URL-only) and company site.

Two properties here are load-bearing for the rest of the pipeline:

* **Stable fact ids** - drafts cite ids and verification resolves them. Tests
  assert the same material produces the same id across adapter instances and
  across URL spellings.
* **A failure is never an empty result** - `no_angle` is a real verdict the
  client acts on, so a swallowed provider error must not be able to forge it.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mazz.integrations.base import (
    Fact,
    FactKind,
    FetchResult,
    FetchStatus,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
    call_with_retry,
    make_fact_id,
    stable_fact_id,
)
from mazz.integrations.company import (
    InvalidDomain,
    StubCompanyAdapter,
    get_company_adapter,
    normalize_domain,
)
from mazz.integrations.linkedin import (
    InvalidProfileURL,
    StubLinkedInAdapter,
    canonical_profile_url,
    get_linkedin_adapter,
    profile_slug,
)

FIXTURES = Path(__file__).parent / "fixtures"


class SleepSpy:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def li() -> StubLinkedInAdapter:
    return StubLinkedInAdapter(fixtures_dir=FIXTURES)


@pytest.fixture
def co() -> StubCompanyAdapter:
    return StubCompanyAdapter(fixtures_dir=FIXTURES)


# =====================================================================
# LinkedIn: URL only, never search
# =====================================================================


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://www.linkedin.com/search/results/people/?keywords=dana",
        "https://www.linkedin.com/company/northwind-logistics",
        "https://www.linkedin.com/pub/dir/Dana/Whitfield",
        "https://example.com/in/dana-whitfield",
        "dana whitfield northwind",
        "",
    ],
)
def test_rejects_anything_that_is_not_a_profile_url(li: StubLinkedInAdapter, bad_url: str):
    """Search and discovery are non-goals: we are always handed the profile link,
    and URL-only lookup is cheaper and lower ToS risk."""
    with pytest.raises(InvalidProfileURL):
        li.fetch_profile(bad_url)


@pytest.mark.parametrize(
    "spelling",
    [
        "https://www.linkedin.com/in/dana-whitfield",
        "https://www.linkedin.com/in/dana-whitfield/",
        "http://linkedin.com/in/Dana-Whitfield",
        "https://uk.linkedin.com/in/dana-whitfield?utm_source=crm",
        "https://www.linkedin.com/in/dana-whitfield#experience",
    ],
)
def test_url_spellings_canonicalize_to_one_target(li: StubLinkedInAdapter, spelling: str):
    assert canonical_profile_url(spelling) == "https://www.linkedin.com/in/dana-whitfield"
    assert profile_slug(spelling) == "dana-whitfield"
    assert li.fetch_profile(spelling).target == "https://www.linkedin.com/in/dana-whitfield"


def test_profile_facts_are_normalized_and_complete(li: StubLinkedInAdapter):
    result = li.fetch_profile(
        "https://www.linkedin.com/in/dana-whitfield", subject_email="dana@northwind-logistics.com"
    )

    assert result.status is FetchStatus.OK
    assert result.ok
    # All profile evidence uses the shared vocabulary's linkedin_profile kind;
    # the section lives in metadata so nothing scores as an unknown kind.
    assert {f.kind for f in result.facts} == {FactKind.LINKEDIN_PROFILE.value}
    assert {f.metadata["section"] for f in result.facts} == {"headline", "role", "about", "post"}
    for fact in result.facts:
        assert isinstance(fact, Fact)
        assert fact.id and fact.kind and fact.content.strip() and fact.source_url
        assert fact.retrieved_at.tzinfo is not None

    role = next(f for f in result.facts if f.metadata["section"] == "role")
    assert role.content == "VP Revenue Operations at Northwind Logistics (since 2024-03)"


def test_profile_facts_carry_subject_email_for_delete_by_person(li: StubLinkedInAdapter):
    """research_cache is company-scoped and is NOT cascade-deleted with a lead,
    so delete-by-person prunes facts by subject_email. Person facts must carry
    one or a deletion request silently misses them."""
    result = li.fetch_profile(
        "https://www.linkedin.com/in/dana-whitfield", subject_email="dana@northwind-logistics.com"
    )
    assert all(f.subject_email == "dana@northwind-logistics.com" for f in result.facts)


def test_company_facts_are_not_person_scoped(co: StubCompanyAdapter):
    result = co.fetch_company("northwind-logistics.com")
    assert all(f.subject_email is None for f in result.facts)


def test_no_vendor_shape_leaks_into_results(li: StubLinkedInAdapter):
    """Bright Data vs ScrapIn is undecided; nothing vendor-specific may escape."""
    payload = json.dumps(li.fetch_profile("https://www.linkedin.com/in/dana-whitfield").as_dict())
    for vendor_token in ("brightdata", "bright_data", "scrapin", "snapshot_id", "dataset_id"):
        assert vendor_token not in payload.lower()


# =====================================================================
# Stable ids
# =====================================================================


def test_fact_ids_are_stable_across_instances(li: StubLinkedInAdapter):
    first = StubLinkedInAdapter(fixtures_dir=FIXTURES).fetch_profile(
        "https://www.linkedin.com/in/dana-whitfield"
    )
    second = StubLinkedInAdapter(fixtures_dir=FIXTURES).fetch_profile(
        "https://uk.linkedin.com/in/DANA-WHITFIELD/?trk=nav"
    )
    assert [f.id for f in first.facts] == [f.id for f in second.facts]


def test_company_fact_ids_are_stable_across_domain_spellings(co: StubCompanyAdapter):
    a = co.fetch_company("northwind-logistics.com")
    b = co.fetch_company("https://WWW.Northwind-Logistics.com/careers?ref=crm")
    assert [f.id for f in a.facts] == [f.id for f in b.facts]


def test_fact_id_derives_from_durable_material_not_position():
    """Ids must survive the same item moving in the source list."""
    assert make_fact_id("job_posting", "https://x.com/careers", "job-revops") == make_fact_id(
        "job_posting", "https://X.com/careers ", " JOB-RevOps"
    )
    assert make_fact_id("job_posting", "https://x.com/careers", "job-revops") != make_fact_id(
        "job_posting", "https://x.com/careers", "job-sdr"
    )


def test_fact_ids_match_the_schemas_id_format():
    """Natural-key ids, but the same `kind:16hex` shape as schemas.stable_fact_id,
    so the two schemes are interchangeable to every consumer."""
    ours = make_fact_id("job_posting", "https://x.com/careers", "job-revops")
    theirs = stable_fact_id("job_posting", "https://x.com/careers", "body text")
    kind_a, hex_a = ours.split(":")
    kind_b, hex_b = theirs.split(":")
    assert kind_a == kind_b == "job_posting"
    assert len(hex_a) == len(hex_b) == 16


def test_fact_id_survives_a_copy_edit_to_the_source_text(co: StubCompanyAdapter, tmp_path: Path):
    """The reason we key on a natural key rather than a content hash: an already
    approved draft's citation must still resolve after the page is edited."""
    import json as _json

    original = _json.loads(
        (FIXTURES / "company" / "northwind-logistics.com.json").read_text(encoding="utf-8")
    )
    edited = _json.loads(_json.dumps(original))
    careers = next(p for p in edited["pages"] if p["kind"] == "careers")
    careers["items"][0]["text"] += " (Updated: now also owns forecasting.)"

    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "northwind-logistics.com.json").write_text(
        _json.dumps(edited), encoding="utf-8"
    )

    before = co.fetch_company("northwind-logistics.com").facts[1]
    after = StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company(
        "northwind-logistics.com"
    ).facts[1]
    assert before.content != after.content
    assert before.id == after.id


# =====================================================================
# Failure is never "nothing found"
# =====================================================================


def test_provider_failure_raises_and_never_returns_empty():
    sleep = SleepSpy()
    adapter = StubLinkedInAdapter(fixtures_dir=FIXTURES, sleep=sleep)
    with pytest.raises(ProviderUnavailable) as exc:
        adapter.fetch_profile("https://www.linkedin.com/in/outage-test")
    assert exc.value.status_code == 502
    assert exc.value.provider == "linkedin-stub"

    company = StubCompanyAdapter(fixtures_dir=FIXTURES, sleep=SleepSpy())
    with pytest.raises(ProviderUnavailable):
        company.fetch_company("outage-test.example")


def test_empty_and_not_found_are_distinct_from_each_other(li: StubLinkedInAdapter, co):
    empty = li.fetch_profile("https://www.linkedin.com/in/empty-profile")
    assert empty.status is FetchStatus.EMPTY
    assert empty.facts == ()
    assert empty.found_nothing and not empty.ok

    missing = li.fetch_profile("https://www.linkedin.com/in/no-such-person-anywhere")
    assert missing.status is FetchStatus.NOT_FOUND
    assert missing.facts == ()
    assert missing.found_nothing

    assert co.fetch_company("stonebridge-partners.com").status is FetchStatus.EMPTY
    assert co.fetch_company("no-such-company.example").status is FetchStatus.NOT_FOUND


def test_fetch_result_cannot_conflate_success_and_emptiness():
    fact = Fact(
        id="x:1",
        kind="blog_post",
        content="c",
        source_url="https://x.com",
        retrieved_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        FetchResult(provider="p", target="t", status=FetchStatus.OK, facts=())
    with pytest.raises(ValueError):
        FetchResult(provider="p", target="t", status=FetchStatus.NOT_FOUND, facts=(fact,))


def test_malformed_payload_is_a_loud_error(tmp_path: Path):
    (tmp_path / "linkedin").mkdir()
    (tmp_path / "linkedin" / "broken.json").write_text("{not json", encoding="utf-8")
    adapter = StubLinkedInAdapter(fixtures_dir=tmp_path)
    with pytest.raises(ProviderBadResponse):
        adapter.fetch_profile("https://www.linkedin.com/in/broken")


def test_bad_domain_is_an_input_error_not_a_provider_error(co: StubCompanyAdapter):
    for junk in ("", "   ", "not a domain", "http://"):
        with pytest.raises(InvalidDomain):
            co.fetch_company(junk)


# =====================================================================
# Bounded retry with backoff
# =====================================================================


def test_retries_are_bounded_with_exponential_backoff():
    sleep = SleepSpy()
    adapter = StubLinkedInAdapter(
        fixtures_dir=FIXTURES,
        retry_policy=RetryPolicy(attempts=3, base_delay=0.25, max_delay=4.0),
        sleep=sleep,
    )
    with pytest.raises(ProviderRateLimited) as exc:
        adapter.fetch_profile("https://www.linkedin.com/in/rate-limit-test")
    assert sleep.delays == [0.25, 0.5]
    assert exc.value.attempts == 3
    assert exc.value.status_code == 429


def test_non_retryable_errors_are_not_retried(tmp_path: Path):
    (tmp_path / "linkedin").mkdir()
    (tmp_path / "linkedin" / "no-auth.json").write_text(
        json.dumps({"__simulate__": "auth", "__status_code__": 401}), encoding="utf-8"
    )
    sleep = SleepSpy()
    adapter = StubLinkedInAdapter(fixtures_dir=tmp_path, sleep=sleep)
    with pytest.raises(ProviderAuthError) as exc:
        adapter.fetch_profile("https://www.linkedin.com/in/no-auth")
    assert sleep.delays == []
    assert exc.value.attempts == 1


def test_retry_surfaces_the_real_error_not_a_generic_one():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise ProviderUnavailable("carrier reset the connection", provider="p", status_code=503)

    sleep = SleepSpy()
    with pytest.raises(ProviderUnavailable, match="carrier reset the connection"):
        call_with_retry(flaky, policy=RetryPolicy(attempts=2, base_delay=0.1), sleep=sleep)
    assert attempts["n"] == 2
    assert sleep.delays == [0.1]


def test_backoff_is_capped():
    policy = RetryPolicy(attempts=8, base_delay=1.0, max_delay=4.0)
    assert [policy.delay_for(i) for i in range(6)] == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]


# =====================================================================
# Company adapter: domain keying and signal kinds
# =====================================================================


@pytest.mark.parametrize(
    "spelling",
    [
        "northwind-logistics.com",
        "www.northwind-logistics.com",
        "https://northwind-logistics.com",
        "https://www.Northwind-Logistics.com/careers",
        "http://northwind-logistics.com:8080/blog?ref=x#top",
        "  NORTHWIND-LOGISTICS.COM/  ",
        "dana@northwind-logistics.com",
    ],
)
def test_domain_normalization_is_the_shared_cache_key(spelling: str):
    assert normalize_domain(spelling) == "northwind-logistics.com"


def test_two_leads_at_one_domain_need_one_provider_call(co: StubCompanyAdapter):
    """The caching precondition: both leads' website fields collapse to one key,
    so one research_cache row and one paid call serve both. Pipeline tests assert
    `adapter.call_count == 1` after enriching both."""
    lead_one_website = "https://www.northwind-logistics.com"
    lead_two_website = "http://northwind-logistics.com/about"
    assert normalize_domain(lead_one_website) == normalize_domain(lead_two_website)

    assert co.call_count == 0
    first = co.fetch_company(lead_one_website)
    assert co.call_count == 1
    # A cache hit for lead two means the adapter is simply not called again.
    assert co.call_count == 1
    assert co.calls_to("fetch_company") == [("northwind-logistics.com",)]

    # Both leads' LinkedIn profiles exist separately - profile fetch is per person.
    li = StubLinkedInAdapter(fixtures_dir=FIXTURES)
    for url in (
        "https://www.linkedin.com/in/dana-whitfield",
        "https://www.linkedin.com/in/marcus-oyelaran",
    ):
        assert li.fetch_profile(url).ok
    assert li.call_count == 2
    assert first.ok


def test_strong_signal_fixture_yields_job_postings(co: StubCompanyAdapter):
    result = co.fetch_company("northwind-logistics.com")
    jobs = [f for f in result.facts if f.kind == FactKind.JOB_POSTING.value]
    assert len(jobs) == 3
    assert any("Revenue Operations Manager" in f.content for f in jobs)
    assert any("Sales Development Representative" in f.content for f in jobs)
    # Dated, so "recently" is decidable downstream.
    assert all(f.content.startswith("[2026-") for f in jobs)
    kinds = {f.kind for f in result.facts}
    assert FactKind.CHANGELOG.value in kinds and FactKind.BLOG_POST.value in kinds


def test_weak_signal_fixture_has_no_job_posting(co: StubCompanyAdapter):
    """Funding news is nearly worthless - every competitor's tool leads with it.
    This lead must reach the scorer with nothing better and end up no_angle."""
    result = co.fetch_company("paperkite.io")
    assert result.ok
    kinds = {f.kind for f in result.facts}
    assert FactKind.JOB_POSTING.value not in kinds
    assert kinds == {FactKind.HOMEPAGE.value, FactKind.FUNDING.value}


def test_unverifiable_claim_material_contains_no_headcount(co: StubCompanyAdapter):
    """The verification-fails-closed fixture: the material is genuinely vague, so
    a draft asserting a specific headcount cites nothing and must be held."""
    result = co.fetch_company("helio-medical.com")
    corpus = " ".join(f.content for f in result.facts).lower()
    assert "the team has grown a lot this year" in corpus
    assert "40" not in corpus
    assert "headcount" not in corpus
    li = StubLinkedInAdapter(fixtures_dir=FIXTURES)
    profile_corpus = " ".join(
        f.content for f in li.fetch_profile("https://www.linkedin.com/in/tom-braddock").facts
    ).lower()
    assert "40" not in profile_corpus


def test_fact_kind_outside_the_shared_vocabulary_is_rejected(tmp_path: Path):
    """An unknown kind would not raise downstream -- Fact.kind is a plain str --
    it would silently score as weak forever."""
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "typo.example.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "kind": "press",
                        "items": [{"key": "k", "text": "t", "fact_kind": "funding_news"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderBadResponse, match="shared FactKind vocabulary"):
        StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("typo.example")


def test_unknown_page_kind_is_rejected(tmp_path: Path):
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "weird.example.json").write_text(
        json.dumps({"pages": [{"kind": "pricing", "items": []}]}), encoding="utf-8"
    )
    with pytest.raises(ProviderBadResponse):
        StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("weird.example")


def test_item_without_key_is_rejected(tmp_path: Path):
    """No key means no stable id, which means a citation that breaks on re-scrape."""
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "keyless.example.json").write_text(
        json.dumps({"pages": [{"kind": "blog", "items": [{"text": "hello"}]}]}), encoding="utf-8"
    )
    with pytest.raises(ProviderBadResponse, match="stable"):
        StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("keyless.example")


# =====================================================================
# Phase 1 guarantees
# =====================================================================


def test_no_network_in_phase_one(monkeypatch, li: StubLinkedInAdapter, co: StubCompanyAdapter):
    def explode(*args, **kwargs):
        raise AssertionError("Phase 1 adapters must not open a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    assert li.fetch_profile("https://www.linkedin.com/in/dana-whitfield").ok
    assert co.fetch_company("northwind-logistics.com").ok


def test_factories_default_to_stubs_with_no_credentials(monkeypatch):
    monkeypatch.delenv("LINKEDIN_PROVIDER", raising=False)
    monkeypatch.delenv("COMPANY_SCRAPER_PROVIDER", raising=False)
    assert isinstance(get_linkedin_adapter(fixtures_dir=FIXTURES), StubLinkedInAdapter)
    assert isinstance(get_company_adapter(fixtures_dir=FIXTURES), StubCompanyAdapter)


def test_undecided_vendors_fail_loudly_rather_than_guessing():
    for vendor in ("brightdata", "scrapin"):
        with pytest.raises(NotImplementedError, match="open question|not implemented"):
            get_linkedin_adapter(provider=vendor)
    with pytest.raises(ValueError):
        get_linkedin_adapter(provider="mystery-vendor")


# =====================================================================
# The fixture manifest other agents build their tests on
# =====================================================================


def test_scenarios_manifest_matches_the_fixtures_on_disk():
    manifest = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
    co = StubCompanyAdapter(fixtures_dir=FIXTURES)
    li = StubLinkedInAdapter(fixtures_dir=FIXTURES)

    for scenario in manifest["scenarios"]:
        name = scenario["name"]
        expected = scenario["expected"]

        if expected.get("raises"):
            with pytest.raises(ProviderUnavailable):
                StubCompanyAdapter(fixtures_dir=FIXTURES, sleep=SleepSpy()).fetch_company(
                    scenario["domain"]
                )
            for url in scenario["profiles"]:
                with pytest.raises(ProviderUnavailable):
                    StubLinkedInAdapter(fixtures_dir=FIXTURES, sleep=SleepSpy()).fetch_profile(url)
            continue

        result = co.fetch_company(scenario["domain"])
        if "fetch_status" in expected:
            assert result.status.value == expected["fetch_status"], name
        if "has_job_posting" in expected:
            has_job = any(f.kind == FactKind.JOB_POSTING.value for f in result.facts)
            assert has_job is expected["has_job_posting"], name
        if "only_fact_kinds" in expected:
            company_kinds = {f.kind for f in result.facts}
            profile_kinds = set()
            for url in scenario["profiles"]:
                profile_kinds |= {f.kind for f in li.fetch_profile(url).facts}
            assert (company_kinds | profile_kinds) <= set(expected["only_fact_kinds"]), name
        if "absent_tokens" in expected:
            corpus = " ".join(f.content for f in result.facts).lower()
            for token in expected["absent_tokens"]:
                assert token.lower() not in corpus, f"{name}: {token!r} should be absent"

        for url in scenario["profiles"]:
            profile = li.fetch_profile(url)
            assert isinstance(profile, FetchResult), name
