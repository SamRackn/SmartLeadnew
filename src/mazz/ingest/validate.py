"""Per-row validation and the derivation of ``company_domain``.

Two responsibilities, both of which have to be exactly right:

1. **Reject a bad row, never a whole upload.** The client pastes or uploads a
   spreadsheet; one row with a missing email must not cost him the other
   twenty-four. Every rejection carries a reason he can act on
   ("row 7: missing required field 'email'"), not a stack trace.

2. **Derive ``company_domain``.** This is THE cache key. Two colleagues at one
   employer must normalize to one identical string, or the domain-keyed
   research cache silently misses and we pay a provider twice for the same
   scrape while producing two cache rows where the design promises one.

   Because normalization has to agree with the adapter that looks the domain
   up, it is NOT reimplemented here: :func:`derive_company_domain` delegates to
   ``mazz.integrations.company.normalize_domain``, which the integrations layer
   exports for precisely this reason. ``tests/test_ingest.py`` pins the exact
   behaviour with a table of cases, including the two spellings of the same
   employer that appear in ``tests/fixtures/sample_leads.csv``.

Nothing here touches the database. Validation is pure so it can be exercised
without Postgres and reused by the review app's manual-entry form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from mazz.integrations.company import InvalidDomain, normalize_domain
from mazz.schemas import LeadCreate

__all__ = [
    "ENRICH_JOB_KIND",
    "LEAD_COLUMNS",
    "MAX_LENGTHS",
    "REQUIRED_FIELDS",
    "RowValidationError",
    "ValidatedRow",
    "canonical_field",
    "derive_company_domain",
    "email_domain",
    "normalize_email",
    "normalize_row",
    "parse_employee_count",
    "validate_row",
]


#: The job kind ingest enqueues. ``jobs.kind`` is free-text varchar, so the
#: pipeline can grow steps without a migration.
ENRICH_JOB_KIND = "enrich"


# --------------------------------------------------------------------------
# Column vocabulary
# --------------------------------------------------------------------------
#
# The canonical field names are the ``leads`` columns. The client's export
# almost certainly spells them differently ("Full Name", "Work Email",
# "Company Website"), and he should not have to re-header a spreadsheet to use
# the tool, so headers are normalized and looked up through an alias table.

#: Canonical order, used when writing a template CSV and by the sample fixture.
LEAD_COLUMNS: tuple[str, ...] = (
    "full_name",
    "company_name",
    "website",
    "job_title",
    "email",
    "employee_count",
    "revenue_range",
    "location",
    "industry_primary",
    "industry_specific",
    "linkedin_url",
    "notes",
)

_ALIASES: Mapping[str, str] = {
    # full_name
    "full_name": "full_name",
    "name": "full_name",
    "fullname": "full_name",
    "contact_name": "full_name",
    "lead_name": "full_name",
    "prospect_name": "full_name",
    # company_name
    "company_name": "company_name",
    "company": "company_name",
    "organization": "company_name",
    "organisation": "company_name",
    "account": "company_name",
    "account_name": "company_name",
    # website
    "website": "website",
    "company_website": "website",
    "web_site": "website",
    "url": "website",
    "domain": "website",
    "company_domain": "website",
    "site": "website",
    # job_title
    "job_title": "job_title",
    "title": "job_title",
    "role": "job_title",
    "position": "job_title",
    # email
    "email": "email",
    "email_address": "email",
    "work_email": "email",
    "business_email": "email",
    "e_mail": "email",
    # employee_count
    "employee_count": "employee_count",
    "employees": "employee_count",
    "headcount": "employee_count",
    "company_size": "employee_count",
    "number_of_employees": "employee_count",
    # revenue_range
    "revenue_range": "revenue_range",
    "revenue": "revenue_range",
    "annual_revenue": "revenue_range",
    # location
    "location": "location",
    "city": "location",
    "exact_location": "location",
    "country": "location",
    # industry
    "industry_primary": "industry_primary",
    "primary_industry": "industry_primary",
    "industry": "industry_primary",
    "industry_specific": "industry_specific",
    "specific_industry": "industry_specific",
    "sub_industry": "industry_specific",
    # linkedin_url
    "linkedin_url": "linkedin_url",
    "linkedin": "linkedin_url",
    "linkedin_page": "linkedin_url",
    "linkedin_profile": "linkedin_url",
    "linkedin_profile_url": "linkedin_url",
    # notes
    "notes": "notes",
    "note": "notes",
    "comments": "notes",
    "free_text": "notes",
}

#: Required for the row to be usable at all. ``full_name`` because the draft
#: addresses a person; ``email`` because it is the unique key and the thing we
#: would be contacting.
REQUIRED_FIELDS: tuple[str, ...] = ("full_name", "email")

#: Mirrors the ``String(n)`` widths in ``mazz.models``. Over-length values are
#: rejected with a reason rather than silently truncated -- quietly mangling a
#: client's data is worse than telling him about it.
MAX_LENGTHS: Mapping[str, int] = {
    "full_name": 255,
    "company_name": 255,
    "job_title": 255,
    "email": 320,
    "revenue_range": 64,
    "location": 255,
    "industry_primary": 128,
    "industry_specific": 128,
    "company_domain": 253,
}

_HEADER_PUNCT_RE = re.compile(r"[^a-z0-9]+")

# Deliberately permissive: this is a syntax sanity check, not RFC 5322. Its job
# is to catch "n/a", "see notes", and an empty cell -- not to adjudicate exotic
# but legal addresses and throw away a real lead.
_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[a-z]{2,}$", re.IGNORECASE)

_INT_RE = re.compile(r"\d[\d,]*")


class RowValidationError(ValueError):
    """One row is unusable. ``reason`` is shown to the client verbatim.

    Raised (and caught) per row: the importer turns it into a
    :class:`~mazz.ingest.csv_import.RejectedRow` and carries on with the next
    line. It never aborts an upload.
    """

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


@dataclass(frozen=True, slots=True)
class ValidatedRow:
    """A row that passed validation, with its derived cache key attached."""

    lead: LeadCreate
    company_domain: str

    @property
    def email(self) -> str:
        return self.lead.email


# --------------------------------------------------------------------------
# Field-level normalizers
# --------------------------------------------------------------------------


def canonical_field(header: str) -> str | None:
    """Map a spreadsheet header to a canonical field name, or None if unknown.

    ``"  Company Website "`` -> ``"website"``; ``"Notes (internal)"`` ->
    ``"notes"``. Unknown columns are ignored rather than fatal: the client's
    export may carry CRM cruft we have no use for.
    """

    key = _HEADER_PUNCT_RE.sub("_", (header or "").strip().lower()).strip("_")
    if key in _ALIASES:
        return _ALIASES[key]
    # "notes_internal" -> try the leading token as a last resort.
    head = key.split("_")[0] if key else ""
    return _ALIASES.get(head)


def normalize_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Re-key an arbitrary spreadsheet row onto canonical field names.

    Later columns never clobber an earlier non-empty value, so a sheet with
    both ``Email`` and ``Work Email`` keeps the first populated one.
    """

    out: dict[str, Any] = {}
    for header, value in raw.items():
        field = canonical_field(str(header))
        if field is None:
            continue
        text = "" if value is None else str(value).strip()
        if not text:
            continue
        out.setdefault(field, text)
    return out


