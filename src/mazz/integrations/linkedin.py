"""LinkedIn enrichment: direct profile fetch BY URL ONLY.

We are always handed the profile link on the inbound lead record, so there is no
identity-resolution step to build. Single-URL lookup is both cheaper per record
and materially lower ToS/legal risk than search or crawl, and the brief is
explicit that search/discovery is a non-goal. This module therefore exposes
exactly one verb -- `fetch_profile(url)` -- and actively rejects anything that is
not a personal profile URL, so "just search for them" can never creep in.

The provider (Bright Data vs ScrapIn) is still undecided. Nothing vendor-shaped
crosses this interface: an adapter takes a URL and returns normalized `Fact`s.
Choosing a vendor means writing one `_payload_to_facts` mapper, not touching a
caller.
"""

from __future__ import annotations

import abc
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a project dependency in practice
    httpx = None  # type: ignore[assignment]

from mazz.integrations._brightdata import (
    LINKEDIN_PEOPLE_DATASET_ID,
    BillingMeter,
    parse_relative_time,
    scrape_one,
)
from mazz.integrations.base import (
    Fact,
    FactKind,
    FetchResult,
    InstrumentedProvider,
    ProviderAuthError,
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
    "InvalidProfileURL",
    "LinkedInAdapter",
    "StubLinkedInAdapter",
    "BrightDataLinkedInAdapter",
    "canonical_profile_url",
    "profile_slug",
    "get_linkedin_adapter",
]

STUB_PROVIDER = "linkedin-stub"
BRIGHTDATA_PROVIDER = "linkedin-brightdata"

