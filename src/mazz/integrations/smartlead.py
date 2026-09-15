"""Smartlead client.

**We never send email.** The client keeps Smartlead as his sending rail and this
tool is entirely upstream of send (CLAUDE.md, non-goals: "No building a second
sender"). The single write this module performs is pushing an approved lead --
with its approved copy in custom fields -- into a campaign that Smartlead already
owns and sequences. There is deliberately no send / start / launch / trigger
method on this interface, and `test_smartlead.py` asserts that absence so nobody
adds one by reflex later.

Campaign mapping is a single configured default. The brief lists the client's
campaign structure (one drafting queue feeding many campaigns vs. one campaign
per mailbox/segment) as an OPEN QUESTION, so there is no routing machinery here
to unpick once he answers -- just `SMARTLEAD_DEFAULT_CAMPAIGN_ID`.

Phase 1 ships `StubSmartleadClient`, which appends the exact outbound payload to
`smartlead_outbox.jsonl` at the repo root and returns a fake id, so the whole push
path is inspectable with no API key. `LiveSmartleadClient` drops in behind the
same interface once the key exists.
"""

from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from mazz.integrations.base import (
    InstrumentedProvider,
    ProviderAuthError,
    ProviderError,
    RetryPolicy,
    find_repo_root,
    setting,
    utc_now,
)

__all__ = [
    "OUTBOX_FILENAME",
    "SmartleadCampaign",
    "SmartleadLead",
    "LeadPushSettings",
    "PushResult",
    "CampaignNotFound",
    "SmartleadClient",
    "StubSmartleadClient",
    "LiveSmartleadClient",
    "get_smartlead_client",
]

OUTBOX_FILENAME = "smartlead_outbox.jsonl"
STUB_PROVIDER = "smartlead-stub"
LIVE_PROVIDER = "smartlead"
DEFAULT_BASE_URL = "https://server.smartlead.ai/api/v1"
STUB_DEFAULT_CAMPAIGN_ID = "stub-campaign-1"


class CampaignNotFound(ProviderError):
    """Asked for a campaign Smartlead does not have. Fails closed: we would
    rather stall a push than drop approved copy into the wrong sequence."""


@dataclass(frozen=True, slots=True)
class SmartleadCampaign:
    id: str
    name: str
    status: str = "ACTIVE"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status}


@dataclass(frozen=True, slots=True)
class SmartleadLead:
    """One lead as Smartlead's lead-import API models it.

    The approved subject/body live in `custom_fields`: Smartlead's own sequence
    templates interpolate custom fields, which is how our copy reaches the wire
    without us ever sending anything.
    """

    email: str
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    website: str = ""
    location: str = ""
    linkedin_profile: str = ""
    phone_number: str = ""
    custom_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError(f"SmartleadLead requires a valid email, got {self.email!r}")

    def to_api_payload(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "company_name": self.company_name,
            "website": self.website,
            "location": self.location,
            "linkedin_profile": self.linkedin_profile,
            "phone_number": self.phone_number,
            "custom_fields": dict(self.custom_fields),
        }


@dataclass(frozen=True, slots=True)
class LeadPushSettings:
    """Smartlead's per-import toggles.

    All three default to False and the client refuses to flip the two suppression
    ones. Compliance controls fail closed: Smartlead's global block list and
    unsubscribe list are the client's legal record of who must never be
    contacted, and nothing upstream of send is entitled to override them.
    """

    ignore_global_block_list: bool = False
    ignore_unsubscribe_list: bool = False
    ignore_duplicate_leads_in_other_campaign: bool = False

    def validate(self) -> None:
        if self.ignore_global_block_list:
            raise ValueError(
                "refusing to push with ignore_global_block_list=True: "
                "the block list is a hard compliance control"
            )
        if self.ignore_unsubscribe_list:
            raise ValueError(
                "refusing to push with ignore_unsubscribe_list=True: "
                "unsubscribes are a hard compliance control"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ignore_global_block_list": self.ignore_global_block_list,
            "ignore_unsubscribe_list": self.ignore_unsubscribe_list,
            "ignore_duplicate_leads_in_other_campaign": (
                self.ignore_duplicate_leads_in_other_campaign
            ),
        }


