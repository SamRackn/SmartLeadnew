"""Ingest: validation, domain normalization, and the suppression gate.

The gate is the point. Two of these tests would be the ones a compliance
auditor reads first: a suppressed lead produces no ``jobs`` row at all, and an
unreadable ``suppression_list`` rejects rather than admits.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import pipeline_helpers as h
from mazz.ingest.csv_import import import_csv_file, import_lead, import_rows
from mazz.ingest.validate import (
    RowValidationError,
    derive_company_domain,
    normalize_email,
    parse_employee_count,
    validate_row,
)
from mazz.integrations.company import normalize_domain
from mazz.models import Job, Lead, LeadStatus


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


# ==========================================================================
# Domain derivation -- THE cache key. Exact, or the shared-research design
# silently stops working and we pay a provider twice for one company.
# ==========================================================================

DOMAIN_CASES = [
    ("https://www.northwind-logistics.com/", "northwind-logistics.com"),
    ("https://northwind-logistics.com/about?utm_source=apollo", "northwind-logistics.com"),
    ("http://WWW.Northwind-Logistics.COM/careers#openings", "northwind-logistics.com"),
    ("northwind-logistics.com", "northwind-logistics.com"),
    ("  Northwind-Logistics.com/  ", "northwind-logistics.com"),
    ("http://stonebridge-partners.com:443/team", "stonebridge-partners.com"),
    ("https://paperkite.io", "paperkite.io"),
    ("https://sub.eu.helio-medical.com/x", "sub.eu.helio-medical.com"),
    ("https://www.foo.co.uk.", "foo.co.uk"),
]


@pytest.mark.parametrize("raw,expected", DOMAIN_CASES)
def test_domain_derivation_is_exact(raw, expected):
    assert derive_company_domain(raw, "someone@example.com") == expected


@pytest.mark.parametrize("raw,expected", DOMAIN_CASES)
def test_domain_derivation_agrees_with_the_adapter(raw, expected):
    """Ingest's cache key and the adapter's lookup key must be one string.

    If these two ever diverge the cache misses silently -- no error, just a
    second bill and a second row.
    """

    assert derive_company_domain(raw, None) == normalize_domain(raw)


def test_the_two_spellings_in_the_sample_csv_collapse_to_one_domain():
    a = derive_company_domain("https://www.northwind-logistics.com/", None)
    b = derive_company_domain(
        "https://northwind-logistics.com/about?utm_source=apollo", None
    )
    assert a == b == h.SHARED_DOMAIN


def test_domain_falls_back_to_the_email_when_the_website_cell_is_blank():
    assert derive_company_domain("", "Dana@Northwind-Logistics.COM") == h.SHARED_DOMAIN
    assert derive_company_domain(None, "dana@northwind-logistics.com") == h.SHARED_DOMAIN


def test_domain_falls_back_when_the_website_cell_is_junk():
    assert derive_company_domain("n/a", "dana@paperkite.io") == "paperkite.io"


def test_undeliverable_domain_is_a_clear_rejection():
    with pytest.raises(RowValidationError) as excinfo:
        derive_company_domain("n/a", "not-an-email")
    assert "company domain" in excinfo.value.reason


# ==========================================================================
# Email normalization -- leads.email is UNIQUE and the DB stores what it is
# given, so this module decides the canonical form.
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dana@Northwind-Logistics.com", "dana@northwind-logistics.com"),
        ("  dana@northwind-logistics.com  ", "dana@northwind-logistics.com"),
        ("Dana Whitfield <Dana@Northwind-Logistics.com>", "dana@northwind-logistics.com"),
        ("MARCUS.OYELARAN@Northwind-Logistics.com", "marcus.oyelaran@northwind-logistics.com"),
    ],
)
def test_email_normalization(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "n/a", "see notes", "dana@", "@foo.com", None])
def test_unusable_email_is_rejected_with_a_reason(raw):
    with pytest.raises(RowValidationError) as excinfo:
        normalize_email(raw)
    assert excinfo.value.field == "email"
    assert excinfo.value.reason


@pytest.mark.parametrize(
    "raw,expected",
    [("240", 240), ("1,200", 1200), ("~500", 500), ("50-200", 50), ("", None), ("n/a", None)],
)
def test_employee_count_is_parsed_leniently(raw, expected):
    """Never a rejection reason -- headcount is colour, not identity."""

    assert parse_employee_count(raw) == expected


# ==========================================================================
# Per-row validation
# ==========================================================================


def test_header_spellings_are_aliased():
    row = validate_row(h.csv_row())
    assert row.lead.full_name == "Dana Whitfield"
    assert row.lead.industry_primary == "Logistics & Supply Chain"
    assert row.lead.industry_specific == "Third-Party Logistics"
    assert row.lead.linkedin_url == h.SHARED_PROFILES[0]
    assert row.company_domain == h.SHARED_DOMAIN


def test_unknown_columns_are_ignored_not_fatal():
    row = validate_row(h.csv_row(**{"CRM Record ID": "0051x00000ABC", "Owner": "sam"}))
    assert row.lead.email == "dana.whitfield@northwind-logistics.com"


@pytest.mark.parametrize("missing", ["Full Name", "Email"])
def test_missing_required_field_is_rejected_with_a_named_reason(missing):
    with pytest.raises(RowValidationError) as excinfo:
        validate_row(h.csv_row(**{missing: ""}))
    assert "required" in excinfo.value.reason


# ==========================================================================
# THE SUPPRESSION GATE
# ==========================================================================


def test_suppressed_email_produces_no_lead_and_no_job(session):
    h.suppress(session, email=h.SUPPRESSED_EMAIL, reason="unsubscribed")

    report = import_rows(
        session,
        [h.csv_row(Email=h.SUPPRESSED_EMAIL, Website="https://fairlane-logistics.example")],
    )

    assert report.accepted == []
    assert len(report.suppressed) == 1
    assert "suppressed" in report.suppressed[0].reason
    assert h.count(session, Lead) == 0
    assert h.count(session, Job) == 0, "a suppressed lead must never reach the queue"


def test_suppressed_domain_produces_no_lead_and_no_job(session):
    h.suppress(session, domain=h.SUPPRESSED_DOMAIN, reason="legal request")

    report = import_rows(
        session,
        [
            h.csv_row(
                Email=f"gregor.halvorsen@{h.SUPPRESSED_DOMAIN}",
                Website=f"https://www.{h.SUPPRESSED_DOMAIN}/contact",
            )
        ],
    )

    assert len(report.suppressed) == 1
    assert h.count(session, Lead) == 0
    assert h.count(session, Job) == 0


def test_suppression_matches_a_subdomain(session):
    """Under-matching a never-contact list is the expensive direction."""

    h.suppress(session, domain="blocked-corp.example")
    report = import_rows(
        session,
        [h.csv_row(Email="x@eu.blocked-corp.example", Website="https://eu.blocked-corp.example")],
    )
    assert len(report.suppressed) == 1
    assert h.count(session, Job) == 0


def test_suppression_matches_the_email_domain_even_when_the_website_differs(session):
    """Working at other.com does not make a blocked address contactable."""

    h.suppress(session, domain=h.SUPPRESSED_DOMAIN)
    report = import_rows(
        session,
        [
            h.csv_row(
                Email=f"gregor@{h.SUPPRESSED_DOMAIN}",
                Website="https://northwind-logistics.com",
            )
        ],
    )
    assert len(report.suppressed) == 1
    assert h.count(session, Job) == 0


def test_suppression_check_fails_closed_when_the_table_cannot_be_read(session):
    """An unreadable never-contact list must reject, never admit.

    The failure is real, not mocked: the table is renamed out from under the
    query so the SELECT genuinely errors, exercising the same code path a
    dropped connection or a permissions change would.
    """

    session.execute(text("ALTER TABLE suppression_list RENAME TO suppression_list__gone"))
    try:
        report = import_rows(session, [h.csv_row()])
    finally:
        session.execute(
            text("ALTER TABLE suppression_list__gone RENAME TO suppression_list")
        )

    assert report.accepted == []
    assert len(report.rejected) == 1
    rejected = report.rejected[0]
    assert rejected.kind == "suppression_unavailable"
    assert "fail closed" in rejected.reason
    assert h.count(session, Lead) == 0
    assert h.count(session, Job) == 0


def test_manual_entry_goes_through_the_same_gate(session):
    """There is deliberately no second, looser way into the pipeline."""

    h.suppress(session, email=h.SUPPRESSED_EMAIL)
    outcome = import_lead(session, h.csv_row(Email=h.SUPPRESSED_EMAIL))
    assert getattr(outcome, "kind", None) == "suppressed"
    assert h.count(session, Job) == 0


# ==========================================================================
# Upsert + enqueue
# ==========================================================================


def test_an_accepted_lead_lands_researching_with_exactly_one_job(session):
    report = import_rows(session, [h.csv_row()])

    assert len(report.accepted) == 1
    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    assert lead is not None
    assert lead.status is LeadStatus.RESEARCHING
    assert lead.company_domain == h.SHARED_DOMAIN
    assert lead.employee_count == 240

    jobs = h.jobs_for(session, lead.id)
    assert len(jobs) == 1
    assert jobs[0].kind == "enrich"
    assert jobs[0].status.value == "pending"


def test_reingest_updates_and_never_duplicates(session):
    import_rows(session, [h.csv_row()])
    import_rows(
        session,
        [h.csv_row(**{"Email": "  DANA.Whitfield@Northwind-Logistics.COM ",
                      "Job Title": "SVP Revenue Operations"})],
    )

    assert h.count(session, Lead) == 1, "leads.email is UNIQUE; re-ingest updates"
    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    assert lead.job_title == "SVP Revenue Operations"
    assert len(h.jobs_for(session, lead.id)) == 1, "no second job for the same work"


def test_reingest_does_not_erase_known_fields_with_blank_cells(session):
    import_rows(session, [h.csv_row()])
    import_rows(session, [h.csv_row(Notes="", **{"Job Title": ""})])

    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    assert lead.job_title == "VP Revenue Operations"
    assert lead.notes


def test_reingest_does_not_drag_an_approved_lead_back_into_research(session):
    import_rows(session, [h.csv_row()])
    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    lead.status = LeadStatus.SENT
    session.flush()

    report = import_rows(session, [h.csv_row(**{"Job Title": "Chief Revenue Officer"})])

    lead = h.lead_by_email(session, "dana.whitfield@northwind-logistics.com")
    assert lead.status is LeadStatus.SENT
    assert lead.job_title == "Chief Revenue Officer", "details still refresh"
    assert report.accepted[0].job_id is None
    assert len(h.jobs_for(session, lead.id)) == 1, "no second draft behind his back"


def test_two_colleagues_share_one_derived_domain(session):
    import_rows(
        session,
        [
            h.csv_row(),
            h.csv_row(
                **{
                    "Full Name": "Marcus Oyelaran",
                    "Email": "MARCUS.OYELARAN@Northwind-Logistics.com",
                    "Website": "https://northwind-logistics.com/about?utm_source=apollo",
                    "LinkedIn URL": h.SHARED_PROFILES[1],
                }
            ),
        ],
    )

    domains = {lead.company_domain for lead in session.query(Lead).all()}
    assert domains == {h.SHARED_DOMAIN}


# ==========================================================================
# One bad row never costs the client the upload
# ==========================================================================


def test_one_bad_row_does_not_fail_the_others(session):
    report = import_rows(
        session,
        [
            h.csv_row(),
            h.csv_row(**{"Full Name": "No Email Here", "Email": ""}),
            h.csv_row(
                **{"Full Name": "Priya Raman", "Email": "priya.raman@paperkite.io",
                   "Website": "https://paperkite.io"}
            ),
        ],
    )

    assert len(report.accepted) == 2
    assert len(report.invalid) == 1
    assert report.invalid[0].row_number == 3, "numbering matches his spreadsheet"
    assert "required" in report.invalid[0].reason
    assert h.count(session, Lead) == 2
    assert h.count(session, Job) == 2


# ==========================================================================
# The whole sample file
# ==========================================================================


def test_sample_csv_import(session):
    h.seed_sample_suppression(session)

    report = import_csv_file(session, h.SAMPLE_CSV)

    assert report.total == 8
    assert len(report.accepted) == 5
    assert len(report.suppressed) == 2
    assert len(report.invalid) == 1
    assert report.errored == []

    # The suppressed pair never entered the pipeline at all.
    for email in (
        f"gregor.halvorsen@{h.SUPPRESSED_DOMAIN}",
        h.SUPPRESSED_EMAIL,
    ):
        assert h.lead_by_email(session, email) is None

    # Two colleagues, one domain -- the shared-research case.
    northwind = [
        lead for lead in session.query(Lead).all()
        if lead.company_domain == h.SHARED_DOMAIN
    ]
    assert len(northwind) == 2

    # Exactly one job per admitted lead, and none for anybody else.
    assert h.count(session, Job) == 5
    assert {lead.status for lead in session.query(Lead).all()} == {LeadStatus.RESEARCHING}
