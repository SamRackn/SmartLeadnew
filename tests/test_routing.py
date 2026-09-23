"""Campaign assignment: `RoutingPolicy`, and the lever it connects.

`docs/DECISIONS.md` settled that the scoring config is per campaign, because
ClientRise runs outbound on behalf of each agency-client at that client's own
target industry. `campaigns.scoring_config` stores the config and
`score.config_for_lead` reads it — but nothing wrote `leads.campaign_id`, so in
production every lead was NULL and scored against the shipped B2B-SaaS defaults.
These tests cover the write side.

The one that matters most is at the bottom: `test_the_campaign_a_lead_is_routed
_to_decides_whether_it_is_worth_an_email`. It ingests four real leads through
the real ingest path and shows the SAME req landing `no_angle` under the shipped
vocabulary and clearing the threshold under the campaign's — and the reverse.
That is the difference between the decision being wired and being live.

Two constraints are re-asserted here rather than assumed, because routing sits
next to them:

  * guardrail 1 — the suppression gate still runs FIRST, and a suppressed lead
    is never even shown to the router;
  * `campaign_id IS NULL` stays a legitimate steady state, so a routing failure
    of any kind costs the client nothing beyond the shipped defaults.

Real Postgres like the rest of the pipeline tests: the argument is about rows in
`campaigns`, a SAVEPOINT surviving a failed read, and an FK, none of which
SQLite would model.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import pipeline_helpers as h
from mazz.ingest.csv_import import import_lead, import_rows
from mazz.models import (
    Campaign,
    Job,
    Lead,
    LeadStatus,
    Mailbox,
    ResearchCache,
    Workspace,
)
from mazz.pipeline.queue import JobContext, enqueue_job
from mazz.pipeline.routing import (
    DEFAULT_ROUTING_POLICY,
    PinnedRoutingPolicy,
    RoutingDecision,
    RoutingPolicy,
    SystemRoutingPolicy,
    assign_campaign,
    candidate_segments,
    segment_slug,
)
from mazz.pipeline.score import DEFAULT_SCORING, config_for_lead, handle_score
from mazz.schemas import Fact, FactKind


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


# --------------------------------------------------------------------------
# Fixtures: an agency-client engagement
# --------------------------------------------------------------------------

#: The engagement: an RPO seller whose ICP is 3PL companies hiring in-house
#: recruiters. The shipped vocabulary (revops / sdr / crm) is written for a B2B
#: SaaS seller and calls a recruiter req OFF-TARGET, which is the whole problem.
RPO_CONFIG = {
    "threshold": 45.0,
    "supported_role_terms": ["recruiter", "talent acquisition", "head of people"],
    "off_target_role_terms": ["revenue operations", "sales engineer"],
}


def make_campaign(
    session,
    *,
    slug: str = "acme-rpo",
    name: str = "Q4 3PL hiring",
    segment: str | None = "Third-Party Logistics",
    scoring_config: dict | None = None,
) -> Campaign:
    workspace = session.execute(
        text("SELECT id FROM workspaces WHERE slug = :s"), {"s": slug}
    ).scalar_one_or_none()
    if workspace is None:
        ws = Workspace(slug=slug, name=slug.replace("-", " ").title())
        session.add(ws)
        session.flush()
        workspace = ws.id
    campaign = Campaign(
        workspace_id=workspace,
        name=name,
        segment=segment,
        scoring_config=dict(RPO_CONFIG if scoring_config is None else scoring_config),
    )
    session.add(campaign)
    session.flush()
    return campaign


def lead_row(**overrides) -> dict:
    """A complete inbound row, in the client's own header spellings."""

    row = h.csv_row()
    row.update(overrides)
    return row