@dataclass(frozen=True, slots=True)
class PushResult:
    smartlead_lead_id: str
    campaign_id: str
    email: str
    accepted: bool
    provider: str
    pushed_at: datetime
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "smartlead_lead_id": self.smartlead_lead_id,
            "campaign_id": self.campaign_id,
            "email": self.email,
            "accepted": self.accepted,
            "provider": self.provider,
            "pushed_at": self.pushed_at.isoformat(),
        }


class SmartleadClient(InstrumentedProvider, abc.ABC):
    """The Smartlead boundary.

    Three methods, all upstream of send:
      - `default_campaign()`  -- the single configured campaign (open question)
      - `get_campaign(id)`    -- campaign lookup
      - `add_lead_to_campaign(...)` -- our only write

    Do NOT add a send/start/pause/launch method. Smartlead owns sending.
    """

    provider: str = LIVE_PROVIDER

    def __init__(self, *, default_campaign_id: str | None = None) -> None:
        InstrumentedProvider.__init__(self)
        self._default_campaign_id = default_campaign_id or str(
            setting("SMARTLEAD_DEFAULT_CAMPAIGN_ID", STUB_DEFAULT_CAMPAIGN_ID)
        )

    @property
    def default_campaign_id(self) -> str:
        return self._default_campaign_id

    def resolve_campaign_id(self, campaign_id: str | None = None) -> str:
        """Single configured default. No segment/mailbox routing -- that is an
        open question in the brief and inventing an answer would be waste."""
        return campaign_id or self._default_campaign_id

    @abc.abstractmethod
    def default_campaign(self) -> SmartleadCampaign:
        """The configured default campaign."""

    @abc.abstractmethod
    def get_campaign(self, campaign_id: str) -> SmartleadCampaign:
        """Look up a campaign. Raises `CampaignNotFound` if absent."""

    @abc.abstractmethod
    def add_lead_to_campaign(
        self,
        lead: SmartleadLead,
        *,
        campaign_id: str | None = None,
        settings: LeadPushSettings | None = None,
    ) -> PushResult:
        """Push one approved lead + copy into a campaign. The only write we make."""


