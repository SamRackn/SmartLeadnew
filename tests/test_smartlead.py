"""Smartlead client tests.

The load-bearing one is `test_interface_exposes_no_send_capability`: the brief's
hardest constraint is that this tool is entirely upstream of send, and a test is
the only thing that keeps a plausible-looking `send_campaign()` from being added
in six months.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mazz.integrations.smartlead import (
    OUTBOX_FILENAME,
    CampaignNotFound,
    LeadPushSettings,
    LiveSmartleadClient,
    SmartleadCampaign,
    SmartleadClient,
    SmartleadLead,
    StubSmartleadClient,
    get_smartlead_client,
)

FIXED_NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    return tmp_path / OUTBOX_FILENAME


@pytest.fixture
def client(outbox: Path) -> StubSmartleadClient:
    return StubSmartleadClient(
        outbox_path=outbox,
        default_campaign_id="camp-outbound-1",
        clock=lambda: FIXED_NOW,
    )


def make_lead(email: str = "dana@northwind-logistics.com") -> SmartleadLead:
    return SmartleadLead(
        email=email,
        first_name="Dana",
        last_name="Whitfield",
        company_name="Northwind Logistics",
        website="https://northwind-logistics.com",
        location="Chicago, Illinois, United States",
        linkedin_profile="https://www.linkedin.com/in/dana-whitfield",
        custom_fields={
            "mazz_subject": "your RevOps req",
            "mazz_body": "Saw the Revenue Operations Manager opening...",
            "mazz_signal_fact_id": "job_posting:abc123def456",
        },
    )


# ---------------------------------------------------------------- the rail


def test_interface_exposes_no_send_capability():
    """We never send email. Smartlead is the sending rail; our only write is
    pushing approved copy into a campaign it already owns."""
    forbidden = ("send", "deliver", "launch", "trigger", "start_campaign", "resume", "mailbox")
    for cls in (SmartleadClient, StubSmartleadClient, LiveSmartleadClient):
        for name in dir(cls):
            if name.startswith("_"):
                continue
            assert not any(word in name.lower() for word in forbidden), (
                f"{cls.__name__}.{name} looks like a send capability. "
                "This tool is entirely upstream of send (CLAUDE.md non-goals)."
            )


def test_only_write_is_add_lead_to_campaign():
    public = {n for n in dir(StubSmartleadClient) if not n.startswith("_")}
    writes = public - {
        "default_campaign", "get_campaign", "list_campaigns", "read_outbox",
        "resolve_campaign_id", "default_campaign_id", "outbox_path", "provider",
        "call_count", "calls", "calls_to", "reset_calls", "fixtures_dir",
    }
    assert writes == {"add_lead_to_campaign"}


# ------------------------------------------------------------- the stub push


def test_stub_push_writes_payload_to_outbox(client: StubSmartleadClient, outbox: Path):
    result = client.add_lead_to_campaign(make_lead())

    assert outbox.is_file()
    lines = outbox.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["request"]["method"] == "POST"
    assert record["request"]["path"] == "/campaigns/camp-outbound-1/leads"
    body = record["request"]["body"]
    assert len(body["lead_list"]) == 1
    pushed = body["lead_list"][0]
    assert pushed["email"] == "dana@northwind-logistics.com"
    assert pushed["company_name"] == "Northwind Logistics"
    assert pushed["custom_fields"]["mazz_subject"] == "your RevOps req"
    assert record["smartlead_lead_id"] == result.smartlead_lead_id
    assert record["pushed_at"] == FIXED_NOW.isoformat()


def test_stub_returns_fake_id_and_is_deterministic(client: StubSmartleadClient):
    first = client.add_lead_to_campaign(make_lead())
    second = client.add_lead_to_campaign(make_lead())

    assert first.smartlead_lead_id.startswith("stub-lead-")
    assert first.smartlead_lead_id == second.smartlead_lead_id
    assert first.accepted is True
    assert first.campaign_id == "camp-outbound-1"
    assert first.provider == "smartlead-stub"


def test_outbox_appends_across_client_instances(outbox: Path):
    a = StubSmartleadClient(outbox_path=outbox, default_campaign_id="camp-outbound-1")
    a.add_lead_to_campaign(make_lead("dana@northwind-logistics.com"))
    b = StubSmartleadClient(outbox_path=outbox, default_campaign_id="camp-outbound-1")
    b.add_lead_to_campaign(make_lead("marcus@northwind-logistics.com"))

    records = b.read_outbox()
    assert len(records) == 2
    emails = [r["request"]["body"]["lead_list"][0]["email"] for r in records]
    assert emails == ["dana@northwind-logistics.com", "marcus@northwind-logistics.com"]


def test_push_is_inspectable_with_no_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SMARTLEAD_API_KEY", raising=False)
    monkeypatch.delenv("SMARTLEAD_DEFAULT_CAMPAIGN_ID", raising=False)
    built = get_smartlead_client(outbox_path=tmp_path / OUTBOX_FILENAME)
    assert isinstance(built, StubSmartleadClient)
    result = built.add_lead_to_campaign(make_lead())
    assert result.accepted
    assert built.read_outbox()


# ----------------------------------------------------------- campaign lookup


def test_default_campaign_comes_from_config(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SMARTLEAD_DEFAULT_CAMPAIGN_ID", "camp-from-env")
    # mazz.config (db-architect's file) takes precedence over the environment by
    # design. If it ever ships a non-blank default, that wins and this env-level
    # assertion no longer applies.
    from mazz.integrations import base as _base

    if _base.setting("SMARTLEAD_DEFAULT_CAMPAIGN_ID") != "camp-from-env":
        pytest.skip("mazz.config supplies SMARTLEAD_DEFAULT_CAMPAIGN_ID; env is shadowed")
    built = StubSmartleadClient(outbox_path=tmp_path / OUTBOX_FILENAME)
    campaign = built.default_campaign()
    assert isinstance(campaign, SmartleadCampaign)
    assert campaign.id == "camp-from-env"
    assert built.resolve_campaign_id(None) == "camp-from-env"


def test_unknown_campaign_fails_closed(client: StubSmartleadClient):
    with pytest.raises(CampaignNotFound):
        client.get_campaign("camp-does-not-exist")
    with pytest.raises(CampaignNotFound):
        client.add_lead_to_campaign(make_lead(), campaign_id="camp-does-not-exist")


def test_no_campaign_routing_machinery(client: StubSmartleadClient):
    """Campaign structure is an open question in the brief - a single configured
    default is the whole mapping, deliberately."""
    assert client.resolve_campaign_id() == "camp-outbound-1"
    assert client.resolve_campaign_id("camp-explicit") == "camp-explicit"
    assert len(client.list_campaigns()) == 1


# -------------------------------------------------------------- compliance


@pytest.mark.parametrize(
    "kwargs",
    [{"ignore_global_block_list": True}, {"ignore_unsubscribe_list": True}],
)
def test_refuses_to_override_suppression(client: StubSmartleadClient, outbox: Path, kwargs):
    with pytest.raises(ValueError, match="compliance control"):
        client.add_lead_to_campaign(make_lead(), settings=LeadPushSettings(**kwargs))
    assert not outbox.exists(), "a refused push must not reach the outbox"


def test_default_push_settings_respect_suppression(client: StubSmartleadClient):
    client.add_lead_to_campaign(make_lead())
    settings = client.read_outbox()[0]["request"]["body"]["settings"]
    assert settings["ignore_global_block_list"] is False
    assert settings["ignore_unsubscribe_list"] is False


def test_lead_requires_valid_email():
    with pytest.raises(ValueError):
        SmartleadLead(email="not-an-email")


# ------------------------------------------------------------ factory / live


def test_factory_returns_live_client_when_key_present(monkeypatch):
    monkeypatch.setenv("SMARTLEAD_API_KEY", "sk-live-xxx")
    built = get_smartlead_client(default_campaign_id="camp-1")
    assert isinstance(built, LiveSmartleadClient)


def test_live_client_is_honestly_unimplemented():
    live = LiveSmartleadClient(api_key="sk-live-xxx", default_campaign_id="camp-1")
    with pytest.raises(NotImplementedError):
        live.get_campaign("camp-1")
    with pytest.raises(NotImplementedError):
        live.default_campaign()
    with pytest.raises(NotImplementedError):
        live.add_lead_to_campaign(make_lead())


def test_factory_can_be_forced(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SMARTLEAD_API_KEY", "sk-live-xxx")
    assert isinstance(
        get_smartlead_client(force="stub", outbox_path=tmp_path / OUTBOX_FILENAME),
        StubSmartleadClient,
    )
    with pytest.raises(ValueError):
        get_smartlead_client(force="whatever")


# ------------------------------------------------------------ observability


def test_call_count_is_observable(client: StubSmartleadClient):
    assert client.call_count == 0
    client.add_lead_to_campaign(make_lead())
    client.get_campaign("camp-outbound-1")
    assert client.call_count == 2
    assert client.calls_to("add_lead_to_campaign") == [("dana@northwind-logistics.com",)]
