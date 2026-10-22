"""Independent QA: cross-subsystem behaviour of the integrated working tree.

Each test drives the real routes (approve, release, reject, edit, /u/, inbox
tag), the real queue worker (`process_next`) and the real send step together,
and asserts what the business needs, not how the code gets there. Senders are
stubs or a fake Gmail service; clocks are the database's and the stub's own;
nothing touches the network.

A test marked xfail(strict=True) is a CONFIRMED defect (QA-NN): it fails today
and must start passing when the defect is fixed.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz import metrics as M
from mazz.config import reload_settings
from mazz.db import get_sessionmaker
from mazz.ingest.csv_import import SuppressionChecker, SuppressionUnavailable
from mazz.models import (
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    Reply,
    SentMessage,
    SuppressionEntry,
)
from mazz.pipeline.draft import unsubscribe_token
from mazz.pipeline.queue import (
    JobContext,
    PermanentJobError,
    claim_job,
    complete_job,
    default_handlers,
    enqueue_job,
    fail_job,
    process_next,
)
from mazz.pipeline.send import handle_send
from mazz.pipeline.sequence import thread_parent
from mazz.pipeline.verify import latest_draft
from mazz.web.app import app
from fastapi.testclient import TestClient
from test_audit_gmail import HTTPS, ReadAndWire, live_sender
from test_audit_web import operator
from test_replies import gmsg, inbound
from test_sequence import Recording, law_lead, mailbox, run_jobs, ssm_campaign
from test_web import TEST_PASSWORD, make_mailbox, make_passed_lead, session  # noqa: F401

from mazz.web.auth import hash_password

EDITED = "Dana - saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM."


@pytest.fixture(autouse=True)
def qa_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 1 Test Street, Testville")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("REVIEW_USER", "client")
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    monkeypatch.setenv("DRYRUN_DIR", str(tmp_path / "dryrun"))
    reload_settings()
    yield
    reload_settings()


@pytest.fixture()
def wire(monkeypatch, tmp_path):
    """Every message the send step hands to a sender, in order."""

    import mazz.pipeline.send as send_module

    rec = Recording(tmp_path)
    monkeypatch.setattr(send_module, "get_email_sender", lambda: rec)
    return rec


def ready_law_lead(session, campaign, **kw) -> Lead:
    """A lead taken through the real draft + verify stages to READY."""

    lead = law_lead(session, campaign, **kw)
    enqueue_job(session, lead.id, "draft")
    run_jobs(session)
    lead = fresh(session, lead.id)
    assert lead.status is LeadStatus.READY, latest_draft(session, lead).verification_notes
    return lead


def approve_and_release(op, lead_id: int) -> None:
    assert op.post(f"/leads/{lead_id}/approve").status_code == 200
    assert op.post(f"/outbox/{lead_id}/send", follow_redirects=False).status_code == 303


def drain_sends() -> None:
    while process_next(kinds=("send",)) is not None:
        pass


def send_jobs(session, lead_id: int, *statuses: JobStatus) -> list[Job]:
    session.expire_all()
    q = select(Job).where(Job.lead_id == lead_id, Job.kind == "send")
    if statuses:
        q = q.where(Job.status.in_(statuses))
    return list(session.execute(q).scalars())


def make_due(session, kind: str) -> None:
    job = session.execute(
        select(Job).where(Job.kind == kind, Job.status == JobStatus.PENDING)
    ).scalar_one()
    job.run_after = h.utcnow() - timedelta(minutes=1)
    session.commit()


def fresh(session, lead_id: int) -> Lead:
    """Re-read after a route or worker committed in another session."""

    session.expire_all()
    return session.get(Lead, lead_id)


def sent_count(session) -> int:
    session.expire_all()
    return session.execute(select(func.count(SentMessage.id))).scalar_one()


class Blocking(Recording):
    """Holds the FIRST send on the wire until `go` is set: a message in flight."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.entered, self.go = threading.Event(), threading.Event()

    def send(self, message, *, credential_ref):
        if not self.entered.is_set():
            self.entered.set()
            self.go.wait(20)
        return super().send(message, credential_ref=credential_ref)


def run_in_thread(target):
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


# ==========================================================================
# 1. The whole path, end to end, through the routes and the worker
# ==========================================================================


