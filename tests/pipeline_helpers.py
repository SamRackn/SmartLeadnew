"""Shared scaffolding for the pipeline tests (ingest / queue / privacy).

Not a conftest on purpose: ``tests/conftest.py`` is unclaimed and another agent
may want it. pytest puts ``tests/`` on ``sys.path``, so ``import
pipeline_helpers`` works from any test module in this directory.

Every test here runs against a real Postgres -- ``mazz_test``, never the dev
database (see ``conftest.py``). The queue's whole
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

from mazz.db import get_engine, get_sessionmaker
from mazz.integrations.company import StubCompanyAdapter
from mazz.integrations.linkedin import StubLinkedInAdapter
from mazz.models import (
    Campaign,
    Draft,
    Job,
    Lead,
    ProofPoint,
    ResearchCache,
    SuppressionEntry,
    Workspace,
)

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
    "proof_points",
    "campaigns",
    "workspaces",
    "research_cache",
    "suppression_list",
    "audit_events",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def scenarios() -> dict[str, dict[str, Any]]:
    """The fixture manifest, keyed by scenario name."""

    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return {s["name"]: s for s in payload["scenarios"]}


def reset_db() -> None:
    """Empty every table. TRUNCATE ... CASCADE, in its own transaction."""

    database = get_engine().url.database or ""
    if not database.endswith("_test"):
        # Second lock on the same door as conftest's: this function is the
        # one that actually destroys data, so it checks for itself.
        raise RuntimeError(
            f"reset_db refused to TRUNCATE {database!r}: not a *_test database"
        )

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


#: Vocabulary matching the qualification pages in the northwind fixture. The
#: cold path cannot draft without one: a campaign with no `vertical_terms`
#: qualifies nobody, by design (see `pipeline.qualify`).
LOGISTICS_QUALIFICATION = {
    "vertical_terms": ["logistics", "freight", "carrier", "shippers", "brokerage"],
}


#: A workspace footer address that has been confirmed by a named person --
#: what every routed lead needs before its email can render.
CONFIRMED_ADDRESS: dict[str, Any] = {
    "postal_address": "Mazz Ltd, 1 Test Street, Testville",
    "postal_address_confirmed_by": "tester@example.test",
    "postal_address_confirmed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
}


def make_campaign(session: Session, **kwargs: Any) -> Campaign:
    """A workspace + campaign complete enough to write email 1.

    Complete means: a vertical noun for the copy, an offer line, a vocabulary
    that confirms the vertical against the fixture's own pages, and one active
    proof point. Leave any of those out and the pipeline correctly declines to
    write, which is a different test.
    """

    workspace = Workspace(
        name="ClientRise Demo",
        slug=f"demo-{utcnow().timestamp()}",
        **CONFIRMED_ADDRESS,
    )
    session.add(workspace)
    session.flush()

    defaults: dict[str, Any] = {
        "workspace_id": workspace.id,
        "name": "Freight Demand",
        "segment": "logistics",
        "vertical": "logistics companies",
        "offer_line": "fill empty lanes through search marketing",
        "qualification_config": dict(LOGISTICS_QUALIFICATION),
    }
    defaults.update(kwargs)
    campaign = Campaign(**defaults)
    session.add(campaign)
    session.flush()
    session.add(
        ProofPoint(
            campaign_id=campaign.id,
            claim_text="One recent client filled 30 empty lanes in a single month.",
            client_name="Cedar Freight Co",
            occurred_on=utcnow().date(),
            source_note="TMS export, shared with permission",
        )
    )
    session.flush()
    return campaign


def make_lead(session: Session, **kwargs: Any) -> Lead:
    """Insert a lead directly, bypassing ingest (for queue/privacy tests).

    Unrouted by default, which is what most tests here want. A lead that has to
    reach the DRAFTING stage needs a campaign complete enough to write email 1
    -- pass ``campaign_id=make_campaign(session).id``, or use `make_drafting_lead`.
    """

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


def make_drafting_lead(session: Session, **kwargs: Any) -> Lead:
    """A lead that can actually reach `draft`: routed to a complete campaign.

    The cold path declines to write for an unrouted lead, and correctly so --
    with no campaign there is no vertical noun to put in "We help ___", so
    there is no email. Tests that want the whole chain need this one.
    """

    kwargs.setdefault("campaign_id", make_campaign(session).id)
    return make_lead(session, **kwargs)


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
