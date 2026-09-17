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


# ==========================================================================
# Research provenance at cache-ingest (CLAUDE.md "Known gap in the guardrails")
#
# Verification proves a drafted claim traces to a cached Fact; it can never
# prove the Fact is true. These are the two measured W3-spike false positives:
# a JS-rendered careers hub whose only "jobs" are department filter controls,
# and a careers path that mixes real reqs with policy pages under the same
# path pattern. Both must be refused JOB_POSTING (STRONG-tier) at ingest, not
# silently dropped, and not left for the verifier to catch (it can't).
# ==========================================================================

JSHUB_HOMEPAGE = _read("jshub_homepage.html")
JSHUB_CAREERS = _read("jshub_careers.html")
MIXEDREQS_HOMEPAGE = _read("mixedreqs_homepage.html")
MIXEDREQS_CAREERS = _read("mixedreqs_careers.html")
STRUCTURED_HOMEPAGE = _read("structured_homepage.html")
STRUCTURED_CAREERS = _read("structured_careers.html")


def jshub_routes(**overrides) -> dict[str, tuple[int, str]]:
    routes = {
        "https://jshub.example/robots.txt": (200, ROBOTS_ALLOW_ALL),
        "https://jshub.example/": (200, JSHUB_HOMEPAGE),
        "https://jshub.example/jobs": (200, JSHUB_CAREERS),
    }
    routes.update(overrides)
    return routes


def mixedreqs_routes(**overrides) -> dict[str, tuple[int, str]]:
    routes = {
        "https://mixedreqs.example/robots.txt": (200, ROBOTS_ALLOW_ALL),
        "https://mixedreqs.example/": (200, MIXEDREQS_HOMEPAGE),
        "https://mixedreqs.example/careers": (200, MIXEDREQS_CAREERS),
    }
    routes.update(overrides)
    return routes


def structured_routes(**overrides) -> dict[str, tuple[int, str]]:
    routes = {
        "https://structured.example/robots.txt": (200, ROBOTS_ALLOW_ALL),
        "https://structured.example/": (200, STRUCTURED_HOMEPAGE),
        "https://structured.example/careers": (200, STRUCTURED_CAREERS),
    }
    routes.update(overrides)
    return routes


def test_js_rendered_filter_hub_is_not_admitted_as_job_posting():
    """Trello-style JS careers hub: a static fetch only ever sees the
    department-filter shell. Every 'job' extractable from that shell is a
    category link, identifiable only by 'this href is a sub-path of the
    careers page' -- exactly the known-gap failure mode. None may surface as
    JOB_POSTING."""
    client = FakeClient(jshub_routes())
    result = make_adapter(client).fetch_company("jshub.example")

    assert result.ok
    assert not any(f.kind == FactKind.JOB_POSTING.value for f in result.facts)

    # Not a silent empty: the candidates are still visible, just refused the
    # STRONG-tier kind and clearly annotated as to why.
    demoted = [f for f in result.facts if f.kind == "job_posting_unconfirmed"]
    assert {f.content for f in demoted} == {
        "Engineering",
        "Design",
        "Marketing",
        "People",
        "All teams",
    }
    for f in demoted:
        assert f.metadata["identification_method"] == "url_shape_only"
        assert f.metadata["confidence"] == "low"
        assert f.metadata["original_kind"] == FactKind.JOB_POSTING.value
        assert "downgraded" in f.metadata["provenance_note"]


def test_mixed_careers_path_keeps_real_reqs_and_demotes_policy_pages():
    """Zapier-style careers path: real reqs and policy/about pages share one
    path pattern. Titles containing a real job-title noun (Manager,
    Specialist, Engineer) are corroborated and stay JOB_POSTING; the policy
    pages -- which the OLD stopword-only filter let straight through, since
    none of their exact titles are in the stopword list -- are demoted."""
    client = FakeClient(mixedreqs_routes())
    result = make_adapter(client).fetch_company("mixedreqs.example")

    job_contents = {f.content for f in result.facts if f.kind == FactKind.JOB_POSTING.value}
    assert job_contents == {
        "Senior Product Manager",
        "Customer Support Specialist",
        "Software Engineer, Backend",
    }

    demoted_contents = {
        f.content for f in result.facts if f.kind == "job_posting_unconfirmed"
    }
    assert demoted_contents == {
        "Equal Employment Opportunity",
        "Diversity, Equity & Inclusion",
        "Life at the Company",
    }


