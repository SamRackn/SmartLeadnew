"""Lead ingest: CSV upload and manual single-lead entry.

The order of operations in :mod:`mazz.ingest.csv_import` is load-bearing and is
the brief's first compliance guardrail:

    validate -> derive company_domain -> CHECK SUPPRESSION -> upsert lead
    -> enqueue exactly one job

A suppressed lead never reaches the fourth step, so it can never produce a
``jobs`` row and can never be researched, drafted, or pushed.
"""

from mazz.ingest.csv_import import (
    ImportReport,
    LeadOutcome,
    RejectedRow,
    SuppressionChecker,
    SuppressionUnavailable,
    import_csv,
    import_csv_file,
    import_lead,
)
from mazz.ingest.validate import (
    ENRICH_JOB_KIND,
    LEAD_COLUMNS,
    RowValidationError,
    derive_company_domain,
    email_domain,
    normalize_email,
    validate_row,
)

__all__ = [
    "ENRICH_JOB_KIND",
    "ImportReport",
    "LEAD_COLUMNS",
    "LeadOutcome",
    "RejectedRow",
    "RowValidationError",
    "SuppressionChecker",
    "SuppressionUnavailable",
    "derive_company_domain",
    "email_domain",
    "import_csv",
    "import_csv_file",
    "import_lead",
    "normalize_email",
    "validate_row",
]