def seed_facts(session, domain: str, *contents: str) -> None:
    """One research_cache row per company, with fresh job-posting facts."""

    today = h.utcnow().date().isoformat()
    facts = [
        Fact.build(
            kind=FactKind.JOB_POSTING.value,
            content=content,
            source_url=f"https://{domain}/jobs/{n}",
            retrieved_at=h.utcnow(),
            metadata={"published_at": today},
        )
        for n, content in enumerate(contents)
    ]
    session.add(
        ResearchCache(
            company_domain=domain,
            company_name=domain,
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=h.utcnow(),
        )
    )
    session.flush()


class RecordingPolicy(RoutingPolicy):
    """Wraps the real policy and records every lead it is asked about."""

    def __init__(self, inner: RoutingPolicy | None = None) -> None:
        self.inner = inner or SystemRoutingPolicy()
        self.calls: list[str] = []

    def route(self, lead, *, session) -> RoutingDecision:
        self.calls.append(lead.email)
        return self.inner.route(lead, session=session)


class BrokenPolicy(RoutingPolicy):
    """A policy with a bug in it."""

    def route(self, lead, *, session) -> RoutingDecision:
        raise RuntimeError("policy blew up")


class BadQueryPolicy(RoutingPolicy):
    """A policy whose database read fails -- the harder failure to survive."""

    def route(self, lead, *, session) -> RoutingDecision:
        session.execute(text("SELECT * FROM a_table_that_does_not_exist"))
        raise AssertionError("unreachable")  # pragma: no cover


# ==========================================================================
# Segments come from lead attributes
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Third-Party Logistics", "third-party-logistics"),
        ("  Logistics & Supply Chain ", "logistics-supply-chain"),
        ("Ecommerce / DTC", "ecommerce-dtc"),
        ("ecommerce-dtc", "ecommerce-dtc"),
        ("", None),
        (None, None),
        ("   ", None),
        ("///", None),
    ],
)
def test_segment_slug_normalises_both_sides_of_the_match(raw, expected):
    """`campaigns.segment` is free text on purpose. Free text with no agreed
    normalisation is free text that silently never matches."""

    assert segment_slug(raw) == expected


def test_candidate_segments_are_most_specific_first():
    lead = Lead(
        full_name="Dana",
        email="dana@x.example",
        industry_primary="Logistics & Supply Chain",
        industry_specific="Third-Party Logistics",
    )
    assert candidate_segments(lead) == (
        "third-party-logistics",
        "logistics-supply-chain",
    )


def test_candidate_segments_dedupe_and_survive_missing_fields():
    assert candidate_segments(
        Lead(full_name="D", email="d@x.example", industry_primary="Logistics")
    ) == ("logistics",)
    assert candidate_segments(
        Lead(
            full_name="D",
            email="d@x.example",
            industry_primary="Logistics",
            industry_specific="  logistics ",
        )
    ) == ("logistics",)
    assert candidate_segments(Lead(full_name="D", email="d@x.example")) == ()


# ==========================================================================
# SystemRoutingPolicy -- the default
# ==========================================================================


def test_system_policy_routes_on_the_specific_industry(session):
    campaign = make_campaign(session)
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.campaign_id == campaign.id
    assert decision.segment == "third-party-logistics"
    assert "third-party-logistics" in decision.reason


def test_system_policy_falls_back_to_the_primary_industry(session):
    """A campaign sold at the broad grain still catches a lead whose specific
    industry nobody wrote a campaign for."""

    campaign = make_campaign(session, segment="Logistics & Supply Chain")
    lead = h.make_lead(
        session,
        industry_primary="Logistics & Supply Chain",
        industry_specific="Cold Chain Warehousing",
    )

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.campaign_id == campaign.id
    assert decision.segment == "logistics-supply-chain"


def test_the_specific_grain_wins_over_the_broad_one(session):
    """An agency running a narrow engagement must not be out-competed by
    another client's catch-all."""

    broad = make_campaign(session, slug="broad-co", segment="Logistics & Supply Chain")
    narrow = make_campaign(
        session, slug="narrow-co", segment="Third-Party Logistics"
    )
    lead = h.make_lead(
        session,
        industry_primary="Logistics & Supply Chain",
        industry_specific="Third-Party Logistics",
    )

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.campaign_id == narrow.id
    assert decision.campaign_id != broad.id