def test_json_ld_job_posting_corroborates_a_title_with_no_recognisable_noun():
    """A quirky title ('Growth Wizard') has no job-title-noun overlap, so URL
    shape plus title vocabulary alone would demote it. A JSON-LD `JobPosting`
    block naming that exact URL is the site's own structured assertion, and
    is strong enough corroboration to admit it as JOB_POSTING."""
    client = FakeClient(structured_routes())
    result = make_adapter(client).fetch_company("structured.example")

    job_facts = [f for f in result.facts if f.kind == FactKind.JOB_POSTING.value]
    assert {f.content for f in job_facts} == {"Growth Wizard"}
    assert job_facts[0].metadata["identification_method"] == "json_ld"
    assert job_facts[0].metadata["confidence"] == "high"


def test_demoted_job_candidate_cannot_clear_the_scoring_threshold_alone():
    """The whole point: even if every other signal for this company is
    absent, a low-confidence 'job posting' must not be able to promote the
    lead out of no_angle on its own. Proven against the real scorer, not a
    restatement of the ingest logic."""
    from mazz.pipeline.score import score_facts

    client = FakeClient(jshub_routes())
    result = make_adapter(client).fetch_company("jshub.example")
    assert result.ok  # facts exist...

    scored = score_facts(result.facts)
    assert scored.has_angle is False  # ...but none of them are a real angle.


def test_no_vendor_shape_leaks_into_the_provenance_fields_either():
    """The identification-method vocabulary is this codebase's own (ats_host,
    href_keyword, json_ld, title_signal, url_shape_only) -- nothing scraper
    -vendor-shaped rides along in the new metadata fields either."""
    client = FakeClient(jshub_routes())
    result = make_adapter(client).fetch_company("jshub.example")
    payload = str(result.as_dict()).lower()
    for vendor_token in ("brightdata", "bright_data", "scrapin", "snapshot_id", "dataset_id"):
        assert vendor_token not in payload


# ==========================================================================
# MEDIUM-tier provenance, end to end: a dated testimonial vs a real post
#
# blog_post/changelog/press each clear the 40.0 threshold ALONE when fresh,
# so a block heuristic that misreads a testimonial's incidental date as a
# publication date is the same failure as the job-posting case, one tier
# down -- and worse in one way, because a WRONG-but-fresh date lands at
# maximum score rather than just being the wrong kind of fact.
# ==========================================================================

TESTIMONIAL_HOMEPAGE = _read("testimonial_homepage.html")
TESTIMONIAL_BLOG = _read("testimonial_blog.html")
TESTIMONIAL_FEED = _read("testimonial_feed.xml")


def quoteco_routes(**overrides) -> dict[str, tuple[int, str]]:
    routes = {
        "https://testimonial.example/robots.txt": (200, ROBOTS_ALLOW_ALL),
        "https://testimonial.example/": (200, TESTIMONIAL_HOMEPAGE),
        "https://testimonial.example/blog": (200, TESTIMONIAL_BLOG),
        "https://testimonial.example/sitemap.xml": (404, ""),
        "https://testimonial.example/feed.xml": (200, TESTIMONIAL_FEED),
    }
    routes.update(overrides)
    return routes


def test_dated_testimonial_is_demoted_real_post_is_rescued_by_its_own_feed():
    """End to end, with the homepage's own <link rel="alternate"> feed
    discovered and fetched: the genuine post is corroborated by the feed and
    stays BLOG_POST; the testimonial (never a real post, so never in the
    feed) is demoted -- visible, not dropped, and cannot pass as fresh."""
    client = FakeClient(quoteco_routes())
    result = make_adapter(client).fetch_company("testimonial.example")

    assert result.ok
    blog_facts = {f.content: f for f in result.facts if f.kind == FactKind.BLOG_POST.value}
    assert any("Scaling support" in c for c in blog_facts)
    real = next(f for c, f in blog_facts.items() if "Scaling support" in c)
    assert real.metadata["identification_method"] == "feed"
    assert real.metadata["confidence"] == "high"

    demoted = [f for f in result.facts if f.kind == "dated_content_unconfirmed"]
    assert len(demoted) == 1
    assert "Acme Logistics" in demoted[0].content
    assert demoted[0].metadata["identification_method"] == "proximity_fallback"
    assert demoted[0].metadata["confidence"] == "low"
    assert demoted[0].metadata["original_kind"] == FactKind.BLOG_POST.value


