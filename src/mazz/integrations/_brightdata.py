"""Shared low-level HTTP boundary for Bright Data's dataset API.

Bright Data is one candidate vendor for LinkedIn profile/company enrichment --
ScrapIn is the other, and the brief leaves that choice open. This module is
the ONLY place in the codebase that knows Bright Data's URL shape, auth
header and response envelope. `linkedin.py`'s `BrightDataLinkedInAdapter` and
`company.py`'s `BrightDataCompanyAdapter` both sit on top of it, but neither
the `LinkedInAdapter`/`CompanyAdapter` interface nor any `Fact` ever sees a
Bright Data field name -- see each adapter's `_payload_to_facts` for the only
place that is allowed to know one. Swapping to ScrapIn means writing a sibling
of this module, not touching a caller.

Two things this module gets right so nothing built on it has to re-derive them:

* **Billing.** Bright Data bills per SUCCESSFUL record. Verified against the
  live account (not guessed): a lookup that finds nothing billable comes back
  200 OK with a bare ``{"timestamp": ..., "input": {...}}`` envelope and no
  scraped fields -- free, and modelled here as ``None``, exactly like a 404.
  Anything with real fields is billable. `BillingMeter` counts exactly that
  distinction so cost is measured, not estimated.
* **Failure vs. empty.** A transport error, bad credentials, a 429, a 5xx, or
  a malformed body raises a `ProviderError` subclass. `None` is the only way
  to signal "ran the lookup, found nothing" -- a caller can never mistake a
  Bright Data outage for a genuinely empty profile or company page.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from mazz.integrations.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)

__all__ = [
    "BASE_URL",
    "LINKEDIN_PEOPLE_DATASET_ID",
    "LINKEDIN_COMPANY_DATASET_ID",
    "BillingMeter",
    "scrape_one",
    "parse_relative_time",
]

BASE_URL = "https://api.brightdata.com"
_SCRAPE_PATH = "/datasets/v3/scrape"

# Confirmed present on the live account (see the W3 spike report). Not
# rederived or guessed here.
LINKEDIN_PEOPLE_DATASET_ID = "gd_l1viktl72bvl7bjuj0"
LINKEDIN_COMPANY_DATASET_ID = "gd_l1vikfnt1wgvvqz95w"


class BillingMeter:
    """Counts records Bright Data actually billed us for.

    Cost is a primary requirement on this project (GAP-ANALYSIS.md section
    5), and billing is per SUCCESSFUL record -- an estimate is not good enough
    once real money is on the line. Every Bright Data adapter instance owns
    one of these; the measurement script reads it straight off the adapter
    rather than inferring spend after the fact from logs.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.records_billed = 0
        self.records_not_found = 0

    def record(self, *, billed: bool) -> None:
        self.calls += 1
        if billed:
            self.records_billed += 1
        else:
            self.records_not_found += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "records_billed": self.records_billed,
            "records_not_found": self.records_not_found,
        }


def _is_not_found_shape(record: Mapping[str, Any]) -> bool:
    """True for Bright Data's "looked, nothing billable" envelope.

    Verified against the live account with a deliberately bogus profile URL
    and a deliberately bogus company URL before spending any of the
    per-record budget on real lookups: both came back exactly
    ``{"timestamp": ..., "input": {...}}``, status 200, no scraped fields.
    """
    substantive = set(record.keys()) - {"timestamp", "input", "warning", "warning_code"}
    return not substantive


def scrape_one(
    dataset_id: str,
    url: str,
    *,
    api_key: str,
    http_client: Any,
    provider: str,
    timeout: float = 100.0,
) -> Mapping[str, Any] | None:
    """POST one URL to Bright Data's synchronous scrape endpoint.

    Request shape (`POST /datasets/v3/scrape?dataset_id=...`, body an array of
    ``{"url": ...}`` objects) and auth header are exactly as verified against
    the live account -- a bare array of URL strings is rejected with a 400
    validation error, so this always sends objects.

    Returns the raw record dict, or `None` if Bright Data ran the lookup and
    found nothing billable (a dead/renamed profile or company page). Raises a
    `ProviderError` subclass for everything else -- bad credentials, rate
    limiting, a 5xx, a malformed body -- so a failure can never be mistaken
    for "nothing found" by the adapter sitting on top of this.
    """
    if not api_key:
        raise ProviderAuthError(
            "Bright Data API key is not configured (LINKEDIN_API_KEY)",
            provider=provider,
            target=url,
        )

    endpoint = f"{BASE_URL}{_SCRAPE_PATH}?dataset_id={dataset_id}"
    body = json.dumps([{"url": url}]).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = http_client.post(endpoint, content=body, headers=headers, timeout=timeout)
    except Exception as exc:  # transport-level only (httpx.HTTPError et al.)
        raise ProviderUnavailable(
            f"network error calling Bright Data: {exc}", provider=provider, target=url
        ) from exc

    status = resp.status_code
    if status in (401, 403):
        raise ProviderAuthError(
            f"Bright Data rejected credentials ({status})",
            provider=provider,
            target=url,
            status_code=status,
        )
    if status == 429:
        raise ProviderRateLimited(
            "Bright Data rate limited this request",
            provider=provider,
            target=url,
            status_code=429,
        )
    if status >= 500:
        raise ProviderUnavailable(
            f"Bright Data returned {status}", provider=provider, target=url, status_code=status
        )
    if status >= 400:
        raise ProviderBadResponse(
            f"Bright Data returned {status} for the request it was sent",
            provider=provider,
            target=url,
            status_code=status,
        )

    try:
        payload = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProviderBadResponse(
            f"Bright Data returned unparseable JSON: {exc}", provider=provider, target=url
        ) from exc

    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, Mapping):
        raise ProviderBadResponse(
            "Bright Data response was not an object", provider=provider, target=url
        )

    if _is_not_found_shape(payload):
        return None
    return payload


_RELATIVE_RE = re.compile(r"^(\d+)\s*([a-z]+)$")
_RELATIVE_DAYS_PER_UNIT: Mapping[str, float] = {
    "h": 0.0, "hr": 0.0, "hrs": 0.0, "hour": 0.0, "hours": 0.0,
    "d": 1.0, "day": 1.0, "days": 1.0,
    "w": 7.0, "wk": 7.0, "week": 7.0, "weeks": 7.0,
    "mo": 30.0, "month": 30.0, "months": 30.0,
    "y": 365.0, "yr": 365.0, "year": 365.0, "years": 365.0,
}


def parse_relative_time(value: Any, *, now: datetime) -> str | None:
    """Bright Data's activity/update timestamps are relative strings like
    ``"5mo"``, ``"2w"``, ``"1yr"`` -- not the ISO dates `score.published_at_of`
    needs. Converts to an approximate ISO *date* so real recent LinkedIn
    activity is not silently flattened to "undated" (which would mask exactly
    the recency signal this data might otherwise contribute).

    Deliberately approximate: LinkedIn never gives us a real calendar date, so
    30 days/month and 365 days/year are only precise enough to place a post in
    or out of the scorer's freshness window, not to reconstruct history.
    Returns `None` for anything unrecognized rather than guessing.
    """
    if not isinstance(value, str):
        return None
    match = _RELATIVE_RE.match(value.strip().lower())
    if not match:
        return None
    count, unit = match.groups()
    days_per = _RELATIVE_DAYS_PER_UNIT.get(unit)
    if days_per is None:
        return None
    approx = now - timedelta(days=int(count) * days_per)
    return approx.date().isoformat()
