"""Verification tests.

Verification is a **compliance control**, not a quality score. The brief is
explicit about why: a hallucinated claim about someone else's company, sent as
cold outreach, is a legal exposure rather than an embarrassment. So the thing
these tests pin down is not "does the verifier usually notice" — it is that the
verifier **fails closed** on every path into it:

* an unsupported number or word in the copy   -> FAILED
* a citation to a fact that does not exist    -> FAILED
* a fact with no `source_url`                 -> FAILED (untraceable by contract)
* a draft with no citations at all            -> FAILED
* a missing compliance footer                 -> FAILED
* anything not PASSED                         -> not approvable, by anyone

and that a failure lands the lead in `held` — never `ready`, never `no_angle`,
never a warning banner a tired reviewer clicks past.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mazz.models import Draft, LeadStatus, VerificationStatus
from mazz.pipeline.verify import (
    VerificationResult,
    apply_verification,
    build_grounding,
    is_approvable,
    latest_draft,
    verify_content,
    verify_draft,
)
from mazz.config import reload_settings
from mazz.pipeline.draft import render_body
from mazz.schemas import Fact
from pipeline_helpers import db_session, make_draft, make_lead, reset_db

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def fact(content: str, *, kind: str = "job_posting", source_url: str | None = "https://northwind-logistics.com/careers/revops") -> Fact:
    return Fact.build(kind=kind, content=content, source_url=source_url, retrieved_at=NOW)


JOB_FACT = fact("Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.")


@pytest.fixture(autouse=True)
def _sender_address(monkeypatch):
    """Every draft needs a CAN-SPAM address; without one, drafting refuses outright."""

    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 12 Rue Centrale, 75002 Paris, France")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-unsubscribe-tokens")
    reload_settings()
    yield
    reload_settings()


@pytest.fixture()
def session():
    reset_db()
    with db_session() as s:
        yield s


@pytest.fixture()
def lead(session):
    return make_lead(
        session,
        email="dana.whitfield@northwind-logistics.com",
        full_name="Dana Whitfield",
        company_name="Northwind Logistics",
        company_domain="northwind-logistics.com",
        job_title="VP Operations",
    )


def check(lead, subject, body, signals, facts=(JOB_FACT,), *, footer=True) -> VerificationResult:
    """Verify copy the way production does: full rendered body, footer included."""

    return verify_content(
        lead=lead,
        subject=subject,
        body=render_body(body, lead) if footer else body,
        signals_used=list(signals),
        available_facts=list(facts),
    )


# --------------------------------------------------------------------------
# The happy path — so the failures below mean something
# --------------------------------------------------------------------------


def test_copy_grounded_in_a_cited_fact_passes(lead):
    result = check(
        lead,
        "Northwind Logistics is hiring a Revenue Operations Manager",
        "Dana — saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.PASSED, result.notes
    assert result.passed
    assert result.lead_status is LeadStatus.READY
    assert JOB_FACT.id in result.resolved_ids


def test_the_clients_own_lead_fields_are_legitimate_grounding(lead):
    """He supplied the name, company and title himself — those aren't hallucinations."""

    result = check(
        lead,
        "Dana — Northwind Logistics",
        "Dana Whitfield, VP Operations at Northwind Logistics. "
        "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.",
        [JOB_FACT.id],
    )
    assert result.passed, result.notes


# --------------------------------------------------------------------------
# Failing closed
# --------------------------------------------------------------------------


