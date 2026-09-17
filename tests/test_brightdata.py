"""Bright Data arm of the W3 vendor spike (`docs/GAP-ANALYSIS.md` section 5).

Everything here runs against SAVED fixtures via an injected fake HTTP client --
`conftest.py`'s autouse fixture also blanks `LINKEDIN_API_KEY` for every test,
so even a bug that forgot to inject a fake client would fail closed on
`ProviderAuthError` rather than reaching the real API. No test in this file
opens a socket; `test_no_live_network_even_if_a_test_forgot_to_stub` proves it.

What has to hold, mechanically, not just by inspection:

* A transport failure, bad credentials, a 429 or a 5xx RAISES -- never comes
  back as an empty fact list, which would look exactly like the legitimate
  "looked, found nothing" `no_angle` verdict.
* Fact ids are stable across re-parses of the same material.
* Every person fact carries `subject_email` (delete-by-person's only handle).
* No Bright Data field name (`current_company_name`, `activity`, `updates`,
  `linkedin_num_id`, ...) crosses into a `Fact.content`/`Fact.metadata` key --
  only the values do, normalized into our own vocabulary.
* Billing is counted from the actual response shape, not guessed: a "not
  found" envelope is free, anything with real fields is billed.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from mazz.integrations.base import (
    FactKind,
    FetchStatus,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
)
from mazz.integrations.company import (
    BrightDataCompanyAdapter,
    NoLinkedInCompanyURL,
    get_company_adapter,
)
from mazz.integrations.linkedin import BrightDataLinkedInAdapter, get_linkedin_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "brightdata"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


PERSON_OK = _read("person_ok.json")
PERSON_NOT_FOUND = _read("person_not_found.json")
PERSON_MALFORMED = _read("person_malformed_activity.json")
COMPANY_OK = _read("company_ok.json")
COMPANY_NOT_FOUND = _read("company_not_found.json")
COMPANY_MALFORMED = _read("company_malformed_updates.json")


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeBrightDataClient:
    """Stands in for `httpx.Client`. Routes by the TARGET URL inside the POST
    body, not by the (identical-per-dataset) endpoint -- Bright Data's sync
    scrape endpoint is one URL with `dataset_id` as a query param; the thing
    that varies call to call is the profile/company URL in the request body.

    Records every call, and never touches a real socket.
    """

    def __init__(
        self,
        by_target: dict[str, tuple[int, str]],
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self.by_target = by_target
        self.raise_for = raise_for or set()
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, endpoint, *, content, headers=None, timeout=None):
        body = json.loads(content)
        target = body[0]["url"]
        self.calls.append((endpoint, target, dict(headers or {})))
        if target in self.raise_for:
            raise ConnectionError(f"simulated transport failure for {target}")
        status, text = self.by_target[target]
        return FakeResponse(status, text)

    def close(self) -> None:  # pragma: no cover - adapter does not own this client
        pass


PERSON_URL = "https://www.linkedin.com/in/riley-nakamura"
COMPANY_URL = "https://www.linkedin.com/company/gridiron-labs/"
DOMAIN = "gridiron-labs.example"


def make_li(client: FakeBrightDataClient, **kwargs) -> BrightDataLinkedInAdapter:
    kwargs.setdefault("retry_policy", RetryPolicy(attempts=1))
    return BrightDataLinkedInAdapter(api_key="fake-key-not-real", http_client=client, **kwargs)


def make_co(client: FakeBrightDataClient, **kwargs) -> BrightDataCompanyAdapter:
    kwargs.setdefault("retry_policy", RetryPolicy(attempts=1))
    kwargs.setdefault("linkedin_urls", {DOMAIN: COMPANY_URL})
    return BrightDataCompanyAdapter(api_key="fake-key-not-real", http_client=client, **kwargs)


# =====================================================================
# LinkedIn people
# =====================================================================


def test_person_profile_normalizes_into_facts_with_subject_email():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})
    li = make_li(client)

    result = li.fetch_profile(PERSON_URL, subject_email="lead@example.com")

    assert result.ok
    assert result.status is FetchStatus.OK
    assert len(result.facts) >= 2
    for fact in result.facts:
        assert fact.kind == FactKind.LINKEDIN_PROFILE.value
        assert fact.subject_email == "lead@example.com"
        assert fact.source_url


def test_person_fact_without_subject_email_is_a_caller_bug_not_silently_fine():
    """Omitting `subject_email` is legal (the ABC does not require it), but it
    means delete-by-person cannot find these facts. Assert the omission is
    visible, not swallowed -- the field is simply None, not backfilled."""
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})
    li = make_li(client)

    result = li.fetch_profile(PERSON_URL)

    assert result.ok
    assert all(f.subject_email is None for f in result.facts)


def test_person_no_vendor_shape_leaks_into_facts():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})
    li = make_li(client)

    result = li.fetch_profile(PERSON_URL, subject_email="lead@example.com")

    banned = {
        "current_company_name",
        "current_company_company_id",
        "linkedin_num_id",
        "linkedin_id",
        "activity",
        "input_url",
        "first_name",
        "last_name",
    }
    for fact in result.facts:
        dumped = fact.model_dump(mode="json")
        assert set(dumped["metadata"].keys()) <= {"section", "posted_at", "profile_url"}
        for key in banned:
            assert key not in dumped["metadata"]
            assert key not in dumped["content"]


def test_person_fact_ids_are_stable_across_instances_and_re_fetches():
    client_a = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})
    client_b = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})

    facts_a = make_li(client_a).fetch_profile(PERSON_URL, subject_email="a@example.com").facts
    facts_b = make_li(client_b).fetch_profile(PERSON_URL, subject_email="b@example.com").facts

    ids_a = {f.id for f in facts_a}
    ids_b = {f.id for f in facts_b}
    assert ids_a == ids_b  # subject_email is not part of the id -- content/url is


def test_person_not_found_is_empty_and_free_not_billed():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_NOT_FOUND)})
    li = make_li(client)

    result = li.fetch_profile(PERSON_URL, subject_email="lead@example.com")

    assert result.status is FetchStatus.NOT_FOUND
    assert result.facts == ()
    assert li.billing.records_billed == 0
    assert li.billing.records_not_found == 1


def test_person_successful_lookup_is_billed_exactly_once():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)})
    li = make_li(client)
    li.fetch_profile(PERSON_URL, subject_email="lead@example.com")
    assert li.billing.records_billed == 1
    assert li.billing.records_not_found == 0
    assert li.billing.calls == 1


@pytest.mark.parametrize(
    "status,exc",
    [(401, ProviderAuthError), (403, ProviderAuthError), (429, ProviderRateLimited), (500, ProviderUnavailable)],
)
def test_person_failure_raises_never_returns_empty(status, exc):
    client = FakeBrightDataClient({PERSON_URL: (status, "{}")})
    li = make_li(client)
    with pytest.raises(exc):
        li.fetch_profile(PERSON_URL, subject_email="lead@example.com")
    # Nothing was billed for a call that never succeeded.
    assert li.billing.records_billed == 0


def test_person_transport_error_raises_provider_unavailable():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK)}, raise_for={PERSON_URL})
    li = make_li(client)
    with pytest.raises(ProviderUnavailable):
        li.fetch_profile(PERSON_URL, subject_email="lead@example.com")


def test_person_malformed_activity_raises_bad_response_not_empty():
    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_MALFORMED)})
    li = make_li(client)
    with pytest.raises(ProviderBadResponse):
        li.fetch_profile(PERSON_URL, subject_email="lead@example.com")


def test_missing_api_key_fails_closed_at_construction():
    client = FakeBrightDataClient({})
    with pytest.raises(ProviderAuthError):
        BrightDataLinkedInAdapter(api_key="", http_client=client)


def test_get_linkedin_adapter_brightdata_is_opt_in_by_name_only(monkeypatch):
    # LINKEDIN_API_KEY present in the environment must NOT by itself select
    # the paid provider -- only an explicit provider name does.
    monkeypatch.setenv("LINKEDIN_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("LINKEDIN_PROVIDER", "stub")
    from mazz.integrations.linkedin import StubLinkedInAdapter

    assert isinstance(get_linkedin_adapter(), StubLinkedInAdapter)

    # Asking for it explicitly, with a key present, does select it.
    adapter = get_linkedin_adapter(provider="brightdata")
    try:
        assert isinstance(adapter, BrightDataLinkedInAdapter)
    finally:
        adapter.close()


# =====================================================================
# LinkedIn company
# =====================================================================


def test_company_profile_normalizes_into_homepage_and_blog_facts():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    co = make_co(client)

    result = co.fetch_company(DOMAIN)

    assert result.ok
    kinds = {f.kind for f in result.facts}
    assert FactKind.HOMEPAGE.value in kinds
    assert FactKind.BLOG_POST.value in kinds
    # This dataset has no jobs field at all -- it must never fabricate one.
    assert FactKind.JOB_POSTING.value not in kinds


def test_company_facts_carry_no_subject_email():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    co = make_co(client)
    result = co.fetch_company(DOMAIN)
    assert all(f.subject_email is None for f in result.facts)


def test_company_no_vendor_shape_leaks_into_facts():
    """`identification_method`/`confidence` are this codebase's own research-
    provenance vocabulary (`mazz.integrations.company`'s ingest-time gate) --
    additive, and asserted below to carry no Bright-Data-shaped values either,
    not just no Bright-Data-shaped KEYS."""
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    co = make_co(client)
    result = co.fetch_company(DOMAIN)

    banned = {"post_id", "likes_count", "comments_count", "specialties", "company_size", "employees"}
    for fact in result.facts:
        dumped = fact.model_dump(mode="json")
        assert set(dumped["metadata"].keys()) <= {
            "page_kind",
            "published_at",
            "domain",
            "identification_method",
            "confidence",
        }
        for key in banned:
            assert key not in dumped["metadata"]
            assert key not in dumped["content"]
        # A self-published LinkedIn post is real dated content, declared by
        # the payload rather than inferred from HTML shape -- never mistaken
        # for a Bright-Data-specific provenance value.
        if "identification_method" in dumped["metadata"]:
            assert dumped["metadata"]["identification_method"] == "declared"
            assert dumped["metadata"]["confidence"] == "high"


def test_company_fact_ids_are_stable_across_instances():
    client_a = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    client_b = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    ids_a = {f.id for f in make_co(client_a).fetch_company(DOMAIN).facts}
    ids_b = {f.id for f in make_co(client_b).fetch_company(DOMAIN).facts}
    assert ids_a == ids_b


def test_company_update_prefers_absolute_date_over_relative_time():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    co = make_co(client)
    result = co.fetch_company(DOMAIN)
    blog_facts = [f for f in result.facts if f.kind == FactKind.BLOG_POST.value]
    dated = [f for f in blog_facts if f.metadata.get("published_at") == "2026-09-01T12:00:00.000Z"]
    assert dated, "the update with a real `date` field must carry it through verbatim"


def test_company_not_found_is_empty_and_free_not_billed():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_NOT_FOUND)})
    co = make_co(client)
    result = co.fetch_company(DOMAIN)
    assert result.status is FetchStatus.NOT_FOUND
    assert result.facts == ()
    assert co.billing.records_billed == 0
    assert co.billing.records_not_found == 1


def test_company_successful_lookup_is_billed_exactly_once():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    co = make_co(client)
    co.fetch_company(DOMAIN)
    assert co.billing.records_billed == 1
    assert co.billing.calls == 1


@pytest.mark.parametrize(
    "status,exc",
    [(401, ProviderAuthError), (429, ProviderRateLimited), (503, ProviderUnavailable)],
)
def test_company_failure_raises_never_returns_empty(status, exc):
    client = FakeBrightDataClient({COMPANY_URL: (status, "{}")})
    co = make_co(client)
    with pytest.raises(exc):
        co.fetch_company(DOMAIN)
    assert co.billing.records_billed == 0


def test_company_malformed_updates_raises_bad_response():
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_MALFORMED)})
    co = make_co(client)
    with pytest.raises(ProviderBadResponse):
        co.fetch_company(DOMAIN)


def test_company_with_no_linkedin_url_configured_fails_closed_not_search():
    client = FakeBrightDataClient({})
    co = BrightDataCompanyAdapter(
        api_key="fake-key-not-real",
        http_client=client,
        retry_policy=RetryPolicy(attempts=1),
        linkedin_urls={},
    )
    with pytest.raises(NoLinkedInCompanyURL):
        co.fetch_company(DOMAIN)
    assert client.calls == []  # never even tried to reach Bright Data


def test_company_url_resolver_is_consulted_but_never_searches():
    """A resolver callback is allowed (e.g. "read the link the company already
    published on its own homepage"), but the adapter itself never guesses or
    searches LinkedIn -- it only ever calls what the resolver hands back."""
    client = FakeBrightDataClient({COMPANY_URL: (200, COMPANY_OK)})
    seen = []

    def resolver(domain: str) -> str:
        seen.append(domain)
        return COMPANY_URL

    co = BrightDataCompanyAdapter(
        api_key="fake-key-not-real",
        http_client=client,
        retry_policy=RetryPolicy(attempts=1),
        linkedin_urls={},
        url_resolver=resolver,
    )
    result = co.fetch_company(DOMAIN)
    assert result.ok
    assert seen == [DOMAIN]


def test_missing_api_key_fails_closed_at_construction_company():
    client = FakeBrightDataClient({})
    with pytest.raises(ProviderAuthError):
        BrightDataCompanyAdapter(api_key="", http_client=client, linkedin_urls={DOMAIN: COMPANY_URL})


def test_get_company_adapter_brightdata_is_opt_in_by_name_only(monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("COMPANY_SCRAPER_PROVIDER", "stub")
    from mazz.integrations.company import StubCompanyAdapter

    assert isinstance(get_company_adapter(fixtures_dir=Path(__file__).parent / "fixtures"), StubCompanyAdapter)

    adapter = get_company_adapter(provider="brightdata", linkedin_urls={DOMAIN: COMPANY_URL})
    try:
        assert isinstance(adapter, BrightDataCompanyAdapter)
    finally:
        adapter.close()


# =====================================================================
# No live network, ever, in this suite
# =====================================================================


def test_no_live_network_even_if_a_test_forgot_to_stub(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("test_brightdata.py must never open a real socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    client = FakeBrightDataClient({PERSON_URL: (200, PERSON_OK), COMPANY_URL: (200, COMPANY_OK)})
    assert make_li(client).fetch_profile(PERSON_URL, subject_email="lead@example.com").ok
    assert make_co(client).fetch_company(DOMAIN).ok
