"""Session-wide safety net: no test may ever reach a paid provider.

This file was deliberately left unclaimed earlier to avoid agent collisions.
It is claimed now because the hazard it closes is real: `mazz.integrations.base
.setting()` reads `mazz.config.get_settings()`, which is a process-wide
`lru_cache`d singleton loaded from the developer's own `.env`. Without this
fixture, a plain `pytest` run's provider selection depends on whatever a
developer happens to have in `.env` at that moment -- which is exactly how a
paid Bright Data / Smartlead / Anthropic call could slip into CI or a laptop
run and bill someone's account.

This autouse fixture pins every provider-selection setting to its safe
stub/off default and blanks every provider credential, for every test, before
that test runs -- regardless of the ambient environment or `.env` contents.
A test that wants to exercise "brightdata is not configured" or similar still
gets to do so explicitly (it is already running with a blanked key); a test
must never be able to opt an *other* test into a live provider by leaving a
key set in the environment.
"""

from __future__ import annotations

import pytest

from mazz.config import reload_settings

# Every provider-selection / credential setting known to this codebase.
# Add to this list, never remove from it, as new providers show up.
_SAFE_ENV: dict[str, str] = {
    "LINKEDIN_PROVIDER": "stub",
    "LINKEDIN_API_KEY": "",
    "COMPANY_SCRAPER_PROVIDER": "stub",
    "SMARTLEAD_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
}


@pytest.fixture(autouse=True)
def _no_live_providers(monkeypatch: pytest.MonkeyPatch):
    """Force every test to start from a paid-provider-free environment."""
    for key, value in _SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    reload_settings()
    yield
    # Re-clamp rather than trusting monkeypatch's own teardown ordering: this
    # leaves the settings cache pinned safe no matter what runs next, even if
    # a future fixture reads settings during its own teardown.
    reload_settings()
