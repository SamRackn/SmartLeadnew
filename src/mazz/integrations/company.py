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

Classifying is not the same as trusting. Verification (a later pipeline step)
proves a drafted claim traces to a cached Fact; it can never prove the Fact
itself is true. So a JOB_POSTING item extracted with nothing to corroborate it
beyond "this href is a sub-path of the careers page" -- indistinguishable from
a department filter control or a policy page sharing that same path pattern --
is refused at that STRONG-tier kind here, at ingest, rather than trusted
through to the drafter. See `UNCONFIRMED_JOB_POSTING_KIND` below for the
mechanism and why it fails closed without any pipeline change.
"""

from __future__ import annotations

import abc
import re
import time
import urllib.robotparser as robotparser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a project dependency in practice
    httpx = None  # type: ignore[assignment]

from mazz.integrations._brightdata import (
    LINKEDIN_COMPANY_DATASET_ID,
    BillingMeter,
    parse_relative_time,
    scrape_one,
)
from mazz.integrations._html import (
    extract_dated_items,
    extract_headings,
    extract_job_items,
    extract_meta_description,
    extract_sitemap_urls,
    extract_title,
    fallback_candidates,
    find_external_ats_link,
    find_feed_link,
    find_page_links,
    page_has_apply_signal,
    parse_feed_dates,
)
from mazz.integrations.base import (
    Fact,
    FactKind,
    FetchResult,
    InstrumentedProvider,
    ProviderAuthError,
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
from mazz.integrations.safe_http import (
    ResearchResponseTooLarge,
    SafeHTTPClient,
    UnsafeResearchURL,
)

__all__ = [
    "ProviderUnsafeTarget",
    "PAGE_KINDS",
    "QUALIFICATION_PAGE_KINDS",
    "EVENT_PAGE_KINDS",
    "PAGE_KIND_TO_FACT_KIND",
    "UNCONFIRMED_JOB_POSTING_KIND",
    "UNCONFIRMED_DATED_CONTENT_KIND",
    "InvalidDomain",
    "CompanyAdapter",
    "StubCompanyAdapter",
    "LiveCompanyAdapter",
    "BrightDataCompanyAdapter",
    "NoLinkedInCompanyURL",
    "normalize_domain",
    "pages_payload_to_facts",
    "get_company_adapter",
]

STUB_PROVIDER = "company-stub"

#: Pages an adapter may emit. The first three are the qualification pages --
#: where a business says what it is and invites new work -- and they are what
#: the cold path actually reads (`pipeline.qualify`). The rest belong to the
#: retired event-research stack and are kept so that cached payloads and
#: fixtures holding them still parse.
PAGE_KINDS = ("homepage", "services", "contact", "careers", "blog", "changelog", "press")

#: Pages fetched on the cold path, in the order they are tried. Two secondary
#: requests per domain, cached by domain, versus the six-to-eight the event
#: stack made -- and the two are the ones the copy depends on.
QUALIFICATION_PAGE_KINDS = ("services", "contact")

#: The event pages. Not fetched by default any more: the ICP is small
#: professional-services firms that publish none of them, so four requests per
#: domain were buying facts nothing reads. The extractors behind them are still
#: here and still tested -- pass ``secondary_page_kinds=EVENT_PAGE_KINDS`` to
#: exercise them -- because deep research on a lead that REPLIED is a different
#: economic proposition from deep research on a cold list.
EVENT_PAGE_KINDS = ("careers", "blog", "changelog", "press")

# Careers-page ITEMS are individual reqs, so they map to JOB_POSTING (the
# highest-value signal downstream), not to CAREERS_PAGE, which describes the page
# as a whole and carries no reason to reach out on its own.
PAGE_KIND_TO_FACT_KIND: Mapping[str, str] = {
    "homepage": FactKind.HOMEPAGE.value,
    "services": FactKind.SERVICES_PAGE.value,
    "contact": FactKind.CONTACT_PAGE.value,
    "careers": FactKind.JOB_POSTING.value,
    "blog": FactKind.BLOG_POST.value,
    "changelog": FactKind.CHANGELOG.value,
    "press": FactKind.PRESS.value,
}

_KNOWN_FACT_KINDS = {k.value for k in FactKind}

# --------------------------------------------------------------------------
# Research provenance at cache-ingest (CLAUDE.md "Known gap in the guardrails")
# --------------------------------------------------------------------------
#
# Verification proves a drafted claim traces to a cached Fact. It does NOT
# prove the Fact itself is true -- a scraper that misidentifies a filter
# control or a policy page as a job req puts a false JOB_POSTING in
# research_cache, and score.py's job_posting_supported weight (75.0, the
# single highest score in the system) lets that one bad fact single-handedly
# clear the 40.0 threshold and produce a verified, human-approvable email
# asserting a role that does not exist. That is legal exposure in cold
# outreach, not a quality nit (measured live in the W3 own-engine spike:
# Trello-style JS careers hubs where every "job" is a department filter, and
# Zapier-style careers paths mixing real reqs with policy pages).
#
# The fix has to live at INGEST, because verification cannot see it: a false
# job req is exactly as "verifiable" as a real one once it is in the cache.
# `_html.extract_job_items` tags every candidate with how it was identified
# (`identification_method`) and how much that method is worth
# (`confidence`: "high" / "medium" / "low"). Below, `pages_payload_to_facts`
# refuses to admit a "low"-confidence candidate at the `JOB_POSTING` kind --
# it demotes it to `UNCONFIRMED_JOB_POSTING_KIND` instead.
#
# That sentinel string is deliberately OUTSIDE `FactKind`'s vocabulary.
# `Fact.kind` is a plain `str` precisely so a producer can express something
# the shared vocabulary has no name for yet (see `mazz.schemas`), and
# `score.py` already treats any kind it does not recognise as
# `SignalTier.WEAK` at its lowest base weight (`unknown_kind`), and
# structurally excludes WEAK facts from ever qualifying alone (the same rule
# that keeps funding news from single-handedly producing a draft). So this
# fails closed with ZERO changes to `mazz/pipeline/score.py`: a downgraded
# fact cannot promote a lead out of `no_angle` by itself, today, as shipped.
#
# It is NOT dropped -- the fact is still emitted, with its content, source
# URL and a `provenance_note` explaining the demotion, so it is visible in
# the research cache and review UI as "we found something and did not trust
# it enough to call it a job posting", never a silent empty that could be
# mistaken for "nothing here."
#
# `score.py` can be taught to do something smarter with this than the
# generic unknown-kind fallback (e.g. recognise the sentinel kind
# explicitly, or read `metadata["confidence"]` for a finer-grained
# multiplier) without a migration -- the string and the metadata are already
# flowing through `research_cache` today. See this module's docstring /
# integrations-engineer's handoff note for the exact ask.
UNCONFIRMED_JOB_POSTING_KIND = "job_posting_unconfirmed"

# The same gap, one tier down. `blog_post` (42.0), `changelog` (45.0) and
# `press` (40.0) each clear the 40.0 threshold ALONE when fresh -- and
# recency is most of that score, so a misidentified block (a nav element, a
# cookie banner, a dated testimonial) does not just add a bad fact, it adds
# one at maximum weight if a wrong-but-fresh-looking date gets attached to
# it. `_html.extract_dated_items` tags every item the same way
# (`identification_method` / `confidence`); a "low"-confidence one is
# demoted here exactly like a low-confidence job candidate, for the same
# fails-closed-with-zero-score.py-changes reason.
UNCONFIRMED_DATED_CONTENT_KIND = "dated_content_unconfirmed"

# Fact kinds this ingest-provenance check covers. A candidate resolving to one
# of these gets identification_method/confidence recorded in metadata, and is
# demoted to the matching *_unconfirmed sentinel if confidence is "low".
_JOB_POSTING_KINDS = {FactKind.JOB_POSTING.value}
_DATED_CONTENT_KINDS = {
    FactKind.BLOG_POST.value,
    FactKind.CHANGELOG.value,
    FactKind.PRESS.value,
}

# identification_method values that are not, on their own, trustworthy enough
# to admit a candidate at its nominal (STRONG- or MEDIUM-tier) kind. Keyed by
# the *confidence* level the producer (`_html.extract_job_items`,
# `_html.extract_dated_items`, or any future producer of this item shape)
# assigns, not by method name, so a new low-trust method can be added there
# without touching this module.
_LOW_CONFIDENCE_LEVELS = {"low"}

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class ProviderUnsafeTarget(ProviderError):
    """The destination is private, loopback, link-local, metadata, or otherwise
    not somewhere research may fetch (see `safe_http`).

    Deliberately NOT `ProviderBlocked`: a blocked site is a site that refused
    US, and qualification may then fall back on the lead's own record. An
    unsafe destination is a refusal on OUR side -- the record points somewhere
    we must not go -- and must never be able to qualify a lead. Not retryable.
    """


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

            # -- provenance at ingest: how was this item identified, and is
            # that good enough evidence to claim its nominal kind (STRONG for
            # a job posting, MEDIUM for blog/changelog/press)? See the
            # module-level comments above `UNCONFIRMED_JOB_POSTING_KIND` /
            # `UNCONFIRMED_DATED_CONTENT_KIND`. Every other kind (funding,
            # homepage, careers_page...) is untouched. Absent fields mean
            # "declared": hand-authored fixture data and already-structured
            # provider payloads (Bright Data's LinkedIn `updates`, emitted as
            # BLOG_POST) are real self-published content, not an inference
            # this check needs to second-guess -- they default to high.
            is_job_posting_candidate = fact_kind in _JOB_POSTING_KINDS
            is_dated_content_candidate = fact_kind in _DATED_CONTENT_KINDS
            has_provenance_signal = is_job_posting_candidate or is_dated_content_candidate
            identification_method = str(item.get("identification_method") or "declared")
            confidence = str(item.get("confidence") or "high")
            provenance_note: str | None = None
            demoted_from_kind: str | None = None
            if is_job_posting_candidate and confidence in _LOW_CONFIDENCE_LEVELS:
                demoted_from_kind = fact_kind
                provenance_note = (
                    f"downgraded from {FactKind.JOB_POSTING.value}: identified only by "
                    f"{identification_method!r} -- no ATS host, href keyword, job-title "
                    "vocabulary, apply-link, or JSON-LD corroboration. Indistinguishable "
                    "from a filter control or a policy page under the same path pattern."
                )
                fact_kind = UNCONFIRMED_JOB_POSTING_KIND
            elif is_dated_content_candidate and confidence in _LOW_CONFIDENCE_LEVELS:
                demoted_from_kind = fact_kind
                provenance_note = (
                    f"downgraded from {fact_kind}: its date was identified only by "
                    f"{identification_method!r} -- no JSON-LD/feed corroboration and "
                    "no machine-readable <time> element in a genuine content block. "
                    "Indistinguishable from a misread nav element, cookie banner or "
                    "dated testimonial."
                )
                fact_kind = UNCONFIRMED_DATED_CONTENT_KIND

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
            metadata = {
                "page_kind": page_kind,
                "published_at": published_at,
                "domain": domain,
            }
            if has_provenance_signal:
                metadata["identification_method"] = identification_method
                metadata["confidence"] = confidence
            if provenance_note:
                metadata["provenance_note"] = provenance_note
                metadata["original_kind"] = demoted_from_kind
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
                    metadata=metadata,
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
        apply_link_probe_limit: int = 8,
        secondary_page_kinds: "Sequence[str]" = QUALIFICATION_PAGE_KINDS,
    ) -> None:
        super().__init__(retry_policy=retry_policy, sleep=sleep)
        # Which pages beyond the homepage to fetch. Defaults to the
        # qualification pages -- the only ones the cold path reads. The event
        # kinds remain reachable for deep research on a lead that replied,
        # where the economics are completely different.
        unknown = [k for k in secondary_page_kinds if k not in PAGE_KINDS]
        if unknown:
            raise ValueError(
                f"unknown secondary page kinds {unknown}; expected some of {PAGE_KINDS}"
            )
        self._secondary_page_kinds = tuple(secondary_page_kinds)
        # Recall recovery for careers items that are STILL "url_shape_only"
        # after every free/cheap corroboration (JSON-LD, ATS host, href
        # keyword, title vocabulary): fetch the candidate's OWN page and look
        # for a real apply mechanism. This is the most expensive
        # signal available (one extra fetch per candidate), so it is bounded
        # per company rather than applied to every ambiguous item -- cost per
        # usable lead is the stated primary objective, and an unbounded probe
        # would scale with the page's junk-link count, not its job count.
        self._apply_link_probe_limit = apply_link_probe_limit
        if http_client is None:
            if httpx is None:  # pragma: no cover - exercised only without the dep installed
                raise ImportError(
                    "LiveCompanyAdapter needs httpx (or an injected http_client); "
                    "it is not installed in this environment"
                )
            # Prospect-controlled addresses: only ever through the safe client
            # (public IPs only, pinned, redirects re-validated, bounded body).
            http_client = SafeHTTPClient(timeout=request_timeout)
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

    def _unsafe(self, url: str, exc: Exception) -> ProviderUnsafeTarget:
        return ProviderUnsafeTarget(
            f"refused to fetch {url}: {exc}", provider=self.provider, target=url
        )

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
            except UnsafeResearchURL as exc:
                raise self._unsafe(robots_url, exc) from exc
            except ResearchResponseTooLarge as exc:
                # An oversize robots.txt cannot be read, so its rules cannot be
                # honoured; failing open would let it hide a disallow.
                raise ProviderBadResponse(
                    f"robots.txt too large: {exc}", provider=self.provider, target=robots_url
                ) from exc
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
        except UnsafeResearchURL as exc:
            raise self._unsafe(url, exc) from exc
        except ResearchResponseTooLarge as exc:
            raise ProviderBadResponse(
                f"response too large: {exc}", provider=self.provider, target=url
            ) from exc
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

        # Recall-recovery corroboration, discovered/fetched ONCE per company
        # and reused across every secondary page it applies to. Best-effort:
        # not existing, or the fetch failing, is never a provider failure --
        # just less corroboration available. NOTE: sitemap discovery is NOT
        # called here -- see `_discover_sitemap_urls`'s docstring for why it
        # was tried, measured unsafe on a real site, and retired rather than
        # spending a request on a signal we now know we do not trust.
        feed_dates = self._discover_feed_dates(homepage_html, homepage_url)

        blocked_pages: list[str] = []
        skipped_pages: list[str] = []
        discovered = find_page_links(homepage_html, homepage_url)
        # The qualification pages, and by default only those -- see
        # `QUALIFICATION_PAGE_KINDS` and `EVENT_PAGE_KINDS`. `PAGE_KINDS` still
        # accepts every kind, so a cached payload or a fixture holding event
        # pages parses unchanged.
        for kind in self._secondary_page_kinds:
            page = self._fetch_secondary_page(
                kind,
                discovered.get(kind),
                homepage_url,
                blocked=blocked_pages,
                skipped=skipped_pages,
                feed_dates=feed_dates,
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
        """Title, meta description and headings -- each as its own item.

        The meta description alone was enough when the homepage was only
        context around an event fact. It is not enough to confirm a vertical:
        plenty of small firms ship a homepage with no meta tag at all, while
        the `<h1>` says "Attorneys at Law". Each string is a separate item so
        that a draft cites the exact line that confirmed the vertical rather
        than a blob of the whole page.
        """

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates = [
            ("homepage", extract_meta_description(html)),
            ("homepage-title", extract_title(html)),
        ]
        candidates += [
            (f"homepage-heading-{i}", text)
            for i, text in enumerate(extract_headings(html))
        ]
        for key, text in candidates:
            cleaned = (text or "").strip()
            if not cleaned or cleaned.lower() in seen:
                continue
            seen.add(cleaned.lower())
            items.append({"key": key, "url": url, "text": cleaned, "published_at": None})
        return items

    def _qualification_items(
        self, html: str, page_url: str, kind: str
    ) -> list[dict[str, Any]]:
        """Items for a services or contact page: its title and its headings.

        No dates, no provenance demotion, no corroboration hop. Those controls
        exist because a job req and a dated post make a claim about WHEN
        something happened, and a misread one puts a false event in the cache.
        A heading on a practice-areas page makes no such claim -- it is the
        business naming what it does, and the words are carried verbatim into
        the fact, so a reviewer sees exactly the line we read.
        """

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        title = (extract_title(html) or "").strip()
        texts = ([title] if title else []) + extract_headings(html)
        for index, text in enumerate(texts):
            cleaned = text.strip()
            if not cleaned or cleaned.lower() in seen:
                continue
            seen.add(cleaned.lower())
            items.append(
                {
                    "key": f"{kind}-{index}",
                    "url": page_url,
                    "text": cleaned,
                    "published_at": None,
                }
            )
        return items

    def _fetch_secondary_page(
        self,
        kind: str,
        discovered_url: str | None,
        homepage_url: str,
        *,
        blocked: list[str],
        skipped: list[str],
        feed_dates: "Mapping[str, str]",
    ) -> dict[str, Any] | None:
        """Fetch one secondary page. Never raises: a page
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
            try:
                if not self._robots_allows(candidate):
                    blocked.append(candidate)
                    continue
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
        if kind in QUALIFICATION_PAGE_KINDS:
            items = self._qualification_items(html, page_url, kind)
        elif kind == "careers":
            items = extract_job_items(html, page_url)
            if not items:
                ats_link = find_external_ats_link(html, page_url)
                if ats_link:
                    ats_resp = None
                    try:
                        if not self._robots_allows(ats_link):
                            blocked.append(ats_link)
                        else:
                            ats_resp = self._get(ats_link)
                    except ProviderError:
                        pass  # incl. an unsafe ATS host: skipped, not trusted
                    if ats_resp is not None:
                        hop_items = extract_job_items(ats_resp.text, ats_link)
                        if hop_items:
                            items, page_url = hop_items, ats_link
            items = self._rescue_low_confidence_job_items(items)
        else:
            items = extract_dated_items(html, page_url, feed_dates=feed_dates)

        if not items:
            return None
        return {"kind": kind, "url": page_url, "items": items}

    # -- recall recovery: cheap, once-per-company corroboration ------------

    def _discover_sitemap_urls(self, homepage_url: str) -> "set[str]":
        """Best-effort `sitemap.xml` fetch, once per company.

        NOT called from `_fetch_payload` -- deliberately dormant. It was
        built and wired in, then measured live against a real careers site
        (asana.com, 2026-09-17): the site's own sitemap.xml lists its
        per-location and per-department FILTER pages ("Chicago", "Dublin",
        "Business & Marketing"...) as real, distinct, crawlable URLs
        alongside actual reqs. "This URL is in the sitemap" turned out to
        prove "this is a real page", not "this is a job requisition", and
        promoted 19 facet pages to JOB_POSTING in one company; they missed
        the scoring threshold by 10 points only because they were undated.
        Retired as a job-confidence signal rather than shipped on a
        near-miss. Left in place, tested, and reachable (`extract_sitemap_urls`
        is a correct, general XML-sitemap reader) for a future, stricter
        combination heuristic -- see the "sitemap" note in
        `_html.extract_job_items`'s docstring for the full writeup, and this
        agent's handoff report for the measurement.

        Absence, a block, or a parse failure all just mean "no sitemap
        corroboration available" -- never a provider failure; a sitemap is a
        bonus signal, not something every site is obligated to publish.
        """
        sitemap_url = urljoin(homepage_url, "/sitemap.xml")
        try:
            if not self._robots_allows(sitemap_url):
                return set()
            resp = self._get(sitemap_url)
        except ProviderError:
            return set()
        if resp is None:
            return set()
        return extract_sitemap_urls(resp.text)

    def _discover_feed_dates(self, homepage_html: str, homepage_url: str) -> "dict[str, str]":
        """Best-effort RSS/Atom feed fetch, once per company, if the homepage
        links to one at all. Same best-effort posture as the sitemap: most
        sites do not publish a feed, and that is not a failure."""
        feed_url = find_feed_link(homepage_html, homepage_url)
        if feed_url is None:
            return {}
        try:
            if not self._robots_allows(feed_url):
                return {}
            resp = self._get(feed_url)
        except ProviderError:
            return {}
        if resp is None:
            return {}
        return parse_feed_dates(resp.text)

    def _rescue_low_confidence_job_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Last-resort recall recovery for careers items still stuck at
        ``confidence="low"`` after every free/cheap signal: fetch the
        candidate's OWN page and look for a real apply mechanism
        (`page_has_apply_signal`). Bounded by `apply_link_probe_limit`
        because this is the single most expensive corroboration available
        -- it is deliberately not run on every ambiguous item, only up to
        the cap, so cost per company stays bounded rather than scaling with
        how many junk links a bad careers page happens to have.

        A probe that fails for any reason (robots, network, non-2xx) just
        leaves that item exactly as it was -- this can only ever RAISE an
        item's confidence, never lower one, so a probe failure is never a
        precision regression.
        """
        budget = self._apply_link_probe_limit
        if budget <= 0:
            return items
        for item in items:
            if budget <= 0:
                break
            if item.get("confidence") != "low":
                continue
            url = item.get("url")
            if not url:
                continue
            try:
                if not self._robots_allows(url):
                    continue
                budget -= 1
                resp = self._get(url)
            except ProviderError:
                continue
            if resp is None:
                continue
            if page_has_apply_signal(resp.text, url):
                item["identification_method"] = "apply_link"
                item["confidence"] = "high"
        return items

    def _payload_to_facts(self, domain: str, payload: Mapping[str, Any]) -> list[Fact]:
        return pages_payload_to_facts(provider=self.provider, domain=domain, payload=payload)


class NoLinkedInCompanyURL(ProviderBadResponse):
    """No LinkedIn company page URL is known for this domain.

    `CompanyAdapter.fetch_company` takes a bare DOMAIN -- that is the
    pipeline's cache key -- but Bright Data's company dataset needs an actual
    `linkedin.com/company/...` URL, and the brief rules out finding that by
    SEARCHING LinkedIn: this adapter is exactly as URL-only as the LinkedIn
    people adapter, just with a domain instead of a lead-supplied profile link
    on the input side. The resolver passed at construction is expected to
    already know the mapping (discovered once from a link the company itself
    publishes on its own homepage, or supplied directly) -- this exception
    means the resolver came up empty, not that Bright Data failed.
    """


LINKEDIN_COMPANY_PROVIDER = "company-brightdata"


class BrightDataCompanyAdapter(CompanyAdapter):
    """Bright Data arm of the W3 vendor spike (`docs/GAP-ANALYSIS.md` section 5).

    Calls Bright Data's LinkedIn COMPANY dataset (`gd_l1vikfnt1wgvvqz95w`) via
    the same synchronous scrape endpoint the LinkedIn people adapter uses.

    This dataset is a company PROFILE, not a jobs board: its live schema (see
    `_payload_to_facts`) has `about`/`description`/`specialties`/`founded`/
    `company_size` and a list of the company's own LinkedIn posts (`updates`)
    -- there is no `jobs`/`postings` field anywhere in it. That is measured
    fact, not a limitation this adapter invents: it means this arm can never
    itself produce a `JOB_POSTING` fact, and the 6 own-engine failures the W3
    spike is trying to rescue are specifically JOB_POSTING/company-signal
    failures. The best this dataset can contribute is a fresh self-published
    LinkedIn post scored as `BLOG_POST` (see the `fact_kind` override below) --
    a MEDIUM signal, never the STRONG one that is actually missing.

    Nothing Bright-Data-shaped crosses `fetch_company`: `_payload_to_facts`
    builds the same `{"pages": [...]}` shape `pages_payload_to_facts` already
    normalizes for the stub and `LiveCompanyAdapter`, so a company fact from
    any of the three sources scores identically downstream.
    """

    provider = LINKEDIN_COMPANY_PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: Any = None,
        linkedin_urls: Mapping[str, str] | None = None,
        url_resolver: Any = None,
        retry_policy: RetryPolicy | None = None,
        sleep=None,
        request_timeout: float = 100.0,
    ) -> None:
        super().__init__(retry_policy=retry_policy, sleep=sleep)
        self._api_key = api_key if api_key is not None else str(setting("LINKEDIN_API_KEY", ""))
        if not self._api_key:
            raise ProviderAuthError(
                "LINKEDIN_API_KEY is required for BrightDataCompanyAdapter",
                provider=self.provider,
            )
        if http_client is None:
            if httpx is None:  # pragma: no cover - exercised only without the dep installed
                raise ImportError(
                    "BrightDataCompanyAdapter needs httpx (or an injected http_client); "
                    "it is not installed in this environment"
                )
            http_client = httpx.Client(timeout=request_timeout)
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = http_client
        self._timeout = request_timeout
        # domain -> linkedin.com/company/... URL. Never populated by search --
        # supplied directly, or via a resolver callback the caller controls.
        self._linkedin_urls: dict[str, str] = dict(linkedin_urls or {})
        self._url_resolver = url_resolver
        self.billing = BillingMeter()

    def close(self) -> None:
        """Release the owned HTTP client. No-op for an injected one."""
        if self._owns_client:
            self._client.close()

    def _resolve_linkedin_url(self, domain: str) -> str:
        url = self._linkedin_urls.get(domain)
        if not url and self._url_resolver is not None:
            url = self._url_resolver(domain)
        if not url:
            raise NoLinkedInCompanyURL(
                f"no LinkedIn company URL configured for domain {domain!r}; this "
                "adapter does not search LinkedIn to find one",
                provider=self.provider,
                target=domain,
            )
        return url

    def _fetch_payload(self, domain: str) -> Mapping[str, Any] | None:
        linkedin_url = self._resolve_linkedin_url(domain)
        record = scrape_one(
            LINKEDIN_COMPANY_DATASET_ID,
            linkedin_url,
            api_key=self._api_key,
            http_client=self._client,
            provider=self.provider,
            timeout=self._timeout,
        )
        self.billing.record(billed=record is not None)
        if record is None:
            return None
        return {
            **record,
            "__source__": f"brightdata:{LINKEDIN_COMPANY_DATASET_ID}:{linkedin_url}",
            "__linkedin_url__": linkedin_url,
        }

    def _payload_to_facts(self, domain: str, payload: Mapping[str, Any]) -> list[Fact]:
        retrieved_at_raw = payload.get("timestamp")
        linkedin_url = payload.get("__linkedin_url__") or f"https://www.linkedin.com/company/{domain}/"

        pages: list[dict[str, Any]] = []

        profile_text = payload.get("about") or payload.get("description")
        if profile_text and str(profile_text).strip():
            pages.append(
                {
                    "kind": "homepage",
                    "url": linkedin_url,
                    "items": [
                        {
                            "key": "linkedin_company_profile",
                            "url": linkedin_url,
                            "text": str(profile_text).strip(),
                            "published_at": None,
                        }
                    ],
                }
            )

        updates = payload.get("updates")
        if updates is not None and not isinstance(updates, list):
            raise ProviderBadResponse(
                "updates must be a list", provider=self.provider, target=domain
            )
        update_items: list[dict[str, Any]] = []
        for post in updates or []:
            if not isinstance(post, Mapping):
                raise ProviderBadResponse(
                    "each update must be an object", provider=self.provider, target=domain
                )
            text = post.get("text") or post.get("title")
            if not text:
                continue
            # Bright Data's company `updates` carry BOTH an absolute
            # `date` (verified real ISO timestamp, not a guess) and a
            # relative `time` label ("5mo", "3h"). Prefer the absolute one;
            # `parse_relative_time` is a fallback for the rare item that is
            # missing `date`, not the primary path.
            published_at = post.get("date") or parse_relative_time(
                post.get("time"), now=parse_timestamp(retrieved_at_raw)
            )
            post_url = post.get("post_url") or post.get("link") or linkedin_url
            key = post.get("post_url") or post.get("link") or str(text)[:60]
            update_items.append(
                {
                    "key": key,
                    "url": post_url,
                    "text": str(text).strip(),
                    "published_at": published_at,
                    # A LinkedIn company post is self-published dated content,
                    # the brief's BLOG/CHANGELOG/PRESS tier -- not a job req.
                    # This dataset carries no jobs field at all, so it can
                    # never itself emit JOB_POSTING; see the class docstring.
                    "fact_kind": FactKind.BLOG_POST.value,
                }
            )
        if update_items:
            pages.append({"kind": "blog", "url": linkedin_url, "items": update_items})

        page_payload = {"retrieved_at": retrieved_at_raw, "pages": pages}
        return pages_payload_to_facts(provider=self.provider, domain=domain, payload=page_payload)


def get_company_adapter(
    *,
    provider: str | None = None,
    fixtures_dir: Path | str | None = None,
    retry_policy: RetryPolicy | None = None,
    linkedin_urls: Mapping[str, str] | None = None,
) -> CompanyAdapter:
    """Pick an adapter from `COMPANY_SCRAPER_PROVIDER`. Phase 1 default: stub.

    Like `get_linkedin_adapter`, the paid path is opt-in by provider NAME
    only -- `LINKEDIN_API_KEY` being set never silently changes this default.
    `linkedin_urls` (domain -> `linkedin.com/company/...`) is only consulted
    for the `brightdata` provider; every other provider ignores it.
    """
    name = str(provider or setting("COMPANY_SCRAPER_PROVIDER", "stub")).strip().lower()
    if name in ("stub", "", "fixture", "fixtures"):
        return StubCompanyAdapter(fixtures_dir=fixtures_dir, retry_policy=retry_policy)
    if name in ("live", "http", "own", "own_engine"):
        return LiveCompanyAdapter(retry_policy=retry_policy)
    if name in ("brightdata", "bright_data", "bright-data"):
        return BrightDataCompanyAdapter(retry_policy=retry_policy, linkedin_urls=linkedin_urls)
    raise NotImplementedError(
        f"company scraper provider {name!r} is not implemented. Subclass "
        "CompanyAdapter and implement _fetch_payload/_payload_to_facts."
    )