def normalize_email(raw: str | None) -> str:
    """Lower-case, trim, and unwrap ``Name <addr>`` form.

    ``leads.email`` is UNIQUE and the database stores exactly what it is given,
    so this is the ONLY place the canonical form is decided. ``Dana@Foo.COM``
    and ``dana@foo.com`` must not become two rows.
    """

    text = (raw or "").strip()
    if not text:
        raise RowValidationError("missing required field 'email'", field="email")
    if "<" in text and ">" in text:  # "Dana Whitfield <dana@foo.com>"
        text = text[text.rindex("<") + 1 : text.rindex(">")].strip()
    text = text.strip().strip(",;").lower()
    if not _EMAIL_RE.match(text):
        raise RowValidationError(f"{raw!r} is not a valid email address", field="email")
    if len(text) > MAX_LENGTHS["email"]:
        raise RowValidationError("email exceeds 320 characters", field="email")
    return text


def email_domain(email: str) -> str | None:
    """The registrable part of an email address, normalized like a website.

    Returns None rather than raising: this is the *fallback* path for domain
    derivation, and a free-mail address ("dana@gmail.com") is a weak but not
    invalid answer -- the caller decides what to do about it.
    """

    if "@" not in (email or ""):
        return None
    try:
        return normalize_domain(email.rsplit("@", 1)[1])
    except InvalidDomain:
        return None


