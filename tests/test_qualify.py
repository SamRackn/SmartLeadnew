"""Qualification: does the premise of email 1 hold?

The control these tests defend is guardrail 2 in its restated form. The old
gate asked "is there a recent event worth writing about" and, against a list of
nine-person law firms, answered no every time — refusing to write to the entire
addressable market. The gate now asks the question the copy actually depends
on: is this business the vertical we are about to call it, and does it take on
new clients.

What must not move: a lead we cannot confirm gets NO email. Not a vaguer one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mazz.pipeline.qualify import (
    DEFAULT_QUALIFICATION,
    QualificationConfig,
    Verdict,
    qualification_config_from_json,
    qualify_facts,
)
from mazz.schemas import Fact, FactKind, LeadStatus, stable_fact_id

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
DOMAIN = "damelegal.com"

LAW = qualification_config_from_json(
    {
        "vertical_terms": [
            "law firm",
            "attorneys at law",
            "attorney",
            "practice areas",
            "criminal defense",
        ]
    }
)


def fact(kind: str, content: str, path: str = "") -> Fact:
    url = f"https://{DOMAIN}/{path}"
    return Fact(
        id=stable_fact_id(kind, url, content),
        kind=kind,
        content=content,
        source_url=url,
        retrieved_at=NOW,
    )


def law_firm_pages() -> list[Fact]:
    return [
        fact(FactKind.HOMEPAGE.value, "DAME Legal - Attorneys at Law serving Nassau County"),
        fact(FactKind.SERVICES_PAGE.value, "Practice Areas: Criminal Defense, DWI", "practice-areas"),
        fact(FactKind.CONTACT_PAGE.value, "Free consultation. Contact us today.", "contact"),
    ]


# --------------------------------------------------------------------------
# The happy path, and what it costs
# --------------------------------------------------------------------------


def test_a_law_firms_own_pages_confirm_the_vertical():
    result = qualify_facts(law_firm_pages(), config=LAW)

    assert result.verdict is Verdict.QUALIFIED
    assert result.lead_status is LeadStatus.READY
    assert "attorneys at law" in result.terms
    # Every piece of evidence the copy will cite is a page the company wrote
    # about itself, addressable by id.
    assert len(result.cited_ids) == 3


def test_qualification_spends_nothing_no_model_no_network(monkeypatch):
    """The cold path's cost argument, as a test.

    AI was ~70% of cost per usable lead and this is the step that used to
    decide whether to spend it. If an import of `anthropic` ever appears under
    this call, that argument is gone and this fails.
    """

    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.split(".")[0] in {"anthropic", "httpx", "requests", "urllib3"}:
            raise AssertionError(f"qualification reached for {name!r}; it must be free")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert qualify_facts(law_firm_pages(), config=LAW).qualified


# --------------------------------------------------------------------------
# Failing closed
# --------------------------------------------------------------------------


def test_a_business_outside_the_vertical_gets_no_email():
    dentist = [
        fact(FactKind.HOMEPAGE.value, "Bright Smiles Dental - family dentistry"),
        fact(FactKind.CONTACT_PAGE.value, "Free consultation, new patients welcome", "contact"),
    ]

    result = qualify_facts(dentist, config=LAW)

    assert result.verdict is Verdict.UNCONFIRMED
    assert result.lead_status is LeadStatus.NO_ANGLE
    assert result.cited_ids == []


def test_an_unconfigured_campaign_writes_to_nobody():
    """The shipped default qualifies no one, and that is the safe direction.

    A permissive default would mean a campaign somebody created and forgot to
    configure starts emailing strangers as whatever the default vertical was.
    """

    result = qualify_facts(law_firm_pages(), config=DEFAULT_QUALIFICATION)

    assert result.verdict is Verdict.UNCONFIRMED
    assert "no vertical_terms configured" in result.rationale


def test_a_site_saying_it_is_not_taking_clients_beats_every_vertical_match():
    closed = law_firm_pages() + [
        fact(FactKind.HOMEPAGE.value, "We are not accepting new clients at this time.", "notice")
    ]

    result = qualify_facts(closed, config=LAW)

    assert result.verdict is Verdict.DISQUALIFIED
    assert result.cited_ids == []
    assert "not accepting new clients" in result.rationale


def test_disqualified_and_unconfirmed_stay_distinguishable():
    """Both end as `no_angle`, and they must not be collapsed.

    A run full of UNCONFIRMED is a vocabulary too narrow or a scrape failing.
    A run full of DISQUALIFIED is a list bought badly. The operator needs to
    know which is happening, and `no_angle` alone does not say.
    """

    unconfirmed = qualify_facts([fact(FactKind.HOMEPAGE.value, "Acme Ltd")], config=LAW)
    disqualified = qualify_facts(
        [fact(FactKind.HOMEPAGE.value, "Attorneys at law. This domain is for sale.")],
        config=LAW,
    )

    assert unconfirmed.lead_status is disqualified.lead_status is LeadStatus.NO_ANGLE
    assert unconfirmed.verdict is not disqualified.verdict


def test_the_opening_questions_presupposition_must_be_evidenced():
    """"Are you looking to bring in more clients?" presumes they take intake.

    On by default, so the gap is caught here rather than one step later as a
    held draft — same outcome, cheaper, and logged as a research gap instead of
    a quality failure.
    """

    no_contact_page = law_firm_pages()[:2]

    result = qualify_facts(no_contact_page, config=LAW)

    assert result.verdict is Verdict.UNCONFIRMED
    assert "intake" in result.rationale


# --------------------------------------------------------------------------
# What counts as evidence
# --------------------------------------------------------------------------


def test_only_pages_where_the_company_describes_itself_can_confirm_a_vertical():
    """A blog post ABOUT law firms is not evidence of BEING one.

    This is the narrow part of the design. The event kinds are still in the
    cache and still parse; they simply cannot make a lead qualified.
    """

    about_not_being = [
        fact(FactKind.BLOG_POST.value, "Why every law firm needs SEO in 2026", "blog/seo"),
        fact(FactKind.JOB_POSTING.value, "Hiring a paralegal - Attorneys at Law", "jobs/1"),
    ]

    result = qualify_facts(about_not_being, config=LAW)

    assert result.verdict is Verdict.UNCONFIRMED


def test_terms_match_on_word_boundaries_not_substrings():
    """"legal" must not match "paralegal", or every firm with a paralegal
    job page becomes a law firm."""

    config = qualification_config_from_json({"vertical_terms": ["legal"]})
    result = qualify_facts(
        [fact(FactKind.HOMEPAGE.value, "Paralegal support for illegal dumping claims")],
        config=config,
    )

    assert result.verdict is Verdict.UNCONFIRMED


def test_a_repeated_word_is_not_repeated_evidence():
    """A page saying "attorney" twelve times has said one thing once."""

    config = qualification_config_from_json(
        {"vertical_terms": ["attorney", "law firm"], "min_vertical_matches": 2}
    )
    shouty = [
        fact(FactKind.HOMEPAGE.value, "Attorney attorney attorney. Attorney."),
        fact(FactKind.CONTACT_PAGE.value, "Free consultation", "contact"),
    ]

    assert qualify_facts(shouty, config=config).verdict is Verdict.UNCONFIRMED


def test_an_empty_cache_is_unconfirmed_not_qualified():
    result = qualify_facts([], config=LAW)

    assert result.verdict is Verdict.UNCONFIRMED
    assert "nothing the vertical could be confirmed from" in result.rationale


# --------------------------------------------------------------------------
# Config loading: a broken config must not fall back to a vocabulary nobody chose
# --------------------------------------------------------------------------


def test_a_typod_knob_is_refused_rather_than_silently_ignored():
    with pytest.raises(ValueError, match="unknown knobs"):
        qualification_config_from_json({"vertical_term": ["law firm"]})


def test_a_bare_string_term_list_is_refused():
    """A string would be iterated character by character, and the campaign
    would silently be matching on "l", "a", "w"."""

    with pytest.raises(ValueError, match="array of terms"):
        qualification_config_from_json({"vertical_terms": "law firm"})


def test_a_non_object_config_raises_rather_than_defaulting():
    with pytest.raises(TypeError, match="must be a JSON object"):
        qualification_config_from_json(["law firm"])  # type: ignore[arg-type]


def test_zero_required_matches_is_refused():
    """Zero would mean every domain that resolves is a law firm — guardrail 2
    switched off from a JSON field."""

    with pytest.raises(ValueError, match="at least 1"):
        QualificationConfig(vertical_terms=frozenset({"law firm"}), min_vertical_matches=0)


def test_a_term_that_both_confirms_and_disqualifies_is_refused():
    with pytest.raises(ValueError, match="both vertical_terms and"):
        QualificationConfig(
            vertical_terms=frozenset({"coming soon"}),
            disqualifier_terms=frozenset({"coming soon"}),
        )


def test_an_empty_override_means_the_shipped_defaults():
    assert qualification_config_from_json({}) is DEFAULT_QUALIFICATION


def test_terms_are_normalised_so_operator_capitalisation_still_matches():
    config = qualification_config_from_json({"vertical_terms": ["Attorneys At Law"]})

    assert qualify_facts(law_firm_pages(), config=config).qualified