def test_no_campaigns_at_all_means_unrouted_not_an_error(session):
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.routed is False
    assert decision.campaign_id is None
    assert "defaults" in decision.reason


def test_a_lead_with_no_industry_is_unrouted(session):
    make_campaign(session)
    lead = h.make_lead(session, industry_primary=None, industry_specific=None)

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.routed is False
    assert "no industry" in decision.reason


def test_a_campaign_with_no_segment_is_never_matched(session):
    """A campaign that has not said who it serves does not get handed
    everybody."""

    make_campaign(session, segment=None)
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    assert SystemRoutingPolicy().route(lead, session=session).routed is False


def test_two_campaigns_claiming_one_segment_refuse_to_guess(session):
    """The expensive mistake this module can make is not "unrouted" — it is
    emailing one agency's prospect on behalf of its competitor."""

    a = make_campaign(session, slug="agency-a", name="A")
    b = make_campaign(session, slug="agency-b", name="B")
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy().route(lead, session=session)

    assert decision.routed is False
    assert str(a.id) in decision.reason and str(b.id) in decision.reason
    assert "refusing to guess" in decision.reason


def test_scoping_to_a_workspace_resolves_the_collision(session):
    a = make_campaign(session, slug="agency-a", name="A")
    make_campaign(session, slug="agency-b", name="B")
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy(workspace_slug="agency-a").route(
        lead, session=session
    )

    assert decision.campaign_id == a.id


def test_a_workspace_scoped_policy_ignores_other_clients_campaigns(session):
    make_campaign(session, slug="agency-a", name="A")
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy(workspace_slug="agency-b").route(
        lead, session=session
    )

    assert decision.routed is False


# ==========================================================================
# The mailbox slot stays empty at INGEST, now that mailboxes exist
# ==========================================================================


def test_routing_does_not_pick_a_mailbox_at_ingest(session):
    """CLAUDE.md's triple is (campaign, segment, mailbox). The mailboxes table
    now exists, and routing still leaves the third slot empty — on purpose.

    A mailbox choice is a fact about a moment: which accounts are active, what
    they have already sent today, whose turn it is. Routing runs at ingest and
    the lead may not clear human review for days, by which time that choice is
    stale — the mailbox could be capped, warming, or disabled. So the selection
    lives in `pipeline.mailbox.select_mailbox` and runs at send time, and the
    relation stays on the MAILBOX row (``mailboxes.campaign_id``), never on the
    lead or the campaign."""

    campaign = make_campaign(session)
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = SystemRoutingPolicy().route(lead, session=session)
    assert decision.campaign_id == campaign.id
    assert decision.mailbox_id is None

    assert "mailbox_id" not in Lead.__table__.columns
    assert "mailbox_id" not in Campaign.__table__.columns
    # The pin lives on the mailbox, which is what keeps
    # one-campaign-per-mailbox reachable without a migration.
    assert "campaign_id" in Mailbox.__table__.columns


# ==========================================================================
# PinnedRoutingPolicy -- "this spreadsheet is Acme's"
# ==========================================================================


def test_pinned_policy_ignores_the_lead_attributes(session):
    campaign = make_campaign(session, segment="Ecommerce")
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = PinnedRoutingPolicy(campaign.id).route(lead, session=session)

    assert decision.campaign_id == campaign.id
    assert decision.segment == "ecommerce"


def test_pinning_to_a_campaign_that_does_not_exist_does_not_raise(session):
    lead = h.make_lead(session)

    decision = PinnedRoutingPolicy(4242).route(lead, session=session)

    assert decision.routed is False
    assert "does not exist" in decision.reason


# ==========================================================================
# assign_campaign -- the only writer of leads.campaign_id
# ==========================================================================


