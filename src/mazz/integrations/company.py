"""Company-site enrichment: homepage, careers/jobs, blog/changelog/press.

Keyed by company DOMAIN, not by person -- multiple leads routinely share an
employer, and the research cache is domain-keyed for exactly that reason. The
adapter therefore takes a domain and `normalize_domain` is exported so the
pipeline's cache key and the adapter's lookup key are provably the same string;
if they drifted, the cache would miss and we would pay for the same scrape twice.

What we look for is anything RECENTLY DATED. Job postings rank highest
downstream: a req for a role the client's product supports is a concrete reason
to reach out now. Funding news is nearly worthless -- every competitor's tool
already opens with it -- so it is tagged as its own kind and left for the scorer
to discount rather than being silently dropped here. An adapter classifies; it
does not judge.

`StubCompanyAdapter` reads canned fixtures; no network. `LiveCompanyAdapter`
does the real fetch -- homepage, then careers/blog/changelog/press discovered
from the homepage's own links -- against real HTTP, and is the "own research
engine" spike: can company-site scraping alone replace paid LinkedIn/company
enrichment? See `pages_payload_to_facts` for why both adapters share one
normalization path, and `mazz.integrations._html` for the (dependency-free)
parsing underneath the live one.
"""

from __future__ import annotations

import abc
import re
import time
import urllib.robotparser as robotparser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a project dependency in practice
    httpx = None  # type: ignore[assignment]

from mazz.integrations._html import (
    extract_dated_items,
    extract_job_items,
    extract_meta_description,
    extract_title,
    fallback_candidates,
    find_external_ats_link,
    find_page_links,
)
from mazz.integrations.base import (
    Fact,
    FactKind,
    FetchResult,
    InstrumentedProvider,
    ProviderBadResponse,
    ProviderBlocked,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
    build_fact,
    call_with_retry,
    default_fixtures_dir,
    load_json_fixture,
    maybe_simulate_failure,
    parse_timestamp,
    setting,
    utc_now,
)

__all__ = [
    "PAGE_KINDS",
    "PAGE_KIND_TO_FACT_KIND",
    "InvalidDomain",
    "CompanyAdapter",
    "StubCompanyAdapter",
    "LiveCompanyAdapter",
    "normalize_domain",
    "pages_payload_to_facts",
    "get_company_adapter",
]

STUB_PROVIDER = "company-stub"

PAGE_KINDS = ("homepage", "careers", "blog", "changelog", "press")

# Careers-page ITEMS are individual reqs, so they map to JOB_POSTING (the
# highest-value signal downstream), not to CAREERS_PAGE, which describes the page
# as a whole and carries no reason to reach out on its own.
PAGE_KIND_TO_FACT_KIND: Mapping[str, str] = {
    "homepage": FactKind.HOMEPAGE.value,
    "careers": FactKind.JOB_POSTING.value,
    "blog": FactKind.BLOG_POST.value,
    "changelog": FactKind.CHANGELOG.value,
    "press": FactKind.PRESS.value,
}

_KNOWN_FACT_KINDS = {k.value for k in FactKind}

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class InvalidDomain(ValueError):
    """Bad domain on the lead record -- an input problem, not a provider outage."""


