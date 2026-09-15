"""Shared plumbing for every outbound provider boundary.

Everything in this module exists because the vendor decisions are NOT made yet
(LinkedIn is Bright Data vs ScrapIn; the company scraper is undecided). The
interfaces here are deliberately vendor-neutral: no provider response shape is
allowed to escape an adapter. Adapters normalize to `Fact` and nothing else.

Two invariants this module enforces mechanically, because the pipeline's
correctness depends on them:

1. **Stable fact ids.** Drafts cite fact ids and the verification step resolves
   those citations back to cached snippets. An id that changes between runs
   silently breaks grounding, so ids are derived from (kind, source_url,
   natural key) -- never from a counter, a timestamp, or list position.

2. **A provider failure can never look like "nothing found".** "No facts" is a
   legitimate, meaningful outcome: it means `no_angle`. A swallowed error that
   returned `[]` would forge that verdict. So failures *raise* (`ProviderError`)
   and successes *return* a `FetchResult` whose status distinguishes OK / EMPTY
   / NOT_FOUND. `FetchResult` refuses to be constructed in a state that conflates
   them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

# The shared Fact contract lives in schemas (db-architect). Re-exported below so
# adapters have a single import site and this package defines no rival shape.
from mazz.schemas import Fact, FactKind, stable_fact_id

__all__ = [
    "Fact",
    "FactKind",
    "build_fact",
    "stable_fact_id",
    "FetchResult",
    "FetchStatus",
    "IntegrationError",
    "ProviderError",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderBlocked",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "RetryPolicy",
    "InstrumentedProvider",
    "call_with_retry",
    "make_fact_id",
    "utc_now",
    "parse_timestamp",
    "find_repo_root",
    "default_fixtures_dir",
    "load_json_fixture",
    "maybe_simulate_failure",
    "setting",
]


# --------------------------------------------------------------------------
# The shared Fact contract
# --------------------------------------------------------------------------
#
# `Fact` and `FactKind` are owned by db-architect in `mazz.schemas` and are
# re-exported here so adapters have one import site. There is deliberately no
# second Fact type in this package: two shapes for one contract is exactly how
# a citation ends up unresolvable.

def make_fact_id(kind: str, source_url: str, natural_key: str) -> str:
    """Deterministic fact id, derived from a NATURAL KEY rather than content.

    `mazz.schemas.stable_fact_id` hashes the content, and it is re-exported above
    for producers that have nothing better. Scraped facts do have something
    better: a job req slug, a post permalink. Keying on that means an id survives
    the source page being copy-edited, where a content hash would not -- and a
    changed id silently orphans every citation an already-approved draft made.
    Same output format (`kind:16hex`) as the schemas default, so the two are
    interchangeable to every consumer.
    """
    digest = hashlib.sha256(
        "\x1f".join((kind, source_url.strip().lower(), natural_key.strip().lower())).encode("utf-8")
    ).hexdigest()
    return f"{kind}:{digest[:16]}"


def build_fact(
    *,
    kind: str,
    content: str,
    source_url: str,
    retrieved_at: datetime,
    natural_key: str,
    subject_email: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Fact:
    """Construct a `schemas.Fact` with a stable id, enforcing what a SCRAPED fact
    must carry.

    `schemas.Fact` allows `source_url=None` for facts drawn from the lead's notes.
    An adapter never has that excuse: a fact with no URL is untraceable and the
    verifier fails it closed, so we refuse to emit one at the source instead of
    letting it surface as a mysteriously held draft later.

    `subject_email` marks person-specific facts (a LinkedIn profile fetch) so
    delete-by-person can prune them. research_cache is company-scoped and is not
    cascade-deleted with a lead, so omitting it is a compliance bug.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("refusing to emit a Fact with empty content")
    if not source_url:
        raise ValueError(f"scraped fact ({kind}) must carry a source_url; it would fail closed")
    if retrieved_at.tzinfo is None:
        raise ValueError("Fact.retrieved_at must be timezone-aware UTC")
    return Fact(
        id=make_fact_id(kind, source_url, natural_key),
        kind=kind,
        content=text,
        source_url=source_url,
        retrieved_at=retrieved_at,
        subject_email=subject_email,
        metadata=dict(metadata or {}),
    )


# --------------------------------------------------------------------------
# Errors -- a failure is loud, always
# --------------------------------------------------------------------------


class IntegrationError(Exception):
    """Base for anything that goes wrong at a boundary we do not control."""


