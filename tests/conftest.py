"""Session-wide safety net: no test may ever reach a paid provider.

This file was deliberately left unclaimed earlier to avoid agent collisions.
It is claimed now because the hazard it closes is real: `mazz.integrations.base
.setting()` reads `mazz.config.get_settings()`, which is a process-wide
`lru_cache`d singleton loaded from the developer's own `.env`. Without this
fixture, a plain `pytest` run's provider selection depends on whatever a
developer happens to have in `.env` at that moment -- which is exactly how a
paid Bright Data / Anthropic call -- or a real email to a stranger -- could
slip into CI or a laptop
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

import os

# -- The test database is never the dev database ----------------------------
#
# Set BEFORE anything below imports `mazz.db`, whose engine is an lru_cached
# singleton built from `DATABASE_URL`. The tests TRUNCATE every table between
# cases, and `.env` points DATABASE_URL at `mazz_dev` -- the database the
# running app, the demo seed and the client's mailbox roster live in. A plain
# `pytest` once emptied it. The environment variable wins over `.env` in
# pydantic-settings, so this redirects every connection the suite makes.
_TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://localhost/mazz_test"
)
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL

import pytest  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from mazz.config import reload_settings  # noqa: E402

reload_settings()

#: Refuse outright rather than trust the redirect above: a TEST_DATABASE_URL
#: typo'd onto a real database would otherwise be truncated just the same.
TEST_DATABASE_SUFFIX = "_test"


def _ensure_test_database() -> None:
    """Create the test database if it is missing, then migrate it to head."""

    url = make_url(_TEST_DATABASE_URL)
    name = url.database or ""
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise pytest.UsageError(
            f"refusing to run the suite against database {name!r}: the tests "
            f"TRUNCATE every table, so the database name must end in "
            f"{TEST_DATABASE_SUFFIX!r}. Set TEST_DATABASE_URL."
        )

    import psycopg
    from psycopg import sql

    admin = url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin.render_as_string(hide_password=False), autocommit=True
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")


def pytest_sessionstart(session: pytest.Session) -> None:
    _ensure_test_database()

# Every provider-selection / credential setting known to this codebase.
# Add to this list, never remove from it, as new providers show up.
_SAFE_ENV: dict[str, str] = {
    "LINKEDIN_PROVIDER": "stub",
    "LINKEDIN_API_KEY": "",
    "COMPANY_SCRAPER_PROVIDER": "stub",
    "EMAIL_SENDER": "stub",
    # The kill switch is ON in tests so the send path runs end to end -- safe
    # only because EMAIL_SENDER is pinned to the stub on the line above, which
    # writes to a local file and has no network code. Tests of the switch
    # itself set it false explicitly.
    "SEND_ENABLED": "true",
    "ANTHROPIC_API_KEY": "",
    # Not a credential, but the same hazard: `DRAFT_COMPOSER=claude` in a
    # developer's `.env` would route every drafting test through a paid,
    # non-deterministic model call.
    "DRAFT_COMPOSER": "template",
    # Same hazard on the verification side: `VERIFY_JUDGE=claude` turns the
    # optional entailment judge on, and `handle_verify` reads it from settings,
    # so a developer's `.env` could route queue tests through a paid model call.
    "VERIFY_JUDGE": "off",
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
