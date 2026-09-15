"""The queue and the enrichment stage.

Two claims are load-bearing here and both are tested against real Postgres
rather than asserted in prose:

* ``FOR UPDATE SKIP LOCKED`` means two workers never claim the same job. The
  test opens two genuine concurrent sessions.
* The domain-keyed cache means two colleagues cost ONE company lookup and
  produce ONE ``research_cache`` row. The test counts adapter calls.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

import pipeline_helpers as h
from mazz.db import get_sessionmaker
from mazz.integrations.base import ProviderError, RetryPolicy
from mazz.config import reload_settings
from mazz.models import (
    Draft,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    ResearchCache,
    VerificationStatus,
)
from mazz.pipeline.enrich import enrich_lead, handle_enrich
from mazz.pipeline.queue import (
    DEFAULT_HANDLERS,
    JobContext,
    PermanentJobError,
    RetrySettings,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    process_next,
    reclaim_stale_jobs,
    run_worker,
    utcnow,
)

FAST = RetryPolicy(attempts=1)


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


def _adapters(**kwargs):
    return h.company_adapter(retry_policy=FAST), h.linkedin_adapter(retry_policy=FAST)


# ==========================================================================
# Claiming: FOR UPDATE SKIP LOCKED
# ==========================================================================


def test_two_workers_never_claim_the_same_job():
    """The reason the queue is a table and not a cron over `status='pending'`.

    Worker A locks the row; worker B's ``SKIP LOCKED`` steps over it and gets
    nothing rather than blocking. B is given a statement timeout so that a
    regression (dropping SKIP LOCKED) fails the test instead of hanging it.
    """

    h.reset_db()
    with h.db_session() as setup:
        lead = h.make_lead(setup)
        enqueue_job(setup, lead.id, "enrich")

    factory = get_sessionmaker()
    worker_a, worker_b = factory(), factory()
    try:
        worker_b.execute(text("SET statement_timeout = '4s'"))

        claimed_a = claim_job(worker_a)
        assert claimed_a is not None, "worker A should get the job"

        claimed_b = claim_job(worker_b)
        assert claimed_b is None, "worker B must skip the locked row, not double-run it"

        worker_a.commit()

        # Even after A releases the lock, the row is 'running' and not claimable.
        assert claim_job(worker_b) is None
    finally:
        worker_a.rollback()
        worker_b.rollback()
        worker_a.close()
        worker_b.close()


def test_claim_respects_run_after(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich", run_after=utcnow() + timedelta(hours=1))
    session.commit()

    assert claim_job(session) is None
    assert claim_job(session, now=utcnow() + timedelta(hours=2)) is not None


def test_claim_can_be_restricted_to_kinds(session):
    lead = h.make_lead(session)
    enqueue_job(session, lead.id, "score")
    session.commit()

    assert claim_job(session, kinds=["enrich"]) is None
    assert claim_job(session, kinds=["score"]) is not None


def test_enqueue_dedupes_outstanding_work(session):
    lead = h.make_lead(session)
    first, created_first = enqueue_job(session, lead.id, "enrich")
    second, created_second = enqueue_job(session, lead.id, "enrich")

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(h.jobs_for(session, lead.id)) == 1


# ==========================================================================
# Failure: backoff, and a BOUND on the retries
# ==========================================================================


def test_failure_records_the_error_backs_off_and_eventually_gives_up(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    settings = RetrySettings(max_attempts=3, base_delay_seconds=60, max_delay_seconds=3600)

    before = utcnow()
    assert fail_job(session, job, RuntimeError("provider timed out"), settings=settings) is False
    assert job.attempts == 1
    assert job.status is JobStatus.PENDING
    assert "provider timed out" in job.last_error
    assert job.run_after - before >= timedelta(seconds=59)

    assert fail_job(session, job, RuntimeError("again"), settings=settings) is False
    assert job.attempts == 2
    assert job.run_after - before >= timedelta(seconds=119), "exponential, not flat"

    assert fail_job(session, job, RuntimeError("and again"), settings=settings) is True
    assert job.attempts == 3
    assert job.status is JobStatus.FAILED
    assert "gave up after 3 attempts" in job.last_error


def test_backoff_is_capped(session):
    settings = RetrySettings(max_attempts=99, base_delay_seconds=60, max_delay_seconds=300)
    assert settings.delay_after(1) == timedelta(seconds=60)
    assert settings.delay_after(2) == timedelta(seconds=120)
    assert settings.delay_after(9) == timedelta(seconds=300)


def test_a_permanent_error_skips_the_retries_entirely(session):
    """A malformed LinkedIn URL will fail identically at 2am."""

    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")

    gave_up = fail_job(
        session, job, PermanentJobError("lead has a garbage linkedin_url"),
        settings=RetrySettings(max_attempts=5),
    )

    assert gave_up is True
    assert job.attempts == 1
    assert job.status is JobStatus.FAILED
    assert "permanent, no retry" in job.last_error


def test_giving_up_holds_the_lead_and_never_calls_it_no_angle(session):
    """An error is an absence of a finding, not the finding 'nothing to say'."""

    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    fail_job(session, job, RuntimeError("boom"), settings=RetrySettings(max_attempts=1))

    session.refresh(lead)
    assert lead.status is LeadStatus.HELD
    assert lead.status is not LeadStatus.NO_ANGLE


def test_complete_job_clears_the_lock_marker(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    session.commit()
    claimed = claim_job(session)
    assert claimed.locked_at is not None

    complete_job(session, claimed)
    assert claimed.status is JobStatus.DONE
    assert claimed.locked_at is None


def test_reclaim_returns_a_crashed_workers_job_to_the_queue(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    session.commit()
    claimed = claim_job(session)
    claimed.locked_at = utcnow() - timedelta(hours=2)
    session.flush()

    reclaimed = reclaim_stale_jobs(session, stale_after=timedelta(minutes=15))

    assert [j.id for j in reclaimed] == [claimed.id]
    assert claimed.status is JobStatus.PENDING
    assert claimed.locked_at is None


def test_reclaim_leaves_a_merely_slow_job_alone(session):
    lead = h.make_lead(session)
    enqueue_job(session, lead.id, "enrich")
    session.commit()
    claim_job(session)

    assert reclaim_stale_jobs(session, stale_after=timedelta(minutes=15)) == []


def test_an_unknown_job_kind_fails_permanently(session):
    lead = h.make_lead(session)
    enqueue_job(session, lead.id, "teleport")
    session.commit()

    job = process_next(handlers={}, session_factory=h.db_session)

    with h.db_session() as check:
        stored = check.get(Job, job.id)
        assert stored.status is JobStatus.FAILED
        assert "no handler registered" in stored.last_error
        assert stored.attempts == 1


# ==========================================================================
# Enrichment: the domain-keyed cache is a cost control, and it is tested
# by counting provider calls.
# ==========================================================================


def test_two_leads_at_one_employer_cost_one_cache_row_and_one_provider_call(session):
    """The entire justification for keying research_cache by company domain.

    Neither lead carries a LinkedIn URL here, so the total across BOTH leads
    and BOTH adapters is exactly one provider call.
    """

    dana = h.make_lead(session, linkedin_url=None)
    marcus = h.make_lead(
        session,
        full_name="Marcus Oyelaran",
        email="marcus.oyelaran@northwind-logistics.com",
        website="https://northwind-logistics.com/about?utm_source=apollo",
        company_domain=h.SHARED_DOMAIN,
        linkedin_url=None,
    )
    company, linkedin = _adapters()

    first = enrich_lead(session, dana, company_adapter=company, linkedin_adapter=linkedin)
    second = enrich_lead(session, marcus, company_adapter=company, linkedin_adapter=linkedin)

    assert first.cache_hit is False
    assert second.cache_hit is True, "the colleague must hit the cache"
    assert company.call_count == 1, "one scrape for the company, not one per person"
    assert linkedin.call_count == 0
    assert company.call_count + linkedin.call_count == 1
    assert h.cache_domains(session) == [h.SHARED_DOMAIN]
    assert h.count(session, ResearchCache) == 1
    assert second.provider_calls == 0
    assert second.facts_total == first.facts_total > 0


def test_company_research_is_shared_but_profile_research_is_per_person(session):
    """LinkedIn facts are about a PERSON; caching them by domain would give the
    second colleague a thinner draft than the first for no reason."""

    dana = h.make_lead(session)
    marcus = h.make_lead(
        session,
        full_name="Marcus Oyelaran",
        email="marcus.oyelaran@northwind-logistics.com",
        linkedin_url=h.SHARED_PROFILES[1],
    )
    company, linkedin = _adapters()

    enrich_lead(session, dana, company_adapter=company, linkedin_adapter=linkedin)
    enrich_lead(session, marcus, company_adapter=company, linkedin_adapter=linkedin)

    assert company.call_count == 1, "domain-keyed: one company scrape"
    assert linkedin.call_count == 2, "person-keyed: one profile fetch each"
    assert h.count(session, ResearchCache) == 1

    cache = h.cache_row(session, h.SHARED_DOMAIN)
    subjects = {f.get("subject_email") for f in cache.facts if f.get("subject_email")}
    assert subjects == {dana.email, marcus.email}


def test_every_person_specific_fact_is_stamped_with_its_subject(session):
    """The delete-by-person hook. research_cache does not cascade, so an
    unstamped profile fact would survive an erasure request."""

    lead = h.make_lead(session)
    company, linkedin = _adapters()
    enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    cache = h.cache_row(session, h.SHARED_DOMAIN)
    profile_facts = [f for f in cache.facts if f["kind"] == "linkedin_profile"]
    assert profile_facts, "the fixture profile should yield facts"
    assert all(f["subject_email"] == lead.email for f in profile_facts)

    company_facts = [f for f in cache.facts if f["kind"] != "linkedin_profile"]
    assert company_facts
    assert all(f["subject_email"] is None for f in company_facts), (
        "company facts belong to the company, not to whoever triggered the scrape"
    )


def test_a_stale_cache_row_is_refetched(session):
    lead = h.make_lead(session, linkedin_url=None)
    company, linkedin = _adapters()
    enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    cache = h.cache_row(session, h.SHARED_DOMAIN)
    cache.fetched_at = h.ago(days=30)
    session.flush()

    outcome = enrich_lead(
        session, lead, company_adapter=company, linkedin_adapter=linkedin,
        max_age=timedelta(days=14),
    )
    assert outcome.cache_hit is False
    assert company.call_count == 2
    assert h.count(session, ResearchCache) == 1, "refetch updates, never duplicates"


def test_freshness_is_fetched_at_not_the_existence_of_the_row(session):
    """A row created by a failed attempt must not read as a warm cache."""

    session.add(ResearchCache(company_domain=h.SHARED_DOMAIN, facts=[], fetched_at=None))
    session.flush()
    lead = h.make_lead(session, linkedin_url=None)
    company, linkedin = _adapters()

    outcome = enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    assert outcome.cache_hit is False
    assert company.call_count == 1


# ==========================================================================
# An error is NOT an empty result
# ==========================================================================


def test_a_provider_failure_raises_and_never_looks_like_nothing_found(session):
    lead = h.make_lead(
        session,
        email="someone@outage-test.example",
        website=f"https://{h.OUTAGE_DOMAIN}",
        company_domain=h.OUTAGE_DOMAIN,
        linkedin_url=None,
    )
    company, linkedin = _adapters()

    with pytest.raises(ProviderError):
        enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    assert lead.status is not LeadStatus.NO_ANGLE


def test_a_legitimately_empty_lookup_is_a_real_finding(session):
    """Reachable site, nothing recently dated. A valid route to no_angle."""

    lead = h.make_lead(
        session,
        email="nadia.okonjo@stonebridge-partners.com",
        website=f"https://{h.EMPTY_DOMAIN}",
        company_domain=h.EMPTY_DOMAIN,
        linkedin_url="https://www.linkedin.com/in/empty-profile",
    )
    company, linkedin = _adapters()

    outcome = enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    assert outcome.found_nothing is True
    assert outcome.facts_total == 0
    assert h.cache_row(session, h.EMPTY_DOMAIN) is not None, "the empty answer is cached"
    assert h.cache_row(session, h.EMPTY_DOMAIN).fetched_at is not None


def test_a_dead_domain_is_also_a_real_finding(session):
    lead = h.make_lead(
        session,
        email="nobody@no-such-company.example",
        website=f"https://{h.MISSING_DOMAIN}",
        company_domain=h.MISSING_DOMAIN,
        linkedin_url=None,
    )
    company, linkedin = _adapters()

    outcome = enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)
    assert outcome.found_nothing is True


def test_a_broken_linkedin_url_does_not_discard_the_company_research(session):
    lead = h.make_lead(session, linkedin_url="https://example.com/not-a-profile")
    company, linkedin = _adapters()

    outcome = enrich_lead(session, lead, company_adapter=company, linkedin_adapter=linkedin)

    assert outcome.facts_total > 0
    assert any("linkedin_url unusable" in note for note in outcome.notes)
    assert linkedin.call_count == 0


# ==========================================================================
# The handler and the worker loop
# ==========================================================================


def test_enrich_handler_marks_a_genuinely_empty_result_no_angle(session):
    lead = h.make_lead(
        session,
        email="nadia.okonjo@stonebridge-partners.com",
        website=f"https://{h.EMPTY_DOMAIN}",
        company_domain=h.EMPTY_DOMAIN,
        linkedin_url="https://www.linkedin.com/in/empty-profile",
    )
    job, _ = enqueue_job(session, lead.id, "enrich")

    handle_enrich(JobContext(session=session, job=job, lead=lead, handlers={}))

    assert lead.status is LeadStatus.NO_ANGLE


def test_enrich_handler_chains_to_the_scorer_only_when_one_is_registered(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")

    handle_enrich(JobContext(session=session, job=job, lead=lead, handlers={}))
    assert [j.kind for j in h.jobs_for(session, lead.id)] == ["enrich"]

    handle_enrich(
        JobContext(
            session=session, job=job, lead=lead,
            handlers={"enrich": handle_enrich, "score": lambda ctx: None},
        )
    )
    assert sorted(j.kind for j in h.jobs_for(session, lead.id)) == ["enrich", "score"]


def test_worker_drains_the_queue_end_to_end(monkeypatch):
    """Two colleagues, from `enrich` all the way through `verify`.

    `DEFAULT_HANDLERS` registers all four stages and each one chains to the next
    via `ctx.enqueue_next`, so a single enqueued `enrich` job per lead fans out
    into the whole pipeline. That is what makes Phase 1 an end-to-end pipe
    rather than four functions that happen to exist.
    """

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 12 Rue Centrale, 75002 Paris, France")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-unsubscribe-tokens")
    reload_settings()
    h.reset_db()
    with h.db_session() as setup:
        dana = h.make_lead(setup)
        marcus = h.make_lead(
            setup,
            full_name="Marcus Oyelaran",
            email="marcus.oyelaran@northwind-logistics.com",
            linkedin_url=h.SHARED_PROFILES[1],
        )
        enqueue_job(setup, dana.id, "enrich")
        enqueue_job(setup, marcus.id, "enrich")

    stats = run_worker(
        handlers=DEFAULT_HANDLERS, session_factory=h.db_session, run_until_empty=True
    )

    # 2 leads x 4 stages. Anything less means the chain broke partway.
    assert stats.claimed == 8, stats
    assert stats.done == 8, stats
    assert stats.failed == 0, stats
    assert stats.kinds == {"enrich": 2, "score": 2, "draft": 2, "verify": 2}, stats

    with h.db_session() as check:
        assert h.count(check, ResearchCache) == 1, "one row for the shared employer"
        assert all(j.status is JobStatus.DONE for j in check.query(Job).all())

        # Each lead came out the far end with a verified draft, not merely a row.
        drafts = check.query(Draft).all()
        assert len(drafts) == 2, "one draft per lead"
        assert all(d.verification_status is not VerificationStatus.PENDING for d in drafts), (
            "every draft must have been through verification"
        )
        assert all(d.signals_used for d in drafts), "a draft must cite what it rests on"

    reload_settings()


def test_worker_failure_path_records_the_error_and_holds_the_lead():
    h.reset_db()
    with h.db_session() as setup:
        lead = h.make_lead(
            setup,
            email="someone@outage-test.example",
            website=f"https://{h.OUTAGE_DOMAIN}",
            company_domain=h.OUTAGE_DOMAIN,
            linkedin_url=None,
        )
        enqueue_job(setup, lead.id, "enrich")
        lead_id = lead.id

    process_next(
        handlers=DEFAULT_HANDLERS,
        session_factory=h.db_session,
        settings=RetrySettings(max_attempts=1),
    )

    with h.db_session() as check:
        job = check.query(Job).one()
        assert job.status is JobStatus.FAILED
        assert "ProviderUnavailable" in job.last_error
        assert check.get(Lead, lead_id).status is LeadStatus.HELD
        cache = check.get(ResearchCache, h.OUTAGE_DOMAIN)
        assert cache is not None and cache.last_error
        assert cache.fetched_at is None, "a failed attempt is never a warm cache"
