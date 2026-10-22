"""Cutover from Smartlead: import its three CSV exports, then adopt its threads.

    python -m mazz.ingest.smartlead suppression  <csv>                    (a)
    python -m mazz.ingest.smartlead exclusions   <csv> --workspace <slug> (c)
    python -m mazz.ingest.smartlead leads        <csv> --campaign <id>    (b)
    python -m mazz.ingest.smartlead adopt

Every command is a dry run unless given ``--commit``: it does all the work in a
transaction and rolls it back, printing what it would have done.

The trap this module exists for: Smartlead emailed the contacted leads from
the SAME four Gmail mailboxes, so we hold no ``sent_messages`` row for them.
Without one, the draft stage would happily write them Email 1 again, a
follow-up could not thread, and reply sync (which only reads threads we sent)
would ignore their replies. So:

* ``leads`` records each contacted lead with a ``legacy`` sent row (no Gmail
  ids yet). That row alone makes Email 1 impossible (`draft.persist_draft`
  refuses anyone with a sent row) and queues nothing.
* ``adopt`` searches the sending mailboxes read-only for the Smartlead
  message, records its Gmail thread, reads the thread once for replies that
  landed before cutover, and only THEN queues the next follow-up. A lead
  whose thread cannot be found is held; `send.send_lead` independently refuses
  any follow-up whose parent has no thread, so nothing unthreaded can leave.

Every command may be re-run, and the files may arrive in any order: a replay
changes nothing already imported, and a suppression or exclusion file that
lands after the leads holds whoever it covers and cancels their queued work.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import and_, delete, or_, select

from mazz import audit
from mazz.ingest.csv_import import (
    SuppressionChecker,
    SuppressionUnavailable,
    _iter_csv,
    _upsert_lead,
    email_sha256,
    import_rows,
)
from mazz.ingest.validate import (
    RowValidationError,
    canonical_field,
    normalize_email,
    normalize_row,
    validate_row,
)
from mazz.models import (
    Campaign,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    SentMessage,
    SuppressionEntry,
    Workspace,
)
from mazz.pipeline.routing import PinnedRoutingPolicy
from mazz.pipeline.sequence import (
    FOLLOWUP_JOB_KIND,
    LIVE_STATES,
    due_after,
    lead_timezone,
    step_for,
)

ACTOR = "import:smartlead"
#: Email 1's subject in the live Smartlead campaign; the adopt search uses it.
LEGACY_SUBJECT = "Quick Question"

#: Our meaning -> the Smartlead export headers that may carry it. UNCONFIRMED:
#: guessed from Smartlead's API field names, because no real export has been
#: seen yet. Matching ignores case and punctuation. Fix here when one arrives.
COLUMNS: dict[str, tuple[str, ...]] = {
    "status": ("status", "lead_status"),
    "emails_sent": ("last_email_sequence_sent", "emails_sent", "sequence_step", "sent_count"),
    "last_sent_at": ("last_sent_time", "last_email_sent_at", "last_sent_at", "last_sent"),
    "replied": ("reply_count", "replied", "is_replied", "replies"),
    "domain": ("domain", "company_domain", "website", "company_url"),
    # Narrower still: these two names exist only in the audit tests. A truthy
    # flag suppresses the address globally, as file (a) would.
    "unsubscribed": ("is_unsubscribed",),
    "bounced": ("is_bounced",),
}

#: Smartlead lead status (normalised: lower case, letters only) -> bucket.
#: Only an unambiguous "not started" is treated as safe to email from scratch,
#: and only "in_progress" may continue a sequence. Anything unlisted
#: ("started", "replied", ...) is held for a person if it has a sent count and
#: refused if it does not; either way --commit refuses until it is mapped here.
STATUSES = {
    "notstarted": "not_started", "notyetcontacted": "not_started",
    "inprogress": "in_progress", "paused": "in_progress",
    "completed": "completed",
    "blocked": "blocked", "stopped": "blocked",
}

#: Emails Smartlead already sent -> the step of OURS that comes next. A count
#: not listed (3 or more) means the sequence is complete. Maaz may change it.
NEXT_STEP_AFTER: dict[int, int] = {1: 2, 2: 3}

#: The client's figures for the live campaign, for the sanity check (read off
#: the Smartlead screen 2026-10-06). The campaign was still sending then, so
#: re-read them after pausing it on cutover day and update this line.
EXPECTED = {"total": 864, "contacted": 223, "not_started": 641,
            "in_progress": 120, "completed": 101, "blocked": 2}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def column(row: Mapping[str, Any], name: str) -> str:
    wanted = {_key(h) for h in COLUMNS[name]}
    for header, value in row.items():
        if _key(str(header)) in wanted and str(value or "").strip():
            return str(value).strip()
    return ""


def _int(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def _truthy(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("true", "yes", "y", "replied") or (_int(t) or 0) > 0


def _when(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# (a) unsubscribes and bounces -> global suppression
# (c) Sean's clients and named companies -> his workspace only
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Report:
    counts: Counter = field(default_factory=Counter)
    lines: list[str] = field(default_factory=list)

    def note(self, bucket: str, line: str | None = None) -> None:
        self.counts[bucket] += 1
        if line:
            self.lines.append(line)


def _email_of(row: Mapping[str, Any]) -> str:
    """The row's address, read through the lead importer's exact header
    aliases ("Email", "Email Address", ...). Raises `RowValidationError`."""

    return normalize_email(normalize_row(row).get("email"))


def _blank(row: Mapping[str, Any]) -> bool:
    return not any(str(v or "").strip() for v in row.values())


_STOPPABLE = (LeadStatus.RESEARCHING, LeadStatus.READY, LeadStatus.APPROVED)


def _hold_suppressed(session, report: Report) -> None:
    """Re-run the gate over every lead with work still ahead of it. The files
    may be imported in any order, and an entry that lands after the leads it
    covers must not leave an enrich job or a follow-up queued for them. (The
    send step re-checks too; this keeps the work out of review.) An unreadable
    list raises, so the whole import fails closed."""

    checker = SuppressionChecker(session)
    pending = select(Job.lead_id).where(Job.status == JobStatus.PENDING)
    live = select(Lead).where(or_(Lead.status.in_(_STOPPABLE), Lead.sequence_state.in_(LIVE_STATES),
                                  Lead.id.in_(pending)))
    # ponytail: one gate query per live lead; fine at cutover size (~1k leads).
    for lead in session.execute(live).scalars().all():
        if checker.check_lead(lead) is None:
            continue
        if lead.status in _STOPPABLE:
            lead.status = LeadStatus.HELD
        if lead.sequence_state in LIVE_STATES:
            lead.sequence_state, lead.next_step_due_at = "stopped", None
        lead.skip_reason = "suppressed"
        session.execute(delete(Job).where(Job.lead_id == lead.id, Job.status == JobStatus.PENDING))
        audit.record(session, ACTOR, "hold_suppressed", "lead", lead.id)
        report.note("held_existing", f"{lead.email}: imported earlier, now suppressed; "
                                     "held and its queued work cancelled")
    session.flush()


def import_suppression(session, rows: Iterable[Mapping[str, Any]]) -> Report:
    """Every address in Smartlead's unsubscribe/bounce export, suppressed
    globally. A row we cannot read is someone who may have asked to stop, so
    it blocks --commit (`BLOCKING`) rather than being skipped."""

    from mazz.web.routes.public import suppress_email

    report = Report()
    for n, row in enumerate(rows, start=2):
        if _blank(row):
            continue
        try:
            email = _email_of(row)
        except RowValidationError as exc:
            report.note("invalid", f"row {n}: {exc.reason}: {dict(row)}")
            continue
        before = session.execute(select(SuppressionEntry.id).where(
            SuppressionEntry.workspace_id.is_(None),
            SuppressionEntry.email_sha256 == email_sha256(email))).first()
        suppress_email(session, email, reason="smartlead export (unsubscribe/bounce)", actor=ACTOR)
        report.note("already" if before else "added")
    _hold_suppressed(session, report)
    return report


def import_exclusions(session, rows: Iterable[Mapping[str, Any]], workspace: Workspace) -> Report:
    """Sean's clients and named companies: blocked for his workspace only.

    A domain blocks the whole company (subdomains too, by the gate's own
    matching); an email blocks that address. A row with both gets both, so a
    client's colleagues are covered and so is an address off the company
    domain. A row with neither cannot be enforced: it blocks --commit."""

    from mazz.ingest.validate import derive_company_domain

    report = Report()
    for n, row in enumerate(rows, start=2):
        if _blank(row):
            continue
        email = domain = None
        try:
            if normalize_row(row).get("email"):
                email = _email_of(row)
            if column(row, "domain"):
                domain = derive_company_domain(column(row, "domain"))
        except RowValidationError as exc:
            report.note("invalid", f"row {n}: {exc.reason}: {dict(row)}")
            continue
        if not (email or domain):
            report.note("no_domain", f"row {n}: no email or domain, cannot block: {dict(row)}")
            continue
        scoped = SuppressionEntry.workspace_id == workspace.id
        new = []
        if email and not session.execute(select(SuppressionEntry.id).where(
                scoped, SuppressionEntry.email == email)).first():
            new.append(SuppressionEntry(email=email, email_sha256=email_sha256(email)))
        if domain and not session.execute(select(SuppressionEntry.id).where(
                scoped, SuppressionEntry.domain == domain)).first():
            new.append(SuppressionEntry(domain=domain))
        for entry in new:
            entry.workspace_id, entry.added_by = workspace.id, ACTOR
            entry.reason = "smartlead export: existing client / named company"
            session.add(entry)
        session.flush()
        report.note("added" if new else "already")
    _hold_suppressed(session, report)
    return report


# --------------------------------------------------------------------------
# (b) the campaign lead export
# --------------------------------------------------------------------------


def import_leads(session, rows: Iterable[Mapping[str, Any]], campaign: Campaign,
                 *, now: datetime | None = None) -> Report:
    """Each source row ends in exactly one outcome bucket (`OUTCOMES`)."""

    from mazz.web.routes.public import suppress_email

    now = now or datetime.now(timezone.utc)
    report = Report()
    fresh: list[Mapping[str, Any]] = []
    seen: dict[str, tuple[int, tuple]] = {}
    checker = SuppressionChecker(session)
    rows = list(rows)
    headers = {_key(str(h)) for r in rows[:1] for h in r}
    for needed in ("status", "emails_sent"):
        if rows and not headers & {_key(h) for h in COLUMNS[needed]}:
            # Without these every contacted lead would look fresh and get Email 1.
            raise ValueError(f"no {needed!r} column among {sorted(headers)}; "
                             f"add the real header to smartlead.COLUMNS[{needed!r}]")

    for n, row in enumerate(rows, start=2):
        report.note("total")
        status = re.sub(r"[^a-z]", "", column(row, "status").lower())
        bucket = STATUSES.get(status)
        sent = _int(column(row, "emails_sent"))
        report.note(bucket or "unknown_status")
        if bucket != "not_started" or (sent or 0) > 0:
            report.note("contacted")
        try:
            email: str | None = _email_of(row)
        except RowValidationError:
            email = None  # reported by whichever path the row takes below

        if _truthy(column(row, "unsubscribed")) or _truthy(column(row, "bounced")):
            if email is None:
                report.note("MUST_FIX", f"row {n}: flagged unsubscribed/bounced, no readable email")
            else:
                # The sweep at the end holds this person if already imported.
                suppress_email(session, email, reason="smartlead export flag (unsubscribed/bounced)",
                               actor=ACTOR)
                report.note("suppressed", f"row {n}: {email} flagged unsubscribed/bounced; suppressed")
            continue

        # One outcome per person: a repeated row is reported, never applied twice.
        sig = (bucket, sent, _truthy(column(row, "replied")))
        if email in seen:
            first, first_sig = seen[email]
            report.note("duplicate", f"row {n}: {email} repeats row {first}")
            if sig != first_sig:
                report.note("duplicate_conflict", f"row {n}: {email} disagrees with row {first} "
                                                  "on status/sent/replied; dedupe the export")
            continue
        if email:
            seen[email] = (n, sig)

        if bucket == "not_started" and not sent:
            if email and session.execute(select(Lead.id).where(Lead.email == email)).first():
                # A replay, or someone another import brought in: whatever
                # the pipeline has done with them since stands.
                report.note("already")
                continue
            fresh.append(row)
            continue
        if bucket is None and not sent:
            # Could have been emailed; we cannot tell. Never treat as fresh.
            report.note("refused", f"row {n}: unknown status {column(row, 'status')!r} "
                                   "and no sent count: not imported")
            continue
        _legacy(session, checker, report, n, row, campaign, bucket, sent, now)

    out = import_rows(session, fresh, policy=PinnedRoutingPolicy(campaign.id))
    report.counts["new_queued"] += len(out.job_ids)
    for rej in out.rejected:
        report.note("new_" + rej.kind, f"not-started {rej}")
    _hold_suppressed(session, report)
    return report


#: Body of a legacy row whose export gave no sent count: it exists only so
#: Email 1 can never go, and must not read as a confirmed send.
UNCONFIRMED_SEND = "(no send confirmed: the Smartlead export gave no sent count)"


def _legacy(session, checker, report, n, row, campaign, bucket, sent, now) -> None:
    try:
        validated = validate_row(row)
    except RowValidationError as exc:
        # Loud: a contacted person we failed to record could get Email 1 from
        # a later ZoomInfo import. Fix the row and re-run.
        report.note("MUST_FIX", f"row {n}: CONTACTED but not importable: {exc.reason}")
        return
    try:
        with session.begin_nested():  # a failed read must not poison the transaction
            match = checker.check(validated.email, validated.company_domain,
                                  workspace_id=campaign.workspace_id)
    except SuppressionUnavailable as exc:
        report.note("refused", f"row {n}: {exc}")
        return
    if match is not None:
        report.note("suppressed")
        return

    existing = session.execute(select(Lead).where(Lead.email == validated.email)).scalar_one_or_none()
    if existing is not None:
        other = session.get(Campaign, existing.campaign_id) if existing.campaign_id else None
        if other is not None and other.workspace_id != campaign.workspace_id:
            # Never move a person between clients' workspaces on an import.
            report.note("refused", f"row {n}: {validated.email} is in campaign {other.id} of "
                                   "another workspace; not moved")
            return
        prior = session.execute(select(SentMessage.legacy).where(
            SentMessage.lead_id == existing.id)).scalars().all()
        if prior:
            report.note("already" if all(prior) else "conflict",
                        None if all(prior) else f"row {n}: {validated.email} was already emailed by us")
            return

    lead, _ = _upsert_lead(session, validated)
    # Anything this system queued for them before it knew is cancelled.
    session.execute(delete(Job).where(Job.lead_id == lead.id, Job.status == JobStatus.PENDING))
    lead.campaign_id = campaign.id
    lead.status = LeadStatus.SENT
    lead.next_step_due_at = None
    raw_when = column(row, "last_sent_at")
    last_sent = _when(raw_when)
    if raw_when and last_sent is None:
        report.note("bad_last_sent", f"row {n}: unreadable last-sent time {raw_when!r}; "
                                     "the import time stands in until adopt reads Gmail's")
    last_sent = last_sent or now  # provisional; adopt reads Gmail's

    replied = _truthy(column(row, "replied"))
    nxt = NEXT_STEP_AFTER.get(sent or 0)
    if replied or bucket == "blocked":
        lead.sequence_state, lead.sequence_step = "stopped", max(sent or 1, 1)
        report.note("stopped_replied" if replied else "stopped_blocked")
    elif not sent:
        lead.sequence_state, lead.skip_reason = "stopped", "legacy_step_unknown"
        report.note("MUST_FIX", f"row {n}: contacted but no sent count; stopped")
    elif bucket not in ("in_progress", "completed"):
        # Emailed, and no status says the sequence may go on (unknown, or
        # "not started" with a sent count). Never resumed: a person decides.
        lead.status, lead.skip_reason = LeadStatus.HELD, "legacy_status_unclear"
        lead.sequence_state, lead.sequence_step = "stopped", sent
        report.note("held_unclear", f"row {n}: status {column(row, 'status')!r} with {sent} "
                                    "sent; held, never resumed")
    elif bucket == "completed" or nxt is None or step_for(session, campaign.id, nxt) is None:
        # Smartlead ended it (whatever the count says), or we have no next step.
        lead.sequence_state, lead.sequence_step = "complete", sent
        report.note("complete")
    else:
        lead.sequence_state, lead.sequence_step = "active", nxt - 1
        lead.next_step_due_at = due_after(
            last_sent, step_for(session, campaign.id, nxt).wait_days, lead_timezone(lead))
        report.note("awaiting_adoption")

    session.add(SentMessage(
        lead_id=lead.id, draft_id=None, mailbox_id=None, legacy=True,
        from_address="", to_address=lead.email, subject=LEGACY_SUBJECT,
        body="" if sent else UNCONFIRMED_SEND, sent_at=last_sent))
    session.flush()
    audit.record(session, ACTOR, "legacy_import", "lead", lead.id,
                 emails_sent=sent, state=lead.sequence_state)


# --------------------------------------------------------------------------
# Thread adoption
# --------------------------------------------------------------------------


#: Holds `adopt` itself puts on a live lead. A later run retries them (a
#: mailbox reconnected, a stray thread cleaned up) and lifts the hold when the
#: thread is found. Any other hold is a person's to lift.
RETRYABLE_HOLDS = ("legacy_thread_not_found", "legacy_thread_ambiguous")


def adopt(session, *, service_for: Callable[[str], Any], now: datetime | None = None) -> Report:
    """Find each legacy lead's Smartlead message in its mailbox (read-only)
    and make the thread ours. Then, and only then, queue a live lead's next
    follow-up. A finished one is adopted too, so a late reply on its thread
    is read, but nothing is ever queued for it."""

    from mazz.integrations import gmail_reader as gr
    from mazz.pipeline.queue import enqueue_job
    from mazz.pipeline.replies import SyncReport, WrongAccount, process_message

    now = now or datetime.now(timezone.utc)
    report = Report()
    boxes = session.execute(select(Mailbox).where(
        Mailbox.status.in_((MailboxStatus.ACTIVE, MailboxStatus.PAUSED)),
        Mailbox.oauth_refresh_token_enc.is_not(None),
    ).order_by(Mailbox.id)).scalars().all()
    services = {}
    for box in boxes:
        svc = service_for(box.email_address)
        if str(gr.profile(svc).get("emailAddress", "")).lower() != box.email_address.lower():
            raise WrongAccount(f"token for {box.email_address} reads as another account")
        services[box.id] = svc
    # A mailbox we cannot read may hold the thread. "Not found" there means
    # "not looked", which must not become a hold.
    unsearched = session.execute(select(Mailbox.email_address).where(
        Mailbox.id.notin_([b.id for b in boxes])).order_by(Mailbox.id)).scalars().all()

    leads = session.execute(
        select(Lead).join(SentMessage, SentMessage.lead_id == Lead.id).where(
            SentMessage.legacy.is_(True), SentMessage.provider_thread_id.is_(None),
            or_(Lead.status == LeadStatus.SENT,
                and_(Lead.status == LeadStatus.HELD, Lead.skip_reason.in_(RETRYABLE_HOLDS))),
        ).distinct().order_by(Lead.id)
    ).scalars().all()

    checker = SuppressionChecker(session)
    for lead in leads:
        live = lead.sequence_state in LIVE_STATES
        # Suppressed since import: stop now rather than queue a follow-up the
        # send step would refuse.
        try:
            with session.begin_nested():
                match = checker.check_lead(lead)
        except SuppressionUnavailable as exc:
            report.note("refused", f"{lead.email}: {exc}; not adopted")
            continue
        if match is not None:
            if live:
                lead.sequence_state, lead.next_step_due_at = "stopped", None
                lead.skip_reason = "suppressed"
            report.note("suppressed", f"{lead.email}: suppressed since import; stopped, not adopted")
            continue

        query = f'in:sent to:{lead.email} subject:"{LEGACY_SUBJECT}"'
        found = []
        for box in boxes:
            listing = services[box.id].users().messages().list(
                userId="me", q=query, maxResults=20).execute()
            msgs = [gr.get_message(services[box.id], m["id"]) for m in listing.get("messages") or []]
            if msgs:
                found.append((box, msgs))
        if len(found) != 1:
            what = "not_found" if not found else "ambiguous"
            if not live:
                # Finished: nothing will be sent, so nothing to hold. Its late
                # replies go unread here until a re-run finds the thread.
                report.note(f"history_{what}", f"{lead.email}: legacy thread {what}; "
                                               f"sequence {lead.sequence_state}, nothing queued")
            elif not found and unsearched:
                report.note("legacy_thread_unsearched",
                            f"{lead.email}: not in the mailboxes read, and {', '.join(unsearched)} "
                            "could not be read; nothing queued. Re-run adopt once reconnected")
            else:
                lead.status, lead.skip_reason = LeadStatus.HELD, f"legacy_thread_{what}"
                report.note(lead.skip_reason, f"{lead.email}: {lead.skip_reason}; held")
            continue

        box, msgs = found[0]
        newest = max(msgs, key=lambda m: m.received_at)
        chain = sorted((m for m in msgs if m.thread_id == newest.thread_id),
                       key=lambda m: m.received_at)
        session.execute(delete(SentMessage).where(
            SentMessage.lead_id == lead.id, SentMessage.legacy.is_(True),
            SentMessage.provider_thread_id.is_(None)))
        for m in chain:
            session.add(SentMessage(
                lead_id=lead.id, draft_id=None, mailbox_id=box.id, legacy=True,
                from_address=box.email_address, to_address=lead.email, subject=m.subject,
                body=m.text[:5000], rfc_message_id=m.headers.get("message-id"),
                provider_message_id=m.message_id, provider_thread_id=m.thread_id,
                sent_at=m.received_at))
        if lead.status is LeadStatus.HELD:  # one of adopt's own holds, now resolved
            lead.status, lead.skip_reason = LeadStatus.SENT, None
        # Gmail over the export: a count read before the pause can be behind,
        # and every send we can see in the thread went out. Fewer than the
        # export says never rewinds: a step may be skipped, never repeated.
        lead.sequence_step = max(lead.sequence_step or 1, len(chain))
        session.flush()

        # Replies that landed before cutover: Smartlead's export predates them.
        sync = SyncReport()
        for msg in gr.thread_messages(services[box.id], newest.thread_id):
            process_message(session, box, msg, sync)
        if not live:
            report.note("adopted_history", f"{lead.email}: {len(chain)} message(s) in "
                                           f"{box.email_address}; sequence {lead.sequence_state}")
            continue
        if lead.sequence_state not in LIVE_STATES:
            report.note("adopted_replied", f"{lead.email}: replied before cutover; stopped")
            continue

        step = step_for(session, lead.campaign_id, (lead.sequence_step or 1) + 1)
        if step is None:
            lead.sequence_state = "complete"
            report.note("adopted_complete")
            continue
        due = due_after(newest.received_at, step.wait_days, lead_timezone(lead))
        if lead.sequence_state == "paused":
            # Out of office: the later of the return date and the step's own
            # due date. `handle_followup` waits out the pause, then resumes.
            due = max(due, lead.next_step_due_at or due)
            report.note("adopted_paused")
        lead.next_step_due_at = due
        enqueue_job(session, lead.id, FOLLOWUP_JOB_KIND, run_after=due)
        audit.record(session, ACTOR, "legacy_thread_adopted", "lead", lead.id,
                     mailbox=box.email_address, messages=len(chain))
        report.note("adopted", f"{lead.email}: {len(chain)} message(s) in {box.email_address}, "
                               f"step {step.step} due {due:%Y-%m-%d}")
    session.flush()
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


#: Buckets that mean a person was not handled as the export says they should
#: be. Row-level: ``--accept-mismatch`` never waives them.
BLOCKING = ("MUST_FIX", "refused", "unknown_status", "held_unclear", "duplicate_conflict",
            "invalid", "no_domain")

#: The terminal buckets of `import_leads`: every source row lands in one.
OUTCOMES = ("awaiting_adoption", "complete", "stopped_replied", "stopped_blocked", "held_unclear",
            "already", "conflict", "suppressed", "refused", "MUST_FIX", "duplicate",
            "new_queued", "new_invalid", "new_suppressed", "new_suppression_unavailable",
            "new_error")


def blockers(report: Report, expected: Mapping[str, int] | None = None) -> list[str]:
    """Why ``--commit`` should refuse: a wrong guess about the export's headers
    shows up here first, and a wrong guess can mean Email 1 to someone already
    emailed. Row blockers first, then any ``MISMATCH`` against ``expected``
    (the only kind ``--accept-mismatch`` waives)."""

    out = [f"{k}: {report.counts[k]}" for k in BLOCKING if report.counts.get(k)]
    out += [f"MISMATCH {k}: got {report.counts.get(k, 0)}, the client's figure is {want}"
            for k, want in (expected or {}).items() if report.counts.get(k, 0) != want]
    return out


def duplicate_headers(text: str) -> list[str]:
    """Headers we read that appear twice: a CSV row keeps only the last one."""

    header = next(csv.reader(io.StringIO(text.lstrip("﻿"))), [])
    ours = {_key(alias) for names in COLUMNS.values() for alias in names}
    return sorted({h for h in header if header.count(h) > 1
                   and (canonical_field(h) or _key(h) in ours)})


def _print(report: Report, problems: list[str]) -> None:
    for line in report.lines:
        print("  " + line)
    for k, v in sorted(report.counts.items()):
        print(f"{k:>22}: {v}")
    for problem in problems:
        print(f"!!! {problem} !!!")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mazz.ingest.smartlead")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("suppression", "exclusions", "leads"):
        p = sub.add_parser(name)
        p.add_argument("csv", type=Path)
    sub.choices["exclusions"].add_argument("--workspace", required=True, help="workspace slug")
    sub.choices["leads"].add_argument("--campaign", required=True, type=int, help="campaign id")
    sub.add_parser("adopt")
    for p in sub.choices.values():
        p.add_argument("--commit", action="store_true", help="write (default: dry run)")
        p.add_argument("--accept-mismatch", action="store_true",
                       help="commit although the totals differ from the client's figures "
                            "(row-level problems are never waived)")
    args = parser.parse_args(argv)

    text = args.csv.read_text(encoding="utf-8-sig") if args.cmd != "adopt" else ""
    if duplicate_headers(text):
        parser.error(f"duplicate column(s) {duplicate_headers(text)}: a row would keep only "
                     "the last; remove the extra column and re-run")

    from mazz.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        rows = lambda: list(_iter_csv(text))  # noqa: E731
        if args.cmd == "suppression":
            report, expected = import_suppression(session, rows()), None
        elif args.cmd == "exclusions":
            ws = session.execute(select(Workspace).where(Workspace.slug == args.workspace)).scalar_one()
            report, expected = import_exclusions(session, rows(), ws), None
        elif args.cmd == "leads":
            campaign = session.get(Campaign, args.campaign)
            if campaign is None:
                parser.error(f"no campaign {args.campaign}")
            report, expected = import_leads(session, rows(), campaign), EXPECTED
        else:
            from mazz.integrations.google_oauth import service_for_mailbox

            report, expected = adopt(session, service_for=service_for_mailbox), None
        problems = blockers(report, expected)
        row_problems = blockers(report)
        _print(report, problems)
        if args.commit and (row_problems or (problems and not args.accept_mismatch)):
            session.rollback()
            print("NOT committed: " + (
                "fix the rows above in the export (or smartlead.COLUMNS / STATUSES) and "
                "re-run; row-level problems are never waived." if row_problems else
                "the totals differ from the client's figures; re-run with --accept-mismatch "
                "only after checking every line above."))
            return 2
        if args.commit:
            session.commit()
            print("committed.")
        else:
            session.rollback()
            print("dry run: nothing written. Re-run with --commit.")
    finally:
        session.close()
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
