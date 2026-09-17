"""Shared scaffolding for the pipeline tests (ingest / queue / privacy).

Not a conftest on purpose: ``tests/conftest.py`` is unclaimed and another agent
may want it. pytest puts ``tests/`` on ``sys.path``, so ``import
pipeline_helpers`` works from any test module in this directory.

Every test here runs against the real ``mazz_dev`` Postgres. The queue's whole
correctness argument is ``FOR UPDATE SKIP LOCKED``, the cache's is a JSONB
column and a unique primary key, and privacy's is ``ON DELETE CASCADE`` -- none
of which SQLite would model. Tables are truncated between tests rather than
mocked.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from mazz.db import get_sessionmaker
from mazz.integrations.company import StubCompanyAdapter
from mazz.integrations.linkedin import StubLinkedInAdapter
from mazz.models import Draft, Job, Lead, ResearchCache, SuppressionEntry

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES / "sample_leads.csv"
SCENARIOS = FIXTURES / "scenarios.json"

#: The two never-contact identities baked into sample_leads.csv. Kept here so
#: the CSV and the suppression rows can never drift apart.
SUPPRESSED_DOMAIN = "blocked-corp.example"
SUPPRESSED_EMAIL = "helen.vasquez@fairlane-logistics.example"

#: From tests/fixtures/scenarios.json (owned by integrations-engineer).
SHARED_DOMAIN = "northwind-logistics.com"
SHARED_PROFILES = (
    "https://www.linkedin.com/in/dana-whitfield",
    "https://www.linkedin.com/in/marcus-oyelaran",
)
WEAK_DOMAIN = "paperkite.io"
EMPTY_DOMAIN = "stonebridge-partners.com"
MISSING_DOMAIN = "no-such-company.example"
OUTAGE_DOMAIN = "outage-test.example"

# Every table, so `reset_db()` actually resets. TRUNCATE ... CASCADE only
# follows references *into* the listed tables, so a parent side (draft_batches,
# campaigns, workspaces) is NOT reached by truncating its children and has to
# be named here or its rows survive into the next test.
_TABLES = (
    "jobs",
    "drafts",
    "draft_batches",
    "leads",
    "campaigns",
    "workspaces",
    "research_cache",
    "suppression_list",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def scenarios() -> dict[str, dict[str, Any]]:
    """The fixture manifest, keyed by scenario name."""

    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return {s["name"]: s for s in payload["scenarios"]}


def reset_db() -> None:
    """Empty every table. TRUNCATE ... CASCADE, in its own transaction."""

    factory = get_sessionmaker()
    with factory() as session:
        session.execute(
            text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        )
        session.commit()


@contextmanager
def db_session() -> Iterator[Session]:
    """A committed-on-exit session, like ``session_scope`` but test-owned."""

    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def company_adapter(**kwargs: Any) -> StubCompanyAdapter:
    return StubCompanyAdapter(fixtures_dir=FIXTURES, **kwargs)


def linkedin_adapter(**kwargs: Any) -> StubLinkedInAdapter:
    return StubLinkedInAdapter(fixtures_dir=FIXTURES, **kwargs)


# --------------------------------------------------------------------------
# Row builders
# --------------------------------------------------------------------------


def suppress(
    session: Session,
    *,
    email: str | None = None,
    domain: str | None = None,
    reason: str = "test",
) -> SuppressionEntry:
    entry = SuppressionEntry(email=email, domain=domain, reason=reason)
    session.add(entry)
    session.flush()
    return entry


def seed_sample_suppression(session: Session) -> None:
    """Insert the two entries ``sample_leads.csv`` expects to be blocked by."""

    suppress(
        session,
        domain=SUPPRESSED_DOMAIN,
        reason="legal asked us in writing never to contact anyone here",
    )
    suppress(session, email=SUPPRESSED_EMAIL, reason="unsubscribed, asked to be removed")


def csv_row(**overrides: Any) -> dict[str, Any]:
    """A complete, valid inbound row in the client's own header spellings."""

    row = {
        "Full Name": "Dana Whitfield",
        "Company": "Northwind Logistics",
        "Website": "https://www.northwind-logistics.com/",
        "Job Title": "VP Revenue Operations",
        "Email": "dana.whitfield@northwind-logistics.com",
        "Employee Count": "240",
        "Revenue Range": "$25M-$50M",
        "Location": "Columbus, OH, United States",
        "Primary Industry": "Logistics & Supply Chain",
        "Specific Industry": "Third-Party Logistics",
        "LinkedIn URL": SHARED_PROFILES[0],
        "Notes": "Met at MODEX; RevOps team is stretched thin.",
    }
    row.update(overrides)
    return row


def make_lead(session: Session, **kwargs: Any) -> Lead:
    """Insert a lead directly, bypassing ingest (for queue/privacy tests)."""

    defaults: dict[str, Any] = {
        "full_name": "Dana Whitfield",
        "email": "dana.whitfield@northwind-logistics.com",
        "company_name": "Northwind Logistics",
        "website": "https://www.northwind-logistics.com/",
        "company_domain": SHARED_DOMAIN,
        "linkedin_url": SHARED_PROFILES[0],
    }
    defaults.update(kwargs)
    lead = Lead(**defaults)
    session.add(lead)
    session.flush()
    return lead


def make_draft(session: Session, lead: Lead, **kwargs: Any) -> Draft:
    defaults: dict[str, Any] = {
        "lead_id": lead.id,
        "subject": "Quick question about your RevOps hiring",
        "body": "Saw the Revenue Operations Manager req.",
        "signals_used": [],
    }
    defaults.update(kwargs)
    draft = Draft(**defaults)
    session.add(draft)
    session.flush()
    return draft


# --------------------------------------------------------------------------
# Assertions / counters
# --------------------------------------------------------------------------


def count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def jobs_for(session: Session, lead_id: int) -> list[Job]:
    return list(
        session.execute(select(Job).where(Job.lead_id == lead_id).order_by(Job.id))
        .scalars()
        .all()
    )


def lead_by_email(session: Session, email: str) -> Lead | None:
    return session.execute(
        select(Lead).where(Lead.email == email.strip().lower())
    ).scalar_one_or_none()


def cache_row(session: Session, domain: str) -> ResearchCache | None:
    return session.get(ResearchCache, domain)


def cache_domains(session: Session) -> list[str]:
    return sorted(
        session.execute(select(ResearchCache.company_domain)).scalars().all()
    )


def ago(**kwargs: Any) -> datetime:
    return utcnow() - timedelta(**kwargs)
