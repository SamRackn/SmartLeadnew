"""Excel ingest: the spreadsheet path must be the CSV path, not a looser one.

Two things are being protected here.

The first is that a workbook and a CSV containing the same leads produce the
same outcome -- same validation, same suppression gate, same routing. A second,
laxer door into the lead table is the failure the single-ingest-path design
exists to prevent, and it is the kind that appears by accident when someone
adds a parser in a hurry.

The second is that spreadsheets corrupt data in ways CSVs do not. Excel has no
integer type, so 240 arrives as 240.0 and a naive str() writes "240.0" into
employee_count. Date cells arrive as midnight datetimes and stringify to
"2026-09-20 00:00:00". Real exports carry title banners above the header. Each
of those is silent -- nothing raises, the data is just wrong -- so each gets a
test.
"""

from __future__ import annotations

import datetime
import io

import openpyxl
import pytest

import pipeline_helpers as h
from mazz.ingest.csv_import import import_csv
from mazz.ingest.excel_import import (
    ExcelImportError,
    import_excel,
    iter_excel_rows,
    looks_like_excel,
)
from mazz.models import Job, Lead, LeadStatus, SuppressionEntry


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


HEADER = ["Full Name", "Company", "Work Email", "Headcount", "Website"]

ROWS = [
    ["Louis Gerber", "DAME Legal", "louis@damelegal.com", 9, "https://damelegal.com"],
    ["Priya Chaudhry", "Chaudhry Law", "priya@chaudhrylaw.com", 12, "https://chaudhrylaw.com"],
]


def workbook_bytes(rows, *, header=HEADER, preamble=(), sheet_title="Leads") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for line in preamble:
        ws.append(line)
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ==========================================================================
# The same leads, either format, the same result
# ==========================================================================


def test_a_workbook_and_a_csv_parse_to_the_same_rows():
    """The whole design claim: by the time rows reach the pipeline, the format
    is gone. Compared at the row level rather than by importing twice, because
    resetting the database mid-test deadlocks against the open session.
    """

    parsed_excel = list(iter_excel_rows(workbook_bytes(ROWS), filename="leads.xlsx"))

    csv_text = ",".join(HEADER) + "\n" + "\n".join(
        ",".join(str(cell) for cell in row) for row in ROWS
    )
    import csv as _csv
    import io as _io

    parsed_csv = list(_csv.DictReader(_io.StringIO(csv_text)))

    assert parsed_excel == parsed_csv


def test_a_workbook_import_creates_the_leads_it_should(session):
    """And the rows, once parsed, go through the ordinary ingest path."""

    report = import_excel(session, workbook_bytes(ROWS), filename="leads.xlsx")

    assert len(report.created) == 2
    assert report.rejected == []
    assert {
        (lead.email, lead.company_domain, lead.employee_count)
        for lead in session.query(Lead).all()
    } == {
        ("louis@damelegal.com", "damelegal.com", 9),
        ("priya@chaudhrylaw.com", "chaudhrylaw.com", 12),
    }


def test_an_imported_workbook_queues_research_and_nothing_else(session):
    """Importing starts research. It must never queue a send."""

    import_excel(session, workbook_bytes(ROWS), filename="leads.xlsx")

    kinds = {job.kind for job in session.query(Job).all()}
    assert kinds == {"enrich"}, f"import must only enqueue enrich, got {kinds}"


# ==========================================================================
# The silent corruptions
# ==========================================================================


def test_whole_numbers_do_not_arrive_as_floats(session):
    """Excel stores 9 as 9.0. "9.0" in employee_count is wrong and silent."""

    import_excel(session, workbook_bytes(ROWS), filename="leads.xlsx")

    lead = session.query(Lead).filter(Lead.email == "louis@damelegal.com").one()
    assert lead.employee_count == 9


