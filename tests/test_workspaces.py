"""workspaces + campaigns, and the per-campaign scoring configuration.

Why this table pair exists: ClientRise runs outbound on behalf of each
agency-client, so the prospect ICP changes per engagement. `pipeline.score`
ships ONE role vocabulary, written for a B2B SaaS seller, and that vocabulary
decides which leads clear the threshold and which land `no_angle`. The config
therefore has to be storable per campaign.

Three things are tested here, in order of how badly they would hurt if wrong:

  1. Delete-by-person (guardrail 5) still completes for a lead that belongs to
     a campaign, and the campaign does not survive as a back-reference to the
     person or block the deletion.
  2. Retiring a campaign or a whole workspace never deletes prospects.
  3. What is stored in `campaigns.scoring_config` actually rebuilds into a
     `ScoringConfig` -- including `__post_init__`, which keeps guardrail 2
     ("funding alone can never clear") whoever wrote the row.

Runs against the real `mazz_dev` Postgres like the other pipeline tests: the
whole argument here is FK cascade behaviour and a JSONB CHECK, neither of which
SQLite would model.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

import pipeline_helpers as h
from mazz.models import Campaign, Draft, Job, Lead, ResearchCache, Workspace
from mazz.pipeline.queue import enqueue_job
from mazz.pipeline.score import (
    DEFAULT_SCORING,
    RoleFit,
    ScoringConfig,
    classify_role_fit,
)
from mazz.privacy import delete_person, find_person_references

DANA = "dana.whitfield@northwind-logistics.com"

#: What a real ClientRise engagement looks like: the seller is an agency, not a
#: SaaS, so the shipped "revops / sdr / crm" vocabulary is the wrong ICP.
AGENCY_CONFIG = {
    "threshold": 45.0,
    "supported_role_terms": [
        "paid media",
        "performance marketing",
        "head of growth",
        "ecommerce manager",
    ],
    "off_target_role_terms": ["welder", "line cook"],
}


def _reset() -> None:
    """`pipeline_helpers.reset_db` does not know about the new tables yet.

    TRUNCATE ... CASCADE on `leads` does not reach `campaigns`: leads is the
    CHILD of that relationship, and CASCADE only follows references INTO the
    truncated tables. So this module clears its own rows rather than leaving
    them for whatever test runs next.
    """

    h.reset_db()
    with h.db_session() as s:
        s.execute(text("TRUNCATE campaigns, workspaces RESTART IDENTITY CASCADE"))


@pytest.fixture
def session():
    _reset()
    with h.db_session() as s:
        yield s
    _reset()


def _campaign(session, *, slug="acme-digital", name="Q4 ecommerce", **kwargs):
    workspace = Workspace(slug=slug, name=slug.replace("-", " ").title())
    session.add(workspace)
    session.flush()
    campaign = Campaign(
        workspace_id=workspace.id,
        name=name,
        segment=kwargs.pop("segment", "ecommerce-smb"),
        scoring_config=kwargs.pop("scoring_config", dict(AGENCY_CONFIG)),
        **kwargs,
    )
    session.add(campaign)
    session.flush()
    return workspace, campaign


# ==========================================================================
# Guardrail 5: delete-by-person stays complete
# ==========================================================================


def test_delete_person_completes_for_a_lead_that_belongs_to_a_campaign(session):
    _, campaign = _campaign(session)
    lead = h.make_lead(session, campaign_id=campaign.id)
    h.make_draft(session, lead)
    enqueue_job(session, lead.id, "score")
    session.flush()

    report = delete_person(DANA, session=session)

    assert report.complete is True
    assert find_person_references(session, DANA).total == 0
    assert h.count(session, Lead) == 0
    assert h.count(session, Draft) == 0
    assert h.count(session, Job) == 0


def test_campaign_survives_the_deletion_and_holds_no_person_data(session):
    """The campaign is config, not a person. It stays; the pointer went with
    the lead, because the pointer lived on the lead row."""

    _, campaign = _campaign(session)
    h.make_lead(session, campaign_id=campaign.id)
    session.flush()

    delete_person(DANA, session=session)

    assert h.count(session, Campaign) == 1
    assert h.count(session, Workspace) == 1
    surviving = session.get(Campaign, campaign.id)
    assert surviving.leads == []


# ==========================================================================
# Cascade semantics: config changes never delete people
# ==========================================================================


def test_deleting_a_campaign_nulls_the_lead_but_keeps_it(session):
    _, campaign = _campaign(session)
    lead = h.make_lead(session, campaign_id=campaign.id)
    h.make_draft(session, lead)
    enqueue_job(session, lead.id, "score")
    session.flush()

    session.delete(campaign)
    session.flush()
    session.refresh(lead)

    assert h.count(session, Campaign) == 0
    assert h.count(session, Lead) == 1
    assert lead.campaign_id is None
    # ON DELETE CASCADE from leads must NOT have been reached.
    assert h.count(session, Draft) == 1
    assert h.count(session, Job) == 1


def test_deleting_a_workspace_takes_its_campaigns_but_not_its_leads(session):
    workspace, campaign = _campaign(session)
    lead = h.make_lead(session, campaign_id=campaign.id)
    session.flush()

    session.delete(workspace)
    session.flush()
    session.refresh(lead)

    assert h.count(session, Workspace) == 0
    assert h.count(session, Campaign) == 0
    assert h.count(session, Lead) == 1
    assert lead.campaign_id is None


def test_campaign_id_is_nullable_so_existing_leads_keep_working(session):
    """Every lead in the database predates campaigns. NULL is a valid steady
    state, not a migration to finish."""

    lead = h.make_lead(session, campaign_id=None)
    assert lead.campaign_id is None
    assert lead.campaign is None


# ==========================================================================
# The stored config
# ==========================================================================


def _as_scoring_config(stored: dict) -> ScoringConfig:
    """The coercion `pipeline.score` must do. JSON has no frozenset.

    Mirrored here (rather than imported) on purpose: this test is the contract
    the score-side loader has to satisfy, and it is written before that loader
    exists.
    """

    kwargs = dict(stored)
    for key in ("supported_role_terms", "off_target_role_terms"):
        if key in kwargs:
            kwargs[key] = frozenset(kwargs[key])
    return ScoringConfig(**kwargs)


def test_stored_config_round_trips_into_a_scoring_config(session):
    _, campaign = _campaign(session)
    session.commit()
    session.expire_all()

    stored = session.get(Campaign, campaign.id).scoring_config
    config = _as_scoring_config(stored)

    assert config.threshold == 45.0
    assert "paid media" in config.supported_role_terms
    # Unspecified knobs fall back to the shipped defaults -- a partial override.
    assert config.job_posting_supported == DEFAULT_SCORING.job_posting_supported
    assert config.funding == DEFAULT_SCORING.funding


def test_the_campaigns_vocabulary_is_what_decides_role_fit(session):
    """The whole point. The same req reads off-target under the shipped SaaS
    vocabulary and supported under the agency's."""

    _, campaign = _campaign(session)
    req = "Hiring a Paid Media Manager to own our ecommerce accounts."

    fit_default, _ = classify_role_fit(req, DEFAULT_SCORING)
    fit_campaign, matched = classify_role_fit(
        req, _as_scoring_config(campaign.scoring_config)
    )

    assert fit_default is RoleFit.UNCLASSIFIED
    assert fit_campaign is RoleFit.SUPPORTED
    assert matched == "paid media"


