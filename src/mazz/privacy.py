"""Delete-by-person. Built now, not retrofitted.

The brief asks for this early for a concrete reason: we store scraped personal
data, and if any lead is EU- or California-based, a deletion request is a legal
obligation with a clock on it. Retrofitting erasure onto a system that was not
designed for it means discovering, under time pressure, that the data is in
four places and one of them is a JSON blob.

Where a person can hide
-----------------------
``drafts`` and ``jobs`` cascade from ``leads``, so deleting the lead row takes
them with it in one statement. ``research_cache`` does **not** cascade, and
that is deliberate, not an oversight: it is keyed by company domain and shared
between colleagues. Dropping the row because one of five people at that company
asked to be erased would destroy the other four's research.

So the row survives and the *person* is cut out of it. Facts carry an optional
``subject_email``; every person-specific fact this pipeline stores is stamped
with it (see ``mazz.pipeline.enrich``), and erasure prunes the JSONB list by
that field. Facts sourced from the person's LinkedIn URL are pruned too, as a
second net in case an adapter ever forgets the tag.

Verification is part of the operation
-------------------------------------
``delete_person`` re-queries afterwards and refuses to report success while
anything still references the person. In ``strict`` mode (the default) a
residual reference raises, which rolls the transaction back rather than leaving
a half-erased record that nobody notices. A loud failure is recoverable; a
quiet partial deletion is the thing that gets discovered by a regulator.

Suppression entries are kept on purpose
---------------------------------------
An entry in ``suppression_list`` is a record of an instruction *not* to contact
someone, and deleting it would let the next CSV upload walk them straight back
into the pipeline. It is reported, never silently removed -- and erasure ends
by making sure one EXISTS: the normalised email plus its sha256 by default, or
with ``remove_suppression=True`` ("forget me entirely") the sha256 alone,
which still matches at every suppression check but names nobody.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mazz.db import session_scope
from mazz.ingest.csv_import import email_sha256
from mazz.models import Draft, Job, Lead, ResearchCache, SentMessage, SuppressionEntry

__all__ = [
    "DeletionIncomplete",
    "DeletionReport",
    "PersonReferences",
    "delete_person",
    "find_person_references",
]

log = logging.getLogger("mazz.privacy")


class DeletionIncomplete(RuntimeError):
    """Something still referenced the person after erasure ran.

    Raised in strict mode so the transaction rolls back and a human looks at
    it, instead of a partially erased person sitting in the database looking
    fine.
    """


def _normalize(email: str) -> str:
    text = (email or "").strip().lower()
    if not text:
        raise ValueError("delete_person requires an email address")
    return text


def _canonical_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    try:
        from mazz.integrations.linkedin import canonical_profile_url

        return canonical_profile_url(url).lower()
    except Exception:  # noqa: BLE001 - a junk URL simply gives us no second net
        return None


# --------------------------------------------------------------------------
# Finding every reference
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PersonReferences:
    """Every row that still points at this person, across all five tables."""

    email: str
    lead_ids: list[int] = field(default_factory=list)
    draft_ids: list[int] = field(default_factory=list)
    job_ids: list[int] = field(default_factory=list)
    sent_ids: list[int] = field(default_factory=list)
    #: ``(company_domain, fact_id, why)`` for each surviving person-specific fact.
    cache_facts: list[tuple[str, str, str]] = field(default_factory=list)
    #: Reported, not counted as a residual: a never-contact record is meant to
    #: outlive the lead.
    suppression_ids: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Everything that must be gone after erasure. Excludes suppression."""

        return (
            len(self.lead_ids)
            + len(self.draft_ids)
            + len(self.job_ids)
            + len(self.sent_ids)
            + len(self.cache_facts)
        )

    def describe(self) -> str:
        return (
            f"leads={self.lead_ids} drafts={self.draft_ids} jobs={self.job_ids} sent={self.sent_ids} "
            f"cache_facts={self.cache_facts}"
        )


def _person_fact_reason(
    fact: Any, email: str, linkedin_urls: set[str]
) -> str | None:
    """Why this stored fact belongs to the person, or None if it does not."""

    if not isinstance(fact, dict):
        return None
    subject = fact.get("subject_email")
    if isinstance(subject, str) and subject.strip().lower() == email:
        return "subject_email"
    source = fact.get("source_url")
    if isinstance(source, str) and source.strip().lower().rstrip("/") in {
        u.rstrip("/") for u in linkedin_urls
    }:
        return "linkedin source_url"
    return None


