"""The outbox: approved messages waiting for a human to put them on the wire.

This module is the ONLY place in the codebase that creates a send job. Approving
a draft (`mazz.web.routes.leads`) records a judgement about copy and queues
nothing; releasing it is a second, separate action by the same human, here.

Why the split is worth an extra click
-------------------------------------
When approve enqueued the send itself, "this copy is correct" and "put this on
the wire now" were one decision, and there was no instant in between at which an
approval could be reconsidered. Now there is. It also means the operational
question -- is a mailbox free, is the roster healthy -- is asked on the screen
that is about sending, instead of blocking a reviewer who only wanted to say the
copy reads right.

What is deliberately absent
---------------------------
There is no select-all, no "send all", no bulk control, and no route that sends
more than one ``lead_id``. Grep this file for "for lead in" -- the only loop is
the one that BUILDS the read-only table. The absence is the feature, and
guardrail 4 is what it implements.

Every check the worker will run at send time is run here too, eagerly, so the
operator learns now rather than after the job fails: the approval is current, the
draft still passes verification, the recipient is still not suppressed, and a
mailbox can serve. None of those is trusted afterwards -- `pipeline.send` runs
all four again at the moment of sending, because every one of them can change
between this click and the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.ingest.csv_import import SuppressionChecker, SuppressionUnavailable
from mazz.models import Campaign, Draft, Job, JobStatus, Lead, LeadStatus, Mailbox
from mazz.pipeline.mailbox import (
    MailboxUnavailable,
    NoMailboxConfigured,
    headroom,
    next_eligible_at,
    select_mailbox,
)
from mazz.pipeline.draft import unresolved_placeholders
from mazz.pipeline.queue import enqueue_job
from mazz.pipeline.send import SEND_JOB_KIND
from mazz.pipeline.sequence import thread_parent
from mazz.pipeline.verify import is_approvable, latest_draft
from mazz.web.auth import Account, current_account, current_user, visible_lead

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One approved message, plus everything the operator needs to judge it."""

    lead: Lead
    draft: Draft
    queued: bool
    #: Why this cannot be released right now, in the operator's words. None
    #: means it can. This is advisory: the send route re-checks regardless.
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class RosterRow:
    """A mailbox's remaining capacity, for the header strip."""

    mailbox: Mailbox
    headroom: int
    next_eligible_at: datetime


def _pending_send_lead_ids(session: Session) -> set[int]:
    """Leads with a send job already queued or running.

    A released message must not offer a Send button that would do nothing --
    ``enqueue_job`` de-duplicates, so a second click is harmless, but showing an
    active button for work already in flight misrepresents the state.
    """

    rows = session.execute(
        select(Job.lead_id).where(
            Job.kind == SEND_JOB_KIND,
            Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
        )
    ).scalars()
    return set(rows)


def cancel_pending_send(session: Session, lead_id: int) -> int:
    """Withdraw a release that has not started: delete the lead's PENDING send job.

    Called by reject/edit/approve in `leads.py`, which deliberately does not
    import the send-job kind itself. A release is a decision about one version
    of the copy; once that copy is rejected or edited the old click must not
    carry the new text out. A RUNNING job is not touched (`send_lead` re-reads
    the lead under a row lock and refuses anything no longer approved). This
    only DELETES; it creates nothing.
    """

    result = session.execute(
        delete(Job).where(
            Job.lead_id == lead_id,
            Job.kind == SEND_JOB_KIND,
            Job.status == JobStatus.PENDING,
        )
    )
    return result.rowcount or 0


def mailbox_for(session: Session, lead: Lead) -> Mailbox:
    """The mailbox that would carry this lead's next message, as `send_lead`
    chooses it: a follow-up leaves from the mailbox that owns the thread (the
    pool is never widened), anything else from the campaign's rotation."""

    parent = thread_parent(session, lead)
    return select_mailbox(
        session,
        campaign_id=lead.campaign_id,
        only=parent.mailbox_id if parent is not None else None,
    )


def _roster(session: Session, *, now: datetime, account: Account | None = None) -> list[RosterRow]:
    query = select(Mailbox).order_by(Mailbox.email_address)
    if account is not None and not account.all_workspaces:
        # A scoped user sees the general rotation and his own campaigns'
        # mailboxes, not another client's pinned sending accounts.
        query = query.where(
            or_(
                Mailbox.campaign_id.is_(None),
                Mailbox.campaign_id.in_(
                    select(Campaign.id).where(Campaign.workspace_id.in_(sorted(account.workspace_ids)))
                ),
            )
        )
    mailboxes = session.execute(query).scalars().all()
    return [
        RosterRow(
            mailbox=mailbox,
            headroom=headroom(session, mailbox, now=now),
            next_eligible_at=next_eligible_at(mailbox, now=now),
        )
        for mailbox in mailboxes
    ]


def _sequence_block(lead: Lead, *, now: datetime | None = None) -> str | None:
    """A reply, bounce or unsubscribe stopped the sequence, or an out-of-office
    paused it, since approval. The send step refuses either; say so first."""

    now = now or datetime.now(timezone.utc)
    if lead.sequence_state == "stopped":
        return "sequence stopped since approval (a reply, bounce or unsubscribe)"
    if lead.sequence_state == "paused" and (lead.next_step_due_at or now) > now:
        return f"paused (out of office) until {lead.next_step_due_at:%Y-%m-%d %H:%M} UTC"
    return None