# /in/<slug> only. Company pages, /pub/dir listings, and /search/ URLs are all
# rejected: they are the discovery shapes we are not building.
_PROFILE_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*linkedin\.com/in/(?P<slug>[^/?#\s]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)


class InvalidProfileURL(ValueError):
    """Not a `linkedin.com/in/<slug>` URL.

    Distinct from `ProviderError` on purpose: this is bad input on the lead
    record, not a provider outage, and the pipeline should route it to "fix the
    lead" rather than "retry later".
    """


def profile_slug(profile_url: str) -> str:
    match = _PROFILE_RE.match((profile_url or "").strip())
    if not match:
        raise InvalidProfileURL(
            f"{profile_url!r} is not a LinkedIn profile URL. "
            "This adapter is URL-only: no search, no discovery."
        )
    return match.group("slug").strip().lower()


def canonical_profile_url(profile_url: str) -> str:
    """Canonical form used as the cache/target key and for fact ids.

    Two spellings of the same profile (trailing slash, tracking query, country
    subdomain, capitalisation) must produce identical fact ids, or a re-fetch
    would orphan every citation a draft made.
    """
    return f"https://www.linkedin.com/in/{profile_slug(profile_url)}"


class LinkedInAdapter(InstrumentedProvider, abc.ABC):
    """Vendor-neutral LinkedIn profile adapter."""

    provider: str = "linkedin"

    def __init__(self, *, retry_policy: RetryPolicy | None = None, sleep=None) -> None:
        InstrumentedProvider.__init__(self)
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def fetch_profile(self, profile_url: str, *, subject_email: str | None = None) -> FetchResult:
        """Fetch one profile by URL. The only public verb.

        `subject_email` is the lead's email and should always be passed: profile
        facts are about a PERSON, and research_cache is company-scoped so it is
        not cascade-deleted with the lead. Without it, delete-by-person cannot
        find these facts to prune -- a compliance bug, not a nicety.

        Raises `InvalidProfileURL` for a non-profile URL and `ProviderError` for a
        provider failure. Returns a `FetchResult` whose status distinguishes
        "found facts" from "profile exists but yielded nothing" from "no such
        profile" -- a failure never returns.
        """
        target = canonical_profile_url(profile_url)
        self._record("fetch_profile", target, subject_email=subject_email)

        kwargs = {"policy": self.retry_policy}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        payload = call_with_retry(lambda: self._fetch_payload(target), **kwargs)

        if payload is None:
            return FetchResult.not_found(self.provider, target)

        facts = self._payload_to_facts(target, payload, subject_email=subject_email)
        return FetchResult.found(
            self.provider,
            target,
            facts,
            retrieved_at=parse_timestamp(payload.get("retrieved_at")),
            raw_ref=str(payload.get("__source__") or ""),
        )

    @abc.abstractmethod
    def _fetch_payload(self, canonical_url: str) -> Mapping[str, Any] | None:
        """Return the raw provider payload, or None if the profile does not exist.
        Raise a `ProviderError` subclass on failure -- never return None for one."""

    @abc.abstractmethod
    def _payload_to_facts(
        self, canonical_url: str, payload: Mapping[str, Any], *, subject_email: str | None = None
    ) -> Sequence[Fact]:
        """Normalize one vendor's payload into `Fact`s. The only vendor-aware code."""


class StubLinkedInAdapter(LinkedInAdapter):
    """Phase 1: reads `tests/fixtures/linkedin/<slug>.json`. No network.

    The fixture format is a plausible neutral profile shape, NOT Bright Data's or
    ScrapIn's -- picking either here would quietly bake in a decision the brief
    leaves open.
    """

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
        self.fixtures_dir = base / "linkedin" if base.name != "linkedin" else base

    def _fetch_payload(self, canonical_url: str) -> Mapping[str, Any] | None:
        slug = profile_slug(canonical_url)
        path = self.fixtures_dir / f"{slug}.json"
        payload = load_json_fixture(path, provider=self.provider, target=canonical_url)
        if payload is None:
            return None
        maybe_simulate_failure(payload, provider=self.provider, target=canonical_url)
        return {**payload, "__source__": str(path)}

    def _payload_to_facts(
        self, canonical_url: str, payload: Mapping[str, Any], *, subject_email: str | None = None
    ) -> list[Fact]:
        retrieved_at = parse_timestamp(payload.get("retrieved_at"))
        facts: list[Fact] = []

        def add(
            natural_key: str,
            content: Any,
            *,
            section: str,
            source_url: str | None = None,
            posted_at: str | None = None,
        ) -> None:
            """Every profile fact uses the shared `linkedin_profile` kind.

            The brief's ranking is about the company signal (a job req beats a
            blog post beats funding news); a headline is not a separate tier of
            evidence from an About section. Inventing `linkedin_headline` etc.
            would put them outside FactKind, where the scorer treats unknown
            kinds as weak -- so the granularity goes in metadata instead, where
            it is available without mis-scoring anything.
            """
            if not content or not str(content).strip():
                return
            facts.append(
                build_fact(
                    kind=FactKind.LINKEDIN_PROFILE.value,
                    content=str(content).strip(),
                    source_url=source_url or canonical_url,
                    retrieved_at=retrieved_at,
                    natural_key=natural_key,
                    subject_email=subject_email,
                    metadata={
                        "section": section,
                        "posted_at": posted_at,
                        "profile_url": canonical_url,
                    },
                )
            )

        add("headline", payload.get("headline"), section="headline")

        position = payload.get("current_position") or {}
        if not isinstance(position, Mapping):
            raise ProviderBadResponse(
                "current_position must be an object",
                provider=self.provider,
                target=canonical_url,
            )
        if position.get("title"):
            started = position.get("started_on")
            company = position.get("company") or ""
            content = f"{position['title']} at {company}" if company else str(position["title"])
            if started:
                content = f"{content} (since {started})"
            add(f"role:{position['title']}:{company}", content, section="role")

        add("about", payload.get("about"), section="about")

        for post in payload.get("recent_activity") or []:
            if not isinstance(post, Mapping):
                raise ProviderBadResponse(
                    "recent_activity entries must be objects",
                    provider=self.provider,
                    target=canonical_url,
                )
            text = post.get("text")
            if not text:
                continue
            posted_at = post.get("posted_at")
            content = f"[{posted_at}] {text}" if posted_at else str(text)
            add(
                str(post.get("key") or post.get("url") or str(text)[:60]),
                content,
                section="post",
                source_url=post.get("url") or canonical_url,
                posted_at=posted_at,
            )

        return facts


class BrightDataLinkedInAdapter(LinkedInAdapter):
    """Bright Data arm of the W3 vendor spike (`docs/GAP-ANALYSIS.md` section 5).

    Calls Bright Data's LinkedIn PEOPLE dataset (`gd_l1viktl72bvl7bjuj0`) via
    the synchronous `POST /datasets/v3/scrape` endpoint, one profile URL at a
    time -- `fetch_profile` is one-URL-by-design (see `LinkedInAdapter`) and
    this class adds no batching on top of it. Batching multiple profiles per
    call would be a cheaper API shape, but it would also be a step toward
    "fetch several while we're at it", which is exactly the discovery-adjacent
    behavior the brief rules out for LinkedIn.

    Nothing Bright-Data-shaped crosses `fetch_profile`: `_payload_to_facts`
    below is the only place a Bright Data field name (`current_company_name`,
    `activity`, ...) is allowed to appear. ScrapIn is still the brief's open
    question; a `ScrapInLinkedInAdapter` would be a sibling of this class with
    the same two abstract methods filled in, not a change to the interface.

    Billing is per successful record (verified against the live account, see
    `mazz.integrations._brightdata`). `self.billing` is a `BillingMeter` so a
    caller can read real spend -- `records_billed` -- off the adapter instead
    of estimating it.
    """

    provider = BRIGHTDATA_PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: Any = None,
        retry_policy: RetryPolicy | None = None,
        sleep=None,
        request_timeout: float = 100.0,
    ) -> None:
        super().__init__(retry_policy=retry_policy, sleep=sleep)
        self._api_key = api_key if api_key is not None else str(setting("LINKEDIN_API_KEY", ""))
        if not self._api_key:
            raise ProviderAuthError(
                "LINKEDIN_API_KEY is required for BrightDataLinkedInAdapter",
                provider=self.provider,
            )
        if http_client is None:
            if httpx is None:  # pragma: no cover - exercised only without the dep installed
                raise ImportError(
                    "BrightDataLinkedInAdapter needs httpx (or an injected http_client); "
                    "it is not installed in this environment"
                )
            http_client = httpx.Client(timeout=request_timeout)
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = http_client
        self._timeout = request_timeout
        self.billing = BillingMeter()

    def close(self) -> None:
        """Release the owned HTTP client. No-op for an injected one."""
        if self._owns_client:
            self._client.close()

    def _fetch_payload(self, canonical_url: str) -> Mapping[str, Any] | None:
        record = scrape_one(
            LINKEDIN_PEOPLE_DATASET_ID,
            canonical_url,
            api_key=self._api_key,
            http_client=self._client,
            provider=self.provider,
            timeout=self._timeout,
        )
        self.billing.record(billed=record is not None)
        if record is None:
            return None
        return {**record, "__source__": f"brightdata:{LINKEDIN_PEOPLE_DATASET_ID}:{canonical_url}"}

    def _payload_to_facts(
        self, canonical_url: str, payload: Mapping[str, Any], *, subject_email: str | None = None
    ) -> list[Fact]:
        retrieved_at = parse_timestamp(payload.get("timestamp"))
        facts: list[Fact] = []

        def add(
            natural_key: str,
            content: Any,
            *,
            section: str,
            source_url: str | None = None,
            posted_at: str | None = None,
        ) -> None:
            # Same reasoning as `StubLinkedInAdapter.add`: every profile fact
            # is `linkedin_profile`, and granularity lives in metadata so a
            # vendor swap can never change how a fact scores.
            if not content or not str(content).strip():
                return
            facts.append(
                build_fact(
                    kind=FactKind.LINKEDIN_PROFILE.value,
                    content=str(content).strip(),
                    source_url=source_url or canonical_url,
                    retrieved_at=retrieved_at,
                    natural_key=natural_key,
                    subject_email=subject_email,
                    metadata={
                        "section": section,
                        "posted_at": posted_at,
                        "profile_url": canonical_url,
                    },
                )
            )

        name = payload.get("name")
        current_company = payload.get("current_company_name")
        if not current_company and isinstance(payload.get("current_company"), Mapping):
            current_company = payload["current_company"].get("name")

        if name and current_company:
            add("headline", f"{name} — currently at {current_company}", section="headline")
        elif name:
            add("headline", str(name), section="headline")

        # `position`/`experience` come back null for every profile this
        # dataset has returned on the live account so far (verified, not
        # assumed) -- handled defensively rather than relied on.
        position = payload.get("position")
        if position is not None and not isinstance(position, Mapping):
            raise ProviderBadResponse(
                "position must be an object", provider=self.provider, target=canonical_url
            )
        if isinstance(position, Mapping) and position.get("title"):
            company = position.get("company") or current_company or ""
            content = f"{position['title']} at {company}" if company else str(position["title"])
            add(f"role:{position['title']}:{company}", content, section="role")
        else:
            experience = payload.get("experience")
            if experience is not None and not isinstance(experience, list):
                raise ProviderBadResponse(
                    "experience must be a list", provider=self.provider, target=canonical_url
                )
            first = experience[0] if experience else None
            if isinstance(first, Mapping) and first.get("title"):
                company = first.get("company") or first.get("subtitle") or current_company or ""
                content = f"{first['title']} at {company}" if company else str(first["title"])
                add(f"role:{first['title']}:{company}", content, section="role")

        add("about", payload.get("about"), section="about")

        activity = payload.get("activity")
        if activity is not None and not isinstance(activity, list):
            raise ProviderBadResponse(
                "activity must be a list", provider=self.provider, target=canonical_url
            )
        for post in activity or []:
            if not isinstance(post, Mapping):
                raise ProviderBadResponse(
                    "activity entries must be objects",
                    provider=self.provider,
                    target=canonical_url,
                )
            text = post.get("text") or post.get("title")
            if not text:
                continue
            posted_at = parse_relative_time(post.get("time"), now=retrieved_at)
            content = f"[{posted_at}] {text}" if posted_at else str(text)
            key = post.get("post_url") or post.get("link") or str(text)[:60]
            add(
                f"activity:{key}",
                content,
                section="activity",
                source_url=post.get("post_url") or post.get("link") or canonical_url,
                posted_at=posted_at,
            )

        return facts


def get_linkedin_adapter(
    *,
    provider: str | None = None,
    fixtures_dir: Path | str | None = None,
    retry_policy: RetryPolicy | None = None,
) -> LinkedInAdapter:
    """Pick an adapter from `LINKEDIN_PROVIDER`. Phase 1 default: stub.

    The paid path is opt-in only: it is selected by the PROVIDER NAME, never
    by the mere presence of `LINKEDIN_API_KEY` in the environment. A
    developer's `.env` may carry a real key for manual/measurement use without
    that key silently flipping what a plain `pytest` run, or an un-configured
    caller, resolves to.
    """
    name = str(provider or setting("LINKEDIN_PROVIDER", "stub")).strip().lower()
    if name in ("stub", "", "fixture", "fixtures"):
        return StubLinkedInAdapter(fixtures_dir=fixtures_dir, retry_policy=retry_policy)
    if name in ("brightdata", "bright_data", "bright-data"):
        return BrightDataLinkedInAdapter(retry_policy=retry_policy)
    if name in ("scrapin", "scrap_in"):
        raise NotImplementedError(
            f"LinkedIn provider {name!r} is not implemented: ScrapIn is still an open "
            "question in the brief (see docs/GAP-ANALYSIS.md section 5). Implement a "
            "LinkedInAdapter subclass with _fetch_payload/_payload_to_facts if it is "
            "chosen over Bright Data."
        )
    raise ValueError(f"unknown LINKEDIN_PROVIDER {name!r}")