def test_eligible_lead_end_to_end_to_a_counted_qualified_conversation(session, wire):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = ready_law_lead(session, campaign)
    op = operator()

    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    drain_sends()
    assert send_jobs(session, lead.id) == [] and wire.sent == [], "approval alone queued or sent"

    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 303
    drain_sends()
    lead = fresh(session, lead.id)
    assert (lead.status, lead.sequence_state, len(wire.sent)) == (LeadStatus.SENT, "active", 1)

    # Follow-up 1 comes due: it is drafted for review, never queued to send.
    make_due(session, "followup")
    run_jobs(session)
    lead = fresh(session, lead.id)
    assert lead.status is LeadStatus.READY and latest_draft(session, lead).step == 2
    assert send_jobs(session, lead.id, JobStatus.PENDING, JobStatus.RUNNING) == []
    assert len(wire.sent) == 1

    approve_and_release(op, lead.id)
    drain_sends()
    first = session.execute(select(SentMessage).order_by(SentMessage.id)).scalars().first()
    assert len(wire.sent) == 2
    assert wire.sent[1].headers["In-Reply-To"] == first.rfc_message_id

    # A genuine reply stops the sequence; the queued follow-up 2 then writes nothing.
    box = session.execute(select(Mailbox)).scalar_one()
    lead = fresh(session, lead.id)
    inbound(session, box, thread_parent(session, lead), lead, "Yes, what does it cost?",
            ts=h.utcnow())
    session.commit()
    assert fresh(session, lead.id).sequence_state == "stopped"
    make_due(session, "followup")
    run_jobs(session)
    lead = fresh(session, lead.id)
    assert latest_draft(session, lead).step == 2 and lead.status is not LeadStatus.READY
    assert len(wire.sent) == 2

    # Only a human tag makes it a qualified conversation, and the metric counts it.
    reply = session.execute(select(Reply)).scalar_one()
    month = h.utcnow().date().replace(day=1)
    assert M.month_metrics(session, month).qualified == 0
    assert op.post(f"/inbox/{reply.id}/tag", data={"tag": "qualified"},
                   follow_redirects=False).status_code < 400
    session.expire_all()
    m = M.month_metrics(session, month)
    assert (m.qualified, m.sent, m.contacted) == (1, 2, 1)


# ==========================================================================
# 2. Stale approval (WEB-01)
# ==========================================================================


def test_a_pending_release_never_carries_edited_copy_and_a_fresh_release_sends_the_edit(
    session, wire
):
    lead, draft = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    approve_and_release(op, lead.id)

    assert op.post(f"/leads/{lead.id}/reject", data={"reason": "tone"}).status_code == 200
    assert op.post(f"/leads/{lead.id}/edit",
                   data={"subject": draft.subject, "body_text": EDITED}).status_code == 200
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    drain_sends()
    assert wire.sent == [] and sent_count(session) == 0

    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 303
    drain_sends()
    assert len(wire.sent) == 1 and EDITED in wire.sent[0].body
    assert fresh(session, lead.id).status is LeadStatus.SENT


def _stale_running_then_second_release(session):
    """Release -> worker claims (RUNNING) -> reject/edit/approve -> release again
    -> the stale job runs exactly as `process_next` would run it."""

    lead, draft = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    approve_and_release(op, lead.id)
    with h.db_session() as s:
        job_id = claim_job(s, kinds=("send",)).id

    assert op.post(f"/leads/{lead.id}/reject", data={"reason": "tone"}).status_code == 200
    assert op.post(f"/leads/{lead.id}/edit",
                   data={"subject": draft.subject, "body_text": EDITED}).status_code == 200
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    second = op.post(f"/outbox/{lead.id}/send", follow_redirects=False)

    try:
        with h.db_session() as s:
            job = s.get(Job, job_id)
            handle_send(JobContext(session=s, job=job, lead=s.get(Lead, lead.id),
                                   handlers=default_handlers()))
            complete_job(s, job)
    except PermanentJobError as exc:
        with h.db_session() as s:
            fail_job(s, s.get(Job, job_id), exc)
    drain_sends()
    return lead, op, second


def test_a_stale_running_release_never_sends_and_the_lead_stays_releasable(session, wire):
    lead, op, _ = _stale_running_then_second_release(session)
    assert wire.sent == [] and sent_count(session) == 0
    assert fresh(session, lead.id).status is LeadStatus.APPROVED

    # Recovery: one more human release sends the edited copy, once.
    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 303
    drain_sends()
    assert len(wire.sent) == 1 and EDITED in wire.sent[0].body


# QA-01 fixed 2026-10-19 (was a strict xfail).
def test_a_release_made_while_a_stale_job_runs_is_honoured_or_refused(session, wire):
    lead, _, second = _stale_running_then_second_release(session)
    assert second.status_code != 303 or len(wire.sent) == 1, (
        "the release returned success, yet nothing was sent and nothing is queued"
    )


