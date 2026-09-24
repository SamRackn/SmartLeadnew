"""Operations: the health check, backups, resolve-send and the service worker.

The health check is the only thing that notices trouble now that nothing
outside this system watches the sending, so each alert is tested both ways:
it fires on the condition, and it stays quiet just short of it. An alert that
fires on healthy data gets muted, and a muted alert is worse than none.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz.models import (
    Job,
    JobStatus,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    SentMessage,
    VerificationStatus,
)
from mazz.ops import (
    CRITICAL,
    WARNING,
    NothingToResolve,
    check_health,
    prune_backups,
    resolve_send,
    run_backup,
    run_service_worker,
)
from mazz.pipeline.mailbox import sent_today
from mazz.pipeline.queue import enqueue_job

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


def make_mailbox(session, **kwargs) -> Mailbox:
    defaults = {
        "email_address": "Gough@getsearchsitemarketing.com",
        "display_name": "Sean Gough",
        "credential_ref": "getsearchsitemarketing.com",
        "daily_cap": 10,
        "min_spacing_seconds": 0,
    }
    defaults.update(kwargs)
    mailbox = Mailbox(**defaults)
    session.add(mailbox)
    session.flush()
    return mailbox


def attempted_send(session, *, at: datetime, status=LeadStatus.HELD):
    """A lead whose approved draft was handed to the sender, never booked."""

    mailbox = make_mailbox(session)
    lead = h.make_lead(session)
    lead.status = status
    lead.approved_at = at - timedelta(hours=1)
    lead.approved_by = "client"
    draft = h.make_draft(
        session,
        lead,
        verification_status=VerificationStatus.PASSED,
        body="Body.\n\n-- \nMazz Ltd\nUnsubscribe (one click): https://x.test/u/1",
    )
    draft.send_attempted_at = at
    draft.send_attempt_mailbox_id = mailbox.id
    session.flush()
    return lead, draft, mailbox


def codes(alerts):
    return [a.code for a in alerts]


# ==========================================================================
# Health
# ==========================================================================


def test_an_empty_system_is_healthy(session):
    assert check_health(session, now=NOW) == []


def test_an_unresolved_send_is_critical(session):
    lead, _, _ = attempted_send(session, at=NOW - timedelta(minutes=30))

    alerts = check_health(session, now=NOW)

    assert codes(alerts) == ["send_unresolved"]
    assert alerts[0].severity == CRITICAL
    assert str(lead.id) in alerts[0].message
    assert "resolve-send" in alerts[0].message


def test_a_send_in_flight_is_not_yet_an_alert(session):
    attempted_send(session, at=NOW - timedelta(minutes=1))
    assert check_health(session, now=NOW) == []


def test_a_booked_send_is_resolved(session):
    lead, draft, mailbox = attempted_send(session, at=NOW - timedelta(hours=1))
    session.add(
        SentMessage(
            lead_id=lead.id,
            draft_id=draft.id,
            mailbox_id=mailbox.id,
            from_address=mailbox.email_address,
            to_address=lead.email,
            subject="s",
            body="b",
            sent_at=NOW - timedelta(hours=1),
        )
    )
    session.flush()
    assert check_health(session, now=NOW) == []


def test_a_disabled_mailbox_is_critical_and_says_why(session):
    make_mailbox(
        session,
        status=MailboxStatus.DISABLED,
        last_error="ProviderAuthError: delegation revoked",
    )
    make_mailbox(
        session, email_address="paused@a.test", status=MailboxStatus.PAUSED
    )

    alerts = check_health(session, now=NOW)

    assert codes(alerts) == ["mailbox_disabled"]
    assert "delegation revoked" in alerts[0].message


def test_a_stalled_queue_is_critical(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    job.run_after = NOW - timedelta(minutes=30)
    session.flush()

    alerts = check_health(session, now=NOW)

    assert codes(alerts) == ["queue_stalled"]
    assert "worker running" in alerts[0].message


def test_a_job_due_a_moment_ago_is_not_a_stall(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    job.run_after = NOW - timedelta(minutes=2)
    session.flush()

    assert check_health(session, now=NOW) == []


def test_a_job_deferred_into_the_future_is_not_a_stall(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "send")
    job.run_after = NOW + timedelta(hours=6)  # waiting on tomorrow's cap
    session.flush()

    assert check_health(session, now=NOW) == []


def test_a_stale_running_job_warns(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    job.status = JobStatus.RUNNING
    job.locked_at = NOW - timedelta(minutes=40)
    session.flush()

    alerts = check_health(session, now=NOW)

    assert codes(alerts) == ["jobs_stale"]
    assert alerts[0].severity == WARNING


def test_recent_failures_warn_and_old_ones_do_not(session):
    lead = h.make_lead(session)
    recent, _ = enqueue_job(session, lead.id, "enrich")
    recent.status = JobStatus.FAILED
    recent.updated_at = NOW - timedelta(hours=2)
    other = h.make_lead(session, email="old@else.test", company_domain="else.test")
    old, _ = enqueue_job(session, other.id, "draft")
    old.status = JobStatus.FAILED
    old.updated_at = NOW - timedelta(days=3)
    session.flush()

    alerts = check_health(session, now=NOW)

    assert codes(alerts) == ["jobs_failed"]
    assert "enrich x1" in alerts[0].message
    assert "draft" not in alerts[0].message


def _drafts(session, passed: int, failed: int) -> None:
    for i in range(passed + failed):
        lead = h.make_lead(
            session, email=f"p{i}@firm{i}.test", company_domain=f"firm{i}.test"
        )
        h.make_draft(
            session,
            lead,
            verification_status=(
                VerificationStatus.PASSED if i < passed else VerificationStatus.FAILED
            ),
        )
    session.flush()


def test_a_high_hold_rate_warns(session):
    _drafts(session, passed=2, failed=4)

    alerts = check_health(session, now=datetime.now(timezone.utc))

    assert codes(alerts) == ["hold_rate_high"]
    assert "67%" in alerts[0].message


def test_a_hold_rate_on_too_few_drafts_is_noise_not_an_alert(session):
    _drafts(session, passed=1, failed=3)
    assert check_health(session, now=datetime.now(timezone.utc)) == []


def test_criticals_come_before_warnings(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "enrich")
    job.status = JobStatus.FAILED
    job.updated_at = NOW - timedelta(hours=1)
    make_mailbox(session, status=MailboxStatus.DISABLED)
    session.flush()

    alerts = check_health(session, now=NOW)

    assert [a.severity for a in alerts] == [CRITICAL, WARNING]


def test_backup_checks(session, tmp_path):
    assert codes(check_health(session, now=NOW, backup_dir=tmp_path)) == [
        "backup_missing"
    ]

    dump = tmp_path / "mazz_dev-old.dump"
    dump.write_bytes(b"x")
    two_days_ago = (NOW - timedelta(days=2)).timestamp()
    os.utime(dump, (two_days_ago, two_days_ago))
    assert codes(check_health(session, now=NOW, backup_dir=tmp_path)) == [
        "backup_stale"
    ]

    fresh = (NOW - timedelta(hours=3)).timestamp()
    os.utime(dump, (fresh, fresh))
    assert check_health(session, now=NOW, backup_dir=tmp_path) == []


# ==========================================================================
# Backups
# ==========================================================================


def _aged(path, hours_ago: int):
    path.write_bytes(b"dump")
    t = (NOW - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (t, t))
    return path


def test_prune_keeps_the_newest_and_touches_nothing_else(tmp_path):
    old = _aged(tmp_path / "a.dump", 72)
    mid = _aged(tmp_path / "b.dump", 48)
    new = _aged(tmp_path / "c.dump", 24)
    notes = tmp_path / "README.txt"
    notes.write_text("keep me")

    assert prune_backups(tmp_path, keep=2) == [old]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "README.txt",
        mid.name,
        new.name,
    ]


def test_prune_refuses_to_delete_every_backup(tmp_path):
    with pytest.raises(ValueError):
        prune_backups(tmp_path, keep=0)


def test_a_failed_dump_leaves_no_partial_and_prunes_nothing(tmp_path):
    good = [_aged(tmp_path / f"{n}.dump", 24 * (i + 1)) for i, n in enumerate("abc")]

    def runner(cmd, **_kwargs):
        if cmd[0] == "pg_dump":
            partial = next(a for a in cmd if a.startswith("--file=")).split("=", 1)[1]
            with open(partial, "wb") as fh:
                fh.write(b"half a dump")
            raise subprocess.CalledProcessError(1, cmd)
        raise AssertionError("pg_restore must not run after a failed dump")

    with pytest.raises(subprocess.CalledProcessError):
        run_backup(
            tmp_path,
            database_url="postgresql+psycopg://localhost/mazz_x",
            keep=1,
            now=NOW,
            runner=runner,
        )

    assert sorted(tmp_path.iterdir()) == sorted(good)


def test_a_real_backup_round_trips(session, tmp_path):
    """Against the real test database: pg_dump, then pg_restore reads it back."""

    path = run_backup(tmp_path, keep=3, now=NOW)

    assert path.exists()
    assert path.name == "mazz_test-20260924T120000Z.dump"
    listing = subprocess.run(
        ["pg_restore", "--list", str(path)], check=True, capture_output=True, text=True
    ).stdout
    assert "TABLE public leads" in listing
    assert not list(tmp_path.glob("*.partial"))


# ==========================================================================
# resolve-send
# ==========================================================================


def test_resolving_as_sent_books_it_and_counts_against_the_cap(session):
    at = NOW - timedelta(minutes=30)
    lead, draft, mailbox = attempted_send(session, at=at)

    message = resolve_send(session, lead.id, sent=True, resolved_by="sam", now=NOW)

    row = session.execute(select(SentMessage)).scalars().one()
    assert (row.lead_id, row.draft_id, row.mailbox_id) == (lead.id, draft.id, mailbox.id)
    assert row.sent_at == at
    assert row.provider_message_id is None  # never received; not invented
    assert lead.status is LeadStatus.SENT
    assert sent_today(session, mailbox, now=NOW) == 1
    assert "sam" in message
    assert check_health(session, now=NOW) == []


def test_resolving_as_not_sent_returns_it_to_the_outbox_without_queueing(session):
    lead, draft, _ = attempted_send(session, at=NOW - timedelta(minutes=30))

    message = resolve_send(session, lead.id, sent=False, resolved_by="sam", now=NOW)

    assert lead.status is LeadStatus.APPROVED
    assert draft.send_attempted_at is None
    assert draft.send_attempt_mailbox_id is None
    # Guardrail 4: back in the outbox is not on the wire. A human presses Send.
    assert session.execute(select(func.count(Job.id))).scalar_one() == 0
    assert "human decision" in message


def test_not_sent_on_a_superseded_draft_needs_review_again(session):
    lead, draft, _ = attempted_send(session, at=NOW - timedelta(minutes=30))
    h.make_draft(
        session,
        lead,
        verification_status=VerificationStatus.PASSED,
        created_at=draft.created_at + timedelta(minutes=5)
        if draft.created_at
        else NOW,
    )
    session.flush()

    message = resolve_send(session, lead.id, sent=False, resolved_by="sam", now=NOW)

    assert lead.status is LeadStatus.HELD
    assert "review again" in message


def test_nothing_to_resolve_is_refused(session):
    lead = h.make_lead(session)
    with pytest.raises(NothingToResolve):
        resolve_send(session, lead.id, sent=True, resolved_by="sam", now=NOW)
    with pytest.raises(NothingToResolve):
        resolve_send(session, 99999, sent=True, resolved_by="sam", now=NOW)


# ==========================================================================
# The service worker
# ==========================================================================


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_the_service_worker_reclaims_then_runs_crashed_work(session):
    lead = h.make_lead(session)
    job, _ = enqueue_job(session, lead.id, "noop")
    job.status = JobStatus.RUNNING
    job.locked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    job_id = job.id
    session.commit()

    ran: list[int] = []
    clock = _FakeClock()
    stats = run_service_worker(
        handlers={"noop": lambda ctx: ran.append(ctx.job.id)},
        should_stop=lambda: clock.t >= 10,
        clock=clock,
        sleep=clock.sleep,
    )

    session.expire_all()
    assert ran == [job_id]
    assert stats.done == 1
    assert session.get(Job, job_id).status is JobStatus.DONE


def test_the_service_worker_stops_when_asked(session):
    clock = _FakeClock()
    stop = {"now": False}

    def sleep(seconds):
        clock.sleep(seconds)
        stop["now"] = True

    stats = run_service_worker(
        handlers={}, should_stop=lambda: stop["now"], clock=clock, sleep=sleep
    )

    assert stats.claimed == 0
    assert clock.t <= 0.25  # stopped inside one idle slice, not a full sleep
