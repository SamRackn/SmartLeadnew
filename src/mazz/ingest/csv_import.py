"""CSV upload and manual single-lead entry.

The order of operations is the point of this module::

    validate  ->  derive company_domain  ->  CHECK SUPPRESSION
              ->  upsert lead at 'researching'  ->  route to a campaign
              ->  enqueue ONE jobs row

The suppression check sits *before* the first write. That placement is the
brief's first compliance guardrail: "check ``suppression_list`` before a lead
ever enters the queue." A suppressed person therefore has no ``leads`` row, no
``jobs`` row, no research, no draft, and nothing for a reviewer to accidentally
approve. Moving that check even one step later would mean a suppressed person
briefly *is* queued work, and a crash in between would leave it queued forever.

**It fails closed.** If ``suppression_list`` cannot be read -- table missing,
connection dropped, permissions changed -- the lead is REJECTED, not admitted.
An unreadable never-contact list is indistinguishable from a list that contains
this person, and the safe reading of that ambiguity is "do not contact".

Rejection is always per row. One malformed line never costs the client the
other twenty-four: each row runs inside its own SAVEPOINT, so a database error
on row 7 rolls back row 7 alone and rows 8..25 still land.

**Routing sits after the gate, and is not one.** `pipeline.routing` decides
which campaign — and therefore which scoring config — a lead belongs to. It runs
only once the lead has already been admitted, it never rejects, and an
unroutable lead is admitted with ``campaign_id`` NULL, which means the shipped
scoring defaults: exactly what every lead did before campaigns existed. Nothing
about routing can reorder or soften the suppression check above it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mazz.ingest.validate import (
    ENRICH_JOB_KIND,
    RowValidationError,
    ValidatedRow,
    email_domain,
    validate_row,
)
from mazz.models import Lead, LeadStatus, SuppressionEntry
from mazz.pipeline.queue import enqueue_job
from mazz.pipeline.routing import RoutingPolicy, assign_campaign

__all__ = [
    "ImportReport",
    "LeadOutcome",
    "RejectedRow",
    "SuppressionChecker",
    "SuppressionMatch",
    "SuppressionUnavailable",
    "import_csv",
    "import_csv_file",
    "import_lead",
    "import_rows",
]


#: Statuses a re-ingest will NOT drag backwards. Re-uploading a spreadsheet
#: that still contains someone already approved or already sent
#: updates his details but must not reset him to 'researching' and generate a
#: second draft for copy that has already gone out.
TERMINAL_STATUSES = frozenset({LeadStatus.APPROVED, LeadStatus.SENT})


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuppressionMatch:
    """Why a lead was refused at the gate."""

    entry_id: int
    matched_on: str  # "email" | "domain"
    value: str
    reason: str | None = None

    def describe(self) -> str:
        why = f" ({self.reason})" if self.reason else ""
        return f"suppression_list {self.matched_on}={self.value!r}{why}"


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row that did not become a lead, and a reason the client can act on."""

    row_number: int
    kind: str  # "invalid" | "suppressed" | "suppression_unavailable" | "error"
    reason: str
    email: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked_by_compliance(self) -> bool:
        return self.kind in ("suppressed", "suppression_unavailable")

    def __str__(self) -> str:
        who = f" [{self.email}]" if self.email else ""
        return f"row {self.row_number}{who}: {self.reason}"


@dataclass(frozen=True, slots=True)
class LeadOutcome:
    """A row that became (or refreshed) a lead."""

    row_number: int
    lead_id: int
    email: str
    company_domain: str
    created: bool
    job_id: int | None
    note: str | None = None

    #: Which campaign this lead was routed to, and why. NULL/"" is a legitimate
    #: steady state, not a failure: it means the shipped scoring defaults.
    campaign_id: int | None = None
    routing_reason: str = ""

    @property
    def routed(self) -> bool:
        return self.campaign_id is not None

    def __str__(self) -> str:
        verb = "created" if self.created else "updated"
        job = f", job {self.job_id}" if self.job_id else ", no new job"
        campaign = f", campaign {self.campaign_id}" if self.routed else ""
        return (
            f"row {self.row_number}: {verb} lead {self.lead_id} "
            f"({self.email}){campaign}{job}"
        )


