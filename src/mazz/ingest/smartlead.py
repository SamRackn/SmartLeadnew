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
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import delete, select

from mazz import audit
from mazz.ingest.csv_import import (
    SuppressionChecker,
    SuppressionUnavailable,
    _iter_csv,
    _upsert_lead,
    email_sha256,
    import_rows,
)
from mazz.ingest.validate import RowValidationError, normalize_email, validate_row
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
from mazz.pipeline.sequence import FOLLOWUP_JOB_KIND, due_after, lead_timezone, step_for

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
}

#: Smartlead lead status (normalised: lower case, letters only) -> bucket.
#: Only an unambiguous "not started" is treated as safe to email from scratch.
#: Anything unlisted ("started", say) is legacy if it has a sent count and
#: refused if it does not.
STATUSES = {
    "notstarted": "not_started", "notyetcontacted": "not_started",
    "inprogress": "in_progress", "paused": "in_progress",
    "completed": "completed",
    "blocked": "blocked", "stopped": "blocked",
}

#: Emails Smartlead already sent -> the step of OURS that comes next. A count
#: not listed (3 or more) means the sequence is complete. Maaz may change it.
NEXT_STEP_AFTER: dict[int, int] = {1: 2, 2: 3}

#: The client's figures for the live campaign, for the sanity check.
EXPECTED = {"total": 864, "contacted": 133, "not_started": 731,
            "in_progress": 124, "completed": 7, "blocked": 2}


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


def import_suppression(session, rows: Iterable[Mapping[str, Any]]) -> Report:
    """Every address in Smartlead's unsubscribe/bounce export, suppressed globally."""

    from mazz.web.routes.public import suppress_email

    report = Report()
    for n, row in enumerate(rows, start=2):
        try:
            email = normalize_email(row.get("email") or row.get("Email"))
        except RowValidationError as exc:
            report.note("invalid", f"row {n}: {exc.reason}")
            continue
        before = session.execute(select(SuppressionEntry.id).where(
            SuppressionEntry.workspace_id.is_(None),
            SuppressionEntry.email_sha256 == email_sha256(email))).first()
        suppress_email(session, email, reason="smartlead export (unsubscribe/bounce)", actor=ACTOR)
        report.note("already" if before else "added")
    return report


def import_exclusions(session, rows: Iterable[Mapping[str, Any]], workspace: Workspace) -> Report:
    """Sean's clients and named companies: blocked for his workspace only. A row
    with a company name but no domain or email cannot be enforced and is listed."""

    from mazz.ingest.validate import derive_company_domain

    report = Report()
    for n, row in enumerate(rows, start=2):
        email = domain = None
        raw_email = row.get("email") or row.get("Email")
        try:
            if raw_email:
                email = normalize_email(raw_email)
            elif column(row, "domain"):
                domain = derive_company_domain(column(row, "domain"))
        except RowValidationError as exc:
            report.note("invalid", f"row {n}: {exc.reason}")
            continue
        if not (email or domain):
            report.note("no_domain", f"row {n}: no email or domain, cannot block: {dict(row)}")
            continue
        exists = session.execute(select(SuppressionEntry.id).where(
            SuppressionEntry.workspace_id == workspace.id,
            (SuppressionEntry.email == email) if email else (SuppressionEntry.domain == domain),
        )).first()
        if exists:
            report.note("already")
            continue
        session.add(SuppressionEntry(
            email=email, email_sha256=email_sha256(email) if email else None, domain=domain,
            workspace_id=workspace.id, reason="smartlead export: existing client / named company",
            added_by=ACTOR))
        report.note("added")
    session.flush()
    return report


# --------------------------------------------------------------------------
# (b) the campaign lead export
# --------------------------------------------------------------------------


