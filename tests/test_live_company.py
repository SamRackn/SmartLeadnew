"""`LiveCompanyAdapter` -- parsing against saved HTML fixtures. No live network
in this suite; the real-domain measurement run is separate and manual (see the
W3 spike report).

What has to hold, mechanically, not just by inspection:

* robots.txt disallow is respected -- a disallowed target is never fetched.
* a blocked or failed fetch RAISES; it must never come back as an empty fact
  list, which would look exactly like the legitimate "researched it, found
  nothing" `no_angle` verdict.
* careers items become `FactKind.JOB_POSTING`, not `CAREERS_PAGE`.
* fact ids are stable across a fresh re-parse of the same material.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mazz.integrations.base import (
    FactKind,
    FetchStatus,
    ProviderBlocked,
    ProviderUnavailable,
    RetryPolicy,
)
from mazz.integrations.company import LiveCompanyAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "company_html"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


ACME_HOMEPAGE = _read("acme_homepage.html")
ACME_CAREERS = _read("acme_careers.html")
ACME_BLOG = _read("acme_blog.html")
ROBOTS_ALLOW_ALL = ""
ROBOTS_DISALLOW_ALL = _read("robots_disallow_all.txt")
ROBOTS_DISALLOW_CAREERS = _read("robots_disallow_careers.txt")


class FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeClient:
    """Stands in for `httpx.Client`: a URL -> (status, body) map, plus the
    ability to make a URL raise a real `httpx` network error. Every call is
    recorded so tests can assert what was (and was not) fetched -- in
    particular, that a robots-disallowed URL is never requested at all."""

    def __init__(self, routes: dict[str, tuple[int, str]], *, raises: set[str] | None = None) -> None:
        self.routes = dict(routes)
        self.raises = raises or set()
        self.calls: list[str] = []

    def get(self, url: str, headers: dict | None = None):
        self.calls.append(url)
        if url in self.raises:
            raise httpx.ConnectError("simulated connection failure", request=None)
        if url not in self.routes:
            return FakeResponse(404, "")
        status, body = self.routes[url]
        return FakeResponse(status, body)

    def close(self) -> None:  # pragma: no cover - adapter does not own this client
        pass


def make_adapter(client: FakeClient, **kwargs) -> LiveCompanyAdapter:
    kwargs.setdefault("retry_policy", RetryPolicy(attempts=1))
    kwargs.setdefault("rate_limit_seconds", 0.0)
    return LiveCompanyAdapter(http_client=client, **kwargs)


def acme_routes(**overrides) -> dict[str, tuple[int, str]]:
    routes = {
        "https://acme-robotics.example/robots.txt": (200, ROBOTS_ALLOW_ALL),
        "https://acme-robotics.example/": (200, ACME_HOMEPAGE),
        "https://acme-robotics.example/careers": (200, ACME_CAREERS),
        "https://acme-robotics.example/blog": (200, ACME_BLOG),
    }
    routes.update(overrides)
    return routes


# ==========================================================================
# robots.txt is respected
# ==========================================================================


def test_robots_disallow_on_homepage_blocks_the_whole_fetch():
    """A robots disallow on `/` means we cannot research this company at all
    -- that must raise, not silently produce zero facts."""
    client = FakeClient(acme_routes(**{
        "https://acme-robotics.example/robots.txt": (200, ROBOTS_DISALLOW_ALL),
    }))
    adapter = make_adapter(client)

    with pytest.raises(ProviderBlocked):
        adapter.fetch_company("acme-robotics.example")

    # We must never have fetched the homepage (or anything else) once robots
    # said no -- checking robots.txt happens strictly before the real request.
    assert "https://acme-robotics.example/" not in client.calls


def test_robots_disallow_on_one_page_skips_only_that_page():
    """A narrower disallow (just /careers) is a partial result, not a total
    failure: the rest of the site is still fair game and the fetch succeeds."""
    client = FakeClient(acme_routes(**{
        "https://acme-robotics.example/robots.txt": (200, ROBOTS_DISALLOW_CAREERS),
    }))
    adapter = make_adapter(client)

    result = adapter.fetch_company("acme-robotics.example")

    assert result.ok
    assert "https://acme-robotics.example/careers" not in client.calls
    assert not any(f.kind == FactKind.JOB_POSTING.value for f in result.facts)
    # The blog page was still allowed and fetched.
    assert any(f.kind == FactKind.BLOG_POST.value for f in result.facts)


# ==========================================================================
# A failure raises. It never comes back as an empty, no_angle-shaped result.
# ==========================================================================


def test_blocked_homepage_raises_rather_than_returning_empty():
    """A 403 on the homepage is the site refusing us outright. This must
    raise -- returning FetchResult.empty() here would be indistinguishable
    from a legitimate 'nothing to find', which is exactly the swallowed
    failure the brief forbids."""
    client = FakeClient(acme_routes(**{
        "https://acme-robotics.example/": (403, ""),
    }))
    adapter = make_adapter(client)

    with pytest.raises(ProviderBlocked):
        adapter.fetch_company("acme-robotics.example")


def test_network_failure_on_homepage_raises_after_retries_exhausted():
    client = FakeClient(
        acme_routes(),
        raises={"https://acme-robotics.example/"},
    )
    adapter = make_adapter(client, retry_policy=RetryPolicy(attempts=2, base_delay=0.0))

    with pytest.raises(ProviderUnavailable):
        adapter.fetch_company("acme-robotics.example")


def test_dead_domain_is_not_found_not_a_silent_empty():
    """A homepage 404 is a genuinely successful lookup ('no such site') --
    FetchResult distinguishes that from OK/EMPTY, and it is not an exception."""
    client = FakeClient({
        "https://nowhere.example/robots.txt": (404, ""),
        "https://nowhere.example/": (404, ""),
        "http://nowhere.example/": (404, ""),
    })
    adapter = make_adapter(client)

    result = adapter.fetch_company("nowhere.example")

    assert result.status is FetchStatus.NOT_FOUND
    assert result.facts == ()


# ==========================================================================
# Careers items -> JOB_POSTING, discovered from the homepage's own nav
# ==========================================================================


def test_careers_items_map_to_job_posting_not_careers_page():
    client = FakeClient(acme_routes())
    adapter = make_adapter(client)

    result = adapter.fetch_company("acme-robotics.example")

    assert result.ok
    job_facts = [f for f in result.facts if f.kind == FactKind.JOB_POSTING.value]
    assert {f.content for f in job_facts} == {
        "Revenue Operations Manager",
        "Sales Development Representative",
        "Senior Backend Engineer",
    }
    for fact in job_facts:
        assert fact.source_url.startswith("https://acme-robotics.example/careers/")
        assert fact.retrieved_at.tzinfo is not None
    # No fact anywhere is tagged as the careers page itself.
    assert not any(f.kind == FactKind.CAREERS_PAGE.value for f in result.facts)


def test_careers_page_discovered_from_homepage_nav_not_guessed():
    """The homepage's own <a href="/careers"> link is what leads us there --
    prove it by pointing the nav link somewhere non-standard and checking we
    still find it (and never had to fall back to a guessed path)."""
    homepage = ACME_HOMEPAGE.replace('href="/careers"', 'href="/join-the-team"')
    careers = ACME_CAREERS.replace("/careers/", "/join-the-team/")
    client = FakeClient(acme_routes(**{
        "https://acme-robotics.example/": (200, homepage),
        "https://acme-robotics.example/join-the-team": (200, careers),
    }))
    del client.routes["https://acme-robotics.example/careers"]
    adapter = make_adapter(client)

    result = adapter.fetch_company("acme-robotics.example")

    assert any(f.kind == FactKind.JOB_POSTING.value for f in result.facts)
    assert "https://acme-robotics.example/careers" not in client.calls


def test_blog_items_get_extracted_dates():
    client = FakeClient(acme_routes())
    adapter = make_adapter(client)

    result = adapter.fetch_company("acme-robotics.example")

    blog_facts = {f.content: f for f in result.facts if f.kind == FactKind.BLOG_POST.value}
    fresh = next(f for content, f in blog_facts.items() if "peak season" in content)
    assert fresh.metadata["published_at"] == "2026-09-01"
    old = next(f for content, f in blog_facts.items() if "safety review" in content)
    assert old.metadata["published_at"] == "2025-01-15"


# ==========================================================================
# Stable ids
# ==========================================================================


def test_fact_ids_are_stable_across_a_fresh_reparse():
    """Two independent adapter instances over byte-identical HTML must
    produce byte-identical fact ids -- drafts cite ids, and an id that moves
    between runs silently orphans every citation an approved draft made."""
    client_a = FakeClient(acme_routes())
    client_b = FakeClient(acme_routes())

    result_a = make_adapter(client_a).fetch_company("acme-robotics.example")
    result_b = make_adapter(client_b).fetch_company("acme-robotics.example")

    ids_a = sorted(f.id for f in result_a.facts)
    ids_b = sorted(f.id for f in result_b.facts)
    assert ids_a == ids_b
    assert ids_a  # not vacuously true


def test_fact_id_survives_an_unrelated_copy_edit():
    """The natural key is the job's URL, not its content -- so a copy-edit to
    the req's title text must not change the fact id, or an approved draft's
    citation would silently orphan."""
    client = FakeClient(acme_routes())
    result_before = make_adapter(client).fetch_company("acme-robotics.example")
    before = {f.source_url: f.id for f in result_before.facts if f.kind == FactKind.JOB_POSTING.value}

    edited_careers = ACME_CAREERS.replace(
        "Revenue Operations Manager", "Revenue Operations Manager (Remote)"
    )
    client2 = FakeClient(acme_routes(**{
        "https://acme-robotics.example/careers": (200, edited_careers),
    }))
    result_after = make_adapter(client2).fetch_company("acme-robotics.example")
    after = {f.source_url: f.id for f in result_after.facts if f.kind == FactKind.JOB_POSTING.value}

    shared_url = "https://acme-robotics.example/careers/revenue-operations-manager"
    assert before[shared_url] == after[shared_url]


# ==========================================================================
# No vendor shape leaks; per-domain politeness is real
# ==========================================================================


def test_requests_to_one_domain_are_rate_limited():
    sleeps: list[float] = []
    client = FakeClient(acme_routes())
    adapter = LiveCompanyAdapter(
        http_client=client,
        retry_policy=RetryPolicy(attempts=1),
        rate_limit_seconds=5.0,
        sleep=sleeps.append,
    )

    adapter.fetch_company("acme-robotics.example")

    # Multiple requests hit acme-robotics.example (robots.txt, homepage,
    # careers, blog) -- with a 5s minimum spacing and a fake clock that does
    # not actually advance, every request after the first must wait ~5s.
    assert sleeps
    assert all(s > 0 for s in sleeps)


def test_no_vendor_shape_leaks_into_the_result():
    """The scraping technique/vendor is explicitly undecided; this is the
    'own engine' arm precisely so nothing Bright-Data- or ScrapIn-shaped ever
    has to appear here."""
    client = FakeClient(acme_routes())
    result = make_adapter(client).fetch_company("acme-robotics.example")
    payload = str(result.as_dict()).lower()
    for vendor_token in ("brightdata", "bright_data", "scrapin", "snapshot_id", "dataset_id"):
        assert vendor_token not in payload