class StubSmartleadClient(SmartleadClient):
    """Phase 1. Appends the real outbound payload to `smartlead_outbox.jsonl`.

    The payload written is byte-for-byte what `LiveSmartleadClient` will POST, so
    the outbox is a genuine review artifact, not a log line.
    """

    provider = STUB_PROVIDER

    def __init__(
        self,
        *,
        outbox_path: Path | str | None = None,
        default_campaign_id: str | None = None,
        campaigns: Iterable[SmartleadCampaign] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(default_campaign_id=default_campaign_id)
        self.outbox_path = Path(outbox_path) if outbox_path else find_repo_root() / OUTBOX_FILENAME
        self._clock = clock
        known = list(campaigns) if campaigns is not None else [
            SmartleadCampaign(
                id=self.default_campaign_id,
                name="Default outbound campaign (stub)",
                status="ACTIVE",
            )
        ]
        self._campaigns: dict[str, SmartleadCampaign] = {c.id: c for c in known}

    # -- campaign lookup ---------------------------------------------------

    def default_campaign(self) -> SmartleadCampaign:
        self._record("default_campaign")
        return self.get_campaign(self.default_campaign_id, _record=False)

    def get_campaign(self, campaign_id: str, *, _record: bool = True) -> SmartleadCampaign:
        if _record:
            self._record("get_campaign", campaign_id)
        try:
            return self._campaigns[campaign_id]
        except KeyError:
            raise CampaignNotFound(
                f"campaign {campaign_id!r} is not configured",
                provider=self.provider,
                target=campaign_id,
            ) from None

    def list_campaigns(self) -> list[SmartleadCampaign]:
        self._record("list_campaigns")
        return list(self._campaigns.values())

    # -- the one write -----------------------------------------------------

    def add_lead_to_campaign(
        self,
        lead: SmartleadLead,
        *,
        campaign_id: str | None = None,
        settings: LeadPushSettings | None = None,
    ) -> PushResult:
        push_settings = settings or LeadPushSettings()
        push_settings.validate()

        resolved = self.resolve_campaign_id(campaign_id)
        campaign = self.get_campaign(resolved, _record=False)  # fails closed if unknown
        self._record("add_lead_to_campaign", lead.email, campaign_id=resolved)

        pushed_at = self._clock()
        lead_id = _stub_lead_id(resolved, lead.email)
        payload = {
            "request": {
                "method": "POST",
                "path": f"/campaigns/{resolved}/leads",
                "body": {
                    "lead_list": [lead.to_api_payload()],
                    "settings": push_settings.as_dict(),
                },
            },
            "campaign": campaign.as_dict(),
            "smartlead_lead_id": lead_id,
            "pushed_at": pushed_at.isoformat(),
            "provider": self.provider,
            "note": "STUB: nothing left this machine. Smartlead does the sending.",
        }

        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

        return PushResult(
            smartlead_lead_id=lead_id,
            campaign_id=resolved,
            email=lead.email,
            accepted=True,
            provider=self.provider,
            pushed_at=pushed_at,
            raw=payload,
        )

    # -- inspection --------------------------------------------------------

    def read_outbox(self) -> list[dict[str, Any]]:
        if not self.outbox_path.is_file():
            return []
        with self.outbox_path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class LiveSmartleadClient(SmartleadClient):
    """Phase 2. Same interface, real HTTP. Not implemented yet, and it must stay
    unimplemented rather than half-implemented: a partial live client is the one
    thing in this repo that could actually put mail in front of a human."""

    provider = LIVE_PROVIDER

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        default_campaign_id: str | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(default_campaign_id=default_campaign_id)
        if not api_key:
            raise ProviderAuthError("SMARTLEAD_API_KEY is required for the live client",
                                    provider=self.provider)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.retry_policy = retry_policy or RetryPolicy()

    def default_campaign(self) -> SmartleadCampaign:
        raise NotImplementedError("Phase 2: GET /campaigns/{id}")

    def get_campaign(self, campaign_id: str) -> SmartleadCampaign:
        raise NotImplementedError("Phase 2: GET /campaigns/{id}")

    def add_lead_to_campaign(
        self,
        lead: SmartleadLead,
        *,
        campaign_id: str | None = None,
        settings: LeadPushSettings | None = None,
    ) -> PushResult:
        raise NotImplementedError("Phase 2: POST /campaigns/{id}/leads")


def get_smartlead_client(
    *,
    api_key: str | None = None,
    default_campaign_id: str | None = None,
    outbox_path: Path | str | None = None,
    force: str | None = None,
) -> SmartleadClient:
    """Pick stub vs live from config.

    No `SMARTLEAD_API_KEY` -> stub. That is the Phase 1 default and means the whole
    pipeline runs, and is reviewable, with zero credentials.
    """
    if force not in (None, "stub", "live"):
        raise ValueError(f"force must be 'stub', 'live' or None, got {force!r}")

    key = api_key if api_key is not None else setting("SMARTLEAD_API_KEY", "")
    use_live = force == "live" or (force is None and bool(key))

    if use_live:
        return LiveSmartleadClient(
            api_key=str(key or ""),
            default_campaign_id=default_campaign_id,
        )
    return StubSmartleadClient(
        outbox_path=outbox_path,
        default_campaign_id=default_campaign_id,
    )


def _stub_lead_id(campaign_id: str, email: str) -> str:
    """Deterministic fake id: re-pushing the same lead to the same campaign yields
    the same id, so tests (and the review app) can assert on it."""
    digest = hashlib.sha256(f"{campaign_id}|{email.strip().lower()}".encode("utf-8")).hexdigest()
    return f"stub-lead-{digest[:12]}"
