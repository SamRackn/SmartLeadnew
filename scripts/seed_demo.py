"""End-to-end Phase 1 demonstration on fake data.

Resets the dev database, seeds the suppression list, ingests
``tests/fixtures/sample_leads.csv``, drains the job queue through every stage,
then prints what actually landed in each table.

The point is not that it runs without error — it is that the four behaviours the
brief cares about are visible in the output:

  * the suppressed lead was rejected at INGEST and never got a jobs row
  * two colleagues at one employer share ONE cache row and ONE provider call
  * the weak-signal lead is `no_angle` with NO draft
  * the unverifiable draft is `failed` / `held` and cannot be approved

Run: .venv/bin/python scripts/seed_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 12 Rue Centrale, 75002 Paris, France")
os.environ.setdefault("SESSION_SECRET", "seed-demo-secret")

import pipeline_helpers as h  # noqa: E402
from sqlalchemy import select  # noqa: E402

from mazz.config import reload_settings  # noqa: E402
from mazz.ingest.csv_import import import_csv_file  # noqa: E402
from mazz.models import Campaign, Draft, Job, Lead, LeadStatus, ResearchCache, Workspace  # noqa: E402
from mazz.pipeline.queue import DEFAULT_HANDLERS, run_worker  # noqa: E402
from mazz.pipeline.draft import render_body  # noqa: E402
from mazz.pipeline.score import facts_for_lead, score_lead  # noqa: E402
from mazz.pipeline.verify import (  # noqa: E402
    apply_verification,
    is_approvable,
    verify_content,
)
from mazz.schemas import Fact, FactKind  # noqa: E402

reload_settings()

OUTBOX = ROOT / "sent_outbox.jsonl"


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    if OUTBOX.exists():
        OUTBOX.unlink()

    rule("1. RESET + MIGRATE")
    h.reset_db()
    print("all tables truncated, schema at head")

    rule("2. SEED SUPPRESSION LIST")
    with h.db_session() as s:
        h.seed_sample_suppression(s)
    print(f"blocked domain : {h.SUPPRESSED_DOMAIN}")
    print(f"blocked email  : {h.SUPPRESSED_EMAIL}")

    rule("3. INGEST sample_leads.csv")
    with h.db_session() as s:
        report = import_csv_file(s, h.SAMPLE_CSV)
    print(f"accepted : {len(report.accepted)}")
    print(f"rejected : {len(report.rejected)}")
    for r in report.rejected:
        print(f"  REJECTED row {r.row_number} [{r.email or '?'}]: {r.reason}")

    rule("4. DRAIN THE QUEUE (enrich -> score -> draft -> verify)")
    stats = run_worker(
        handlers=DEFAULT_HANDLERS, session_factory=h.db_session, run_until_empty=True
    )
    print(f"claimed={stats.claimed} done={stats.done} failed={stats.failed} retried={stats.retried}")
    print(f"by stage: {stats.kinds}")

    rule("5. LEADS BY STATUS")
    with h.db_session() as s:
        for lead in s.execute(select(Lead).order_by(Lead.id)).scalars():
            print(f"  {lead.status.value:<12} {lead.email:<46} {lead.company_domain}")

    rule("6. RESEARCH CACHE (keyed by company domain)")
    with h.db_session() as s:
        for row in s.execute(select(ResearchCache).order_by(ResearchCache.company_domain)).scalars():
            n_leads = len(s.execute(
                select(Lead).where(Lead.company_domain == row.company_domain)
            ).scalars().all())
            print(f"  {row.company_domain:<32} facts={len(row.facts or []):<3} leads sharing it={n_leads}")

    rule("7. DRAFTS + THEIR CITATIONS")
    with h.db_session() as s:
        drafts = s.execute(select(Draft).order_by(Draft.id)).scalars().all()
        if not drafts:
            print("  (none)")
        for d in drafts:
            lead = s.get(Lead, d.lead_id)
            print(f"\n  lead        : {lead.email}")
            print(f"  status      : {d.verification_status.value}  (approvable: {is_approvable(d)})")
            print(f"  subject     : {d.subject}")
            print(f"  cites       : {d.signals_used}")
            if d.verification_notes:
                first = d.verification_notes.strip().splitlines()[0]
                print(f"  verdict     : {first}")

    rule("7b. WHAT HAPPENS TO A HALLUCINATED CLAIM")
    print("  Phase 1's composer only quotes retrieved facts verbatim, so it cannot")
    print("  invent anything and every draft above passed. Phase 2 swaps in a real")
    print("  Claude call, which can. Simulating that here against the same lead:\n")
    with h.db_session() as s:
        lead = s.execute(
            select(Lead).where(Lead.company_domain == "helio-medical.com")
        ).scalars().one()
        facts = facts_for_lead(s, lead)
        draft = s.execute(
            select(Draft).where(Draft.lead_id == lead.id).order_by(Draft.id.desc())
        ).scalars().first()

        tampered = render_body(
            "Saw you're hiring a Customer Support Specialist — and that you've "
            "scaled to 40 support reps since the Series B.",
            lead,
        )
        result = verify_content(
            lead=lead,
            subject=draft.subject,
            body=tampered,
            signals_used=list(draft.signals_used),
            available_facts=facts,
        )
        apply_verification(s, lead, draft, result)

        print(f"  claim inserted : \"...scaled to 40 support reps since the Series B.\"")
        print(f"  verdict        : {result.status.value}")
        print(f"  lead status    : {lead.status.value}")
        print(f"  approvable     : {is_approvable(draft)}")
        for claim in result.untraceable:
            print(f"  caught         : {claim}")

    rule("8. THE FOUR BEHAVIOURS THAT MATTER")
    with h.db_session() as s:
        lead_ids = [l.id for l in s.execute(select(Lead)).scalars()]
        blocked_jobs = s.execute(
            select(Job).join(Lead).where(Lead.email == h.SUPPRESSED_EMAIL)
        ).scalars().all()
        shared = s.execute(
            select(Lead).where(Lead.company_domain == h.SHARED_DOMAIN)
        ).scalars().all()
        shared_cache = s.execute(
            select(ResearchCache).where(ResearchCache.company_domain == h.SHARED_DOMAIN)
        ).scalars().all()
        weak = s.execute(
            select(Lead).where(Lead.company_domain == h.WEAK_DOMAIN)
        ).scalars().first()
        weak_drafts = (
            s.execute(select(Draft).where(Draft.lead_id == weak.id)).scalars().all()
            if weak else []
        )
        held = [
            d for d in s.execute(select(Draft)).scalars()
            if not is_approvable(d)
        ]

        suppressed_present = s.execute(
            select(Lead).where(Lead.email == h.SUPPRESSED_EMAIL)
        ).scalars().first()

        print(f"  suppressed lead in DB at all?          {bool(suppressed_present)}  (expect False)")
        print(f"  jobs rows for suppressed lead:         {len(blocked_jobs)}  (expect 0)")
        print(f"  leads at {h.SHARED_DOMAIN}: {len(shared)}, cache rows: {len(shared_cache)}  (expect 2 / 1)")
        print(f"  weak-signal lead status:               {weak.status.value if weak else 'n/a'}  (expect no_angle)")
        print(f"  drafts written for weak lead:          {len(weak_drafts)}  (expect 0)")
        print(f"  drafts not approvable (held/failed):   {len(held)}  (the tampered one, from 7b)")

    rule("9. SENT OUTBOX")
    if OUTBOX.exists():
        print(f"  {OUTBOX.name}: {len(OUTBOX.read_text().splitlines())} line(s)")
    else:
        print("  no outbox file — nothing was sent, because nothing was approved.")
        print("  Approval is a human action in the review app, one lead at a time.")
        print("  The pipeline chain stops at `verify`; only that click enqueues a")
        print("  `send` job. There is no auto-send.")

    rule("10. WORKSPACES + CAMPAIGNS (per-engagement scoring)")
    print("  Same idea as the review app's /workspaces screen, done here so a")
    print("  developer gets a working workspace + campaign without hand-written SQL.")
    print("  The demonstration: ONE cached req, scored under two ICPs.")
    print("  `score.py` ships a B2B-SaaS vocabulary where a Recruiter req is")
    print("  off-target; an agency reselling recruiting has the opposite ICP.\n")
    with h.db_session() as s:
        workspace = Workspace(
            slug="clientrise-staffing-demo",
            name="ClientRise — Staffing Agency Demo",
        )
        s.add(workspace)
        s.flush()

        campaign = Campaign(
            workspace_id=workspace.id,
            name="Staffing Agency Outbound",
            segment="staffing-agencies",
            # A PARTIAL override: only the vocabulary changes, every other
            # ScoringConfig weight stays at the shipped default.
            scoring_config={"supported_role_terms": ["recruiter", "talent partner"]},
        )
        s.add(campaign)
        s.flush()

        demo_domain = "example-staffing-demo.example"
        # No `published_at`: this req is scraped, not dated, which is the common
        # case for a careers page. `retrieved_at` is when WE looked and is never
        # read as a publication date, so the undated discount applies to BOTH
        # leads below (0.55, `ScoringConfig.undated_multiplier`) — it is not the
        # thing under demonstration, it is just the recency term both sides pay.
        req = Fact.build(
            kind=FactKind.JOB_POSTING.value,
            content="Hiring a Recruiter to place candidates with our agency clients.",
            source_url=f"https://{demo_domain}/careers/recruiter",
            retrieved_at=h.utcnow(),
        )
        s.add(
            ResearchCache(
                company_domain=demo_domain,
                company_name="Example Staffing Co",
                facts=[req.model_dump(mode="json")],
                fetched_at=h.utcnow(),
            )
        )

        unrouted = Lead(
            full_name="Casey Unrouted",
            email="casey.unrouted@example-staffing-demo.example",
            company_name="Example Staffing Co",
            company_domain=demo_domain,
            industry_primary="Staffing & Recruiting",
            industry_specific="Staffing Agencies",
            status=LeadStatus.RESEARCHING,
        )
        routed = Lead(
            full_name="Jordan Routed",
            email="jordan.routed@example-staffing-demo.example",
            company_name="Example Staffing Co",
            company_domain=demo_domain,
            industry_primary="Staffing & Recruiting",
            industry_specific="Staffing Agencies",
            status=LeadStatus.RESEARCHING,
            campaign_id=campaign.id,
        )
        s.add_all([unrouted, routed])
        s.flush()

        unrouted_result = score_lead(s, unrouted)
        routed_result = score_lead(s, routed)

        print(f"  workspace : {workspace.slug!r} ({workspace.name})")
        print(f"  campaign  : {campaign.name!r}  segment={campaign.segment!r}")
        print(f"  scoring_config override: {campaign.scoring_config}\n")
        print(f"  same cached fact, same company ({demo_domain}):")
        print(
            f"    unrouted lead (shipped SaaS vocabulary) : "
            f"has_angle={unrouted_result.has_angle}  "
            f"top_score={unrouted_result.top.score if unrouted_result.top else '—'}  "
            f"(expect no_angle, Recruiter is off_target upstream)"
        )
        print(
            f"    routed lead   (agency vocabulary)       : "
            f"has_angle={routed_result.has_angle}  "
            f"top_score={routed_result.top.score if routed_result.top else '—'}  "
            f"(expect a STRONG angle: 75.0 supported-req weight x 0.55 undated = 41.25)"
        )
        print(
            "\n  The vocabulary is the whole difference. Same fact, same 40.0 bar,"
            "\n  same 0.55 undated discount on both sides: 20.0 x 0.55 = 11.0 "
            "off-target\n  for the SaaS ICP, 75.0 x 0.55 = 41.25 STRONG for the "
            "agency's."
        )

    print("\nDone. Review app: .venv/bin/uvicorn mazz.web.app:app --reload\n")
    print("Manage workspaces/campaigns without SQL at: /workspaces\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