class ProviderError(IntegrationError):
    """A provider call failed. NEVER caught-and-converted to an empty result."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        target: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.target = target
        self.status_code = status_code
        self.attempts = 1

    def __str__(self) -> str:
        base = super().__str__()
        bits = [f"provider={self.provider}"]
        if self.target:
            bits.append(f"target={self.target}")
        if self.status_code is not None:
            bits.append(f"status={self.status_code}")
        if self.attempts > 1:
            bits.append(f"attempts={self.attempts}")
        return f"{base} ({', '.join(bits)})"


class ProviderUnavailable(ProviderError):
    """Timeout, connection reset, 5xx. Worth retrying."""

    retryable = True


class ProviderRateLimited(ProviderUnavailable):
    """429 / quota. Worth retrying, with backoff."""

    retryable = True


class ProviderAuthError(ProviderError):
    """Bad or missing credentials. Retrying cannot help."""


class ProviderBadResponse(ProviderError):
    """Provider answered, but with something we cannot parse. Not retryable --
    a malformed payload is a mapping bug or a vendor change, not a blip."""


class ProviderBlocked(ProviderError):
    """robots.txt disallows the fetch, or the site answered with a hard
    access-control refusal (401/403 on a page we did not try to evade).

    Deliberately NOT retryable and deliberately NOT `FetchResult.not_found` /
    `EMPTY`: this is not "we looked and there was nothing", it is "we did not
    look, or were refused, on compliance grounds". Conflating the two would
    let a blocked site silently present to the client as `no_angle` -- which
    is exactly the swallowed-failure case the brief forbids. Callers should
    catch this at the batch/measurement level, log it, and move on to the
    next target; they must never retry past it or attempt to work around it.
    """


# --------------------------------------------------------------------------
# Results -- success carries a status that cannot be confused with failure
# --------------------------------------------------------------------------


class FetchStatus(str, Enum):
    OK = "ok"
    """Lookup succeeded and produced at least one fact."""

    EMPTY = "empty"
    """Lookup succeeded; the resource exists but yielded nothing usable.
    A legitimate input to `no_angle`."""

    NOT_FOUND = "not_found"
    """The resource does not exist (e.g. a dead profile URL). Still a successful
    lookup -- we know the answer. Also a legitimate input to `no_angle`."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    provider: str
    target: str
    status: FetchStatus
    facts: tuple[Fact, ...] = ()
    retrieved_at: datetime = field(default_factory=lambda: utc_now())
    raw_ref: str | None = None
    """Where the raw payload came from (fixture path in Phase 1, provider request
    id later). For audit trails; never parsed."""

    def __post_init__(self) -> None:
        if self.status is FetchStatus.OK and not self.facts:
            raise ValueError("FetchStatus.OK requires at least one fact; use EMPTY")
        if self.status is not FetchStatus.OK and self.facts:
            raise ValueError(f"FetchStatus.{self.status.name} must carry no facts")

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK

    @property
    def found_nothing(self) -> bool:
        """True for a *successful* lookup that produced no facts. This is the
        only legitimate route to `no_angle` from an adapter."""
        return self.status in (FetchStatus.EMPTY, FetchStatus.NOT_FOUND)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "target": self.target,
            "status": self.status.value,
            "facts": [f.model_dump(mode="json") for f in self.facts],
            "retrieved_at": self.retrieved_at.isoformat(),
            "raw_ref": self.raw_ref,
        }

    @classmethod
    def found(
        cls,
        provider: str,
        target: str,
        facts: Sequence[Fact],
        *,
        retrieved_at: datetime | None = None,
        raw_ref: str | None = None,
    ) -> "FetchResult":
        facts = tuple(facts)
        status = FetchStatus.OK if facts else FetchStatus.EMPTY
        return cls(
            provider=provider,
            target=target,
            status=status,
            facts=facts,
            retrieved_at=retrieved_at or utc_now(),
            raw_ref=raw_ref,
        )

    @classmethod
    def empty(cls, provider: str, target: str, *, raw_ref: str | None = None) -> "FetchResult":
        return cls(provider=provider, target=target, status=FetchStatus.EMPTY, raw_ref=raw_ref)

    @classmethod
    def not_found(cls, provider: str, target: str, *, raw_ref: str | None = None) -> "FetchResult":
        return cls(provider=provider, target=target, status=FetchStatus.NOT_FOUND, raw_ref=raw_ref)