def test_an_invented_number_is_untraceable_and_fails(lead):
    """The `unverifiable_claim_material` scenario, in miniature.

    '40' appears nowhere in the evidence. This is the exact shape of the
    hallucination the control exists to stop.
    """

    result = check(
        lead,
        "Northwind Logistics Revenue Operations",
        "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM, "
        "and that you've scaled to 40 support reps this year.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.FAILED
    assert result.lead_status is LeadStatus.HELD
    assert any(c.token == "40" and c.kind == "number" for c in result.untraceable), result.untraceable


def test_an_invented_word_is_untraceable_and_fails(lead):
    result = check(
        lead,
        "Northwind Logistics Revenue Operations",
        "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM "
        "after the acquisition of Brightpath Freight.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.FAILED
    tokens = {c.token.lower() for c in result.untraceable}
    assert "brightpath" in tokens, result.untraceable


def test_citing_a_fact_that_does_not_exist_fails(lead):
    """A dangling citation is a structural failure — not merely unsupported copy."""

    result = check(lead, "Northwind Logistics", "Northwind Logistics.", ["job_posting:deadbeefdeadbeef"])
    assert result.status is VerificationStatus.FAILED
    assert result.problems, "a dangling citation must be reported as a structural problem"


def test_a_fact_without_a_source_url_is_untraceable(lead):
    """Contract from schemas.Fact: no source_url => the verifier must fail closed."""

    floating = fact("Northwind Logistics is hiring a Revenue Operations Manager.", source_url=None)
    result = check(
        lead,
        "Northwind Logistics Revenue Operations",
        "Saw Northwind Logistics is hiring a Revenue Operations Manager.",
        [floating.id],
        facts=(floating,),
    )
    assert result.status is VerificationStatus.FAILED
    assert result.problems, "a fact with no source_url must be reported as untraceable"


def test_a_draft_citing_nothing_fails(lead):
    """No citations means nothing to check against. That is a failure, not a pass."""

    result = check(lead, "Northwind Logistics", "Saw Northwind Logistics is hiring.", [])
    assert result.status is VerificationStatus.FAILED


def test_a_draft_missing_its_compliance_footer_fails(lead):
    """verify_content takes the FULL rendered body precisely so this is catchable."""

    grounded = "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM."
    subject = "Northwind Logistics is hiring a Revenue Operations Manager"

    assert check(lead, subject, grounded, [JOB_FACT.id]).passed, "sanity: the copy itself is fine"

    bare = check(lead, subject, grounded, [JOB_FACT.id], footer=False)
    assert bare.status is VerificationStatus.FAILED
    assert bare.problems, "the missing footer must be reported as a structural problem"


# --------------------------------------------------------------------------
# Persisting the verdict
# --------------------------------------------------------------------------


def test_failure_holds_the_lead_and_records_why(session, lead):
    draft = make_draft(
        session,
        lead,
        subject="Scaling support",
        body="You've scaled to 40 support reps this year.",
        signals_used=[JOB_FACT.id],
    )
    result = verify_draft(session, lead, draft, available_facts=[JOB_FACT])
    apply_verification(session, lead, draft, result)

    assert draft.verification_status is VerificationStatus.FAILED
    assert lead.status is LeadStatus.HELD
    assert draft.verification_notes, "the reviewer must be told why it was held"
    assert "40" in draft.verification_notes


def test_a_failed_draft_is_never_approvable(session, lead):
    draft = make_draft(session, lead, signals_used=[JOB_FACT.id])
    draft.verification_status = VerificationStatus.FAILED
    session.flush()
    assert is_approvable(draft) is False


def test_a_pending_draft_is_never_approvable(session, lead):
    """Unverified is not the same as verified-good. Fail closed on the default state."""

    draft = make_draft(session, lead, signals_used=[JOB_FACT.id])
    draft.verification_status = VerificationStatus.PENDING
    session.flush()
    assert is_approvable(draft) is False


def test_only_a_passed_draft_is_approvable(session, lead):
    draft = make_draft(session, lead, signals_used=[JOB_FACT.id])
    draft.verification_status = VerificationStatus.PASSED
    session.flush()
    assert is_approvable(draft) is True


def test_the_newest_draft_governs(session, lead):
    make_draft(session, lead, subject="First attempt", signals_used=[])
    second = make_draft(session, lead, subject="Second attempt", signals_used=[])
    session.flush()
    assert latest_draft(session, lead).id == second.id


# --------------------------------------------------------------------------
# Grounding internals
# --------------------------------------------------------------------------


def test_build_grounding_reports_every_gap_rather_than_the_first(lead):
    missing_a = "job_posting:aaaaaaaaaaaaaaaa"
    missing_b = "job_posting:bbbbbbbbbbbbbbbb"
    grounding = build_grounding(lead, [missing_a, missing_b], [JOB_FACT])
    assert len(grounding.problems) >= 2, grounding.problems


def test_resolved_citations_are_exposed_for_the_review_ui(lead):
    """The review screen shows the draft beside its sources; this is that data."""

    grounding = build_grounding(lead, [JOB_FACT.id], [JOB_FACT])
    assert JOB_FACT.id in grounding.resolved
    assert grounding.sources, "the reviewer needs the source URL, not just the id"