def test_assign_campaign_writes_the_campaign_id(session):
    campaign = make_campaign(session)
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    assign_campaign(session, lead)
    session.flush()

    assert lead.campaign_id == campaign.id


def test_assign_campaign_never_moves_a_lead_between_campaigns(session):
    """Re-importing a spreadsheet refreshes details. It must not hand a person
    from one client's engagement to another's."""

    first = make_campaign(session, slug="agency-a", name="A", segment="Ecommerce")
    make_campaign(session, slug="agency-b", name="B", segment="Third-Party Logistics")
    lead = h.make_lead(
        session, industry_specific="Third-Party Logistics", campaign_id=first.id
    )

    decision = assign_campaign(session, lead)
    session.flush()

    assert lead.campaign_id == first.id
    assert decision.campaign_id == first.id
    assert "already assigned" in decision.reason


def test_a_broken_policy_leaves_the_lead_unrouted_rather_than_raising(session):
    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = assign_campaign(session, lead, BrokenPolicy())
    session.flush()

    assert decision.routed is False
    assert lead.campaign_id is None
    assert "RuntimeError" in decision.reason


def test_a_failed_database_read_inside_routing_does_not_poison_the_session(session):
    """The SAVEPOINT claim in `assign_campaign`. A failed read inside routing
    would otherwise abort the transaction that is trying to admit the lead."""

    lead = h.make_lead(session, industry_specific="Third-Party Logistics")

    decision = assign_campaign(session, lead, BadQueryPolicy())

    assert decision.routed is False
    assert lead.campaign_id is None
    # The session is still usable: the lead lands, and so does new work.
    enqueue_job(session, lead.id, "enrich")
    session.flush()
    assert h.count(session, Lead) == 1
    assert h.count(session, Job) == 1


def test_routing_never_clears_a_campaign(session):
    """`campaign_id` going NULL is ON DELETE SET NULL's job (retiring a campaign
    keeps its prospects) and `delete_person`'s. Never routing's."""

    campaign = make_campaign(session, segment="Ecommerce")
    lead = h.make_lead(
        session, industry_specific="Nothing Matches This", campaign_id=campaign.id
    )

    assign_campaign(session, lead, BrokenPolicy())
    assign_campaign(session, lead, SystemRoutingPolicy())
    session.flush()

    assert lead.campaign_id == campaign.id


# ==========================================================================
# Ingest wiring -- and guardrail 1 keeping its place in the order
# ==========================================================================


def test_ingest_assigns_the_campaign(session):
    campaign = make_campaign(session)

    outcome = import_lead(
        session, lead_row(**{"Specific Industry": "Third-Party Logistics"})
    )
    session.flush()

    lead = h.lead_by_email(session, outcome.email)
    assert lead.campaign_id == campaign.id
    assert outcome.campaign_id == campaign.id
    assert outcome.routed is True


def test_an_unroutable_lead_is_still_admitted_and_still_queued(session):
    """Routing is not a compliance gate. NULL means the shipped defaults, which
    is what every lead in the database already does."""

    make_campaign(session, segment="Ecommerce")

    report = import_rows(session, [lead_row()])
    session.flush()

    outcome = report.accepted[0]
    lead = h.lead_by_email(session, outcome.email)
    assert lead.campaign_id is None
    assert lead.status is LeadStatus.RESEARCHING
    assert outcome.job_id is not None
    assert report.unrouted == [outcome]
    assert config_for_lead(session, lead) == DEFAULT_SCORING


def test_a_broken_router_never_costs_the_client_a_lead(session):
    make_campaign(session)

    report = import_rows(
        session,
        [lead_row(**{"Specific Industry": "Third-Party Logistics"})],
        policy=BrokenPolicy(),
    )
    session.flush()

    assert len(report.accepted) == 1
    assert report.rejected == []
    lead = h.lead_by_email(session, report.accepted[0].email)
    assert lead.campaign_id is None
    assert h.count(session, Job) == 1