def normalize_domain(value: str) -> str:
    """`https://WWW.Foo.com/careers?ref=x` -> `foo.com`.

    THE cache key. Export-and-share rather than reimplement: two leads at one
    employer must normalize to one identical string or the domain-keyed cache
    quietly stops working and we pay twice.
    """
    text = (value or "").strip().lower()
    if not text:
        raise InvalidDomain("company domain is empty")
    text = _SCHEME_RE.sub("", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:  # tolerate an email being passed in
        text = text.rsplit("@", 1)[1]
    text = text.split(":", 1)[0]  # strip port
    if text.startswith("www."):
        text = text[4:]
    text = text.rstrip(".")
    if not _DOMAIN_RE.match(text):
        raise InvalidDomain(f"{value!r} does not look like a company domain")
    return text


class CompanyAdapter(InstrumentedProvider, abc.ABC):
    """Vendor-neutral company-site adapter. The scraping vendor/technique is not
    decided either, so nothing about it crosses this interface."""

    provider: str = "company"

    def __init__(self, *, retry_policy: RetryPolicy | None = None, sleep=None) -> None:
        InstrumentedProvider.__init__(self)
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def fetch_company(self, domain: str) -> FetchResult:
        """Fetch everything recently dated for one company domain.

        Raises `InvalidDomain` for junk input and `ProviderError` on failure.
        Returns a `FetchResult` whose status separates "found facts" from "site
        reachable but nothing dated" from "no such site" -- a failure never
        returns an empty list that could be mistaken for `no_angle`.
        """
        target = normalize_domain(domain)
        self._record("fetch_company", target)

        kwargs = {"policy": self.retry_policy}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        payload = call_with_retry(lambda: self._fetch_payload(target), **kwargs)

        if payload is None:
            return FetchResult.not_found(self.provider, target)

        facts = self._payload_to_facts(target, payload)
        return FetchResult.found(
            self.provider,
            target,
            facts,
            retrieved_at=parse_timestamp(payload.get("retrieved_at")),
            raw_ref=str(payload.get("__source__") or ""),
        )

    @abc.abstractmethod
    def _fetch_payload(self, domain: str) -> Mapping[str, Any] | None:
        """Raw payload, or None if there is nothing at that domain. Raise a
        `ProviderError` subclass on failure -- never return None for one."""

    @abc.abstractmethod
    def _payload_to_facts(self, domain: str, payload: Mapping[str, Any]) -> Sequence[Fact]:
        """Normalize into `Fact`s. The only scrape-shape-aware code."""


def pages_payload_to_facts(
    *, provider: str, domain: str, payload: Mapping[str, Any]
) -> list[Fact]:
    """Normalize a `{"pages": [...]}` payload into `Fact`s.

    Shared by every adapter that speaks this page/item shape -- the stub (from
    a fixture) and `LiveCompanyAdapter` (from a real scrape) alike -- so the two
    cannot drift into two different notions of what a "careers item" or a
    "dated post" becomes downstream. A vendor swap only has to produce this
    shape; it never has to reimplement the scoring-relevant normalization.
    """
    default_retrieved_at = parse_timestamp(payload.get("retrieved_at"))
    pages = payload.get("pages") or []
    if not isinstance(pages, list):
        raise ProviderBadResponse("pages must be a list", provider=provider, target=domain)

    facts: list[Fact] = []
    for page in pages:
        if not isinstance(page, Mapping):
            raise ProviderBadResponse(
                "each page must be an object", provider=provider, target=domain
            )
        page_kind = str(page.get("kind") or "").strip().lower()
        if page_kind not in PAGE_KIND_TO_FACT_KIND:
            raise ProviderBadResponse(
                f"unknown page kind {page_kind!r} (expected one of {PAGE_KINDS})",
                provider=provider,
                target=domain,
            )
        page_url = page.get("url") or f"https://{domain}/"

        for item in page.get("items") or []:
            if not isinstance(item, Mapping):
                raise ProviderBadResponse(
                    "each item must be an object", provider=provider, target=domain
                )
            text = item.get("text")
            if not text or not str(text).strip():
                continue
            # An item may override the page's default kind: a press page can
            # carry funding news, which the scorer treats very differently
            # from a product announcement.
            fact_kind = str(item.get("fact_kind") or PAGE_KIND_TO_FACT_KIND[page_kind])
            if fact_kind not in _KNOWN_FACT_KINDS:
                # A typo here would not raise downstream -- Fact.kind is a
                # plain str -- it would just score as unknown/weak forever.
                raise ProviderBadResponse(
                    f"fact_kind {fact_kind!r} is outside the shared FactKind vocabulary",
                    provider=provider,
                    target=domain,
                )
            source_url = item.get("url") or page_url
            natural_key = item.get("key")
            if not natural_key:
                raise ProviderBadResponse(
                    f"item on {source_url} has no 'key'; fact ids must be stable "
                    "and a key is the only durable handle we have",
                    provider=provider,
                    target=domain,
                )
            published_at = item.get("published_at")
            content = str(text).strip()
            if published_at:
                content = f"[{published_at}] {content}"
            facts.append(
                build_fact(
                    kind=fact_kind,
                    content=content,
                    source_url=source_url,
                    retrieved_at=parse_timestamp(
                        item.get("retrieved_at"), default=default_retrieved_at
                    ),
                    natural_key=str(natural_key),
                    # Company facts are about the COMPANY, so no subject_email:
                    # they are not pruned by delete-by-person.
                    metadata={
                        "page_kind": page_kind,
                        "published_at": published_at,
                        "domain": domain,
                    },
                )
            )
    return facts


class StubCompanyAdapter(CompanyAdapter):
    """Phase 1: reads `tests/fixtures/company/<domain>.json`. No network."""

    provider = STUB_PROVIDER

    def __init__(
        self,
        *,
        fixtures_dir: Path | str | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep=None,
    ) -> None:
        super().__init__(retry_policy=retry_policy, sleep=sleep)
        base = Path(fixtures_dir) if fixtures_dir else default_fixtures_dir()
        self.fixtures_dir = base / "company" if base.name != "company" else base

    def _fetch_payload(self, domain: str) -> Mapping[str, Any] | None:
        path = self.fixtures_dir / f"{domain}.json"
        payload = load_json_fixture(path, provider=self.provider, target=domain)
        if payload is None:
            return None
        maybe_simulate_failure(payload, provider=self.provider, target=domain)
        return {**payload, "__source__": str(path)}

    def _payload_to_facts(self, domain: str, payload: Mapping[str, Any]) -> list[Fact]:
        return pages_payload_to_facts(provider=self.provider, domain=domain, payload=payload)


LIVE_PROVIDER = "company-live"

# Identify honestly. No browser spoofing, no rotating UA strings -- if a site
# wants to block this, "we look like a bot" must be enough information for it
# to do so, which is the whole point of an honest User-Agent.
DEFAULT_USER_AGENT = "MazzResearchBot/0.1 (+outbound sales research; contact via site operator)"


class LiveCompanyAdapter(CompanyAdapter):
    """The "own research engine" spike: fetch the company's own site for real.

    Homepage first, then careers/blog/changelog/press discovered from the
    homepage's own nav/footer links (a fixed set of common paths is tried only
    as a fallback, per-URL, if discovery finds nothing). Careers items map to
    `JOB_POSTING`; the rest map per `PAGE_KIND_TO_FACT_KIND` via the same
    `pages_payload_to_facts` the stub uses, so this class's only real job is
    turning HTML into that shared `{"pages": [...]}` shape.

    Compliance posture (non-negotiable, not a preference):
      * `robots.txt` is checked before every fetch, including a same-hop
        follow onto a third-party ATS the careers page links out to.
      * requests to one domain are spaced at least `rate_limit_seconds` apart.
      * the User-Agent always identifies this as a bot.
      * a robots disallow or a 401/403 STOPS on that target -- there is no
        retry-with-different-headers, no proxy rotation, no CAPTCHA solving.

    Failure semantics: if the homepage itself cannot be reached or is
    robots-disallowed, `_fetch_payload` raises (`ProviderBlocked` /
    `ProviderUnavailable`) -- we could not research this company at all, which
    must never look like the legitimate "researched it, nothing dated" EMPTY
    result. A secondary page (careers/blog/changelog/press) that is blocked,
    missing, or fails is skipped and recorded in the payload's
    `blocked_pages`/`skipped_pages`; that is a normal, honest partial result,
    not a provider failure.
    """

    provider = LIVE_PROVIDER

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep=None,
        http_client: Any = None,
        user_agent: str | None = None,
        rate_limit_seconds: float = 1.0,
        request_timeout: float = 10.0,
    ) -> None:
        super().__init__(retry_policy=retry_policy, sleep=sleep)
        if http_client is None:
            if httpx is None:  # pragma: no cover - exercised only without the dep installed
                raise ImportError(
                    "LiveCompanyAdapter needs httpx (or an injected http_client); "
                    "it is not installed in this environment"
                )
            http_client = httpx.Client(follow_redirects=True, timeout=request_timeout)
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = http_client
        self._user_agent = user_agent or str(
            setting("COMPANY_SCRAPER_USER_AGENT", DEFAULT_USER_AGENT)
        )
        self._rate_limit_seconds = rate_limit_seconds
        self._robots_cache: dict[str, robotparser.RobotFileParser] = {}
        self._last_request_at: dict[str, float] = {}

    def close(self) -> None:
        """Release the owned HTTP client. No-op for an injected one."""
        if self._owns_client:
            self._client.close()

    # -- politeness: robots + per-domain rate limit --------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}

    def _throttle(self, netloc: str) -> None:
        wait_fn = self._sleep or time.sleep
        last = self._last_request_at.get(netloc)
        now = time.monotonic()
        if last is not None:
            remaining = self._rate_limit_seconds - (now - last)
            if remaining > 0:
                wait_fn(remaining)
        self._last_request_at[netloc] = time.monotonic()

    def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_cache.get(origin)
        if rp is None:
            rp = robotparser.RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                self._throttle(parsed.netloc)
                resp = self._client.get(robots_url, headers=self._headers)
                if resp.status_code >= 400:
                    rp.parse([])  # no robots.txt on this host -> default allow
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError:
                # Could not retrieve robots.txt at all. Fail OPEN here rather
                # than manufacturing a block: a transient network hiccup on
                # /robots.txt is not a site telling us to stay away, and the
                # target fetch itself is still separately subject to the
                # site's real access controls (401/403 -> ProviderBlocked).
                rp.parse([])
            self._robots_cache[origin] = rp
        return rp.can_fetch(self._user_agent, url)

    # -- one fetch, honestly classified ---------------------------------

    def _get(self, url: str):
        """GET one URL. Returns the response, or None for a 404/410 (the
        resource genuinely is not there). Raises for everything else that
        is not a plain 2xx."""
        netloc = urlparse(url).netloc
        self._throttle(netloc)
        try:
            resp = self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"network error fetching {url}: {exc}", provider=self.provider, target=url
            ) from exc

        if resp.status_code in (404, 410):
            return None
        if resp.status_code == 429:
            raise ProviderRateLimited(
                f"rate limited fetching {url}",
                provider=self.provider,
                target=url,
                status_code=429,
            )
        if resp.status_code in (401, 403):
            # The site is refusing us outright. We do not evade this -- we
            # stop and report it, per the hard compliance directive.
            raise ProviderBlocked(
                f"{resp.status_code} fetching {url} -- site refused the request",
                provider=self.provider,
                target=url,
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise ProviderUnavailable(
                f"{resp.status_code} fetching {url}",
                provider=self.provider,
                target=url,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            # Some other 4xx (400 on a guessed fallback path, etc.) -- treat
            # as "not there", the same as a 404, not as a block or an outage.
            return None
        return resp

    # -- the crawl --------------------------------------------------------

    def _fetch_payload(self, domain: str) -> Mapping[str, Any] | None:
        homepage_url = f"https://{domain}/"
        if not self._robots_allows(homepage_url):
            raise ProviderBlocked(
                f"robots.txt disallows {homepage_url}", provider=self.provider, target=domain
            )
        resp = self._get(homepage_url)
        if resp is None:
            # Try plain HTTP once before giving up on the domain entirely --
            # some small sites still do not redirect cleanly to HTTPS.
            fallback_url = f"http://{domain}/"
            if not self._robots_allows(fallback_url):
                raise ProviderBlocked(
                    f"robots.txt disallows {fallback_url}", provider=self.provider, target=domain
                )
            resp = self._get(fallback_url)
            if resp is None:
                return None
            homepage_url = fallback_url

        homepage_html = resp.text
        pages: list[dict[str, Any]] = [
            {
                "kind": "homepage",
                "url": homepage_url,
                "items": self._homepage_items(homepage_html, homepage_url),
            }
        ]

        blocked_pages: list[str] = []
        skipped_pages: list[str] = []
        discovered = find_page_links(homepage_html, homepage_url)
        for kind in ("careers", "blog", "changelog", "press"):
            page = self._fetch_secondary_page(
                kind,
                discovered.get(kind),
                homepage_url,
                blocked=blocked_pages,
                skipped=skipped_pages,
            )
            if page is not None:
                pages.append(page)

        return {
            "domain": domain,
            "retrieved_at": utc_now().isoformat(),
            "pages": pages,
            "blocked_pages": blocked_pages,
            "skipped_pages": skipped_pages,
            "__source__": homepage_url,
        }

    def _homepage_items(self, html: str, url: str) -> list[dict[str, Any]]:
        description = extract_meta_description(html) or extract_title(html)
        if not description:
            return []
        return [{"key": "homepage", "url": url, "text": description, "published_at": None}]

    def _fetch_secondary_page(
        self,
        kind: str,
        discovered_url: str | None,
        homepage_url: str,
        *,
        blocked: list[str],
        skipped: list[str],
    ) -> dict[str, Any] | None:
        """Fetch one of careers/blog/changelog/press. Never raises: a page
        that is blocked, missing, or errors is recorded and skipped -- only
        the homepage itself is load-bearing enough to fail the whole company.
        """
        candidates: list[str] = []
        if discovered_url:
            candidates.append(discovered_url)
        for candidate in fallback_candidates(kind, homepage_url):
            if candidate not in candidates:
                candidates.append(candidate)

        resp = None
        page_url: str | None = None
        for candidate in candidates:
            if not self._robots_allows(candidate):
                blocked.append(candidate)
                continue
            try:
                resp = self._get(candidate)
            except ProviderBlocked:
                blocked.append(candidate)
                continue
            except ProviderError:
                skipped.append(candidate)
                continue
            if resp is None:
                skipped.append(candidate)
                continue
            page_url = candidate
            break

        if resp is None or page_url is None:
            return None

        html = resp.text
        if kind == "careers":
            items = extract_job_items(html, page_url)
            if not items:
                ats_link = find_external_ats_link(html, page_url)
                if ats_link:
                    if not self._robots_allows(ats_link):
                        blocked.append(ats_link)
                    else:
                        try:
                            ats_resp = self._get(ats_link)
                        except ProviderError:
                            ats_resp = None
                        if ats_resp is not None:
                            hop_items = extract_job_items(ats_resp.text, ats_link)
                            if hop_items:
                                items, page_url = hop_items, ats_link
        else:
            items = extract_dated_items(html, page_url)

        if not items:
            return None
        return {"kind": kind, "url": page_url, "items": items}

    def _payload_to_facts(self, domain: str, payload: Mapping[str, Any]) -> list[Fact]:
        return pages_payload_to_facts(provider=self.provider, domain=domain, payload=payload)


def get_company_adapter(
    *,
    provider: str | None = None,
    fixtures_dir: Path | str | None = None,
    retry_policy: RetryPolicy | None = None,
) -> CompanyAdapter:
    """Pick an adapter from `COMPANY_SCRAPER_PROVIDER`. Phase 1 default: stub."""
    name = str(provider or setting("COMPANY_SCRAPER_PROVIDER", "stub")).strip().lower()
    if name in ("stub", "", "fixture", "fixtures"):
        return StubCompanyAdapter(fixtures_dir=fixtures_dir, retry_policy=retry_policy)
    if name in ("live", "http", "own", "own_engine"):
        return LiveCompanyAdapter(retry_policy=retry_policy)
    raise NotImplementedError(
        f"company scraper provider {name!r} is not implemented. Subclass "
        "CompanyAdapter and implement _fetch_payload/_payload_to_facts."
    )
