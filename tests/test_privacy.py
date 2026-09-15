"""Delete-by-person.

The bar is absolute: after ``delete_person`` runs, zero rows anywhere may
reference that person. "Anywhere" includes the JSONB fact list inside
``research_cache``, which does not cascade and which a naive implementation
would miss entirely.

The other half of the bar is that erasing one person must not erase his
colleagues' research. The company row survives; the person comes out of it.
"""

from __future__ import annotations

import pytest

import pipeline_helpers as h
from mazz.models import Draft, Job, Lead, ResearchCache
from mazz.pipeline.enrich import enrich_lead
from mazz.pipeline.queue import enqueue_job
from mazz.privacy import DeletionIncomplete, delete_person, find_person_references

DANA = "dana.whitfield@northwind-logistics.com"
MARCUS = "marcus.oyelaran@northwind-logistics.com"


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


def _two_colleagues(session):
    """Two leads at one employer, both enriched, both with drafts and jobs."""

    dana = h.make_lead(session)
    marcus = h.make_lead(
        session,
        full_name="Marcus Oyelaran",
        email=MARCUS,
        linkedin_url=h.SHARED_PROFILES[1],
    )
    company, linkedin = h.company_adapter(), h.linkedin_adapter()
    enrich_lead(session, dana, company_adapter=company, linkedin_adapter=linkedin)
    enrich_lead(session, marcus, company_adapter=company, linkedin_adapter=linkedin)

    for lead in (dana, marcus):
        h.make_draft(session, lead)
        enqueue_job(session, lead.id, "score")
    session.flush()
    return dana, marcus


# ==========================================================================
# The bar: zero residual references
# ==========================================================================


def test_delete_person_leaves_zero_references_anywhere(session):
    dana, _ = _two_colleagues(session)

    before = find_person_references(session, DANA)
    assert before.lead_ids and before.draft_ids and before.job_ids and before.cache_facts

    report = delete_person(DANA, session=session)

    assert report.complete is True
    after = find_person_references(session, DANA)
    assert after.total == 0
    assert after.lead_ids == []
    assert after.draft_ids == []
    assert after.job_ids == []
    assert after.cache_facts == []


def test_drafts_and_jobs_go_with_the_lead_by_cascade(session):
    dana, marcus = _two_colleagues(session)
    assert h.count(session, Draft) == 2
    assert h.count(session, Job) == 2

    delete_person(DANA, session=session)

    assert h.count(session, Lead) == 1
    assert h.count(session, Draft) == 1
    assert h.count(session, Job) == 1
    assert session.get(Lead, marcus.id) is not None


def test_person_facts_are_pruned_from_the_shared_company_row(session):
    dana, marcus = _two_colleagues(session)
    cache = h.cache_row(session, h.SHARED_DOMAIN)
    assert any(f.get("subject_email") == DANA for f in cache.facts)

    report = delete_person(DANA, session=session)

    assert report.facts_pruned > 0
    assert report.cache_rows_pruned == 1
    session.refresh(cache)
    assert not any(f.get("subject_email") == DANA for f in cache.facts)


def test_the_colleagues_research_survives(session):
    """research_cache is company-scoped and deliberately NOT cascaded.

    Dropping the row because one of the people at that company asked to be
    erased would destroy everybody else's research.
    """

    dana, marcus = _two_colleagues(session)
    cache = h.cache_row(session, h.SHARED_DOMAIN)
    company_facts_before = [f for f in cache.facts if f.get("subject_email") is None]
    marcus_facts_before = [f for f in cache.facts if f.get("subject_email") == MARCUS]
    assert company_facts_before and marcus_facts_before

    delete_person(DANA, session=session)

    assert h.count(session, ResearchCache) == 1, "the company row must survive"
    session.refresh(cache)
    assert [f for f in cache.facts if f.get("subject_email") is None] == company_facts_before
    assert [f for f in cache.facts if f.get("subject_email") == MARCUS] == marcus_facts_before


def test_the_pruned_fact_ids_are_reported(session):
    _two_colleagues(session)
    report = delete_person(DANA, session=session)

    assert report.pruned_fact_ids
    assert len(report.pruned_fact_ids) == report.facts_pruned
    assert all(fid.startswith("linkedin_profile:") for fid in report.pruned_fact_ids)


# ==========================================================================
# Edge cases that a naive implementation gets wrong
# ==========================================================================


