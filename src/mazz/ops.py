"""Operations: the long-running worker, the health check, backups, and the
one manual repair the send step can ask a human for.

    python -m mazz.ops worker              # drain the queue, forever
    python -m mazz.ops health [--notify]   # exit 1 if anything needs a human
    python -m mazz.ops preflight           # exit 1 if a go-live gate is unmet
    python -m mazz.ops backup              # pg_dump, verified, rotated
    python -m mazz.ops resolve-send LEAD_ID --sent | --not-sent --by EMAIL

Before this module the queue had no process of its own: pressing Send in the
outbox, or importing a spreadsheet in the browser, enqueued jobs that nothing
ran until someone happened to invoke a seed script. With Smartlead gone,
nothing outside this system watches the sending either, so the health check
is the only thing that will notice a dead credential or a stalled queue.

**Nothing here creates a send job.** Guardrail 4 has exactly one call site
(the outbox release action) and a test that counts them. ``resolve-send
--not-sent`` puts a lead back in the outbox; a human still presses Send.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, func, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session

from mazz.config import deployment_problems, get_settings
from mazz.db import get_engine, session_scope
from mazz.models import (
    Campaign,
    Draft,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    ProofPoint,
    SentMessage,
    VerificationStatus,
    Workspace,
)
from mazz.pipeline.queue import (
    DEFAULT_STALE_AFTER,
    JobHandler,
    WorkerStats,
    process_next,
    reclaim_stale_jobs,
)

log = logging.getLogger("mazz.ops")

__all__ = [
    "Alert",
    "HealthThresholds",
    "check_health",
    "check_preflight",
    "prune_backups",
    "resolve_send",
    "run_backup",
    "run_service_worker",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ==========================================================================
# Health
# ==========================================================================

CRITICAL = "critical"
WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Alert:
    severity: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    #: A due job older than this means no worker is draining the queue.
    queue_stall_after: timedelta = timedelta(minutes=10)
    stale_running_after: timedelta = DEFAULT_STALE_AFTER
    #: Failed jobs are reported over this window, not forever: a failure
    #: someone already looked at should not page them every fifteen minutes.
    failed_job_window: timedelta = timedelta(hours=24)
    #: A rising hold rate is the early warning that verifier precision has
    #: regressed or a template change went bad (PHASE2.md W6).
    hold_rate_window: timedelta = timedelta(days=7)
    hold_rate_max: float = 0.5
    hold_rate_min_sample: int = 5
    backup_max_age: timedelta = timedelta(hours=36)
    #: A send marked less than this long ago is still in flight, not lost.
    send_in_flight_grace: timedelta = timedelta(minutes=5)
    #: Replies not read for this long are not reaching /inbox, and nothing
    #: stops a sequence on them until they are. (A follow-up reads its own
    #: mailbox before it leaves: `send._read_replies_first`.)
    reply_sync_stale_after: timedelta = timedelta(minutes=30)


def check_health(
    session: Session,
    *,
    now: datetime | None = None,
    thresholds: HealthThresholds = HealthThresholds(),
    backup_dir: Path | None = None,
) -> list[Alert]:
    """Everything that needs a human, most urgent first. Empty means healthy.

    Pure reads. Each check is one query, so running this every few minutes
    costs nothing next to what it catches.
    """

    now = now or utcnow()
    alerts: list[Alert] = []

    # -- a send that may already be in someone's inbox ---------------------
    unresolved = session.execute(
        select(Draft.lead_id).where(
            Draft.send_attempted_at.isnot(None),
            Draft.send_attempted_at < now - thresholds.send_in_flight_grace,
            ~exists().where(SentMessage.draft_id == Draft.id),
        )
    ).scalars().all()
    if unresolved:
        leads = ", ".join(str(lead_id) for lead_id in unresolved)
        alerts.append(
            Alert(
                CRITICAL,
                "send_unresolved",
                f"{len(unresolved)} send(s) may already have gone out with no "
                f"record (lead {leads}). Check the mailbox's Sent folder, then "
                "`python -m mazz.ops resolve-send LEAD_ID --sent|--not-sent --by EMAIL`.",
            )
        )

    # -- a mailbox the system switched off --------------------------------
    disabled = session.execute(
        select(Mailbox.email_address, Mailbox.last_error).where(
            Mailbox.status == MailboxStatus.DISABLED
        )
    ).all()
    for address, last_error in disabled:
        alerts.append(
            Alert(
                CRITICAL,
                "mailbox_disabled",
                f"{address} is disabled: {(last_error or 'no error recorded')[:200]}",
            )
        )

    # -- a mailbox that cannot send or be read until a person reconnects it --
    reconnect = session.execute(
        select(Mailbox.email_address).where(Mailbox.status == MailboxStatus.NEEDS_RECONNECT)
    ).scalars().all()
    if reconnect:
        alerts.append(
            Alert(
                CRITICAL,
                "mailbox_needs_reconnect",
                f"{len(reconnect)} mailbox(es) need reconnecting on /mailboxes "
                f"({', '.join(reconnect)}): they neither send nor sync replies.",
            )
        )

    # -- replies not being read --------------------------------------------
    if get_settings().email_sender == "google":
        stale_sync = session.execute(
            select(Mailbox.email_address, Mailbox.last_synced_at).where(
                Mailbox.status == MailboxStatus.ACTIVE,
                Mailbox.oauth_refresh_token_enc.is_not(None),
                (Mailbox.last_synced_at.is_(None))
                | (Mailbox.last_synced_at < now - thresholds.reply_sync_stale_after),
            )
        ).all()
        for address, synced in stale_sync:
            alerts.append(
                Alert(
                    CRITICAL,
                    "reply_sync_stale",
                    f"{address}: replies last read "
                    f"{f'{synced:%Y-%m-%d %H:%M} UTC' if synced else 'never'}: its "
                    "replies are not reaching /inbox.",
                )
            )

    # -- no worker draining the queue --------------------------------------
    oldest_due = session.execute(
        select(func.min(Job.run_after)).where(Job.status == JobStatus.PENDING)
    ).scalar_one()
    if oldest_due is not None and oldest_due < now - thresholds.queue_stall_after:
        waiting = session.execute(
            select(func.count(Job.id)).where(
                Job.status == JobStatus.PENDING, Job.run_after <= now
            )
        ).scalar_one()
        alerts.append(
            Alert(
                CRITICAL,
                "queue_stalled",
                f"{waiting} job(s) due and not picked up; the oldest has been "
                f"due since {oldest_due:%Y-%m-%d %H:%M} UTC. Is the worker running?",
            )
        )

    # -- a worker that claimed a job and vanished --------------------------
    stale = session.execute(
        select(func.count(Job.id)).where(
            Job.status == JobStatus.RUNNING,
            Job.locked_at < now - thresholds.stale_running_after,
        )
    ).scalar_one()
    if stale:
        alerts.append(
            Alert(
                WARNING,
                "jobs_stale",
                f"{stale} job(s) running for over "
                f"{_minutes(thresholds.stale_running_after)} minutes; the "
                "worker reclaims these on its next sweep.",
            )
        )

    # -- work that gave up -------------------------------------------------
    failed = session.execute(
        select(Job.kind, func.count(Job.id))
        .where(
            Job.status == JobStatus.FAILED,
            Job.updated_at >= now - thresholds.failed_job_window,
        )
        .group_by(Job.kind)
        .order_by(Job.kind)
    ).all()
    if failed:
        by_kind = ", ".join(f"{kind} x{count}" for kind, count in failed)
        alerts.append(
            Alert(
                WARNING,
                "jobs_failed",
                f"jobs gave up in the last "
                f"{_hours(thresholds.failed_job_window)}h ({by_kind}); their "
                "leads are held. See the dashboard's queue panel.",
            )
        )

    # -- verification holding too much ------------------------------------
    verdicts = dict(
        session.execute(
            select(Draft.verification_status, func.count(Draft.id))
            .where(
                Draft.created_at >= now - thresholds.hold_rate_window,
                Draft.verification_status.in_(
                    [VerificationStatus.PASSED, VerificationStatus.FAILED]
                ),
            )
            .group_by(Draft.verification_status)
        ).all()
    )
    judged = sum(verdicts.values())
    if judged >= thresholds.hold_rate_min_sample:
        rate = verdicts.get(VerificationStatus.FAILED, 0) / judged
        if rate > thresholds.hold_rate_max:
            alerts.append(
                Alert(
                    WARNING,
                    "hold_rate_high",
                    f"verification held {rate:.0%} of {judged} drafts in the "
                    f"last {thresholds.hold_rate_window.days} days (alert above "
                    f"{thresholds.hold_rate_max:.0%}).",
                )
            )

    # -- backups -----------------------------------------------------------
    if backup_dir is not None:
        newest = _newest_backup(backup_dir)
        if newest is None:
            alerts.append(
                Alert(WARNING, "backup_missing", f"no backup found in {backup_dir}")
            )
        else:
            taken = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
            if taken < now - thresholds.backup_max_age:
                alerts.append(
                    Alert(
                        WARNING,
                        "backup_stale",
                        f"newest backup is {newest.name}, taken "
                        f"{taken:%Y-%m-%d %H:%M} UTC",
                    )
                )

    order = {CRITICAL: 0, WARNING: 1}
    return sorted(alerts, key=lambda a: order[a.severity])


# ==========================================================================
# Go-live preflight
# ==========================================================================


def check_preflight(session: Session, *, settings=None) -> list[Alert]:
    """The go-live gates the code can see, judged as if sending were on.

    Run before flipping ``SEND_ENABLED``. The human gates it cannot see
    (Zapmail's written OK, rehearsal headers, the Smartlead pause and detach,
    Sam's go) stay on the checklist in docs/LAUNCH-PLAN.md.
    """

    from mazz.launch_copy import CAMPAIGN_NAME, DAILY_CAP, MAILBOXES
    from mazz.pipeline.draft import placeholder_marker_in
    from mazz.pipeline.send import SEND_JOB_KIND

    live = (settings or get_settings()).model_copy(
        update={"send_enabled": True, "email_sender": "google"}
    )
    alerts = [Alert(CRITICAL, "config", p) for p in deployment_problems(live, sending=True)]

    senders = select(Workspace).where(exists().where(Campaign.workspace_id == Workspace.id))
    for ws in session.execute(senders).scalars():
        address = (ws.postal_address or "").strip()
        if not address or ws.postal_address_confirmed_at is None:
            alerts.append(Alert(CRITICAL, "postal_address",
                                f"{ws.name}: no confirmed postal address (confirm it on /workspaces/{ws.id}/edit)"))
        elif marker := placeholder_marker_in(address):
            alerts.append(Alert(CRITICAL, "postal_address",
                                f"{ws.name}: address contains placeholder {marker!r}"))

    roster = {m.email_address: m for m in session.execute(select(Mailbox)).scalars()}
    for address in MAILBOXES:
        mailbox = roster.get(address)
        if mailbox is None:
            alerts.append(Alert(CRITICAL, "mailbox_missing",
                                f"{address} is not on the roster (python -m mazz.users seed-launch)"))
        elif mailbox.status != MailboxStatus.ACTIVE or not mailbox.oauth_refresh_token_enc:
            alerts.append(Alert(CRITICAL, "mailbox_not_connected",
                                f"{address} is {mailbox.status.value}, "
                                f"{'connected' if mailbox.oauth_refresh_token_enc else 'never connected'}"))
    for mailbox in (roster[a] for a in MAILBOXES if a in roster):
        if mailbox.daily_cap > DAILY_CAP:
            alerts.append(Alert(CRITICAL, "mailbox_cap",
                                f"{mailbox.email_address} caps at {mailbox.daily_cap}/day; "
                                f"the launch decision is {DAILY_CAP}"))

    # Released while sending was off and deferred (window, cap): these go out
    # for real the moment the switch flips. RUNNING too: a dry-run worker
    # killed mid-job leaves one that is reclaimed, and then runs live.
    pending = session.execute(
        select(Job.lead_id).where(
            Job.kind == SEND_JOB_KIND,
            Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
        )
    ).scalars().all()
    if pending:
        alerts.append(Alert(CRITICAL, "send_jobs_pending",
                            f"{len(pending)} send job(s) released during the dry run are still "
                            f"pending (lead {', '.join(map(str, pending))}); they go live when "
                            "SEND_ENABLED flips. Let them run dry first, or reject those leads."))

    unresolved = session.execute(
        select(Draft.lead_id).where(
            Draft.send_attempted_at.isnot(None),
            ~exists().where(SentMessage.draft_id == Draft.id),
        )
    ).scalars().all()
    if unresolved:
        alerts.append(Alert(CRITICAL, "send_unresolved",
                            f"{len(unresolved)} send attempt(s) with no booked result (lead "
                            f"{', '.join(map(str, unresolved))}); resolve each with "
                            "`python -m mazz.ops resolve-send` first."))

    stats = session.execute(
        select(func.count(ProofPoint.id))
        .join(Campaign, Campaign.id == ProofPoint.campaign_id)
        .where(Campaign.name == CAMPAIGN_NAME, ProofPoint.active.is_(True))
    ).scalar_one()
    if stats:
        alerts.append(Alert(CRITICAL, "proof_points",
                            f"{stats} active proof point(s) on {CAMPAIGN_NAME!r}; the client "
                            "launches with no performance statistics (2026-10-14)"))
    return alerts


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)


def notify_macos(title: str, message: str) -> None:
    """A desktop notification. The text travels as an argument, never as
    AppleScript source, so an error message cannot inject a script."""

    subprocess.run(
        [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            message[:240],
        ],
        check=False,
        capture_output=True,
    )


# ==========================================================================
# Backups
# ==========================================================================

BACKUP_SUFFIX = ".dump"


def default_backup_dir() -> Path:
    configured = os.environ.get("BACKUP_DIR")
    if configured:
        return Path(configured).expanduser()
    # Outside the repository on purpose: `git clean -xdf` should never be
    # able to take the backups with it.
    return Path.home() / "Library" / "Application Support" / "MazzSmartLead" / "backups"


def _libpq_url(database_url: str) -> str:
    """SQLAlchemy's `postgresql+psycopg://` is not a URL pg_dump understands."""

    return make_url(database_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def _newest_backup(backup_dir: Path) -> Path | None:
    dumps = sorted(backup_dir.glob(f"*{BACKUP_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    return dumps[-1] if dumps else None


def run_backup(
    backup_dir: Path,
    *,
    database_url: str | None = None,
    keep: int = 14,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Dump the database, prove the dump is readable, then rotate.

    Written to a temporary name and renamed only after `pg_restore --list`
    reads it back, so a dump that died halfway never counts as the newest
    backup -- and never pushes a good one out of the rotation.
    """

    database_url = database_url or get_settings().database_url
    now = now or utcnow()
    backup_dir.mkdir(parents=True, exist_ok=True)

    name = make_url(database_url).database or "mazz"
    final = backup_dir / f"{name}-{now:%Y%m%dT%H%M%SZ}{BACKUP_SUFFIX}"
    partial = final.with_suffix(".partial")

    try:
        runner(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                f"--file={partial}",
                _libpq_url(database_url),
            ],
            check=True,
            capture_output=True,
        )
        runner(
            ["pg_restore", "--list", str(partial)],
            check=True,
            capture_output=True,
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.rename(final)
    pruned = prune_backups(backup_dir, keep=keep)
    log.info("backup written: %s (pruned %d)", final, len(pruned))
    return final


def prune_backups(backup_dir: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest `keep` dumps. Never touches other files."""

    if keep < 1:
        raise ValueError("keep must be at least 1; pruning every backup is not a rotation")
    dumps = sorted(backup_dir.glob(f"*{BACKUP_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    doomed = dumps[:-keep]
    for path in doomed:
        path.unlink()
    return doomed


# ==========================================================================
# resolve-send: the human answer to "may have been sent"
# ==========================================================================


class NothingToResolve(Exception):
    pass


def resolve_send(
    session: Session,
    lead_id: int,
    *,
    sent: bool,
    resolved_by: str,
    now: datetime | None = None,
) -> str:
    """Record what the operator found in the mailbox's Sent folder.

    ``sent=True`` books the message as sent -- a `sent_messages` row with no
    provider ids, because we never received them -- so the daily cap, the
    dashboard and delete-by-person all see it. ``sent=False`` clears the
    marker and, if the approved draft is still the current one, returns the
    lead to ``approved``: back in the outbox, where sending it is still one
    human pressing Send. Neither branch enqueues anything.
    """

    from mazz.pipeline.send import unresolved_send_attempt
    from mazz.pipeline.verify import is_approvable, latest_draft

    now = now or utcnow()
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise NothingToResolve(f"lead {lead_id} does not exist")
    draft = unresolved_send_attempt(session, lead)
    if draft is None:
        raise NothingToResolve(
            f"lead {lead_id} has no unresolved send attempt; nothing to do"
        )

    mailbox = (
        session.get(Mailbox, draft.send_attempt_mailbox_id)
        if draft.send_attempt_mailbox_id is not None
        else None
    )

    if sent:
        session.add(
            SentMessage(
                lead_id=lead.id,
                draft_id=draft.id,
                mailbox_id=mailbox.id if mailbox is not None else None,
                from_address=mailbox.email_address if mailbox else "unknown",
                to_address=lead.email,
                subject=draft.subject,
                body=draft.body,
                sent_at=draft.send_attempted_at,
            )
        )
        lead.status = LeadStatus.SENT
        # No Gmail ids came back, so the thread cannot be continued from here
        # (the send step refuses it): say so on the row rather than leave a
        # sent lead with no sequence state, invisible to the company cap.
        lead.sequence_state, lead.skip_reason = "stopped", "resolved_by_hand"
        lead.next_step_due_at = None
        outcome = (
            f"lead {lead.id}: recorded as SENT at {draft.send_attempted_at} "
            f"(confirmed by {resolved_by}, {now:%Y-%m-%d %H:%M} UTC)"
        )
    else:
        draft.send_attempted_at = None
        draft.send_attempt_mailbox_id = None
        current = latest_draft(session, lead)
        if (
            lead.status == LeadStatus.HELD
            and current is not None
            and current.id == draft.id
            and is_approvable(draft)
            and lead.approved_at is not None
        ):
            lead.status = LeadStatus.APPROVED
            where = "back in the outbox; pressing Send is still a human decision"
        else:
            where = (
                f"left {lead.status.value}: the attempted draft is no longer "
                "the approved current one, so it needs review again"
            )
        outcome = f"lead {lead.id}: confirmed NOT sent by {resolved_by} -- {where}"

    from mazz import audit

    session.flush()
    audit.record(session, resolved_by, "resolve_send", "lead", lead.id,
                 draft_id=draft.id, sent=sent, status=lead.status.value)
    log.warning(outcome)
    return outcome


# ==========================================================================
# The service worker
# ==========================================================================


def run_service_worker(
    *,
    handlers: Mapping[str, JobHandler] | None = None,
    idle_sleep: float = 2.0,
    reclaim_every: float = 60.0,
    sync_every: float = 300.0,
    sync: Callable[[], object] | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WorkerStats:
    """Drain the queue until told to stop, reclaiming crashed work as it goes.

    Run ONE of these. Mailbox selection takes no row lock, so two workers
    sending at once could both pick the same mailbox and break its spacing.

    Reclaim is safe for every job kind, sends included, because the send step
    commits its attempt marker before touching the wire: a reclaimed send
    whose worker died mid-call is held for a human, not sent again.
    """

    stats = WorkerStats()
    last_reclaim: float | None = None
    last_sync: float | None = None
    log.info("worker started (pid %s)", os.getpid())

    while not should_stop():
        if last_reclaim is None or clock() - last_reclaim >= reclaim_every:
            with session_scope() as session:
                reclaimed = reclaim_stale_jobs(session, stale_after=stale_after)
            if reclaimed:
                log.warning("reclaimed %d stale job(s)", len(reclaimed))
            last_reclaim = clock()

        if sync is not None and (last_sync is None or clock() - last_sync >= sync_every):
            last_sync = clock()
            try:
                sync()
            except Exception:  # noqa: BLE001 - reading mail must never stop sending's worker
                log.exception("reply sync failed")

        job = process_next(handlers=handlers, stats=stats)
        if job is None:
            # Sleep in short slices so a stop signal is honoured promptly.
            deadline = clock() + idle_sleep
            while clock() < deadline and not should_stop():
                sleep(min(0.25, idle_sleep))

    log.info("worker stopping: %s", stats)
    return stats


# ==========================================================================
# CLI
# ==========================================================================


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


#: Session-level advisory lock a worker holds for its whole life. Caps,
#: spacing and stale-job reclaim all assume one worker; this makes a second
#: (a manual `ops worker` beside the systemd unit) refuse instead of assume.
WORKER_LOCK_KEY = 0x6D617A7A00


def acquire_worker_lock() -> Connection | None:
    """A connection holding `WORKER_LOCK_KEY`, or None if another worker has it.

    Keep it for as long as the worker runs, then `release_worker_lock`. The
    process dying releases it too (Postgres drops the session).
    """

    conn = get_engine().connect()
    got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": WORKER_LOCK_KEY}).scalar()
    conn.commit()  # session-level lock survives; do not sit idle in a transaction
    if got:
        return conn
    conn.close()
    return None


def release_worker_lock(conn: Connection) -> None:
    # Unlocked explicitly: close() only returns a pooled connection, and the
    # session-level lock would ride along with it.
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": WORKER_LOCK_KEY})
        conn.commit()
    finally:
        conn.close()


def _cmd_worker(args: argparse.Namespace) -> int:
    problems = deployment_problems(get_settings(), sending=True)
    if problems:
        for problem in problems:
            log.error("refusing to start the worker: %s", problem)
        return 2
    lock = acquire_worker_lock()
    if lock is None:
        log.error("refusing to start the worker: another worker is already running "
                  "against this database (caps and spacing assume exactly one)")
        return 2
    try:
        return _run_worker_cli(args)
    finally:
        release_worker_lock(lock)


def _run_worker_cli(args: argparse.Namespace) -> int:
    stopping = False

    def request_stop(signum, _frame):
        nonlocal stopping
        log.info("received %s; finishing the current job", signal.Signals(signum).name)
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log.info("email sender: %s", get_settings().email_sender)
    sync = None
    if get_settings().email_sender == "google":
        from mazz.pipeline.replies import sync_all

        sync = sync_all  # reply + bounce sync only when mail is live
    run_service_worker(
        sync=sync,
        idle_sleep=args.idle_sleep,
        reclaim_every=args.reclaim_every,
        stale_after=timedelta(minutes=args.stale_after_minutes),
        should_stop=lambda: stopping,
    )
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    backup_dir = None if args.no_backup_check else Path(args.backup_dir).expanduser()
    with session_scope() as session:
        alerts = check_health(session, backup_dir=backup_dir)
    if not alerts:
        print("healthy")
        return 0
    for alert in alerts:
        print(alert)
    if args.notify:
        worst = alerts[0]
        notify_macos(
            f"MazzSmartLead: {len(alerts)} issue(s)",
            f"{worst.code}: {worst.message}",
        )
    return 1


def _cmd_preflight(args: argparse.Namespace) -> int:
    with session_scope() as session:
        alerts = check_preflight(session)
    for alert in alerts:
        print(alert)
    print("ready to flip SEND_ENABLED" if not alerts else f"{len(alerts)} gate(s) unmet")
    return 1 if alerts else 0


def _cmd_backup(args: argparse.Namespace) -> int:
    path = run_backup(Path(args.backup_dir).expanduser(), keep=args.keep)
    print(path)
    return 0


def _cmd_resolve_send(args: argparse.Namespace) -> int:
    try:
        with session_scope() as session:
            print(
                resolve_send(
                    session, args.lead_id, sent=args.sent, resolved_by=args.by
                )
            )
    except NothingToResolve as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mazz.ops", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker", help="drain the job queue until stopped")
    worker.add_argument("--idle-sleep", type=float, default=2.0)
    worker.add_argument("--reclaim-every", type=float, default=60.0)
    worker.add_argument(
        "--stale-after-minutes", type=float, default=_minutes(DEFAULT_STALE_AFTER)
    )
    worker.set_defaults(func=_cmd_worker)

    health = sub.add_parser("health", help="report anything that needs a human")
    health.add_argument(
        "--notify", action="store_true", help="macOS notification on alerts"
    )
    health.add_argument("--backup-dir", default=str(default_backup_dir()))
    health.add_argument("--no-backup-check", action="store_true")
    health.set_defaults(func=_cmd_health)

    preflight = sub.add_parser("preflight", help="check the go-live gates the code can see")
    preflight.set_defaults(func=_cmd_preflight)

    backup = sub.add_parser("backup", help="pg_dump the database, verified and rotated")
    backup.add_argument("--backup-dir", default=str(default_backup_dir()))
    backup.add_argument("--keep", type=int, default=14)
    backup.set_defaults(func=_cmd_backup)

    resolve = sub.add_parser(
        "resolve-send", help="record whether a may-have-been-sent email actually left"
    )
    resolve.add_argument("lead_id", type=int)
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--sent", dest="sent", action="store_true")
    outcome.add_argument("--not-sent", dest="sent", action="store_false")
    # A person, not the OS account: it is the actor on the audit event.
    resolve.add_argument("--by", required=True, help="who checked the Sent folder (their login email)")
    resolve.set_defaults(func=_cmd_resolve_send)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
