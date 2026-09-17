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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mazz.integrations import _html

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
# Research provenance at cache-ingest (CLAUDE.md known guardrail gap)
# =====================================================================


def test_low_confidence_job_item_is_demoted_not_dropped(tmp_path: Path):
    """A producer-declared low-confidence job candidate must not surface as
    JOB_POSTING (the STRONG tier) -- but it must also not vanish. It stays
    visible, under a distinct kind, with the reason recorded."""
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "lowconf.example.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "kind": "careers",
                        "url": "https://lowconf.example/careers",
                        "items": [
                            {
                                "key": "https://lowconf.example/careers/engineering",
                                "url": "https://lowconf.example/careers/engineering",
                                "text": "Engineering",
                                "identification_method": "url_shape_only",
                                "confidence": "low",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("lowconf.example")

    assert result.ok
    assert not any(f.kind == FactKind.JOB_POSTING.value for f in result.facts)
    fact = result.facts[0]
    assert fact.kind == "job_posting_unconfirmed"
    assert fact.content == "Engineering"
    assert fact.metadata["original_kind"] == FactKind.JOB_POSTING.value
    assert fact.metadata["confidence"] == "low"
    assert fact.metadata["identification_method"] == "url_shape_only"
    assert "downgraded" in fact.metadata["provenance_note"]


def test_declared_job_item_without_confidence_field_is_trusted_by_default(
    tmp_path: Path,
):
    """Backward compatibility: hand-authored fixtures (and any provider payload
    that never set identification_method/confidence) are 'declared', not
    inferred -- they default to high confidence and are not second-guessed.
    This is what keeps every pre-existing stub fixture unaffected."""
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "declared.example.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "kind": "careers",
                        "url": "https://declared.example/careers",
                        "items": [
                            {
                                "key": "https://declared.example/careers/whatever",
                                "url": "https://declared.example/careers/whatever",
                                "text": "Whatever",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("declared.example")

    fact = result.facts[0]
    assert fact.kind == FactKind.JOB_POSTING.value
    assert fact.metadata["identification_method"] == "declared"
    assert fact.metadata["confidence"] == "high"


def test_low_confidence_fact_id_is_still_stable():
    """The demotion changes `kind`, which is part of the id's hash input by
    design (make_fact_id(kind, source_url, natural_key)) -- but for one fixed
    (kind, source_url, natural_key), re-parsing must still produce the same
    id every time. A copy-edit to the item's TEXT must not move it."""
    payload = {
        "pages": [
            {
                "kind": "careers",
                "url": "https://lowconf2.example/careers",
                "items": [
                    {
                        "key": "https://lowconf2.example/careers/engineering",
                        "url": "https://lowconf2.example/careers/engineering",
                        "text": "Engineering",
                        "confidence": "low",
                    }
                ],
            }
        ]
    }

    def make(fixtures_dir: Path, text: str) -> None:
        (fixtures_dir / "company").mkdir(exist_ok=True)
        edited = json.loads(json.dumps(payload))
        edited["pages"][0]["items"][0]["text"] = text
        (fixtures_dir / "company" / "lowconf2.example.json").write_text(
            json.dumps(edited), encoding="utf-8"
        )

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        make(Path(d1), "Engineering")
        make(Path(d2), "Engineering (all locations)")
        before = StubCompanyAdapter(fixtures_dir=Path(d1)).fetch_company(
            "lowconf2.example"
        )
        after = StubCompanyAdapter(fixtures_dir=Path(d2)).fetch_company(
            "lowconf2.example"
        )
        assert before.facts[0].id == after.facts[0].id
        assert before.facts[0].kind == after.facts[0].kind == "job_posting_unconfirmed"


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


def test_undecided_vendors_fail_loudly_rather_than_guessing(monkeypatch):
    # ScrapIn is still an open question in the brief (docs/GAP-ANALYSIS.md
    # section 5) -- it must keep failing loudly rather than silently falling
    # back to a guess.
    with pytest.raises(NotImplementedError, match="open question|not implemented"):
        get_linkedin_adapter(provider="scrapin")
    with pytest.raises(ValueError):
        get_linkedin_adapter(provider="mystery-vendor")

    # Bright Data IS implemented now, but it is a paid vendor: asking for it
    # with no credentials configured must fail loudly too, not silently
    # degrade to a stub or a guess.
    monkeypatch.setenv("LINKEDIN_API_KEY", "")
    with pytest.raises(ProviderAuthError):
        get_linkedin_adapter(provider="brightdata")


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


# =====================================================================
# _html: dated-content provenance ladder (pure functions, no adapter)
#
# blog_post (42.0), changelog (45.0) and press (40.0) each clear the 40.0
# scoring threshold ALONE when fresh, so this is the same known-gap failure
# as job postings, one tier down -- and recency being most of the score
# means a WRONG-but-fresh date is worse than a merely undated fact.
# =====================================================================


def test_time_datetime_attribute_in_a_real_block_is_high_confidence():
    html = """
    <article>
      <a href="/blog/post-1">A real post</a>
      <time datetime="2026-09-01">September 1, 2026</time>
    </article>
    """
    items = _html.extract_dated_items(html, "https://x.example/blog")
    assert len(items) == 1
    assert items[0]["identification_method"] == "time_attr"
    assert items[0]["confidence"] == "high"
    assert items[0]["published_at"] == "2026-09-01"


def test_bare_text_date_scan_in_a_real_block_is_low_confidence():
    """No <time> tag at all -- the date came from scanning the block's raw
    text. This is where a testimonial dateline or a copyright year becomes a
    publication date."""
    html = """
    <article>
      <a href="/blog/post-2">A block with no time tag</a>
      <p>Proudly serving logistics teams since March 3, 2019.</p>
    </article>
    """
    items = _html.extract_dated_items(html, "https://x.example/blog")
    assert len(items) == 1
    assert items[0]["identification_method"] == "text_scan"
    assert items[0]["confidence"] == "low"
    assert items[0]["published_at"] == "2019-03-03"


def test_whole_page_proximity_fallback_is_capped_at_low_confidence():
    """No <article>/<li> blocks at all: the item comes from pairing a bare
    <time> tag with the nearest PRECEDING anchor anywhere on the page. That
    pairing is itself a guess, so it is capped at low confidence even though
    the date was read from a real <time datetime> attribute -- a real,
    correctly-dated post and a testimonial's incidental date are equally
    plausible outputs of this path, and the ingest gate cannot tell them
    apart without stronger corroboration."""
    html = """
    <p>
      <a href="/blog/scaling-support">Scaling support without burning out the team</a>
      <time datetime="2026-09-01">September 1, 2026</time>
    </p>
    <p>
      <a href="/customers/acme-logistics">Acme Logistics</a> has trusted us
      <time>since March 3, 2019</time> with their entire support desk.
    </p>
    """
    items = _html.extract_dated_items(html, "https://testimonial.example/blog")
    by_url = {i["url"]: i for i in items}
    assert by_url["https://testimonial.example/blog/scaling-support"]["identification_method"] == (
        "proximity_fallback"
    )
    assert by_url["https://testimonial.example/blog/scaling-support"]["confidence"] == "low"
    testimonial = by_url["https://testimonial.example/customers/acme-logistics"]
    assert testimonial["identification_method"] == "proximity_fallback"
    assert testimonial["confidence"] == "low"
    # The regression this guards: the testimonial's "since March 3, 2019" is
    # parseable as a real date, which is exactly the risk -- it must still be
    # marked low confidence, not silently trusted because it happened to parse.
    assert testimonial["published_at"] == "2019-03-03"


def test_feed_date_rescues_the_real_post_but_not_the_testimonial():
    html = """
    <p>
      <a href="/blog/scaling-support">Scaling support without burning out the team</a>
      <time datetime="2026-09-01">September 1, 2026</time>
    </p>
    <p>
      <a href="/customers/acme-logistics">Acme Logistics</a> has trusted us
      <time>since March 3, 2019</time> with their entire support desk.
    </p>
    """
    feed_dates = {
        "https://testimonial.example/blog/scaling-support": "2026-09-01",
    }
    items = _html.extract_dated_items(
        html, "https://testimonial.example/blog", feed_dates=feed_dates
    )
    by_url = {i["url"]: i for i in items}
    rescued = by_url["https://testimonial.example/blog/scaling-support"]
    assert rescued["identification_method"] == "feed"
    assert rescued["confidence"] == "high"
    # The testimonial is NOT in the feed (it is not a real post) and stays low.
    testimonial = by_url["https://testimonial.example/customers/acme-logistics"]
    assert testimonial["identification_method"] == "proximity_fallback"
    assert testimonial["confidence"] == "low"


def test_json_ld_article_rescues_an_undated_block():
    html = """
    <article>
      <a href="/changelog/growth-wizard-update">Ship notes</a>
    </article>
    <script type="application/ld+json">
    {
      "@type": "BlogPosting",
      "url": "https://x.example/changelog/growth-wizard-update",
      "datePublished": "2026-09-10"
    }
    </script>
    """
    items = _html.extract_dated_items(html, "https://x.example/changelog")
    assert len(items) == 1
    assert items[0]["identification_method"] == "json_ld"
    assert items[0]["confidence"] == "high"
    assert items[0]["published_at"] == "2026-09-10"


def test_undated_block_is_not_marked_low_confidence():
    """No date evidence anywhere means published_at stays None -- that is
    already heavily discounted downstream by score.py's undated_multiplier,
    so it must not ALSO be marked "low" confidence; there is nothing wrong
    to be low-confidence about."""
    html = """
    <article>
      <a href="/blog/no-date-anywhere">A post with genuinely no date</a>
      <p>Just some words, no dates in sight.</p>
    </article>
    """
    items = _html.extract_dated_items(html, "https://x.example/blog")
    assert len(items) == 1
    assert items[0]["published_at"] is None
    assert items[0]["identification_method"] == "undated"
    assert items[0]["confidence"] == "n/a"


def test_extract_json_ld_dated_urls_walks_a_graph():
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {"@type": "BlogPosting", "url": "https://x.example/blog/a", "datePublished": "2026-01-01"},
        {"@type": "WebPage", "url": "https://x.example/blog"}
      ]
    }
    </script>
    """
    dated = _html.extract_json_ld_dated_urls(html, "https://x.example/blog")
    assert dated == {"https://x.example/blog/a": "2026-01-01"}


def test_parse_feed_dates_reads_rss_and_atom():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <link>https://x.example/blog/a</link>
        <pubDate>Tue, 15 Sep 2026 00:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """
    assert _html.parse_feed_dates(rss) == {"https://x.example/blog/a": "2026-09-15"}

    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <link rel="alternate" href="https://x.example/blog/b"/>
        <updated>2026-09-16T10:00:00Z</updated>
      </entry>
    </feed>
    """
    assert _html.parse_feed_dates(atom) == {"https://x.example/blog/b": "2026-09-16"}


def test_parse_feed_dates_never_raises_on_garbage():
    assert _html.parse_feed_dates("not xml at all <<<") == {}
    assert _html.parse_feed_dates("") == {}


def test_find_feed_link_prefers_first_rss_or_atom_alternate():
    html = """
    <head>
      <link rel="stylesheet" href="/style.css">
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    </head>
    """
    assert _html.find_feed_link(html, "https://x.example/") == "https://x.example/feed.xml"
    assert _html.find_feed_link("<head></head>", "https://x.example/") is None


def test_extract_sitemap_urls_is_never_raising_on_garbage():
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x.example/careers/a</loc></url>
      <url><loc>https://x.example/blog/b</loc></url>
    </urlset>
    """
    assert _html.extract_sitemap_urls(xml) == {
        "https://x.example/careers/a",
        "https://x.example/blog/b",
    }
    assert _html.extract_sitemap_urls("not xml") == set()


def test_page_has_apply_signal_detects_apply_text_and_ats_links():
    assert _html.page_has_apply_signal(
        '<a href="/apply/here">Apply Now</a>', "https://x.example/careers/role"
    )
    assert _html.page_has_apply_signal(
        '<a href="https://boards.greenhouse.io/x/jobs/1">See listing</a>',
        "https://x.example/careers/role",
    )
    assert not _html.page_has_apply_signal(
        '<a href="/">Home</a><p>Nothing to apply for here.</p>',
        "https://x.example/careers/role",
    )


def test_page_has_apply_signal_reads_a_same_page_apply_anchor():
    """Confirmed live against buffer.com's own careers pages: a real apply
    CTA that jumps to an embedded form on the SAME page (`href="#apply"`)
    rather than linking out. `extract_links` drops fragment-only hrefs on
    purpose (not a distinct page to discover); this check must not inherit
    that filtering, or it silently misses this common real-world pattern."""
    assert _html.page_has_apply_signal(
        '<a href="#apply">Apply</a><section id="apply">...</section>',
        "https://x.example/careers/role",
    )


def test_extract_job_items_does_not_accept_a_sitemap_signal():
    """Regression guard for a deliberate retirement, not an oversight: a
    sitemap-listed URL was tried as a job-confidence signal and measured
    live against a real careers site (asana.com) to promote 19
    per-location/per-department FILTER pages -- real, distinct, sitemap-
    indexed pages that are not job reqs -- to JOB_POSTING at once. See
    `_html.extract_job_items`'s docstring and
    `LiveCompanyAdapter._discover_sitemap_urls`'s docstring for the full
    writeup. `extract_job_items` must not silently accept a `sitemap_urls`
    kwarg again without that decision being revisited on purpose."""
    with pytest.raises(TypeError):
        _html.extract_job_items(
            "<a href='/careers/x'>X</a>",
            "https://x.example/careers",
            sitemap_urls={"https://x.example/careers/x"},
        )


# =====================================================================
# company.pages_payload_to_facts: MEDIUM-tier (blog/changelog/press)
# demotion, mirroring the job-posting mechanism above it in the file.
# =====================================================================


def test_low_confidence_dated_item_is_demoted_not_dropped(tmp_path: Path):
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "lowdate.example.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "kind": "blog",
                        "url": "https://lowdate.example/blog",
                        "items": [
                            {
                                "key": "https://lowdate.example/customers/acme",
                                "url": "https://lowdate.example/customers/acme",
                                "text": "Acme Logistics testimonial",
                                "published_at": "2019-03-03",
                                "identification_method": "proximity_fallback",
                                "confidence": "low",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("lowdate.example")

    assert result.ok
    assert not any(f.kind == FactKind.BLOG_POST.value for f in result.facts)
    fact = result.facts[0]
    assert fact.kind == "dated_content_unconfirmed"
    assert fact.metadata["original_kind"] == FactKind.BLOG_POST.value
    assert fact.metadata["confidence"] == "low"
    assert fact.metadata["identification_method"] == "proximity_fallback"
    assert "downgraded" in fact.metadata["provenance_note"]


def test_high_confidence_dated_item_keeps_its_kind(tmp_path: Path):
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "highdate.example.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "kind": "changelog",
                        "url": "https://highdate.example/changelog",
                        "items": [
                            {
                                "key": "https://highdate.example/changelog/a",
                                "url": "https://highdate.example/changelog/a",
                                "text": "Shipped: real thing",
                                "published_at": "2026-09-01",
                                "identification_method": "time_attr",
                                "confidence": "high",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = StubCompanyAdapter(fixtures_dir=tmp_path).fetch_company("highdate.example")

    fact = result.facts[0]
    assert fact.kind == FactKind.CHANGELOG.value
    assert fact.metadata["identification_method"] == "time_attr"
    assert fact.metadata["confidence"] == "high"
    assert "provenance_note" not in fact.metadata


def test_dated_content_confidence_fails_closed_against_the_real_scorer():
    """The compliance-critical claim, proven against mazz.pipeline.score
    directly (read-only import; score.py is not owned by this agent): four
    maximally-fresh demoted facts must not be able to promote a lead out of
    no_angle on their own, while a genuine fresh blog post still qualifies."""
    from mazz.pipeline.score import score_facts

    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    payload = {
        "retrieved_at": now.isoformat(),
        "pages": [
            {
                "kind": "blog",
                "url": "https://fresh.example/blog",
                "items": [
                    {
                        "key": f"https://fresh.example/blog/testimonial-{i}",
                        "url": f"https://fresh.example/blog/testimonial-{i}",
                        "text": f"Testimonial {i}",
                        "published_at": now.date().isoformat(),
                        "identification_method": "proximity_fallback",
                        "confidence": "low",
                    }
                    for i in range(4)
                ],
            }
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        fixtures = Path(d)
        (fixtures / "company").mkdir()
        (fixtures / "company" / "demoted.example.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        demoted_result = StubCompanyAdapter(fixtures_dir=fixtures).fetch_company(
            "demoted.example"
        )

        genuine_payload = {
            "retrieved_at": now.isoformat(),
            "pages": [
                {
                    "kind": "blog",
                    "url": "https://fresh.example/blog",
                    "items": [
                        {
                            "key": "https://fresh.example/blog/real-post",
                            "url": "https://fresh.example/blog/real-post",
                            "text": "A genuine fresh post",
                            "published_at": now.date().isoformat(),
                        }
                    ],
                }
            ],
        }
        (fixtures / "company" / "genuine.example.json").write_text(
            json.dumps(genuine_payload), encoding="utf-8"
        )
        genuine_result = StubCompanyAdapter(fixtures_dir=fixtures).fetch_company(
            "genuine.example"
        )

    demoted_scored = score_facts(demoted_result.facts, now=now)
    assert demoted_scored.has_angle is False

    genuine_scored = score_facts(genuine_result.facts, now=now)
    assert genuine_scored.has_angle is True
    assert genuine_scored.top.tier.value == "medium"