@dataclass(slots=True)
class ImportReport:
    """Everything that happened, per row. Rendered straight into the UI."""

    accepted: list[LeadOutcome] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.rejected)

    @property
    def created(self) -> list[LeadOutcome]:
        return [o for o in self.accepted if o.created]

    @property
    def updated(self) -> list[LeadOutcome]:
        return [o for o in self.accepted if not o.created]

    @property
    def suppressed(self) -> list[RejectedRow]:
        return [r for r in self.rejected if r.blocked_by_compliance]

    @property
    def invalid(self) -> list[RejectedRow]:
        return [r for r in self.rejected if r.kind == "invalid"]

    @property
    def errored(self) -> list[RejectedRow]:
        return [r for r in self.rejected if r.kind == "error"]

    @property
    def job_ids(self) -> list[int]:
        return [o.job_id for o in self.accepted if o.job_id is not None]

    @property
    def routed(self) -> list[LeadOutcome]:
        return [o for o in self.accepted if o.routed]

    @property
    def unrouted(self) -> list[LeadOutcome]:
        """Admitted, but scoring against the shipped defaults.

        Worth surfacing rather than hiding: a client who expects his campaign's
        ICP to be in force wants to know these leads are not using it.
        """

        return [o for o in self.accepted if not o.routed]

    def summary(self) -> str:
        return (
            f"{self.total} rows: {len(self.created)} created, "
            f"{len(self.updated)} updated, {len(self.suppressed)} suppressed, "
            f"{len(self.invalid)} invalid, {len(self.errored)} errored, "
            f"{len(self.routed)} routed to a campaign"
        )


# --------------------------------------------------------------------------
# The suppression gate
# --------------------------------------------------------------------------


class SuppressionUnavailable(RuntimeError):
    """``suppression_list`` could not be read.

    Callers MUST treat this as "suppressed". It is raised, never swallowed and
    turned into "not suppressed", because an unreadable never-contact list and
    a list containing this person look identical from here.
    """


