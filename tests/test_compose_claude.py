"""Tests for the batched, cached Claude drafting path.

The interesting assertions here are not "does it draft" — `test_draft.py`
already pins the draft contract, and the batch path reuses it. These tests pin
the three things that can silently cost money or silently weaken a guardrail:

1. **The cached prefix is actually cacheable and actually frozen.** Caching
   fails silently: no error, no wrong output, just a bigger bill, for months.
   The only defence is a standing test that the prefix is byte-identical
   between requests and that the breakpoint is where we think it is.
2. **A refusal never becomes `no_angle`.** `no_angle` means "there was no
   reason to reach out". A refusal means we do not know. Collapsing the two is
   the exact failure the client is paying to escape, arriving by a new route.
3. **Every failure path writes a row.** A lead that vanishes from the batch
   without a draft and without a failed job is a lead the client believes was
   researched.

No network. The Anthropic SDK is not installed in this environment and these
tests must run without it, which is also a check that the lazy imports are
really lazy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from mazz.config import reload_settings
from mazz.db import get_engine
from mazz.integrations.company import StubCompanyAdapter
from mazz.models import (
    Draft,
    DraftBatch,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    ResearchCache,
)
from mazz.pipeline import compose_claude as cc
from mazz.pipeline import draft_batch as db_mod
from mazz.pipeline.draft import DraftContent, build_request
from mazz.pipeline.queue import enqueue_job
from mazz.pipeline.score import score_lead

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = {
    s["name"]: s
    for s in json.loads((FIXTURES / "scenarios.json").read_text())["scenarios"]
}

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
TEST_ADDRESS = "Mazz Outreach LLC, 1100 W Fulton Market, Suite 300, Chicago IL 60607"


@pytest.fixture(autouse=True)
def compliance_env(monkeypatch):
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", TEST_ADDRESS)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def session():
    connection = get_engine().connect()
    transaction = connection.begin()
    db = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


def seed(session, domain: str, *, email: str, company: str) -> Lead:
    facts = list(StubCompanyAdapter().fetch_company(domain).facts)
    session.merge(
        ResearchCache(
            company_domain=domain,
            company_name=company,
            facts=[f.model_dump(mode="json") for f in facts],
            fetched_at=NOW,
        )
    )
    lead = Lead(
        full_name="Dana Whitfield",
        email=email,
        company_name=company,
        job_title="VP Revenue",
        company_domain=domain,
        status=LeadStatus.RESEARCHING,
    )
    session.add(lead)
    session.flush()
    return lead


def request_for(session, lead):
    return build_request(lead, score_lead(session, lead, now=NOW))


# ==========================================================================
# A fake Anthropic client
# ==========================================================================
#
# Shaped like the bits of the SDK these modules touch, and nothing else. It is
# deliberately dumb: the point of these tests is our handling of the response,
# not a re-implementation of the API.


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 200
    cache_read_input_tokens: int = 1500
    cache_creation_input_tokens: int = 0


@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    content: list
    usage: FakeUsage
    stop_reason: str = "end_turn"
    stop_details: Any = None


@dataclass
class FakeResultBody:
    type: str
    message: Any = None
    error: Any = None


@dataclass
class FakeResult:
    custom_id: str
    result: FakeResultBody


@dataclass
class FakeBatch:
    id: str
    processing_status: str = "ended"


class FakeBatches:
    def __init__(self, owner):
        self.owner = owner

    def create(self, *, requests):
        self.owner.submitted = list(requests)
        return FakeBatch(id="msgbatch_test")

    def retrieve(self, batch_id):
        return FakeBatch(id=batch_id, processing_status=self.owner.processing_status)

    def results(self, batch_id):
        return iter(self.owner.results)


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner
        self.batches = FakeBatches(owner)

    def create(self, **kwargs):
        self.owner.creates.append(kwargs)
        return FakeMessage(content=[], usage=FakeUsage(cache_creation_input_tokens=1500))


class FakeClient:
    def __init__(self, results=None, processing_status="ended"):
        self.creates: list[dict] = []
        self.submitted: list = []
        self.results = results or []
        self.processing_status = processing_status
        self.messages = FakeMessages(self)


def ok_result(custom_id: str, content: DraftContent, **usage) -> FakeResult:
    return FakeResult(
        custom_id=custom_id,
        result=FakeResultBody(
            type="succeeded",
            message=FakeMessage(
                content=[FakeText(text=content.model_dump_json())],
                usage=FakeUsage(**usage),
            ),
        ),
    )


# ==========================================================================
# 1. The cached prefix
# ==========================================================================


def test_the_prefix_is_byte_identical_across_leads(session):
    """Two different leads must share one cache entry.

    This is the whole saving. If any per-lead value leaks into `system`, every
    lead writes its own entry and reads none — which costs MORE than not
    caching, because writes carry a premium.
    """

    a = seed(session, "northwind-logistics.com", email="a@northwind-logistics.com", company="Northwind")
    b = seed(session, "northwind-logistics.com", email="b@northwind-logistics.com", company="Northwind")
    b.full_name = "Marcus Oyelaran"
    session.flush()

    pa = cc.build_params(request_for(session, a))
    pb = cc.build_params(request_for(session, b))

    assert pa["system"] == pb["system"]
    assert json.dumps(pa["system"]) == json.dumps(pb["system"])
    assert pa["messages"] != pb["messages"], "the per-lead part must be in messages"


def test_the_prefix_contains_nothing_volatile():
    """No date, no per-run value, no drift between two calls.

    A year is the classic silent invalidator: "today is 2026-09-15" at the top
    of a system prompt makes the entire prefix uncacheable, every day, with no
    error anywhere. The `{first_name}` in the prompt's own "do not emit merge
    fields" instruction is literal text and is deliberately allowed.
    """

    assert cc._system_blocks() == cc._system_blocks()
    assert str(datetime.now().year) not in cc.SYSTEM_PROMPT
    assert datetime.now().strftime("%Y-%m-%d") not in cc.SYSTEM_PROMPT
    # The prompt is a module constant, not something assembled per call.
    assert cc._system_blocks()[0]["text"] is cc.SYSTEM_PROMPT


def test_the_breakpoint_is_on_system_not_on_the_lead(session):
    """Explicit breakpoint at the end of the SHARED portion.

    Marking the last block of the whole request instead — which is what
    top-level automatic caching would do — puts the write after the per-lead
    tail, so every request pays the write premium on bytes nothing reads back.
    """

    lead = seed(session, "northwind-logistics.com", email="c@northwind-logistics.com", company="Northwind")
    params = cc.build_params(request_for(session, lead))

    assert params["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in params, "no top-level automatic caching"
    assert "cache_control" not in json.dumps(params["messages"])


def test_effort_is_pinned_not_per_request(session):
    """Varying effort would invalidate the messages cache on every call."""

    lead = seed(session, "northwind-logistics.com", email="d@northwind-logistics.com", company="Northwind")
    one = cc.build_params(request_for(session, lead))
    two = cc.build_params(request_for(session, lead))
    assert one["output_config"]["effort"] == two["output_config"]["effort"] == "high"


def test_the_output_schema_is_strict():
    """Every property required, nothing extra allowed.

    `signals_used` has a pydantic default, so it is NOT in the generated
    `required` list — and an absent `signals_used` would read as "the model
    chose to cite nothing" rather than as a malformed response.
    """

    schema = cc._schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "signals_used" in schema["required"]


# ==========================================================================
# 2. Response handling
# ==========================================================================


def test_a_refusal_raises_rather_than_returning_an_empty_draft():
    message = FakeMessage(content=[], usage=FakeUsage(), stop_reason="refusal")
    with pytest.raises(cc.ComposerRefused):
        cc.parse_message(message)


def test_a_truncated_generation_raises():
    message = FakeMessage(
        content=[FakeText(text='{"subject":"a","body_text":"b"')],
        usage=FakeUsage(),
        stop_reason="max_tokens",
    )
    with pytest.raises(cc.ComposerTruncated):
        cc.parse_message(message)


def test_a_good_response_parses_into_the_draft_contract():
    content = DraftContent(subject="s", body_text="b", signals_used=["job_posting:abc"])
    message = FakeMessage(
        content=[FakeText(text=content.model_dump_json())], usage=FakeUsage()
    )
    assert cc.parse_message(message) == content


# ==========================================================================
# 3. Cost accounting
# ==========================================================================


def test_batch_is_half_price_and_cache_reads_are_a_tenth():
    usage = cc.Usage(input_tokens=1_000_000)
    cached = cc.Usage(cache_read_input_tokens=1_000_000)

    full = cc.price_usd(usage, model="claude-opus-5")
    assert full == pytest.approx(5.00)
    assert cc.price_usd(usage, model="claude-opus-5", batch=True) == pytest.approx(2.50)
    assert cc.price_usd(cached, model="claude-opus-5") == pytest.approx(0.50)


def test_input_tokens_is_the_uncached_remainder_not_the_prompt_size():
    usage = cc.Usage(input_tokens=100, cache_read_input_tokens=1900)
    assert usage.total_input_tokens == 2000
    assert usage.cache_hit_rate == pytest.approx(0.95)


# ==========================================================================
# 4. The batch runner
# ==========================================================================


def test_a_no_angle_lead_never_enters_the_batch(session):
    """The cheapest optimisation in the pipeline: don't call the model.

    A funding-only lead is `no_angle` by scoring. Batching it would pay for a
    model call whose only correct answer is "do not write this email".
    """

    scenario = SCENARIOS["weak_signal_only"]
    lead = seed(session, scenario["domain"], email="weak@paperkite.io", company="PaperKite")
    enqueue_job(session, lead.id, "draft")

    jobs = db_mod.claim_draft_jobs(session)
    plan = db_mod.build_batch(session, jobs, now=NOW)

    assert plan.requests == []
    assert plan.no_angle == 1
    assert lead.status is LeadStatus.NO_ANGLE
    assert _draft_count(session, lead.id) == 0


def test_a_landed_batch_writes_a_pending_draft_and_chains_to_verify(session):
    lead = seed(session, "northwind-logistics.com", email="batch@northwind-logistics.com", company="Northwind")
    job, _ = enqueue_job(session, lead.id, "draft")

    jobs = db_mod.claim_draft_jobs(session)
    plan = db_mod.build_batch(session, jobs, now=NOW)
    assert len(plan.requests) == 1

    ids = score_lead(session, lead, now=NOW).ids()
    content = DraftContent(
        subject="Northwind — Revenue Operations Manager",
        body_text="Hi Dana,\n\nI saw your opening.",
        signals_used=[ids[0]],
    )
    client = FakeClient(results=[ok_result(f"job-{job.id}", content)])

    row = db_mod.submit_batch(session, plan, client=client)
    report = db_mod.poll_batch(session, row, client=client, handlers={"verify": object()})

    draft = session.execute(select(Draft).where(Draft.lead_id == lead.id)).scalar_one()
    assert draft.subject == content.subject
    assert draft.signals_used == [ids[0]]
    assert draft.verification_status.value == "pending"
    assert lead.status is LeadStatus.RESEARCHING, "only verify may mark a lead ready"
    assert TEST_ADDRESS in draft.body, "footer is appended outside the composer"
    assert "Unsubscribe" in draft.body

    assert report.drafted == 1
    assert report.failed == 0
    assert _job(session, job.id).status is JobStatus.DONE
    assert _pending_kinds(session, lead.id) == {"verify"}


def test_a_refusal_fails_the_job_and_never_becomes_no_angle(session):
    """The guardrail, arriving by a new route.

    `no_angle` is a business answer meaning "nothing to say". A refusal means
    we never got an answer. If a refusal wrote `no_angle`, the client would be
    told there was no reason to contact someone nobody ever asked about.
    """

    lead = seed(session, "northwind-logistics.com", email="refused@northwind-logistics.com", company="Northwind")
    job, _ = enqueue_job(session, lead.id, "draft")
    jobs = db_mod.claim_draft_jobs(session)
    plan = db_mod.build_batch(session, jobs, now=NOW)

    refusal = FakeResult(
        custom_id=f"job-{job.id}",
        result=FakeResultBody(
            type="succeeded",
            message=FakeMessage(content=[], usage=FakeUsage(), stop_reason="refusal"),
        ),
    )
    client = FakeClient(results=[refusal])
    row = db_mod.submit_batch(session, plan, client=client)
    report = db_mod.poll_batch(session, row, client=client)

    assert report.drafted == 0
    assert report.failed == 1
    assert _draft_count(session, lead.id) == 0
    assert lead.status is not LeadStatus.NO_ANGLE
    assert "declined" in (_job(session, job.id).last_error or "")


def test_a_claimed_job_with_no_result_is_failed_not_dropped(session):
    """A lead must never leave the pipeline without a row."""

    lead = seed(session, "northwind-logistics.com", email="lost@northwind-logistics.com", company="Northwind")
    job, _ = enqueue_job(session, lead.id, "draft")
    jobs = db_mod.claim_draft_jobs(session)
    plan = db_mod.build_batch(session, jobs, now=NOW)

    client = FakeClient(results=[])  # batch ended, result never arrived
    row = db_mod.submit_batch(session, plan, client=client)
    report = db_mod.poll_batch(session, row, client=client)

    assert report.failed == 1
    assert "no result in batch" in (_job(session, job.id).last_error or "")


def test_a_batch_that_read_nothing_from_cache_says_so_on_its_own_row(session):
    """Broken caching produces no error and no wrong output. Only a bill.

    So the batch row records it. This is the standing check the caching guide
    asks for: something must assert `cache_read_input_tokens > 0`, or a
    regression in prompt assembly goes unnoticed for months.
    """

    leads = [
        seed(session, "northwind-logistics.com", email=f"cold{i}@northwind-logistics.com", company="Northwind")
        for i in range(2)
    ]
    jobs = [enqueue_job(session, lead.id, "draft")[0] for lead in leads]
    claimed = db_mod.claim_draft_jobs(session)
    plan = db_mod.build_batch(session, claimed, now=NOW)

    ids = score_lead(session, leads[0], now=NOW).ids()
    content = DraftContent(subject="s", body_text="b", signals_used=[ids[0]])
    results = [
        ok_result(f"job-{job.id}", content, cache_read_input_tokens=0) for job in jobs
    ]
    client = FakeClient(results=results)
    row = db_mod.submit_batch(session, plan, client=client)
    db_mod.poll_batch(session, row, client=client)

    assert row.succeeded_count == 2
    assert "prompt caching did no work" in (row.last_error or "")


def test_a_healthy_batch_records_its_measured_cost(session):
    """The number the gap analysis has only ever projected."""

    lead = seed(session, "northwind-logistics.com", email="cost@northwind-logistics.com", company="Northwind")
    job, _ = enqueue_job(session, lead.id, "draft")
    plan = db_mod.build_batch(session, db_mod.claim_draft_jobs(session), now=NOW)

    ids = score_lead(session, lead, now=NOW).ids()
    content = DraftContent(subject="s", body_text="b", signals_used=[ids[0]])
    client = FakeClient(results=[ok_result(f"job-{job.id}", content)])
    row = db_mod.submit_batch(session, plan, client=client)
    report = db_mod.poll_batch(session, row, client=client)

    assert row.usage["cache_read_input_tokens"] == 1500
    assert row.last_error is None
    assert float(row.cost_usd) > 0
    assert report.cost_per_draft == pytest.approx(float(row.cost_usd))
    # Batch pricing: 100 uncached + 1500 cached in, 200 out, at Opus-5 rates,
    # halved. Cheap enough that the arithmetic is worth pinning.
    expected = ((100 * 5.0 + 1500 * 0.5 + 200 * 25.0) / 1_000_000) * 0.5
    assert float(row.cost_usd) == pytest.approx(expected)


def test_the_prewarm_runs_before_the_batch_is_submitted(session):
    """Without it, a fan-out of identical prefixes all miss the cache.

    A cache entry is only readable once the request writing it has started
    responding, so N simultaneous requests with the same prefix each write
    their own. The prewarm turns those writes into reads.
    """

    lead = seed(session, "northwind-logistics.com", email="warm@northwind-logistics.com", company="Northwind")
    enqueue_job(session, lead.id, "draft")
    plan = db_mod.build_batch(session, db_mod.claim_draft_jobs(session), now=NOW)

    client = FakeClient()
    db_mod.submit_batch(session, plan, client=client)

    assert len(client.creates) == 1, "exactly one prewarm call"
    warm = client.creates[0]
    assert warm["max_tokens"] == 1
    assert warm["system"] == cc._system_blocks(), "must warm the prefix the batch reads"

    # The prewarm MUST carry the output config, because the cached prefix is
    # tools -> system and the structured-output schema is serialized into it
    # ahead of the system block. Measured on claude-opus-5: 1441 tokens
    # without it, 1946 with. A prewarm that omitted it wrote an entry at a
    # prefix no batch request sends -- it paid the 2x write premium for an
    # entry nothing read, and the batch's first request paid its own write
    # premium anyway, making prewarming worse than not prewarming.
    #
    # This is asserted rather than commented because the batch SELF-HEALS and
    # hides it: request 1 writes the real prefix, requests 2..N read it, so
    # `cache_read_input_tokens` stays nonzero and the existing assertion on it
    # passes while the prewarm is silently orphaned. Nothing else catches this.
    assert warm["output_config"] == cc._output_config(), (
        "prewarm must send the same prefix the batch sends, schema included"
    )
    batched = plan.requests[0]["params"]
    assert warm["system"] == batched["system"]
    assert warm["output_config"] == batched["output_config"]


def test_an_in_flight_batch_refreshes_its_jobs_lock(session):
    """`reclaim_stale_jobs` gives up after 15 minutes; a batch takes longer.

    Without this the reclaimer would decide the worker died and hand the same
    leads to a second drafting run — paying twice and racing two drafts into
    the same lead.
    """

    lead = seed(session, "northwind-logistics.com", email="slow@northwind-logistics.com", company="Northwind")
    job, _ = enqueue_job(session, lead.id, "draft")
    plan = db_mod.build_batch(session, db_mod.claim_draft_jobs(session), now=NOW)

    client = FakeClient(processing_status="in_progress")
    row = db_mod.submit_batch(session, plan, client=client)
    before = _job(session, job.id).locked_at

    db_mod.poll_batch(session, row, client=client)

    after = _job(session, job.id).locked_at
    assert after is not None and after >= before
    assert _job(session, job.id).status is JobStatus.RUNNING
    assert row.ended_at is None


# --------------------------------------------------------------------------


def _draft_count(session, lead_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(Draft).where(Draft.lead_id == lead_id)
    ).scalar_one()


def _job(session, job_id: int) -> Job:
    session.expire_all()
    return session.get(Job, job_id)


def _pending_kinds(session, lead_id: int) -> set[str]:
    return set(
        session.execute(
            select(Job.kind).where(
                Job.lead_id == lead_id, Job.status == JobStatus.PENDING
            )
        )
        .scalars()
        .all()
    )
