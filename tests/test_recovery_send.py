"""Recovered audit gaps: release identity and in-flight company reservations.

Provider fakes only. Run with TEST_DATABASE_URL pointing to a disposable *_test
database, through the isolation runner; never against a working application DB.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import pipeline_helpers as h
from mazz.integrations.email_sender import StubEmailSender
from mazz.models import Draft, Job, JobStatus, Lead, LeadStatus, SentMessage
from mazz.pipeline.queue import (
    JobContext,
    JobDeferred,
    PermanentJobError,
    claim_job,
    default_handlers,
)
from mazz.pipeline.send import handle_send, send_lead
from test_audit_web import operator
from test_web import env, make_mailbox, make_passed_lead, session  # noqa: F401


def test_captured_running_release_cannot_send_reapproved_edited_copy(session, monkeypatch, tmp_path):
    """Capture the handler's ORM job before cancellation, not just its ID.

    This models process_next having loaded its RUNNING job before a human
    rejects, edits and re-approves. A cached ORM object must not prove a
    release still exists after those HTTP transactions commit.
    """

    import mazz.pipeline.send as sending

    calls = []

    class RecordingSender(StubEmailSender):
        def send(self, message, *, credential_ref):
            calls.append(message)
            return super().send(message, credential_ref=credential_ref)

    monkeypatch.setattr(sending, "get_email_sender",
                        lambda: RecordingSender(outbox_path=tmp_path / "wire.jsonl"))
    lead, draft = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 303

    with h.db_session() as claim_session:
        claimed = claim_job(claim_session, kinds=("send",))
        assert claimed is not None
        job_id = claimed.id

    # A separate handler session is essential: web requests commit independently.
    with h.db_session() as handler_session:
        captured_job = handler_session.get(Job, job_id)
        captured_lead = handler_session.get(Lead, lead.id)
        assert captured_job.status == JobStatus.RUNNING
        assert op.post(f"/leads/{lead.id}/reject", data={"reason": "wrong tone"}).status_code == 200
        assert op.post(f"/leads/{lead.id}/edit", data={
            "subject": draft.subject,
            "body_text": "Dana - saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.",
        }).status_code == 200
        assert op.post(f"/leads/{lead.id}/approve").status_code == 200

        ctx = JobContext(session=handler_session, job=captured_job,
                         lead=captured_lead, handlers=default_handlers())
        try:
            handle_send(ctx)
        except PermanentJobError:
            # Either an explicit refusal or a no-op is safe; reaching the
            # fake wire would mean the old release authorized new copy.
            pass

    session.expire_all()
    assert calls == [], "a cancelled RUNNING release sent a later approved version"
    assert session.execute(select(SentMessage.id)).first() is None
    assert session.get(Lead, lead.id).status == LeadStatus.APPROVED


def test_unbooked_first_step_attempt_reserves_company_capacity(session, tmp_path):
    """Persist the exact state while a provider call is outstanding.

    A first-step marker is committed before the wire; active sequence state
    is set after receipt. Testing that durable interval needs no timing race
    or sleeping threads, and also covers a crash leaving an ambiguous send.
    """

    campaign = h.make_campaign(session, max_active_per_company=1)
    first, first_draft = make_passed_lead(session, email="first@northwind-logistics.com")
    second, _ = make_passed_lead(session, email="second@northwind-logistics.com")
    box = make_mailbox(session)
    # The relationship, not the id: these sessions keep loaded attributes
    # across commits (expire_on_commit=False), so a stale `campaign` would read None.
    first.campaign = second.campaign = campaign
    first.status = second.status = LeadStatus.APPROVED
    first.sequence_state = second.sequence_state = None
    now = datetime.now(timezone.utc)
    first_draft.send_attempted_at = now
    first_draft.send_attempt_mailbox_id = box.id
    session.commit()

    with pytest.raises(JobDeferred, match="company cap"):
        send_lead(session, second,
                  sender=StubEmailSender(outbox_path=tmp_path / "wire.jsonl"), now=now)
    assert session.execute(select(SentMessage.id)).first() is None


@pytest.mark.parametrize("action", ["reject", "edit"])
def test_unresolved_attempt_copy_cannot_be_mutated(session, action):
    lead, draft = make_passed_lead(session)
    box = make_mailbox(session)
    lead.status = LeadStatus.HELD
    draft.send_attempted_at = datetime.now(timezone.utc)
    draft.send_attempt_mailbox_id = box.id
    before = (draft.subject, draft.body, draft.verification_status, draft.verification_notes)
    draft_id = draft.id
    session.commit()

    data = {"reason": "rewrite it"} if action == "reject" else {
        "subject": "Different subject", "body_text": "Different copy",
    }
    response = operator().post(f"/leads/{lead.id}/{action}", data=data)
    assert response.status_code == 409
    session.expire_all()
    after = session.get(Draft, draft_id)
    assert (after.subject, after.body, after.verification_status, after.verification_notes) == before