def test_empty_config_means_the_shipped_defaults(session):
    _, campaign = _campaign(session, scoring_config={})
    assert campaign.scoring_config == {}
    assert _as_scoring_config(campaign.scoring_config) == DEFAULT_SCORING


def test_guardrail_2_still_has_the_last_word_over_a_stored_config(session):
    """A campaign config that would let funding alone clear the threshold is
    refused when it is rebuilt, not quietly honoured."""

    _, campaign = _campaign(
        session, scoring_config={"threshold": 10.0, "funding": 60.0}
    )
    with pytest.raises(ValueError, match="funding must score below the threshold"):
        _as_scoring_config(campaign.scoring_config)


def test_scoring_config_must_be_a_json_object(session):
    """A list or a scalar in that column would silently fall back to the SaaS
    defaults for a client who is not a SaaS. The database refuses it."""

    workspace = Workspace(slug="bad-config", name="Bad Config")
    session.add(workspace)
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO campaigns (workspace_id, name, scoring_config) "
                "VALUES (:ws, 'broken', '[1, 2]'::jsonb)"
            ),
            {"ws": workspace.id},
        )
    session.rollback()


def test_campaign_names_are_unique_within_a_workspace_not_across(session):
    ws_a, _ = _campaign(session, slug="agency-a", name="Q4 push")
    ws_b = Workspace(slug="agency-b", name="Agency B")
    session.add(ws_b)
    session.flush()

    # Same campaign name under a different workspace: fine.
    session.add(Campaign(workspace_id=ws_b.id, name="Q4 push"))
    session.flush()
    assert h.count(session, Campaign) == 2

    with pytest.raises(IntegrityError):
        session.add(Campaign(workspace_id=ws_a.id, name="Q4 push"))
        session.flush()
    session.rollback()


def test_workspace_slugs_are_unique(session):
    session.add(Workspace(slug="acme-digital", name="Acme Digital"))
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(Workspace(slug="acme-digital", name="Acme Digital (dup)"))
        session.flush()
    session.rollback()


# ==========================================================================
# The cost lever: research is NOT scoped to a tenant
# ==========================================================================


def test_research_cache_is_shared_across_workspaces(session):
    """Two agency-clients targeting the same employer must still cost ONE
    research operation. If `research_cache` ever grows a workspace column this
    test is the thing that should stop it."""

    _, campaign_a = _campaign(session, slug="agency-a", name="A")
    _, campaign_b = _campaign(session, slug="agency-b", name="B")

    h.make_lead(session, campaign_id=campaign_a.id)
    h.make_lead(
        session,
        campaign_id=campaign_b.id,
        full_name="Marcus Oyelaran",
        email="marcus.oyelaran@northwind-logistics.com",
        linkedin_url=h.SHARED_PROFILES[1],
    )
    session.add(
        ResearchCache(company_domain=h.SHARED_DOMAIN, company_name="Northwind")
    )
    session.flush()

    assert "workspace_id" not in ResearchCache.__table__.columns
    assert "campaign_id" not in ResearchCache.__table__.columns

    domains = list(
        session.execute(select(ResearchCache.company_domain)).scalars().all()
    )
    assert domains == [h.SHARED_DOMAIN]