def test_a_date_cell_does_not_grow_a_midnight_timestamp():
    rows = [
        ["Louis Gerber", "DAME Legal", "louis@damelegal.com", 9, "https://damelegal.com", datetime.datetime(2026, 9, 20)],
        ["Priya Chaudhry", "Chaudhry Law", "priya@chaudhrylaw.com", 12, "https://chaudhrylaw.com", datetime.datetime(2026, 9, 21, 14, 30)],
    ]
    data = workbook_bytes(rows, header=[*HEADER, "Added"])

    parsed = list(iter_excel_rows(data, filename="leads.xlsx"))

    # A date cell is a date. A real timestamp keeps its time.
    assert parsed[0]["Added"] == "2026-09-20"
    assert parsed[1]["Added"] == "2026-09-21 14:30:00"


def test_a_title_banner_above_the_header_is_skipped(session):
    """Real exports carry a title row and a blank spacer. Both must not break it."""

    data = workbook_bytes(
        ROWS,
        preamble=[["Cold Email List - September"], []],
    )

    report = import_excel(session, data, filename="leads.xlsx")

    assert len(report.created) == 2
    assert report.rejected == []


def test_blank_rows_inside_the_sheet_are_not_rows(session):
    data = workbook_bytes([ROWS[0], [None] * 5, ROWS[1], [None] * 5])

    report = import_excel(session, data, filename="leads.xlsx")

    assert report.total == 2


# ==========================================================================
# The gate still applies -- this is the one that matters
# ==========================================================================


def test_a_suppressed_lead_in_a_workbook_is_refused_and_queues_nothing(session):
    """A spreadsheet must not be a way around the do-not-contact list."""

    session.add(SuppressionEntry(email="louis@damelegal.com", reason="unsubscribed"))
    session.flush()

    report = import_excel(session, workbook_bytes(ROWS), filename="leads.xlsx")

    assert len(report.suppressed) == 1
    assert report.suppressed[0].email == "louis@damelegal.com"
    assert len(report.created) == 1  # the other one is fine

    emails = {lead.email for lead in session.query(Lead).all()}
    assert "louis@damelegal.com" not in emails
    assert session.query(Job).count() == 1  # only the admitted lead got research


def test_a_row_with_no_email_is_invalid_not_admitted(session):
    data = workbook_bytes([["Nameless Person", "Some Firm", "", 4, "https://example.com"]])

    report = import_excel(session, data, filename="leads.xlsx")

    assert len(report.invalid) == 1
    assert "email" in report.invalid[0].reason
    assert session.query(Lead).count() == 0


# ==========================================================================
# Bad files are a message, not a traceback
# ==========================================================================


def test_content_decides_the_format_not_the_extension():
    """Exporters mislabel files. Magic numbers do not lie."""

    assert looks_like_excel(workbook_bytes(ROWS), "leads.xlsx") is True
    assert looks_like_excel(b"Full Name,Email\nA,a@b.com\n", "leads.xlsx") is False


def test_an_empty_file_raises_a_readable_error():
    with pytest.raises(ExcelImportError, match="empty"):
        list(iter_excel_rows(b"", filename="leads.xlsx"))


def test_a_missing_sheet_name_lists_the_ones_that_exist():
    data = workbook_bytes(ROWS, sheet_title="Leads")

    with pytest.raises(ExcelImportError, match="'Leads'"):
        list(iter_excel_rows(data, sheet="Sheet3", filename="leads.xlsx"))


def test_header_aliasing_works_the_same_as_it_does_for_csv(session):
    """"Work Email" and "Headcount" are aliases; no re-heading required."""

    data = workbook_bytes(
        [["Louis Gerber", "DAME Legal", "louis@damelegal.com", 9, "https://damelegal.com"]],
        header=["Contact Name", "Organization", "Business Email", "Company Size", "Company Website"],
    )

    report = import_excel(session, data, filename="leads.xlsx")

    assert len(report.created) == 1
    lead = session.query(Lead).one()
    assert lead.full_name == "Louis Gerber"
    assert lead.email == "louis@damelegal.com"
    assert lead.employee_count == 9
    assert lead.company_domain == "damelegal.com"