def find_person_references(
    session: Session,
    email: str,
    *,
    linkedin_urls: set[str] | None = None,
) -> PersonReferences:
    """Locate every row referencing a person. Used by erasure and to verify it.

    ``research_cache`` is scanned in full rather than filtered with a JSONB
    containment operator. Containment is case-sensitive, and a fact stamped
    ``Dana@Foo.com`` by some future adapter would slip through a
    ``@> '[{"subject_email":"dana@foo.com"}]'`` probe and survive a deletion
    request. Missing a row here is a compliance failure; a sequential scan over
    a few hundred company rows is not a performance problem.
    """

    email = _normalize(email)
    refs = PersonReferences(email=email)

    leads = (
        session.execute(select(Lead).where(func.lower(Lead.email) == email))
        .scalars()
        .all()
    )
    refs.lead_ids = [lead.id for lead in leads]

    urls = set(linkedin_urls or set())
    for lead in leads:
        canonical = _canonical_linkedin(lead.linkedin_url)
        if canonical:
            urls.add(canonical)

    if refs.lead_ids:
        refs.draft_ids = list(
            session.execute(
                select(Draft.id).where(Draft.lead_id.in_(refs.lead_ids))
            ).scalars()
        )
        refs.job_ids = list(
            session.execute(
                select(Job.id).where(Job.lead_id.in_(refs.lead_ids))
            ).scalars()
        )

    refs.sent_ids = list(
        session.execute(
            select(SentMessage.id).where(
                or_(
                    func.lower(SentMessage.to_address) == email,
                    SentMessage.lead_id.in_(refs.lead_ids or [-1]),
                )
            )
        ).scalars()
    )

    for cache in session.execute(select(ResearchCache)).scalars():
        for fact in cache.facts or []:
            why = _person_fact_reason(fact, email, urls)
            if why:
                refs.cache_facts.append(
                    (cache.company_domain, str(fact.get("id")), why)
                )

    refs.suppression_ids = list(
        session.execute(
            select(SuppressionEntry.id).where(
                func.lower(func.coalesce(SuppressionEntry.email, "")) == email
            )
        ).scalars()
    )
    return refs


# --------------------------------------------------------------------------
# Erasure
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DeletionReport:
    email: str
    leads_deleted: int = 0
    lead_ids: list[int] = field(default_factory=list)
    drafts_deleted: int = 0
    jobs_deleted: int = 0
    cache_rows_scanned: int = 0
    cache_rows_pruned: int = 0
    facts_pruned: int = 0
    pruned_fact_ids: list[str] = field(default_factory=list)
    suppression_ids: list[int] = field(default_factory=list)
    suppression_deleted: int = 0
    residual: PersonReferences | None = None

    @property
    def complete(self) -> bool:
        return self.residual is not None and self.residual.total == 0

    @property
    def found_anything(self) -> bool:
        return bool(self.leads_deleted or self.facts_pruned)

    def summary(self) -> str:
        kept = (
            f"; kept {len(self.suppression_ids)} suppression entr"
            f"{'y' if len(self.suppression_ids) == 1 else 'ies'}"
            if self.suppression_ids and not self.suppression_deleted
            else ""
        )
        return (
            f"{self.email}: deleted {self.leads_deleted} lead(s), "
            f"{self.drafts_deleted} draft(s), {self.jobs_deleted} job(s); "
            f"pruned {self.facts_pruned} fact(s) from {self.cache_rows_pruned} "
            f"of {self.cache_rows_scanned} cache row(s); "
            f"residual references: {self.residual.total if self.residual else '?'}"
            f"{kept}"
        )


def _session(session: Session | None) -> AbstractContextManager[Session]:
    return nullcontext(session) if session is not None else session_scope()