def test_facts_are_pruned_even_when_the_lead_row_is_already_gone(session):
    """The exact shape of a botched earlier deletion. It must still clean up."""

    dana, _ = _two_colleagues(session)
    session.delete(dana)
    session.flush()
    assert find_person_references(session, DANA).cache_facts, "facts outlive the lead"

    report = delete_person(DANA, session=session)

    assert report.leads_deleted == 0
    assert report.facts_pruned > 0
    assert find_person_references(session, DANA).total == 0


def test_delete_person_is_case_insensitive(session):
    _two_colleagues(session)
    report = delete_person("  DANA.Whitfield@Northwind-Logistics.COM ", session=session)

    assert report.leads_deleted == 1
    assert report.facts_pruned > 0
    assert find_person_references(session, DANA).total == 0


def test_a_fact_an_adapter_forgot_to_stamp_is_still_caught(session):
    """The second net: a person-specific fact sourced from their profile URL.

    ``enrich`` stamps ``subject_email`` itself, but compliance controls do not
    rely on somebody else remembering.
    """

    dana, _ = _two_colleagues(session)
    cache = h.cache_row(session, h.SHARED_DOMAIN)
    facts = list(cache.facts)
    facts.append(
        {
            "id": "linkedin_profile:unstamped",
            "kind": "linkedin_profile",
            "content": "Dana has been at Northwind for six years.",
            "source_url": h.SHARED_PROFILES[0],
            "retrieved_at": h.utcnow().isoformat(),
            "subject_email": None,
            "metadata": {},
        }
    )
    cache.facts = facts
    session.flush()

    report = delete_person(DANA, session=session)

    assert "linkedin_profile:unstamped" in report.pruned_fact_ids
    assert find_person_references(session, DANA).total == 0


def test_delete_person_is_idempotent(session):
    _two_colleagues(session)
    delete_person(DANA, session=session)

    second = delete_person(DANA, session=session)

    assert second.complete is True
    assert second.found_anything is False
    assert h.count(session, Lead) == 1


def test_deleting_an_unknown_person_is_not_an_error(session):
    report = delete_person("nobody@example.com", session=session)
    assert report.complete is True
    assert report.leads_deleted == 0


def test_a_residual_reference_raises_and_rolls_the_deletion_back(monkeypatch):
    """Strict mode: a loud failure is recoverable, a quiet partial one is not.

    The verification pass is forced to report a leftover reference. Erasure
    must then refuse to claim success AND must not leave a half-erased person
    sitting in the database looking fine.
    """

    h.reset_db()
    with h.db_session() as setup:
        _two_colleagues(setup)

    import mazz.privacy as privacy

    real = privacy.find_person_references
    seen = {"calls": 0}

    def fake(session, email, **kwargs):
        refs = real(session, email, **kwargs)
        seen["calls"] += 1
        if seen["calls"] > 1:  # the post-deletion verification pass
            refs.cache_facts.append(
                (h.SHARED_DOMAIN, "linkedin_profile:leftover", "subject_email")
            )
        return refs

    monkeypatch.setattr(privacy, "find_person_references", fake)

    with pytest.raises(DeletionIncomplete):
        delete_person(DANA)

    with h.db_session() as check:
        assert real(check, DANA).lead_ids, "the half-done deletion was rolled back"


# ==========================================================================
# Suppression entries outlive the lead, on purpose
# ==========================================================================


def test_a_suppression_entry_is_kept_and_reported_not_silently_removed(session):
    _two_colleagues(session)
    h.suppress(session, email=DANA, reason="asked to be removed")

    report = delete_person(DANA, session=session)

    assert report.suppression_ids, "the never-contact record is reported"
    assert report.suppression_deleted == 0
    assert report.complete is True, "it is not counted as a residual reference"
    assert find_person_references(session, DANA).suppression_ids, (
        "forgetting that we were told not to contact someone is how they get "
        "contacted again by the next CSV upload"
    )


def test_suppression_can_be_removed_when_that_is_really_the_request(session):
    _two_colleagues(session)
    h.suppress(session, email=DANA)

    report = delete_person(DANA, session=session, remove_suppression=True)

    assert report.suppression_deleted == 1
    assert find_person_references(session, DANA).suppression_ids == []


# ==========================================================================
# The standalone form: delete_person(email), its own session and commit
# ==========================================================================


def test_delete_person_opens_and_commits_its_own_transaction():
    h.reset_db()
    with h.db_session() as setup:
        _two_colleagues(setup)

    report = delete_person(DANA)

    assert report.complete is True
    with h.db_session() as check:
        assert find_person_references(check, DANA).total == 0
        assert h.count(check, Lead) == 1
        assert h.count(check, ResearchCache) == 1