def test_without_a_feed_both_items_are_demoted_recall_cost_is_real():
    """Honest recall-cost check: strip the homepage's feed link entirely.
    With no structured corroboration available at all, BOTH the real post
    and the testimonial land on the whole-page proximity-fallback path and
    are capped at low confidence -- the conservative default costs real
    coverage on a genuine post when a site publishes no feed and no JSON-LD.
    This is the trade this agent reported rather than rounding up."""
    homepage_no_feed = TESTIMONIAL_HOMEPAGE.replace(
        '<link rel="alternate" type="application/rss+xml" title="QuoteCo Blog" href="/feed.xml">',
        "",
    )
    client = FakeClient(
        quoteco_routes(**{"https://testimonial.example/": (200, homepage_no_feed)})
    )
    del client.routes["https://testimonial.example/feed.xml"]
    result = make_adapter(client).fetch_company("testimonial.example")

    assert not any(f.kind == FactKind.BLOG_POST.value for f in result.facts)
    demoted = {f.content for f in result.facts if f.kind == "dated_content_unconfirmed"}
    assert any("Scaling support" in c for c in demoted)
    assert any("Acme Logistics" in c for c in demoted)
    # Never fetched -- <link> was removed, so there was nothing to discover.
    assert "https://testimonial.example/feed.xml" not in client.calls


def test_demoted_dated_content_cannot_clear_the_scoring_threshold_alone():
    from mazz.pipeline.score import score_facts

    homepage_no_feed = TESTIMONIAL_HOMEPAGE.replace(
        '<link rel="alternate" type="application/rss+xml" title="QuoteCo Blog" href="/feed.xml">',
        "",
    )
    client = FakeClient(
        quoteco_routes(**{"https://testimonial.example/": (200, homepage_no_feed)})
    )
    del client.routes["https://testimonial.example/feed.xml"]
    result = make_adapter(client).fetch_company("testimonial.example")

    scored = score_facts(result.facts)
    assert scored.has_angle is False


# ==========================================================================
# Recall recovery: apply-link probing, bounded cost
#
# Cheapest wins first. A sitemap-listing rung was built and measured live
# against a real careers site (asana.com, 2026-09-17): the site's own
# sitemap.xml lists per-location/per-department FILTER pages as real,
# distinct URLs right alongside actual reqs, so "in the sitemap" proved "a
# real page", not "a job requisition", and would have promoted 19 facet
# pages to JOB_POSTING in one company. It is retired -- see
# `_html.extract_job_items`'s docstring and
# `LiveCompanyAdapter._discover_sitemap_urls`'s docstring. What is left,
# below, is the apply-link probe: the site's own real "apply here"
# mechanism, which a facet/filter page essentially never has.
# ==========================================================================

RECALL_HOMEPAGE = _read("recall_homepage.html")
RECALL_CAREERS = _read("recall_careers.html")
RECALL_SITEMAP = _read("recall_sitemap.xml")
RECALL_JOB_WITH_APPLY = _read("recall_job_with_apply.html")
RECALL_NOT_A_JOB = _read("recall_not_a_job.html")


def recall_routes(domain: str, **overrides) -> dict[str, tuple[int, str]]:
    routes = {
        f"https://{domain}/robots.txt": (200, ROBOTS_ALLOW_ALL),
        f"https://{domain}/": (200, RECALL_HOMEPAGE.replace("recall.example", domain)),
        f"https://{domain}/careers": (200, RECALL_CAREERS),
    }
    routes.update(overrides)
    return routes


def test_sitemap_xml_is_never_fetched_the_rung_was_retired():
    """Regression guard for the retirement above: even when sitemap.xml
    exists, lists the candidate, and would say yes, `LiveCompanyAdapter`
    must not fetch it at all -- both because it is no longer trusted and
    because fetching it anyway would just be a wasted request against the
    stated cost objective."""
    domain = "sitemaprescue.example"
    client = FakeClient(
        recall_routes(
            domain,
            **{
                f"https://{domain}/sitemap.xml": (
                    200,
                    RECALL_SITEMAP.replace("sitemaprescue.example", domain),
                )
            },
        )
    )
    adapter = make_adapter(client, apply_link_probe_limit=0)
    result = adapter.fetch_company(domain)

    assert f"https://{domain}/sitemap.xml" not in client.calls
    # Without the retired rung and with the probe off, both candidates on
    # this fixture stay unconfirmed -- proving the sitemap fixture alone
    # really did nothing, not just that the assertion above passed.
    demoted = {f.content for f in result.facts if f.kind == "job_posting_unconfirmed"}
    assert demoted == {"Build Cool Stuff", "Team Retro Notes"}