def delete_person(
    email: str,
    *,
    session: Session | None = None,
    remove_suppression: bool = False,
    strict: bool = True,
    added_by: str = "system",
) -> DeletionReport:
    """Erase everything this system holds about one person.

    Removes the ``leads`` row (taking ``drafts`` and ``jobs`` with it by
    cascade) and prunes that person's facts out of every shared, company-keyed
    ``research_cache`` row, leaving the company research intact for colleagues.

    Pass ``session`` to run inside an existing transaction; otherwise it opens
    and commits its own. With ``strict`` (the default) a surviving reference
    raises :class:`DeletionIncomplete`, rolling the work back so the failure is
    impossible to miss.
    """

    email = _normalize(email)

    with _session(session) as sess:
        # 1. Enumerate first: once the lead row is gone we can no longer learn
        #    the person's LinkedIn URL, and that is the second net for facts an
        #    adapter forgot to stamp.
        before = find_person_references(sess, email)
        linkedin_urls = set()
        for lead_id in before.lead_ids:
            lead = sess.get(Lead, lead_id)
            canonical = _canonical_linkedin(lead.linkedin_url if lead else None)
            if canonical:
                linkedin_urls.add(canonical)

        report = DeletionReport(
            email=email,
            lead_ids=list(before.lead_ids),
            drafts_deleted=len(before.draft_ids),
            jobs_deleted=len(before.job_ids),
            suppression_ids=list(before.suppression_ids),
        )

        # 2. Prune the shared, company-keyed research rows. The row stays; the
        #    person comes out of it.
        caches = (
            sess.execute(select(ResearchCache).with_for_update()).scalars().all()
        )
        report.cache_rows_scanned = len(caches)
        for cache in caches:
            kept: list[Any] = []
            removed: list[str] = []
            for fact in cache.facts or []:
                why = _person_fact_reason(fact, email, linkedin_urls)
                if why:
                    removed.append(str(fact.get("id")))
                else:
                    kept.append(fact)
            if removed:
                cache.facts = kept
                report.cache_rows_pruned += 1
                report.facts_pruned += len(removed)
                report.pruned_fact_ids.extend(removed)

        # 3. Delete the lead. drafts, jobs and sent_messages go with it
        #    (ON DELETE CASCADE). Dry-run MIME on disk carries the address and
        #    body too, so it goes as well.
        from mazz.pipeline.send import dry_run_dir

        for lead_id in before.lead_ids:
            lead = sess.get(Lead, lead_id)
            if lead is not None:
                sess.delete(lead)
                report.leads_deleted += 1
            for eml in dry_run_dir().glob(f"*-lead{lead_id}-draft*.eml"):
                eml.unlink(missing_ok=True)

        # 4. Suppression entries are kept unless explicitly asked otherwise --
        #    forgetting that we were told not to contact someone is how they
        #    get contacted again.
        if remove_suppression:
            for entry_id in before.suppression_ids:
                entry = sess.get(SuppressionEntry, entry_id)
                if entry is not None:
                    sess.delete(entry)
                    report.suppression_deleted += 1

        # 5. ...and there is ALWAYS a tombstone afterwards. The next ZoomInfo
        #    pull will contain this person again; without an entry, erasure
        #    is followed by a fresh cold email. Default: the normalised email
        #    plus its sha256. With remove_suppression (forget them entirely):
        #    the sha256 alone, which still matches at every gate but no
        #    longer names anyone.
        sess.flush()
        digest = email_sha256(email)
        has_tombstone = sess.execute(
            select(SuppressionEntry.id).where(
                SuppressionEntry.workspace_id.is_(None),
                (func.lower(func.coalesce(SuppressionEntry.email, "")) == email)
                | (SuppressionEntry.email_sha256 == digest),
            )
        ).first()
        if has_tombstone is None:
            sess.add(
                SuppressionEntry(
                    email=None if remove_suppression else email,
                    email_sha256=digest,
                    reason="erased on request (delete-by-person)",
                    added_by=added_by,
                )
            )

        sess.flush()

        # 5. Verify. Erasure is not done because the code ran; it is done
        #    because nothing is left.
        report.residual = find_person_references(sess, email, linkedin_urls=linkedin_urls)
        if report.residual.total:
            log.error(
                "delete_person(%s) left references behind: %s",
                email,
                report.residual.describe(),
            )
            if strict:
                raise DeletionIncomplete(
                    f"delete_person({email!r}) left references behind: "
                    f"{report.residual.describe()}"
                )
        else:
            log.info("delete_person(%s): %s", email, report.summary())

    return report
