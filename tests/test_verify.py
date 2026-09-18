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

W1 adds the second half of that: **precision is a safety property here**. A
verifier that holds every legitimate draft is not a strict control, it is a
rubber stamp with extra steps, because the reviewer learns to click past it. So
the bottom of this file runs `fixtures/verification_eval.json` — 63 labelled
drafts — and measures both error rates:

* false negatives (a fabricated claim that PASSED) — must be zero, always
* false positives (legitimate paraphrase that was HELD) — must stay low enough
  that `held` still means something

That set is also the regression suite for every later change to the verifier,
the lexicon, or the drafting prompt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mazz.models import Draft, Lead, LeadStatus, VerificationStatus
from mazz.pipeline.lexicon import stem
from mazz.pipeline.verify import (
    JudgeUnavailable,
    JudgeVerdict,
    VerificationResult,
    apply_verification,
    build_grounding,
    default_judge,
    extract_claims,
    is_approvable,
    latest_draft,
    sender_voice_spans,
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


# --------------------------------------------------------------------------
# The optional entailment judge
# --------------------------------------------------------------------------
#
# The judge is an ADDITION to the deterministic checks and never a replacement.
# These tests pin that shape down without a network call: a judge here is just a
# callable, and the real Claude one is never constructed.


def _judge(entailed: bool, *unsupported: str):
    def judge(subject, message, evidence):
        assert evidence, "the judge must be given the resolved evidence, not the ids"
        return JudgeVerdict(entailed=entailed, unsupported=unsupported, notes="stub")

    return judge


def test_the_judge_can_fail_a_draft_the_deterministic_checks_passed(lead):
    """The grey area: grounded words, unsupported inference. That is its job."""

    # Built entirely from ordinary English: it names nothing, counts nothing and
    # dates nothing, so the deterministic layer has no handle on it — and it
    # still asserts something about their week that no fact supports. This is
    # the grey area, demonstrated rather than asserted.
    subject = "Northwind Logistics is hiring a Revenue Operations Manager"
    body = (
        "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM. "
        "Your people must be spending most of the week on it."
    )
    assert check(lead, subject, body, [JOB_FACT.id]).passed, "sanity: deterministic layer is clean"

    judged = verify_content(
        lead=lead,
        subject=subject,
        body=render_body(body, lead),
        signals_used=[JOB_FACT.id],
        available_facts=[JOB_FACT],
        judge=_judge(False, "Your people must be spending most of the week on it."),
    )
    assert judged.status is VerificationStatus.FAILED
    assert judged.lead_status is LeadStatus.HELD
    assert "most of the week" in judged.notes


def test_the_judge_can_never_rescue_a_draft_the_hard_rules_failed(lead):
    """A judge can be wrong. The exact-match rules for numbers cannot be."""

    result = verify_content(
        lead=lead,
        subject="Northwind Logistics Revenue Operations",
        body=render_body(
            "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the "
            "CRM, and that you've scaled to 40 support reps this year.",
            lead,
        ),
        signals_used=[JOB_FACT.id],
        available_facts=[JOB_FACT],
        judge=_judge(True),  # the judge says it is fine. It does not get a vote.
    )
    assert result.status is VerificationStatus.FAILED
    assert any(c.token == "40" for c in result.untraceable)


def test_a_judge_that_cannot_answer_holds_the_draft(lead):
    """Fail closed. 'Could not check' is not 'checked and fine'."""

    def broken_judge(subject, message, evidence):
        raise JudgeUnavailable("the model declined")

    result = verify_content(
        lead=lead,
        subject="Northwind Logistics is hiring a Revenue Operations Manager",
        body=render_body(
            "Saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.",
            lead,
        ),
        signals_used=[JOB_FACT.id],
        available_facts=[JOB_FACT],
        judge=broken_judge,
    )
    assert result.status is VerificationStatus.FAILED
    assert result.lead_status is LeadStatus.HELD
    assert "judge" in result.notes.lower()


def test_no_judge_is_configured_by_default(monkeypatch):
    """A key in the environment is not consent to spend. Off unless asked for."""

    monkeypatch.delenv("VERIFY_JUDGE", raising=False)
    reload_settings()
    assert default_judge() is None


def test_an_unrecognised_judge_setting_raises_rather_than_disabling_the_check(monkeypatch):
    monkeypatch.setenv("VERIFY_JUDGE", "gpt-whatever")
    reload_settings()
    with pytest.raises(ValueError):
        default_judge()


# --------------------------------------------------------------------------
# WHO IS THE SENTENCE ABOUT? (the W2 speaker model)
# --------------------------------------------------------------------------
#
# The first live `compose_claude` run held 4 of 4 drafts and an operator read
# every one against its evidence: not one flagged token was a fabrication. Most
# of the holds were the SENDER'S own prose being checked as if it were a claim
# about the recipient — `entity "I'm"`, `number '15'` from "a 15-minute call",
# `word 'curious'` from "I'd be curious".
#
# These tests pin both directions. The exemption has to work, and — far more
# importantly — it must not become a way to smuggle a claim about the recipient
# past the control by putting "I" in front of it.


def test_a_sender_voice_sentence_is_recognised():
    spans = sender_voice_spans("I work with support teams on exactly that handoff.")
    assert spans, "first person, nobody else named: this is the sender talking"


@pytest.mark.parametrize(
    "text, why",
    [
        ("I saw you scaled to 40 reps.", "second person"),
        ("I think Northwind doubled headcount.", "names their company"),
        ("We think headcount doubled.", "cognition verb introduces a proposition"),
        ("I noticed the support team just passed 40 people.", "reporting verb"),
        ("I hear they are hiring.", "third person"),
        ("Headcount doubled last quarter.", "no first person at all"),
    ],
)
def test_these_are_never_sender_voice(text, why):
    """The safety half. Every one of these carries a claim about somebody else."""

    prospect = frozenset({stem("northwind"), stem("logistics")})
    assert sender_voice_spans(text, prospect) == (), why


def test_a_first_person_fabrication_about_the_prospect_is_still_caught(lead):
    """The headline adversarial case. 'I saw' does not launder '40 reps'."""

    result = check(
        lead,
        "Your team",
        "Dana — I saw you scaled to 40 reps this year, and I work with teams at that size.",
        [JOB_FACT.id],
    )

    assert result.status is VerificationStatus.FAILED
    assert any(c.kind == "number" and c.token == "40" for c in result.untraceable)


def test_a_cognition_verb_cannot_hide_a_claim_in_first_person(lead):
    """No 'you', no company name, first person throughout — and still checked,
    because what follows "we think" is a proposition about somebody else."""

    result = check(
        lead,
        "Notes",
        "Dana — we think headcount doubled, and I work with teams in that spot.",
        [JOB_FACT.id],
    )

    assert result.status is VerificationStatus.FAILED
    assert any(c.token.lower() == "headcount" for c in result.untraceable)


def test_names_and_titles_are_checked_in_the_senders_own_sentences(lead):
    """The exemption covers numbers, dates and prose. It does NOT cover the
    claim types that carry the legal exposure."""

    named = check(
        lead,
        "Routing",
        "Dana — I can share what we built with Brightpath Freight last year.",
        [JOB_FACT.id],
    )
    assert named.status is VerificationStatus.FAILED
    assert any(c.kind == "entity" for c in named.untraceable)

    titled = check(
        lead,
        "Revenue operations",
        "Dana — I spend my week with companies hiring a Head of Revenue Operations.",
        [JOB_FACT.id],
    )
    assert titled.status is VerificationStatus.FAILED
    assert any(c.kind == "role" for c in titled.untraceable)


def test_the_senders_own_offer_is_not_a_claim_about_the_recipient(lead):
    """What the live run held four drafts for."""

    result = check(
        lead,
        "Revenue Operations Manager",
        "Dana — I saw the Revenue Operations Manager opening.\n\n"
        "I'm happy to do a 15-minute call and share what I'm seeing elsewhere. "
        "I'd be curious how you're handling it today.",
        [JOB_FACT.id],
    )

    assert result.passed, result.notes


def test_a_duration_only_counts_when_a_meeting_is_being_proposed(lead):
    """'a 20-minute call' is an offer; '40 minutes a day' measures THEM."""

    offered = check(
        lead,
        "Revenue Operations Manager",
        "Dana — I saw the Revenue Operations Manager opening. Would you be open "
        "to a 20-minute call?",
        [JOB_FACT.id],
    )
    assert offered.passed, offered.notes

    measured = check(
        lead,
        "Revenue Operations Manager",
        "Dana — I know your team loses 40 minutes a day to routing leads by hand.",
        [JOB_FACT.id],
    )
    assert measured.status is VerificationStatus.FAILED
    assert any(c.kind == "number" and c.token.startswith("40") for c in measured.untraceable)


# --------------------------------------------------------------------------
# Dates, titles and word forms
# --------------------------------------------------------------------------


def test_a_forward_or_present_deictic_is_not_a_dated_claim(lead):
    result = check(
        lead,
        "Revenue Operations Manager",
        "Dana — the Revenue Operations Manager opening is live. Worth a short "
        "call in the next week or two?",
        [JOB_FACT.id],
    )
    assert result.passed, result.notes


def test_a_past_period_is_still_a_dated_claim(lead):
    """The other half of the same change: 'last quarter' dates an event."""

    result = check(
        lead,
        "Revenue Operations Manager",
        "Dana — you posted the Revenue Operations Manager role last quarter.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.FAILED
    assert any(c.kind == "date" and "last quarter" in c.token.lower() for c in result.untraceable)


def test_a_common_noun_before_hire_is_not_a_job_title(lead):
    """`role 'layer'` on the live run: "that layer before the new hire lands"
    matched the possessive pattern as `that … hire`, and the words after the
    determiner were read as a title. The title is what sits against the noun."""

    result = check(
        lead,
        "Revenue Operations Manager",
        "Dana — I saw the Revenue Operations Manager opening. I spend most of my "
        "time with teams standing up that layer before the new hire lands.",
        [JOB_FACT.id],
    )
    assert result.passed, result.notes


def test_a_real_invented_title_in_the_same_construction_still_fails(lead):
    """…and reading backwards must not cost the catch it was there for."""

    result = check(
        lead,
        "Revenue operations",
        "Dana — your Head of Revenue Operations opening caught my eye.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.FAILED
    assert any(c.kind == "role" for c in result.untraceable)


def test_a_part_of_speech_change_is_restatement_not_fabrication(lead):
    """The req says "escalate integration problems"; the copy says "the
    escalation path". One-way: the base still has to be in the evidence."""

    supported = fact("Answer inbound clinic questions and escalate integration problems.")
    result = check(
        lead,
        "Escalating integration problems",
        "Dana — the escalation path for integration problems is where it gets messy.",
        [supported.id],
        facts=(supported,),
    )
    assert result.passed, result.notes


def test_derivational_folding_cannot_invent_support_that_is_absent(lead):
    """If the evidence contains no form of the word, no amount of suffix
    stripping rescues it."""

    result = check(
        lead,
        "Migration",
        "Dana — your CRM migration is the hard part.",
        [JOB_FACT.id],
    )
    assert result.status is VerificationStatus.FAILED


def test_the_senders_contractions_are_not_proper_nouns(lead):
    """`entity "I'm"` — the sender's own contraction read as somebody's name."""

    grounding = build_grounding(lead, [JOB_FACT.id], [JOB_FACT])
    claims = extract_claims("I'm glad I'll see you; I'd like that.", "body", grounding)
    assert [c for c in claims if c.kind == "entity"] == []


# --------------------------------------------------------------------------
# The compliance footer, and the address in it
# --------------------------------------------------------------------------


def test_a_placeholder_sender_address_fails_the_draft_closed(lead, monkeypatch):
    """A fabricated CAN-SPAM address must fail the way an empty one does.

    The live run rendered `PLACEHOLDER - Mazz Ltd, 12 Rue Centrale, 75002 Paris,
    France` into all four footers. That looks compliant to a reviewer and to any
    check that asserts "footer present", which is exactly what makes it worse
    than a missing address rather than better.
    """

    body = render_body("Dana — I saw the Revenue Operations Manager opening.", lead)
    monkeypatch.setenv(
        "SENDER_POSTAL_ADDRESS", "PLACEHOLDER - Mazz Ltd, 12 Rue Centrale, 75002 Paris, France"
    )
    reload_settings()

    result = verify_content(
        lead=lead,
        subject="Revenue Operations Manager",
        body=body,
        signals_used=[JOB_FACT.id],
        available_facts=[JOB_FACT],
    )

    assert result.status is VerificationStatus.FAILED
    assert any("footer" in p.lower() for p in result.problems), result.problems


# --------------------------------------------------------------------------
# THE LABELLED EVALUATION SET
# --------------------------------------------------------------------------
#
# 63 drafts with a known verdict, in tests/fixtures/verification_eval.json.
# Every case carries its own `why`, which is what a future reader needs when one
# of them flips.
#
# The leads here are constructed in memory rather than inserted: `verify_content`
# is pure, so the whole set runs without touching Postgres and cannot be
# disturbed by another agent's fixtures.

EVAL_PATH = Path(__file__).parent / "fixtures" / "verification_eval.json"
EVAL = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

#: The bar. Zero false negatives is non-negotiable — a fabricated claim that
#: passes is the failure the product exists to prevent. The false-positive bar
#: is a product decision, not a taste one: at 10% the reviewer still reads the
#: warnings, and above that `held` stops meaning anything. It is asserted at the
#: measured value so a regression has to be argued for in a diff, not absorbed.
MAX_FALSE_NEGATIVE_RATE = 0.0
MAX_FALSE_POSITIVE_RATE = 0.0


def _eval_fact(key: str) -> Fact:
    spec = EVAL["facts"][key]
    return Fact.build(
        kind=spec["kind"],
        content=spec["content"],
        source_url=spec["source_url"],
        retrieved_at=NOW,
    )


EVAL_FACTS = {key: _eval_fact(key) for key in EVAL["facts"]}


def _eval_lead(key: str) -> Lead:
    return Lead(**EVAL["leads"][key])


def _eval_body(case: dict, lead: Lead) -> str:
    """Render the case body the way production would, including its footer defect."""

    mode = case.get("footer", "ok")
    if mode == "missing":
        return case["body"]
    if mode == "other_lead":
        # Same address, someone else's one-click unsubscribe link.
        return render_body(case["body"], _eval_lead("tom"))
    rendered = render_body(case["body"], lead)
    if mode == "tampered":
        return rendered.replace("12 Rue Centrale", "99 Rue Inventée")
    return rendered


def run_eval_case(case: dict) -> VerificationResult:
    lead = _eval_lead(case["lead"])
    cited = [EVAL_FACTS[key].id if key in EVAL_FACTS else key for key in case["cite"]]
    return verify_content(
        lead=lead,
        subject=case["subject"],
        body=_eval_body(case, lead),
        signals_used=cited,
        available_facts=list(EVAL_FACTS.values()),
    )


@pytest.mark.parametrize("case", EVAL["cases"], ids=lambda c: c["id"])
def test_labelled_eval_case(case):
    """Each labelled draft, individually, so a failure names itself."""

    result = run_eval_case(case)

    if case["expect"] == "fail":
        assert result.status is VerificationStatus.FAILED, (
            f"FALSE NEGATIVE — {case['id']} was supposed to be caught.\n"
            f"{case['why']}\n{result.notes}"
        )
        assert result.lead_status is LeadStatus.HELD
        expected = case.get("expect_claim")
        if expected:
            assert any(
                c.kind == expected["kind"] and expected["token"].lower() in c.token.lower()
                for c in result.untraceable
            ), (
                f"{case['id']} failed, but not for the labelled reason "
                f"({expected}); got {[(c.kind, c.token) for c in result.untraceable]} "
                f"and problems {result.problems}"
            )
        if case.get("expect_problem"):
            assert result.problems, f"{case['id']}: expected a structural problem"
    else:
        assert result.passed, (
            f"FALSE POSITIVE — {case['id']} is legitimate copy and was held.\n"
            f"{case['why']}\n{result.notes}"
        )
        assert result.lead_status is LeadStatus.READY


def test_eval_set_error_rates_are_within_the_bar(capsys):
    """The W1 acceptance measurement. Prints the numbers it asserts on."""

    false_negatives: list[str] = []
    false_positives: list[tuple[str, str]] = []
    positives = [c for c in EVAL["cases"] if c["expect"] == "fail"]
    negatives = [c for c in EVAL["cases"] if c["expect"] == "pass"]

    for case in EVAL["cases"]:
        result = run_eval_case(case)
        if case["expect"] == "fail" and result.passed:
            false_negatives.append(case["id"])
        if case["expect"] == "pass" and not result.passed:
            held_for = "; ".join(
                f"{c.kind} {c.token!r}" for c in result.untraceable
            ) or "; ".join(result.problems)
            false_positives.append((case["id"], held_for))

    fn_rate = len(false_negatives) / len(positives)
    fp_rate = len(false_positives) / len(negatives)

    with capsys.disabled():
        print(f"\n  labelled drafts          : {len(EVAL['cases'])}")
        print(f"  must-fail (fabrication)  : {len(positives)}")
        print(f"  must-pass (legitimate)   : {len(negatives)}")
        print(
            f"  FALSE NEGATIVES          : {len(false_negatives)}/{len(positives)} "
            f"= {fn_rate:.1%}  (fabrication that passed)"
        )
        for case_id in false_negatives:
            print(f"      ! {case_id}")
        print(
            f"  FALSE POSITIVES          : {len(false_positives)}/{len(negatives)} "
            f"= {fp_rate:.1%}  (legitimate copy held)"
        )
        for case_id, why in false_positives:
            print(f"      ! {case_id}: {why}")

    assert fn_rate <= MAX_FALSE_NEGATIVE_RATE, (
        f"{len(false_negatives)} fabricated claim(s) passed verification: "
        f"{false_negatives}. This side is non-negotiable."
    )
    assert fp_rate <= MAX_FALSE_POSITIVE_RATE, (
        f"{len(false_positives)} legitimate draft(s) were held: {false_positives}. "
        "A queue of false positives is how a compliance control becomes a rubber stamp."
    )


def test_the_eval_set_covers_both_sides_and_stays_big_enough():
    """Guard the set itself: a shrinking corpus silently weakens every number above."""

    cases = EVAL["cases"]
    assert len(cases) >= 50, "the labelled set is the deliverable; do not shrink it"
    assert len({c["id"] for c in cases}) == len(cases), "duplicate case ids"
    categories = {c["category"] for c in cases}
    for required in (
        "fabricated_number",
        "fabricated_entity",
        "fabricated_date",
        "fabricated_role",
        "fabricated_quote",
        "unsupported_by_citation",
        "structural",
        "known_false_positive",
    ):
        assert required in categories, f"the eval set lost its {required} cases"
    assert sum(1 for c in cases if c["expect"] == "fail") >= 25
    assert sum(1 for c in cases if c["expect"] == "pass") >= 25