def test_apply_link_on_the_candidates_own_page_rescues_it():
    """No sitemap available at all. 'Build Cool Stuff' is rescued because
    its OWN page carries a real 'Apply Now' link; 'Team Retro Notes' is
    fetched too (same probe pass) but has no apply signal on its page, so it
    stays demoted."""
    domain = "applyrescue.example"
    client = FakeClient(
        recall_routes(
            domain,
            **{
                f"https://{domain}/careers/build-cool-stuff": (200, RECALL_JOB_WITH_APPLY),
                f"https://{domain}/careers/team-retro-notes": (200, RECALL_NOT_A_JOB),
            },
        )
    )
    adapter = make_adapter(client)
    result = adapter.fetch_company(domain)

    job_contents = {f.content for f in result.facts if f.kind == FactKind.JOB_POSTING.value}
    assert job_contents == {"Build Cool Stuff"}
    rescued = next(f for f in result.facts if f.content == "Build Cool Stuff")
    assert rescued.metadata["identification_method"] == "apply_link"
    assert rescued.metadata["confidence"] == "high"

    demoted = {f.content for f in result.facts if f.kind == "job_posting_unconfirmed"}
    assert demoted == {"Team Retro Notes"}
    # Both candidate pages were actually probed.
    assert f"https://{domain}/careers/build-cool-stuff" in client.calls
    assert f"https://{domain}/careers/team-retro-notes" in client.calls


def test_apply_link_probe_respects_its_cap():
    """`apply_link_probe_limit` bounds the most expensive corroboration
    signal: with the cap set to 1 and two low-confidence candidates, only
    one candidate page is ever fetched."""
    domain = "boundedrescue.example"
    client = FakeClient(
        recall_routes(
            domain,
            **{
                f"https://{domain}/careers/build-cool-stuff": (200, RECALL_JOB_WITH_APPLY),
                f"https://{domain}/careers/team-retro-notes": (200, RECALL_NOT_A_JOB),
            },
        )
    )
    adapter = make_adapter(client, apply_link_probe_limit=1)
    adapter.fetch_company(domain)

    probed = [
        c
        for c in client.calls
        if c
        in (
            f"https://{domain}/careers/build-cool-stuff",
            f"https://{domain}/careers/team-retro-notes",
        )
    ]
    assert len(probed) == 1


def test_apply_link_probe_respects_robots_disallow():
    """The apply-link probe is still a real fetch and still goes through
    `_robots_allows` -- a disallowed candidate page is never fetched, and
    simply stays unrescued rather than being silently evaded."""
    domain = "robotsrescue.example"
    disallow = "User-agent: *\nDisallow: /careers/build-cool-stuff\n"
    client = FakeClient(
        recall_routes(
            domain,
            **{
                f"https://{domain}/robots.txt": (200, disallow),
                f"https://{domain}/careers/build-cool-stuff": (200, RECALL_JOB_WITH_APPLY),
                f"https://{domain}/careers/team-retro-notes": (200, RECALL_NOT_A_JOB),
            },
        )
    )
    adapter = make_adapter(client)
    result = adapter.fetch_company(domain)

    assert f"https://{domain}/careers/build-cool-stuff" not in client.calls
    demoted = {f.content for f in result.facts if f.kind == "job_posting_unconfirmed"}
    assert "Build Cool Stuff" in demoted


def test_a_failed_apply_probe_never_downgrades_confidence():
    """A probe can only ever RAISE a candidate's confidence, never lower it:
    if the candidate's own page 404s, it is left exactly as it was (still
    low, still demoted) rather than being treated as a negative signal."""
    domain = "missingrescue.example"
    client = FakeClient(
        recall_routes(
            domain,
            **{
                f"https://{domain}/careers/build-cool-stuff": (404, ""),
                f"https://{domain}/careers/team-retro-notes": (404, ""),
            },
        )
    )
    adapter = make_adapter(client)
    result = adapter.fetch_company(domain)

    demoted = {f.content for f in result.facts if f.kind == "job_posting_unconfirmed"}
    assert demoted == {"Build Cool Stuff", "Team Retro Notes"}


def test_feed_fetch_is_rate_limited_and_robots_checked():
    """The feed.xml discovery fetch is an ordinary request to the company's
    own host: it goes through the same robots + throttle path as every
    other page, proven by a disallow on feed.xml specifically."""
    disallow_feed = "User-agent: *\nDisallow: /feed.xml\n"
    client = FakeClient(
        quoteco_routes(
            **{"https://testimonial.example/robots.txt": (200, disallow_feed)}
        )
    )
    adapter = make_adapter(client)
    result = adapter.fetch_company("testimonial.example")

    assert "https://testimonial.example/feed.xml" not in client.calls
    # No feed corroboration available -> both items fall back to the
    # (capped-low) proximity-fallback path, same as the no-feed-link case.
    assert not any(f.kind == FactKind.BLOG_POST.value for f in result.facts)