def import_leads(session, rows: Iterable[Mapping[str, Any]], campaign: Campaign,
                 *, now: datetime | None = None) -> Report:
    now = now or datetime.now(timezone.utc)
    report = Report()
    fresh: list[Mapping[str, Any]] = []
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

        if bucket == "not_started" and not sent:
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
    return report


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
    last_sent = _when(column(row, "last_sent_at")) or now  # provisional; adopt reads Gmail's

    replied = _truthy(column(row, "replied"))
    nxt = NEXT_STEP_AFTER.get(sent or 0)
    if replied or bucket == "blocked":
        lead.sequence_state, lead.sequence_step = "stopped", max(sent or 1, 1)
        report.note("stopped_replied" if replied else "stopped_blocked")
    elif not sent:
        lead.sequence_state, lead.skip_reason = "stopped", "legacy_step_unknown"
        report.note("MUST_FIX", f"row {n}: contacted but no sent count; stopped")
    elif nxt is None or step_for(session, campaign.id, nxt) is None:
        lead.sequence_state, lead.sequence_step = "complete", sent
        report.note("complete")
    else:
        lead.sequence_state, lead.sequence_step = "active", nxt - 1
        lead.next_step_due_at = due_after(
            last_sent, step_for(session, campaign.id, nxt).wait_days, lead_timezone(lead))
        report.note("awaiting_adoption")

    session.add(SentMessage(
        lead_id=lead.id, draft_id=None, mailbox_id=None, legacy=True,
        from_address="", to_address=lead.email, subject=LEGACY_SUBJECT, body="",
        sent_at=last_sent))
    session.flush()
    audit.record(session, ACTOR, "legacy_import", "lead", lead.id,
                 emails_sent=sent, state=lead.sequence_state)


# --------------------------------------------------------------------------
# Thread adoption
# --------------------------------------------------------------------------


def adopt(session, *, service_for: Callable[[str], Any], now: datetime | None = None) -> Report:
    """Find each live legacy lead's Smartlead message in its mailbox (read-only)
    and make the thread ours. Then, and only then, queue its next follow-up."""

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

    leads = session.execute(
        select(Lead).join(SentMessage, SentMessage.lead_id == Lead.id).where(
            SentMessage.legacy.is_(True), SentMessage.provider_thread_id.is_(None),
            Lead.sequence_state == "active", Lead.status == LeadStatus.SENT,
        ).distinct().order_by(Lead.id)
    ).scalars().all()

    checker = SuppressionChecker(session)
    for lead in leads:
        # Suppressed since import (e.g. `suppression` run after `leads`):
        # stop now rather than queue a follow-up the send step would refuse.
        try:
            with session.begin_nested():
                match = checker.check_lead(lead)
        except SuppressionUnavailable as exc:
            report.note("refused", f"{lead.email}: {exc}; not adopted")
            continue
        if match is not None:
            lead.sequence_state, lead.next_step_due_at = "stopped", None
            report.note("suppressed", f"{lead.email}: suppressed since import; stopped")
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
            lead.status = LeadStatus.HELD
            lead.skip_reason = "legacy_thread_not_found" if not found else "legacy_thread_ambiguous"
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
        session.flush()

        # Replies that landed before cutover: Smartlead's export predates them.
        sync = SyncReport()
        for msg in gr.thread_messages(services[box.id], newest.thread_id):
            process_message(session, box, msg, sync)
        if lead.sequence_state not in ("active", "paused"):
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


#: Buckets that mean a person was not handled as the export says they should be.
BLOCKING = ("MUST_FIX", "refused", "unknown_status")


def blockers(report: Report, expected: Mapping[str, int] | None = None) -> list[str]:
    """Why ``--commit`` should refuse: a wrong guess about the export's headers
    shows up here first, and a wrong guess can mean Email 1 to someone already
    emailed."""

    out = [f"{k}: {report.counts[k]}" for k in BLOCKING if report.counts.get(k)]
    out += [f"MISMATCH {k}: got {report.counts.get(k, 0)}, the client's figure is {want}"
            for k, want in (expected or {}).items() if report.counts.get(k, 0) != want]
    return out


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
                       help="commit even though the counts or row checks above failed")
    args = parser.parse_args(argv)

    from mazz.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        rows = lambda: list(_iter_csv(args.csv.read_text(encoding="utf-8-sig")))  # noqa: E731
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
        _print(report, problems)
        if args.commit and problems and not args.accept_mismatch:
            session.rollback()
            print("NOT committed: fix the export or smartlead.COLUMNS, or re-run with "
                  "--accept-mismatch after checking every line above.")
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