# --------------------------------------------------------------------------
# Bounded retry
# --------------------------------------------------------------------------

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff. Deterministic by default (no jitter) so tests
    can assert the exact delay sequence; jitter is opt-in for production."""

    attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 4.0
    jitter: float = 0.0

    def delay_for(self, attempt_index: int) -> float:
        """Delay before retry N (0-based index of the attempt that just failed)."""
        return min(self.base_delay * (2**attempt_index), self.max_delay)


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] | None = None,
) -> T:
    """Call `fn`, retrying only retryable provider errors, at most
    `policy.attempts` times.

    The *original* exception is re-raised once attempts run out -- never wrapped
    in a generic "provider failed" so the real cause survives to the logs. The
    number of attempts made is attached as `.attempts`.
    """
    policy = policy or RetryPolicy()
    if policy.attempts < 1:
        raise ValueError("RetryPolicy.attempts must be >= 1")

    last: ProviderError | None = None
    for i in range(policy.attempts):
        try:
            return fn()
        except ProviderError as exc:
            exc.attempts = i + 1
            last = exc
            if not exc.retryable or i == policy.attempts - 1:
                raise
            delay = policy.delay_for(i)
            if policy.jitter and rand is not None:
                delay += delay * policy.jitter * rand()
            sleep(delay)
    assert last is not None  # unreachable
    raise last


# --------------------------------------------------------------------------
# Call instrumentation -- so pipeline tests can prove the cache worked
# --------------------------------------------------------------------------


class InstrumentedProvider:
    """Mixin giving every adapter/client an observable call count.

    The domain-keyed research cache is a real cost control (two leads at one
    employer must not mean two paid lookups). Pipeline tests assert that by
    reading `adapter.call_count` after processing both leads, so every public
    entry point records itself here.
    """

    def __init__(self) -> None:
        self._calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return list(self._calls)

    def calls_to(self, method: str) -> list[tuple[Any, ...]]:
        return [args for name, args, _ in self._calls if name == method]

    def reset_calls(self) -> None:
        self._calls.clear()

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self._calls.append((method, args, kwargs))


# --------------------------------------------------------------------------
# Time, config and fixture helpers
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any, *, default: datetime | None = None) -> datetime:
    """Parse an ISO-8601 string (or date) from a fixture into an aware datetime."""
    if value is None:
        return default or utc_now()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ProviderBadResponse(f"unparseable timestamp {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ProviderBadResponse(f"unparseable timestamp {value!r}")


def find_repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def default_fixtures_dir() -> Path:
    """Phase 1 reads canned provider responses from here. No live HTTP."""
    override = os.environ.get("MAZZ_FIXTURES_DIR")
    if override:
        return Path(override)
    return find_repo_root() / "tests" / "fixtures"


def load_json_fixture(path: Path, *, provider: str, target: str) -> dict[str, Any] | None:
    """Read a fixture. Returns None when the file is absent -- that models a
    genuine 404 from the provider, not a failure."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ProviderBadResponse(
            f"fixture {path.name} is not valid JSON", provider=provider, target=target
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderBadResponse(
            f"fixture {path.name} must be a JSON object", provider=provider, target=target
        )
    return payload


_SIMULATED: Mapping[str, type[ProviderError]] = {
    "unavailable": ProviderUnavailable,
    "rate_limited": ProviderRateLimited,
    "auth": ProviderAuthError,
    "bad_response": ProviderBadResponse,
}


def maybe_simulate_failure(payload: Mapping[str, Any], *, provider: str, target: str) -> None:
    """Fixtures may carry `__simulate__` to model a provider outage.

    This exists so the "a failure is not an empty result" rule is testable with
    real code paths rather than a mock -- the stub raises exactly what the live
    client would.
    """
    kind = payload.get("__simulate__")
    if not kind:
        return
    exc_type = _SIMULATED.get(str(kind))
    if exc_type is None:
        raise ProviderBadResponse(
            f"fixture requests unknown simulated failure {kind!r}", provider=provider, target=target
        )
    raise exc_type(
        f"simulated provider failure ({kind})",
        provider=provider,
        target=target,
        status_code=payload.get("__status_code__"),
    )


def setting(name: str, default: Any = None) -> Any:
    """Read a setting from `mazz.config` if it exists, else the environment.

    Deliberately tolerant: `mazz.config` is owned by another agent and may not
    exist (or may expose `settings`, `get_settings()` or `Settings`). Integrations
    must not fail to import because of that.
    """
    try:  # pragma: no cover - exercised implicitly
        from mazz import config as _config  # type: ignore
    except Exception:
        _config = None  # type: ignore[assignment]

    if _config is not None:
        obj = None
        for attr in ("settings", "get_settings", "Settings"):
            candidate = getattr(_config, attr, None)
            if candidate is None:
                continue
            try:
                obj = candidate() if callable(candidate) else candidate
            except Exception:
                obj = None
            if obj is not None:
                break
        if obj is not None:
            for key in (name.lower(), name):
                value = getattr(obj, key, None)
                if value not in (None, ""):
                    return value

    value = os.environ.get(name)
    if value not in (None, ""):
        return value
    return default