def derive_company_domain(website: str | None, email: str | None = None) -> str:
    """Derive the research-cache key: scheme, ``www.``, path, port all stripped.

        https://WWW.Northwind-Analytics.com/careers?ref=x  ->  northwind-analytics.com
        northwind-analytics.com/about                      ->  northwind-analytics.com

    Prefers ``website``; falls back to the email's domain when the client left
    the website cell blank (he often has the address but not the URL).

    Delegates to ``mazz.integrations.company.normalize_domain`` on purpose --
    the adapter looks facts up by this exact string, and two implementations
    would eventually disagree by one character and break the cache silently.
    """

    if website and website.strip():
        try:
            return normalize_domain(website)
        except InvalidDomain as exc:
            # A junk website cell should not sink a lead whose email domain is
            # perfectly good; fall through and try that instead.
            fallback = email_domain(email or "")
            if fallback:
                return fallback
            raise RowValidationError(
                f"cannot derive a company domain: {exc}", field="website"
            ) from exc

    fallback = email_domain(email or "")
    if fallback:
        return fallback
    raise RowValidationError(
        "cannot derive a company domain: no usable 'website' and no domain in 'email'",
        field="website",
    )


def parse_employee_count(raw: Any) -> int | None:
    """``"1,200"`` -> 1200, ``"50-200"`` -> 50, ``"~500"`` -> 500, junk -> None.

    Never a rejection reason. Headcount is a nice-to-have colour field; losing
    it is not worth refusing to contact someone over.
    """

    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw if raw >= 0 else None
    match = _INT_RE.search(str(raw))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:  # pragma: no cover - guarded by the regex
        return None


# --------------------------------------------------------------------------
# Row validation
# --------------------------------------------------------------------------


def validate_row(raw: Mapping[str, Any]) -> ValidatedRow:
    """Validate one CSV row / manual-entry payload.

    Raises :class:`RowValidationError` with a client-readable reason. Returns a
    :class:`ValidatedRow` carrying a ``LeadCreate`` whose ``email`` is
    normalized and whose ``company_domain`` is derived.
    """

    row = normalize_row(raw)

    for field in REQUIRED_FIELDS:
        if not row.get(field):
            raise RowValidationError(
                f"missing required field {field!r}", field=field
            )

    email = normalize_email(row.get("email"))
    company_domain = derive_company_domain(row.get("website"), email)

    if len(company_domain) > MAX_LENGTHS["company_domain"]:
        raise RowValidationError(
            "derived company domain exceeds 253 characters", field="website"
        )

    for field, limit in MAX_LENGTHS.items():
        value = row.get(field)
        if field != "email" and isinstance(value, str) and len(value) > limit:
            raise RowValidationError(
                f"field {field!r} exceeds {limit} characters", field=field
            )

    lead = LeadCreate(
        full_name=row["full_name"],
        email=email,
        company_name=row.get("company_name"),
        website=row.get("website"),
        job_title=row.get("job_title"),
        employee_count=parse_employee_count(row.get("employee_count")),
        revenue_range=row.get("revenue_range"),
        location=row.get("location"),
        industry_primary=row.get("industry_primary"),
        industry_specific=row.get("industry_specific"),
        linkedin_url=row.get("linkedin_url"),
        notes=row.get("notes"),
        company_domain=company_domain,
    )
    return ValidatedRow(lead=lead, company_domain=company_domain)