# ==========================================================================
# 3. Suppression between release and send
# ==========================================================================


def test_one_click_unsubscribe_after_release_stops_the_queued_send(session, wire):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = ready_law_lead(session, campaign)
    approve_and_release(operator(), lead.id)

    resp = TestClient(app).post(f"/u/{unsubscribe_token(lead)}", content="List-Unsubscribe=One-Click",
                                headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200
    drain_sends()

    assert wire.sent == [] and sent_count(session) == 0
    assert fresh(session, lead.id).status is LeadStatus.HELD
    assert lead.email in {e.email for e in session.execute(select(SuppressionEntry)).scalars()}
    assert [j.status for j in send_jobs(session, lead.id)] == [JobStatus.FAILED]


def test_unreadable_suppression_at_send_time_holds_and_sends_nothing(session, wire, monkeypatch):
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = ready_law_lead(session, campaign)
    approve_and_release(operator(), lead.id)

    def unreadable(self, *a, **kw):
        raise SuppressionUnavailable("simulated: relation suppression_list is unreadable")

    monkeypatch.setattr(SuppressionChecker, "check", unreadable)
    drain_sends()

    assert wire.sent == [] and sent_count(session) == 0
    assert fresh(session, lead.id).status is LeadStatus.HELD


# ==========================================================================
# 4. Company cap (SND-02) through the real draft stage
# ==========================================================================


def test_company_cap_binds_at_drafting_for_a_colleague_of_a_live_lead(session):
    campaign = ssm_campaign(session)
    campaign.max_active_per_company = 1
    mailbox(session)
    ready_law_lead(session, campaign)
    b = law_lead(session, campaign, email="ann@damelegal.com", full_name="Ann Dame",
                 first_name="Ann")
    enqueue_job(session, b.id, "draft")
    run_jobs(session)
    assert latest_draft(session, fresh(session, b.id)) is None
    job = session.execute(select(Job).where(Job.lead_id == b.id, Job.kind == "draft")).scalar_one()
    assert job.status is JobStatus.PENDING and "company cap" in job.last_error


# QA-03 fixed 2026-10-19 (was a strict xfail).
def test_company_cap_holds_at_send_after_a_reject_edit_cycle(session, wire):
    from mazz.pipeline.draft import split_body

    campaign = ssm_campaign(session)
    campaign.max_active_per_company = 1
    mailbox(session)
    a = ready_law_lead(session, campaign)
    op = operator()
    assert op.post(f"/leads/{a.id}/reject", data={"reason": "check the name"}).status_code == 200

    b = ready_law_lead(session, campaign, email="ann@damelegal.com", full_name="Ann Dame",
                       first_name="Ann")  # A is held, so B's drafting sees a free slot
    draft_a = latest_draft(session, fresh(session, a.id))
    assert op.post(f"/leads/{a.id}/edit", data={
        "subject": draft_a.subject, "body_text": split_body(draft_a.body)[0],
    }).status_code == 200
    assert fresh(session, a.id).status is LeadStatus.READY
    approve_and_release(op, a.id)
    approve_and_release(op, b.id)
    drain_sends()

    assert len(wire.sent) == 1, (
        f"{len(wire.sent)} Email 1s went to damelegal.com under max_active_per_company=1"
    )


# ==========================================================================
# 5. Double release / two workers
# ==========================================================================


def _two_concurrent_releases(session) -> Lead:
    """Two release requests in flight at once: each runs the route's
    `enqueue_job` in its own transaction before either commits."""

    lead, _ = make_passed_lead(session)
    make_mailbox(session)
    assert operator().post(f"/leads/{lead.id}/approve").status_code == 200
    factory = get_sessionmaker()

    def second_click():
        with factory() as s2:
            enqueue_job(s2, lead.id, "send")
            s2.commit()

    # The second click starts while the first is uncommitted. Run it on its
    # own thread: a correct dedupe makes it WAIT for the first commit, which
    # in a single thread would be a self-deadlock rather than a race.
    with factory() as s1:
        enqueue_job(s1, lead.id, "send")
        other = run_in_thread(second_click)
        other.join(1)  # give it time to reach (and block in) its own enqueue
        s1.commit()
    other.join(20)
    assert not other.is_alive()
    return lead


# QA-02 fixed 2026-10-19 (was a strict xfail).
def test_two_concurrent_release_clicks_queue_one_job(session):
    lead = _two_concurrent_releases(session)
    assert len(send_jobs(session, lead.id)) == 1


def test_two_send_jobs_on_two_workers_still_send_exactly_once(session, monkeypatch, tmp_path):
    import mazz.pipeline.send as send_module

    lead = _two_concurrent_releases(session)
    assert len(send_jobs(session, lead.id)) >= 1
    blocking = Blocking(tmp_path)
    monkeypatch.setattr(send_module, "get_email_sender", lambda: blocking)
    worker = run_in_thread(lambda: process_next(kinds=("send",)))
    try:
        assert blocking.entered.wait(20)
        process_next(kinds=("send",))
    finally:
        blocking.go.set()
        worker.join(30)
    drain_sends()

    assert len(blocking.sent) == 1 and sent_count(session) == 1
    assert fresh(session, lead.id).status is LeadStatus.SENT


# ==========================================================================
# 6. SEND_ENABLED off, then on
# ==========================================================================


def test_a_dry_run_release_does_not_go_live_when_the_switch_flips(session, wire, monkeypatch, tmp_path):
    from mazz.ops import check_preflight

    monkeypatch.setenv("SEND_ENABLED", "false")
    reload_settings()
    lead, _ = make_passed_lead(session)
    make_mailbox(session)
    approve_and_release(operator(), lead.id)
    drain_sends()
    assert list((tmp_path / "dryrun").glob("*.eml")), "dry run wrote no MIME"
    assert fresh(session, lead.id).status is LeadStatus.APPROVED

    monkeypatch.setenv("SEND_ENABLED", "true")
    reload_settings()
    drain_sends()
    assert wire.sent == [] and sent_count(session) == 0
    assert not [a for a in check_preflight(session) if a.code == "send_jobs_pending"]


def test_a_release_deferred_while_off_is_a_preflight_blocker(session, wire, monkeypatch):
    from mazz.ops import check_preflight

    monkeypatch.setenv("SEND_ENABLED", "false")
    reload_settings()
    lead, _ = make_passed_lead(session)
    make_mailbox(session, min_spacing_seconds=3600, last_sent_at=h.utcnow())
    approve_and_release(operator(), lead.id)
    drain_sends()  # spacing defers it; nothing is rendered or sent
    (job,) = send_jobs(session, lead.id)
    assert job.status is JobStatus.PENDING

    monkeypatch.setenv("SEND_ENABLED", "true")
    reload_settings()
    flagged = [a for a in check_preflight(session) if a.code == "send_jobs_pending"]
    assert flagged and str(lead.id) in flagged[0].message
    assert wire.sent == []


# ==========================================================================
# 7. A reply waiting in Gmail stops a released live follow-up (GML-1)
# ==========================================================================


def test_a_released_live_follow_up_reads_the_thread_and_holds_on_a_new_reply(
    session, wire, monkeypatch
):
    import mazz.pipeline.send as send_module

    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", HTTPS)
    reload_settings()
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = ready_law_lead(session, campaign)
    op = operator()
    approve_and_release(op, lead.id)
    drain_sends()
    make_due(session, "followup")
    run_jobs(session)
    approve_and_release(op, lead.id)

    box = session.execute(select(Mailbox)).scalar_one()
    box.gmail_history_id = "600"
    session.commit()
    first = thread_parent(session, fresh(session, lead.id))
    svc = ReadAndWire(box.email_address, [
        gmsg("late", first.provider_thread_id, lead.email, "Re: Quick Question",
             "Yes, call me Thursday.", ts=h.utcnow())])
    monkeypatch.setattr(send_module, "get_email_sender", lambda: live_sender(svc))
    drain_sends()

    assert svc.bodies == [], "the follow-up went out over a reply it had not read"
    session.expire_all()
    lead = fresh(session, lead.id)
    assert (lead.status, lead.sequence_state) == (LeadStatus.HELD, "stopped")
    assert session.execute(select(Reply)).scalar_one().lead_id == lead.id
    assert session.get(Mailbox, box.id).gmail_history_id == "700"
    assert send_jobs(session, lead.id, JobStatus.PENDING, JobStatus.RUNNING) == []


# ==========================================================================
# 8. Guardrail 4's structural invariant
# ==========================================================================


def test_exactly_one_send_job_creation_site_still_holds():
    import test_audit_sending
    import test_sending

    test_sending.test_exactly_one_place_in_the_codebase_creates_a_send_job()
    test_audit_sending.test_no_send_job_or_direct_send_outside_the_known_sites()
