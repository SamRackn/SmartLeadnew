"""Application settings.

Every value is read from the environment (or a local ``.env``); see
``.env.example`` for the canonical list. Phase 1 needs none of the provider
keys — they exist here so Phase 2 can drop in without a settings change.

Usage::

    from mazz.config import get_settings

    settings = get_settings()
    settings.database_url

``get_settings()`` is cached, so it is cheap to call anywhere. Tests that
change the environment should call ``reload_settings()`` afterwards.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/mazz_dev"


class Settings(BaseSettings):
    """Typed view of the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Postgres -------------------------------------------------------
    database_url: str = DEFAULT_DATABASE_URL

    # --- Review app -----------------------------------------------------
    session_secret: str = "changeme"

    # --- Compliance -----------------------------------------------------
    # CAN-SPAM requires a physical mailing address in every outbound email.
    # The drafting layer is expected to refuse to render a draft without it.
    sender_postal_address: str = ""

    # --- Phase 2 providers (unused in Phase 1; everything is stubbed) ----
    anthropic_api_key: str = ""

    # --- Sending --------------------------------------------------------
    # Which sender is in force. "stub" appends the real MIME to
    # `sent_outbox.jsonl` and touches no network; "google" sends as the
    # client's own Google Workspace mailboxes over the Gmail API.
    #
    # Explicit, exactly like `draft_composer`, and for a sharper reason: a
    # service-account key appearing in the environment is not consent to start
    # emailing strangers. Presence of GMAIL_SA_* must never flip this.
    email_sender: str = "stub"
    # THE kill switch, checked in the send path before any sender is even
    # built. False (the default) renders each released message's MIME to
    # out/dryrun/ and sends nothing, whatever EMAIL_SENDER says. Only the
    # go-live gate sets it true.
    send_enabled: bool = False
    # Hold each send to Mon-Fri 9:00-17:00 in the lead's timezone (deferred,
    # not failed, outside it). Off only in tests.
    send_window: bool = True
    # Host that serves the public unsubscribe page. Requests arriving on it may
    # reach /u/* and /health only; everything else is a 404 there. Blank = no
    # dedicated host (single-host dev).
    unsubscribe_host: str = ""
    # Per-mailbox daily send ceiling used when a mailbox row does not set its
    # own. Small on purpose -- Workspace permits ~2000/day, and cold outreach
    # from a burner domain does not survive anything near that.
    default_daily_cap: int = 30
    # Minimum gap between two sends from the SAME mailbox, in seconds.
    default_min_spacing_seconds: int = 300

    # --- Drafting -------------------------------------------------------
    # Which composer is in force. "template" is the deterministic Phase 1
    # composer -- no API key, no network, reproducible. "claude" switches on
    # the real model call. Deliberately an explicit setting rather than
    # "use Claude if a key is present": a key in the environment is not
    # consent to start spending, and the difference between the two composers
    # is the difference between a demo and a bill.
    draft_composer: str = "template"
    # Drafting model. Opus-tier by default because this call writes the thing
    # the client is buying. Move it only behind a measured eval -- and mind
    # that a cheaper model may have a HIGHER minimum cacheable prefix, so a
    # downgrade can cost more than it saves. See
    # `compose_claude.MIN_CACHEABLE_PREFIX_TOKENS`.
    draft_model: str = ""
    # Thinking effort for drafting. Blank means the API default (`high`), i.e.
    # no quality trade out of the box. This is the first lever to sweep once
    # there is an eval set. Pinned per route: varying it per request would
    # invalidate the prompt cache on every call.
    draft_effort: str = ""
    # Requests per Batch API submission. The API ceiling is 100,000; this is
    # sized to the client's volume, not to the limit.
    draft_batch_size: int = 500
    linkedin_provider: str = "stub"
    # Bright Data account key (also usable for ScrapIn later if that env var
    # name is kept). Presence of this key must NEVER by itself change which
    # adapter `get_linkedin_adapter()` returns -- only `linkedin_provider`
    # (or an explicit `provider=` argument) does that. See
    # `mazz.integrations.linkedin.get_linkedin_adapter`.
    linkedin_api_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""

    return Settings()


def reload_settings() -> Settings:
    """Drop the cached settings and re-read the environment (tests only)."""

    get_settings.cache_clear()
    return get_settings()