class SuppressionChecker:
    """Reads ``suppression_list`` for one lead at a time. Fails closed.

    Matching is deliberately wider than equality:

    * **email** -- exact, case-folded.
    * **domain** -- the lead's company domain *and* the domain of its email
      address, so ``dana@blocked-corp.com`` working at ``other.com`` is still
      caught, and so is the reverse.
    * **subdomains** -- an entry for ``blocked-corp.com`` also suppresses
      ``eu.blocked-corp.com``. Under-matching a never-contact list is the
      expensive direction of the error.

    Not cached: at ~25 leads/day a per-row SELECT costs nothing, and a cache
    would happily keep serving a stale "not suppressed" after the client adds
    someone mid-upload.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def check(self, email: str, company_domain: str | None) -> SuppressionMatch | None:
        """Return the matching entry, or None. Raises on any read failure."""

        domains = {d for d in (company_domain, email_domain(email)) if d}
        try:
            stmt = select(SuppressionEntry).where(
                or_(
                    SuppressionEntry.email.isnot(None)
                    & (SuppressionEntry.email.ilike(email)),
                    SuppressionEntry.domain.isnot(None)
                    & sql_text(
                        "lower(suppression_list.domain) = ANY(:dom) "
                        "OR EXISTS (SELECT 1 FROM unnest(CAST(:dom AS text[])) AS d(v) "
                        "           WHERE d.v LIKE '%.' || lower(suppression_list.domain))"
                    ).bindparams(dom=sorted(domains) or [""]),
                )
            )
            entries = self.session.execute(stmt).scalars().all()
        except SQLAlchemyError as exc:
            raise SuppressionUnavailable(
                f"could not read suppression_list: {exc.__class__.__name__}: {exc}"
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive, still fails closed
            raise SuppressionUnavailable(
                f"could not read suppression_list: {exc.__class__.__name__}: {exc}"
            ) from exc

        for entry in entries:
            if entry.email and entry.email.strip().lower() == email:
                return SuppressionMatch(entry.id, "email", entry.email, entry.reason)
        for entry in entries:
            if entry.domain:
                return SuppressionMatch(entry.id, "domain", entry.domain, entry.reason)
        return None


# --------------------------------------------------------------------------
# Upsert + enqueue
# --------------------------------------------------------------------------

_LEAD_FIELDS = (
    "full_name",
    "company_name",
    "website",
    "job_title",
    "employee_count",
    "revenue_range",
    "location",
    "industry_primary",
    "industry_specific",
    "linkedin_url",
    "notes",
    "company_domain",
)


def _upsert_lead(session: Session, validated: ValidatedRow) -> tuple[Lead, bool]:
    """Insert or refresh the lead keyed on email. Never duplicates.

    ``leads.email`` is UNIQUE, so the row is locked FOR UPDATE while we decide,
    and a lost race falls back to the update path rather than raising.
    """

    data = validated.lead
    existing = session.execute(
        select(Lead).where(Lead.email == data.email).with_for_update()
    ).scalar_one_or_none()

    if existing is None:
        lead = Lead(email=data.email, status=LeadStatus.RESEARCHING)
        for name in _LEAD_FIELDS:
            setattr(lead, name, getattr(data, name))
        session.add(lead)
        session.flush()
        return lead, True

    for name in _LEAD_FIELDS:
        value = getattr(data, name)
        # A blank cell on a re-upload means "no new information", not "erase
        # what we already know".
        if value is not None and value != "":
            setattr(existing, name, value)
    session.flush()
    return existing, False


def _admit(
    session: Session,
    validated: ValidatedRow,
    row_number: int,
    policy: RoutingPolicy | None = None,
) -> LeadOutcome:
    """Upsert the lead, route it to a campaign, enqueue exactly one job."""

    lead, created = _upsert_lead(session, validated)

    if not created and lead.status in TERMINAL_STATUSES:
        # Details refreshed, but do not drag an approved/sent lead back into
        # research and generate a second draft behind the client's back.
        return LeadOutcome(
            row_number=row_number,
            lead_id=lead.id,
            email=lead.email,
            company_domain=lead.company_domain or validated.company_domain,
            created=False,
            job_id=None,
            campaign_id=lead.campaign_id,
            routing_reason=(
                "not routed: an approved/sent lead keeps the campaign its copy "
                "was written under"
            ),
            note=f"lead is {lead.status.value}; details updated, not re-queued",
        )

    lead.status = LeadStatus.RESEARCHING

    # Routing: strictly after the suppression gate, and not a gate itself. It
    # only ever fills a NULL `campaign_id`, and an unroutable lead is still
    # admitted -- NULL means the shipped scoring defaults. See pipeline.routing.
    decision = assign_campaign(session, lead, policy)
    session.flush()

    # `enqueue_job` de-duplicates against an outstanding job of the same kind,
    # so uploading the same sheet twice does not mean researching twice.
    job, enqueued = enqueue_job(session, lead.id, ENRICH_JOB_KIND)
    return LeadOutcome(
        row_number=row_number,
        lead_id=lead.id,
        email=lead.email,
        company_domain=lead.company_domain or validated.company_domain,
        created=created,
        job_id=job.id,
        campaign_id=lead.campaign_id,
        routing_reason=decision.reason,
        note=None if enqueued else "reused the outstanding enrich job",
    )


# --------------------------------------------------------------------------
# Row pipeline
# --------------------------------------------------------------------------


def _process_row(
    session: Session,
    checker: SuppressionChecker,
    raw: Mapping[str, Any],
    row_number: int,
    policy: RoutingPolicy | None = None,
) -> LeadOutcome | RejectedRow:
    raw_dict = {str(k): v for k, v in raw.items()}

    # 1. validate + 2. derive company_domain
    try:
        validated = validate_row(raw)
    except RowValidationError as exc:
        return RejectedRow(
            row_number=row_number,
            kind="invalid",
            reason=exc.reason,
            email=str(raw_dict.get("email") or "").strip().lower() or None,
            raw=raw_dict,
        )

    # 3. SUPPRESSION GATE -- before any write, so a suppressed person can never
    #    own a leads row or a jobs row. Inside a SAVEPOINT so that a failed read
    #    leaves the session usable for the remaining rows.
    try:
        with session.begin_nested():
            match = checker.check(validated.email, validated.company_domain)
    except SuppressionUnavailable as exc:
        return RejectedRow(
            row_number=row_number,
            kind="suppression_unavailable",
            reason=(
                f"refused (fail closed): {exc}. A lead is never admitted while "
                "the suppression list cannot be read."
            ),
            email=validated.email,
            raw=raw_dict,
        )
    if match is not None:
        return RejectedRow(
            row_number=row_number,
            kind="suppressed",
            reason=f"suppressed: matched {match.describe()}",
            email=validated.email,
            raw=raw_dict,
        )

    # 4. upsert at 'researching' + 5. route + 6. enqueue one job
    try:
        with session.begin_nested():
            return _admit(session, validated, row_number, policy)
    except SQLAlchemyError as exc:
        return RejectedRow(
            row_number=row_number,
            kind="error",
            reason=f"database error: {exc.__class__.__name__}: {exc}",
            email=validated.email,
            raw=raw_dict,
        )


def import_rows(
    session: Session,
    rows: Iterable[Mapping[str, Any]],
    *,
    first_row_number: int = 2,
    policy: RoutingPolicy | None = None,
) -> ImportReport:
    """Ingest an iterable of already-parsed rows.

    ``first_row_number`` defaults to 2 so reported numbers line up with what
    the client sees in his spreadsheet (row 1 being the header).

    ``policy`` defaults to `routing.DEFAULT_ROUTING_POLICY` — the system decides
    the campaign from the lead's attributes, which is the client's stated
    workflow. Pass `routing.PinnedRoutingPolicy` to drop a whole spreadsheet
    into one named engagement.
    """

    checker = SuppressionChecker(session)
    report = ImportReport()

    for offset, raw in enumerate(rows):
        row_number = first_row_number + offset
        if not any(str(v or "").strip() for v in raw.values()):
            continue  # blank line
        outcome = _process_row(session, checker, raw, row_number, policy)
        if isinstance(outcome, RejectedRow):
            report.rejected.append(outcome)
        else:
            report.accepted.append(outcome)

    return report


def _iter_csv(source: str | bytes | io.IOBase) -> Iterator[dict[str, Any]]:
    if isinstance(source, bytes):
        source = source.decode("utf-8-sig")
    if isinstance(source, str):
        handle: Any = io.StringIO(source.lstrip("﻿"))
    else:
        handle = source
    reader = csv.DictReader(handle)
    if reader.fieldnames is None:
        return
    for row in reader:
        # DictReader puts overflow columns under None; drop them.
        yield {k: v for k, v in row.items() if k is not None}


def import_csv(
    session: Session,
    source: str | bytes | io.IOBase,
    *,
    policy: RoutingPolicy | None = None,
) -> ImportReport:
    """Ingest a CSV given as text, bytes, or an open file object.

    Unknown columns are ignored and header spellings are aliased, so the
    client's CRM export works without being re-headered.
    """

    return import_rows(session, _iter_csv(source), policy=policy)


def import_csv_file(
    session: Session, path: str | Path, *, policy: RoutingPolicy | None = None
) -> ImportReport:
    """Ingest a CSV from disk."""

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return import_rows(session, list(_iter_csv(handle)), policy=policy)


def import_lead(
    session: Session,
    record: Mapping[str, Any],
    *,
    policy: RoutingPolicy | None = None,
) -> LeadOutcome | RejectedRow:
    """Manual single-lead entry -- the review app's "add a lead" form.

    Exactly the same path as a CSV row, including the suppression gate and the
    routing step after it. There is deliberately no second, looser way into the
    pipeline.
    """

    checker = SuppressionChecker(session)
    return _process_row(session, checker, record, row_number=1, policy=policy)