def _blocked_reason(session: Session, lead: Lead, draft: Draft | None) -> str | None:
    """The first reason this message cannot go out, or None."""

    if draft is None:
        return "no draft"
    if not is_approvable(draft):
        return (
            f"verification is {draft.verification_status.value}, not passed — "
            "it was re-drafted or rejected after approval"
        )
    holes = unresolved_placeholders(draft.subject, draft.body)
    if holes:
        return f"unfilled merge fields or no subject {holes}"
    stopped = _sequence_block(lead)
    if stopped:
        return stopped
    try:
        match = SuppressionChecker(session).check_lead(lead)
        if match is not None:
            return f"suppressed since approval — {match.describe()}"
    except SuppressionUnavailable:
        # Fail closed. An unreadable suppression list is not permission.
        return "suppression list unreadable — refusing to send"
    try:
        mailbox_for(session, lead)
    except MailboxUnavailable as exc:
        return f"every mailbox is capped or spacing ({exc})"
    except NoMailboxConfigured as exc:
        return str(exc)
    return None


@router.get("/outbox", response_class=HTMLResponse)
async def outbox(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """Approved messages that have not left yet. Read-only; sending is a POST."""

    now = datetime.now(timezone.utc)

    leads = list(
        session.execute(
            select(Lead)
            .where(Lead.status == LeadStatus.APPROVED, account.lead_filter())
            .order_by(
                Lead.sequence_step.desc(),  # follow-ups ahead of new leads
                Lead.approved_at.asc().nulls_last(),
                Lead.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    queued = _pending_send_lead_ids(session)

    rows = []
    for lead in leads:
        draft = latest_draft(session, lead)
        rows.append(
            OutboxRow(
                lead=lead,
                draft=draft,
                queued=lead.id in queued,
                blocked_reason=_blocked_reason(session, lead, draft),
            )
        )

    return _templates(request).TemplateResponse(
        request,
        "outbox.html",
        {
            "rows": rows,
            "roster": _roster(session, now=now, account=account),
            "now": now,
            "user": user,
        },
    )


@router.post("/outbox/{lead_id}/send")
async def release_one(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """Put exactly one approved message on the queue. One lead_id, one job.

    Every refusal below is a 409 or 503 rather than a silent skip, because a
    release that quietly does nothing is indistinguishable from one that worked.
    """

    lead = visible_lead(session, account, lead_id)

    if lead.status == LeadStatus.SENT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"lead {lead_id} has already been sent. Releasing it again "
                "would email the same person the same thing twice."
            ),
        )

    if lead.status != LeadStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"lead {lead_id} is {lead.status.value}, not approved. Only an "
                "approved lead can be released, and approval is a human "
                "decision made on the review screen."
            ),
        )

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to send")

    if not is_approvable(draft):
        raise HTTPException(
            status_code=409,
            detail=(
                f"draft {draft.id} is not approvable "
                f"(verification_status={draft.verification_status.value}); "
                "a failed or pending draft can never be sent. "
                f"verification_notes: {draft.verification_notes or '(none)'}"
            ),
        )

    holes = unresolved_placeholders(draft.subject, draft.body)
    if holes:
        raise HTTPException(
            status_code=409,
            detail=(
                f"draft {draft.id} has unfilled merge fields or no subject {holes}; "
                "edit it first. Not released."
            ),
        )

    # Suppression, now. The approval may be hours old; an unsubscribe since then
    # outranks it. An unreadable list is a refusal, never a pass.
    # NOTE ON THE EXPLICIT COMMITS BELOW
    # `get_session` rolls the request back on any exception, so raising an
    # HTTPException straight after a flush would discard the hold and leave the
    # lead sitting in /outbox as approved-but-blocked, to be retried forever.
    # The hold is the durable half of the refusal, so it is committed before
    # the exception is raised, not flushed and hoped for.
    blocked = _sequence_block(lead)
    if blocked:
        if lead.sequence_state == "stopped":
            lead.status = LeadStatus.HELD  # as the send step would; committed, see below
            session.commit()
        raise HTTPException(status_code=409, detail=f"lead {lead_id}: {blocked}. Not released.")

    try:
        match = SuppressionChecker(session).check_lead(lead)
    except SuppressionUnavailable as exc:
        lead.status = LeadStatus.HELD
        session.commit()
        raise HTTPException(
            status_code=503,
            detail=(
                f"suppression list unreadable, refusing to send: {exc}. Lead "
                "held — an unreadable never-contact list and a list with this "
                "person on it look identical from here."
            ),
        ) from exc
    if match is not None:
        lead.status = LeadStatus.HELD
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=(
                f"{lead.email} is suppressed ({match.describe()}) — approved "
                "earlier, suppressed since. Held, not sent."
            ),
        )

    try:
        mailbox_for(session, lead)
    except NoMailboxConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MailboxUnavailable:
        # Capped or spacing is fine: the job waits, and a deferral does not
        # spend a retry attempt. Queue it and let the worker pace it.
        pass

    # THE one place a send job is created. enqueue_job is idempotent for an
    # identical pending job, so a double-click releases one message, not two.
    job, _ = enqueue_job(session, lead.id, SEND_JOB_KIND)
    if lead.approved_at is not None and job.created_at < lead.approved_at:
        # Deduped onto a job released for an EARLIER approval, still running:
        # it will refuse to send this copy, so this click would vanish (QA-01).
        # Both stamps are the database clock.
        raise HTTPException(
            status_code=409,
            detail=(
                f"lead {lead_id}: a send released before the latest approval is "
                "still finishing and will not send this copy. Release again in "
                "a minute. Not released."
            ),
        )
    session.flush()
    audit.record(session, user, "release_send", "lead", lead.id, draft_id=draft.id)

    return RedirectResponse(url="/outbox", status_code=303)
