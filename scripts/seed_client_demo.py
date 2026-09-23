"""Seed the client's own scenario for a live demo. ADDITIVE -- resets nothing.

`seed_demo.py` drops and rebuilds the database, which would delete the client's
mailbox roster. This one only inserts, so it is safe to run against the dev
database the demo will be given on.

It deliberately does NOT produce eight happy leads. A demo where everything
succeeds shows none of what the client is paying for, so the seeded list
contains:

  * five law firms whose own pages confirm the vertical  -> drafts written
  * one firm whose pages never say what it does          -> `no_angle`, no draft
  * one firm whose site says it is not taking new clients-> `no_angle`, no draft
  * one address already on the suppression list          -> refused at ingest

The proof point is seeded INACTIVE and clearly marked as a placeholder. A
statistic may only enter the copy through a proof_points row with a named
client, and inventing a plausible-looking client name for a demo is exactly the
fabricated record that table exists to prevent -- it would also be one flag away
from going out in real mail. Replace the text, name the real client, then
activate it.

Run: .venv/bin/python scripts/seed_client_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault(
    "SENDER_POSTAL_ADDRESS", "Search Site Marketing, 1 Example St, Columbus, OH"
)
os.environ.setdefault("SESSION_SECRET", "seed-client-demo")

from sqlalchemy import select  # noqa: E402

from mazz.config import reload_settings  # noqa: E402
from mazz.db import get_sessionmaker  # noqa: E402
from mazz.ingest.csv_import import import_rows  # noqa: E402
from mazz.models import (  # noqa: E402
    Campaign,
    Draft,
    Lead,
    LeadStatus,
    ProofPoint,
    ResearchCache,
    SuppressionEntry,
    Workspace,
)
from mazz.pipeline.queue import DEFAULT_HANDLERS, run_worker  # noqa: E402
from mazz.pipeline.routing import PinnedRoutingPolicy  # noqa: E402
from mazz.schemas import Fact, FactKind, stable_fact_id  # noqa: E402

NOW = datetime.now(timezone.utc)

VERTICAL_TERMS = [
    "law firm",
    "attorneys at law",
    "attorney",
    "practice areas",
    "criminal defense",
    "legal representation",
]

# (name, company, email, domain, "pages" describing the site)
#
# "qualified"     -> homepage + services + contact, vertical and intake both clear
# "no_vertical"   -> a real site that never says what the business is
# "not_taking"    -> says plainly it is not accepting new clients
# "suppressed"    -> on the do-not-contact list before ingest runs
LEADS = [
    ("Louis Gerber", "DAME Legal", "louis@damelegal.com", "damelegal.com", "qualified"),
    ("Priya Chaudhry", "Chaudhry Law", "priya@chaudhrylaw.com", "chaudhrylaw.com", "qualified"),
    ("Bruce Barket", "Barket Epstein", "bbarket@barketepstein.com", "barketepstein.com", "qualified"),
    ("Russell Cate", "Riley Cate", "rcate@rileycate.com", "rileycate.com", "qualified"),
    ("Louis Fasulo", "Fasulo Braverman", "lfasulo@fbdmlaw.com", "fbdmlaw.com", "qualified"),
    ("Paul Krieger", "Krieger Kim", "paul.krieger@kklllp.com", "kklllp.com", "no_vertical"),
    ("Adam Konta", "Konta Georges", "akonta@kontageorges.com", "kontageorges.com", "not_taking"),
    ("David Gelfand", "Jason Goldman Law", "dg@jasongoldmanlaw.com", "jasongoldmanlaw.com", "suppressed"),
]


def fact(domain: str, kind: str, content: str, path: str = "") -> Fact:
    url = f"https://{domain}/{path}"
    return Fact(
        id=stable_fact_id(kind, url, content),
        kind=kind,
        content=content,
        source_url=url,
        retrieved_at=NOW,
    )


def pages_for(company: str, domain: str, shape: str) -> list[Fact]:
    if shape == "no_vertical":
        # A real site that never says what the business is. This is the common
        # case the vertical gate exists for, not an exotic one.
        return [
            fact(domain, FactKind.HOMEPAGE.value, f"{company} — Welcome"),
            fact(domain, FactKind.CONTACT_PAGE.value, "Get in touch.", "contact"),
        ]
    if shape == "not_taking":
        return [
            fact(domain, FactKind.HOMEPAGE.value, f"{company} — Attorneys at Law"),
            fact(
                domain,
                FactKind.CONTACT_PAGE.value,
                "We are not accepting new clients at this time.",
                "contact",
            ),
        ]
    return [
        fact(domain, FactKind.HOMEPAGE.value, f"{company} — Attorneys at Law"),
        fact(
            domain,
            FactKind.SERVICES_PAGE.value,
            "Practice Areas: Criminal Defense, Personal Injury, Family Law.",
            "practice-areas",
        ),
        fact(
            domain,
            FactKind.CONTACT_PAGE.value,
            "Free consultation — new clients welcome. Contact us today.",
            "contact",
        ),
    ]


def main() -> int:
    reload_settings()
    session = get_sessionmaker()()
    try:
        # --- workspace + campaign ---------------------------------------
        workspace = session.execute(
            select(Workspace).where(Workspace.slug == "search-site-marketing")
        ).scalar_one_or_none()
        if workspace is None:
            workspace = Workspace(slug="search-site-marketing", name="Search Site Marketing")
            session.add(workspace)
            session.flush()

        campaign = session.execute(
            select(Campaign).where(
                Campaign.workspace_id == workspace.id,
                Campaign.name == "SearchSiteMarketing",
            )
        ).scalar_one_or_none()
        if campaign is None:
            campaign = Campaign(
                workspace_id=workspace.id,
                name="SearchSiteMarketing",
                segment="law-firms",
                scoring_config={},
                vertical="law firms",
                # No digit anywhere in this sentence -- a number here is refused,
                # because nothing holds a figure in sender voice to any evidence.
                offer_line="generate qualified client opportunities through search marketing",
                qualification_config={"vertical_terms": VERTICAL_TERMS},
                generic_vertical_fallback=False,
            )
            session.add(campaign)
            session.flush()
            print(f"campaign  : {campaign.name!r} vertical={campaign.vertical!r}")
        else:
            print(f"campaign  : {campaign.name!r} (already present)")

        # --- placeholder proof point, INACTIVE --------------------------
        if not session.execute(
            select(ProofPoint).where(ProofPoint.campaign_id == campaign.id)
        ).scalars().first():
            session.add(
                ProofPoint(
                    campaign_id=campaign.id,
                    claim_text=(
                        "PLACEHOLDER — replace with the real claim before activating"
                    ),
                    client_name="PLACEHOLDER — name the real client",
                    source_note=(
                        "Seeded inactive by scripts/seed_client_demo.py. A number may "
                        "only reach the copy through an ACTIVE row here, and this one "
                        "is deliberately not one. Do not activate until both fields "
                        "above are real."
                    ),
                    active=False,
                )
            )
            print("proof pt  : placeholder seeded INACTIVE (cannot enter copy)")

        # --- the suppression entry, before ingest -----------------------
        suppressed_email = next(e for _, _, e, _, s in LEADS if s == "suppressed")
        if not session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == suppressed_email)
        ).scalars().first():
            session.add(
                SuppressionEntry(
                    email=suppressed_email,
                    reason="unsubscribed via a previous campaign",
                )
            )
            session.flush()
            print(f"suppressed: {suppressed_email}")

        # --- research cache, keyed by domain ----------------------------
        for _name, company, _email, domain, shape in LEADS:
            if shape == "suppressed":
                continue  # never researched: the gate stops it before enrichment
            if session.get(ResearchCache, domain) is not None:
                continue
            session.add(
                ResearchCache(
                    company_domain=domain,
                    company_name=company,
                    facts=[f.model_dump(mode="json") for f in pages_for(company, domain, shape)],
                    fetched_at=NOW,
                )
            )
        session.flush()

        # --- the leads themselves ---------------------------------------
        rows = [
            {
                "Full Name": name,
                "Company": company,
                "Work Email": email,
                "Website": f"https://{domain}",
                "Job Title": "Partner",
                "Headcount": 9,
                "Location": "New York, NY",
                "Primary Industry": "Legal Services",
            }
            for name, company, email, domain, _shape in LEADS
        ]
        # Pin every row to this campaign. The default SystemRoutingPolicy
        # decides from lead attributes, and an unrouted lead falls back to
        # DEFAULT_QUALIFICATION -- whose vertical vocabulary is EMPTY, so it
        # qualifies nobody and the whole demo comes out `no_angle`. That is
        # the fail-closed behaviour working correctly, and it is exactly what
        # a one-campaign demo must not trip over.
        report = import_rows(
            session, rows, policy=PinnedRoutingPolicy(campaign.id)
        )
        session.commit()
        print(f"\nimport    : {report.summary()}\n")

        # --- drain the queue --------------------------------------------
        # A previous run may have admitted these leads before they were pinned,
        # and `no_angle` is terminal -- nothing re-queues a lead that was
        # already judged. Re-point them at the campaign and re-enqueue.
        from mazz.pipeline.queue import enqueue_job  # noqa: PLC0415

        emails = [email for _n, _c, email, _d, shape in LEADS if shape != "suppressed"]
        stale = session.execute(
            select(Lead).where(Lead.email.in_(emails))
        ).scalars().all()
        requeued = 0
        for lead in stale:
            if lead.campaign_id == campaign.id and lead.status is not LeadStatus.NO_ANGLE:
                continue
            lead.campaign_id = campaign.id
            lead.status = LeadStatus.RESEARCHING
            enqueue_job(session, lead.id, "qualify")
            requeued += 1
        session.commit()
        if requeued:
            print(f"re-queued : {requeued} lead(s) now pinned to the campaign")

        print("draining the queue (enrich -> qualify -> draft -> verify)...")
        run_worker(handlers=DEFAULT_HANDLERS, max_jobs=200, run_until_empty=True)

        # --- what landed -------------------------------------------------
        session.expire_all()  # the worker used its own sessions; do not print stale rows
        print("\nLeads:")
        for lead in session.execute(select(Lead).order_by(Lead.id)).scalars():
            draft = session.execute(
                select(Draft).where(Draft.lead_id == lead.id).order_by(Draft.id.desc())
            ).scalars().first()
            verdict = draft.verification_status.value if draft else "no draft"
            print(f"  {lead.email:34s} {lead.status.value:12s} {verdict}")

        print("\nNothing has been sent. EMAIL_SENDER is", os.environ.get("EMAIL_SENDER", "stub"))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