def test_the_suppression_gate_still_runs_before_the_router(session):
    """Guardrail 1. The router is never even shown a suppressed lead — it runs
    after the gate, and the gate refuses before the first write."""

    make_campaign(session)
    h.suppress(session, domain=h.SUPPRESSED_DOMAIN, reason="legal")
    spy = RecordingPolicy()

    report = import_rows(
        session,
        [
            lead_row(
                Email="rita@blocked-corp.example",
                Website="https://www.blocked-corp.example/",
                **{"Specific Industry": "Third-Party Logistics"},
            ),
            lead_row(**{"Specific Industry": "Third-Party Logistics"}),
        ],
        policy=spy,
    )
    session.flush()

    assert len(report.suppressed) == 1
    assert spy.calls == ["dana.whitfield@northwind-logistics.com"]
    assert h.count(session, Lead) == 1
    assert h.count(session, Job) == 1


def test_a_suppressed_lead_produces_no_campaign_assignment_at_all(session):
    make_campaign(session)
    h.suppress(session, email="rita@blocked-corp.example", reason="unsubscribed")

    outcome = import_lead(
        session,
        lead_row(
            Email="rita@blocked-corp.example",
            **{"Specific Industry": "Third-Party Logistics"},
        ),
    )

    assert outcome.kind == "suppressed"
    assert h.count(session, Lead) == 0
    assert h.count(session, Job) == 0


def test_re_ingesting_does_not_move_a_lead_to_another_campaign(session):
    first = make_campaign(session, slug="agency-a", name="A", segment="3PL")
    second = make_campaign(session, slug="agency-b", name="B", segment="Ecommerce")

    import_rows(session, [lead_row(**{"Specific Industry": "3PL"})])
    session.flush()
    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    assert lead.campaign_id == first.id

    import_rows(session, [lead_row(**{"Specific Industry": "Ecommerce"})])
    session.flush()
    session.refresh(lead)

    assert lead.campaign_id == first.id
    assert lead.campaign_id != second.id


def test_an_approved_lead_keeps_the_campaign_its_copy_was_written_under(session):
    make_campaign(session, segment="3PL")
    lead = h.make_lead(session, status=LeadStatus.APPROVED)
    session.flush()

    outcome = import_lead(session, lead_row(**{"Specific Industry": "3PL"}))
    session.flush()
    session.refresh(lead)

    assert lead.status is LeadStatus.APPROVED
    assert lead.campaign_id is None
    assert outcome.job_id is None


def test_a_pinned_import_drops_a_whole_sheet_into_one_engagement(session):
    campaign = make_campaign(session, segment="Ecommerce")

    report = import_rows(
        session,
        [
            lead_row(),
            lead_row(
                Email="marcus.oyelaran@northwind-logistics.com",
                **{"Full Name": "Marcus Oyelaran"},
            ),
        ],
        policy=PinnedRoutingPolicy(campaign.id),
    )
    session.flush()

    assert len(report.routed) == 2
    assert {o.campaign_id for o in report.accepted} == {campaign.id}


def test_the_default_policy_is_the_system_one(session):
    assert isinstance(DEFAULT_ROUTING_POLICY, SystemRoutingPolicy)
    assert DEFAULT_ROUTING_POLICY.workspace_slug is None


# ==========================================================================
# THE POINT: the campaign a lead is routed to decides whether it gets an email
# ==========================================================================

RECRUITER_REQ = "Hiring a Recruiter to build out our in-house hiring team."
REVOPS_REQ = "Hiring a Revenue Operations Manager to own our CRM."

ALPHA = "midway-3pl.example"
BETA = h.SHARED_DOMAIN  # northwind-logistics.com


