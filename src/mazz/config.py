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
    review_user: str = "client"
    review_password_hash: str = ""

    # --- Compliance -----------------------------------------------------
    # CAN-SPAM requires a physical mailing address in every outbound email.
    # The drafting layer is expected to refuse to render a draft without it.
    sender_postal_address: str = ""

    # --- Phase 2 providers (unused in Phase 1; everything is stubbed) ----
    smartlead_api_key: str = ""
    smartlead_default_campaign_id: str = ""
    anthropic_api_key: str = ""
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
