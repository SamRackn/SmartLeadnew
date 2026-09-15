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

Phase 1 reads canned fixtures. No live HTTP, no crawler.
"""

from __future__ import annotations

import abc
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from mazz.integrations.base import (
    Fact,
    FactKind,
    FetchResult,
    InstrumentedProvider,
    ProviderBadResponse,
    RetryPolicy,
    build_fact,
    call_with_retry,
    default_fixtures_dir,
    load_json_fixture,
    maybe_simulate_failure,
    parse_timestamp,
    setting,
)

__all__ = [
    "PAGE_KINDS",
    "PAGE_KIND_TO_FACT_KIND",
    "InvalidDomain",
    "CompanyAdapter",
    "StubCompanyAdapter",
    "normalize_domain",
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
        default_retrieved_at = parse_timestamp(payload.get("retrieved_at"))
        pages = payload.get("pages") or []
        if not isinstance(pages, list):
            raise ProviderBadResponse(
                "pages must be a list", provider=self.provider, target=domain
            )

        facts: list[Fact] = []
        for page in pages:
            if not isinstance(page, Mapping):
                raise ProviderBadResponse(
                    "each page must be an object", provider=self.provider, target=domain
                )
            page_kind = str(page.get("kind") or "").strip().lower()
            if page_kind not in PAGE_KIND_TO_FACT_KIND:
                raise ProviderBadResponse(
                    f"unknown page kind {page_kind!r} (expected one of {PAGE_KINDS})",
                    provider=self.provider,
                    target=domain,
                )
            page_url = page.get("url") or f"https://{domain}/"

            for item in page.get("items") or []:
                if not isinstance(item, Mapping):
                    raise ProviderBadResponse(
                        "each item must be an object", provider=self.provider, target=domain
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
                        provider=self.provider,
                        target=domain,
                    )
                source_url = item.get("url") or page_url
                natural_key = item.get("key")
                if not natural_key:
                    raise ProviderBadResponse(
                        f"item on {source_url} has no 'key'; fact ids must be stable "
                        "and a key is the only durable handle we have",
                        provider=self.provider,
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
    raise NotImplementedError(
        f"company scraper provider {name!r} is not implemented. Subclass "
        "CompanyAdapter and implement _fetch_payload/_payload_to_facts."
    )