def _ingest(session, *, email: str, website: str, industry: str | None) -> Lead:
    row = lead_row(Email=email, Website=website)
    row["Specific Industry"] = industry or ""
    row["Primary Industry"] = industry or ""
    outcome = import_lead(session, row)
    assert not isinstance(outcome, type(None))
    assert getattr(outcome, "lead_id", None), f"ingest rejected the row: {outcome}"
    session.flush()
    return session.get(Lead, outcome.lead_id)


def test_the_campaign_a_lead_is_routed_to_decides_whether_it_is_worth_an_email(
    session,
):
    """End to end, through the real ingest path, with nothing set by hand.

    Four leads, two employers, one req each. The routed leads carry the RPO
    client's vocabulary; the unrouted ones carry the shipped SaaS defaults. The
    SAME req flips between `no_angle` and an angle depending only on which
    campaign the router assigned — in both directions, so this is not just a
    looser threshold.
    """

    campaign = make_campaign(session)  # segment "Third-Party Logistics", RPO config
    seed_facts(session, ALPHA, RECRUITER_REQ)
    seed_facts(session, BETA, REVOPS_REQ)

    # Employer ALPHA is hiring a recruiter: the RPO client's exact signal, and
    # off-target under the shipped SaaS vocabulary.
    alpha_routed = _ingest(
        session,
        email="priya@midway-3pl.example",
        website="https://www.midway-3pl.example/",
        industry="Third-Party Logistics",
    )
    alpha_unrouted = _ingest(
        session,
        email="sam@midway-3pl.example",
        website="https://www.midway-3pl.example/",
        industry=None,
    )
    # Employer BETA is hiring a RevOps manager: the shipped vocabulary's
    # strongest signal, and somebody else's opportunity for the RPO client.
    beta_routed = _ingest(
        session,
        email="dana.whitfield@northwind-logistics.com",
        website="https://www.northwind-logistics.com/",
        industry="Third-Party Logistics",
    )
    beta_unrouted = _ingest(
        session,
        email="marcus.oyelaran@northwind-logistics.com",
        website="https://www.northwind-logistics.com/",
        industry=None,
    )

    # The router did the assigning, not the test.
    assert alpha_routed.campaign_id == campaign.id
    assert beta_routed.campaign_id == campaign.id
    assert alpha_unrouted.campaign_id is None
    assert beta_unrouted.campaign_id is None

    for lead in (alpha_routed, alpha_unrouted, beta_routed, beta_unrouted):
        job = enqueue_job(session, lead.id, "score")[0]
        handle_score(JobContext(session=session, job=job, lead=lead, handlers={}))
    session.flush()

    # The recruiter req: worth an email to the RPO client, worth nothing under
    # the shipped SaaS vocabulary that calls it off-target.
    assert alpha_routed.status is not LeadStatus.NO_ANGLE
    assert alpha_unrouted.status is LeadStatus.NO_ANGLE

    # And the reverse, which is what makes this a vocabulary and not a
    # loosened threshold: the RevOps req is the shipped vocabulary's strongest
    # signal and is off-target for this client.
    assert beta_routed.status is LeadStatus.NO_ANGLE
    assert beta_unrouted.status is not LeadStatus.NO_ANGLE

    # ...and the config each lead scored under is its campaign's.
    assert config_for_lead(session, alpha_routed).threshold == 45.0
    assert config_for_lead(session, alpha_unrouted) == DEFAULT_SCORING


def test_routing_did_not_break_the_shared_research_cache(session):
    """Two colleagues at one employer in DIFFERENT routing states still share
    one `research_cache` row. Campaigns scope config, never research."""

    make_campaign(session)
    seed_facts(session, ALPHA, RECRUITER_REQ)

    _ingest(
        session,
        email="priya@midway-3pl.example",
        website="https://www.midway-3pl.example/",
        industry="Third-Party Logistics",
    )
    _ingest(
        session,
        email="sam@midway-3pl.example",
        website="https://www.midway-3pl.example/",
        industry=None,
    )

    assert h.count(session, ResearchCache) == 1
    assert "workspace_id" not in ResearchCache.__table__.columns
