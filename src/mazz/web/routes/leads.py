"""Lead table, lead detail, and the approve / edit / reject actions.

This module is the entire control surface for sending anything. Two rules
are enforced here, not just in the templates:

* No auto-send. There is no route that approves more than one lead, and no
  route that approves anything without a specific ``lead_id`` a human clicked
  into. Grep this file for "for lead in" if you doubt it — it is not here.
* A draft that is not ``is_approvable()`` (verification_status != PASSED)
  is rejected by ``approve()`` itself, with a 409, before a send job is ever
  created. The template hides the button too, but that is cosmetic; this
  check is the control.

Approve ENQUEUES; it does not send. The click is the human decision and it is
final in the sense that matters -- nothing else can create a send job -- but the
message leaves from the worker, which owns mailbox rotation, daily caps and
spacing, and which re-checks suppression at the moment of sending. A route
handler that talked to Gmail directly would either ignore the client's daily
caps or block an HTTP request on a provider call, and it would put the
last-moment compliance re-checks in the place least able to retry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.models import (
    Draft,
    Lead,
    LeadStatus,
    Mailbox,
    SentMessage,
    VerificationStatus,
)
from mazz.pipeline.draft import MissingSenderAddress, render_body
from mazz.pipeline.mailbox import (
    MailboxUnavailable,
    NoMailboxConfigured,
    select_mailbox,
)
from mazz.pipeline.queue import enqueue_job
from mazz.pipeline.send import SEND_JOB_KIND
from mazz.pipeline.score import ScoredFact, ScoreResult, score_lead
from mazz.pipeline.verify import (
    Grounding,
    apply_verification,
    build_grounding,
    is_approvable,
    latest_draft,
    verify_content,
)
from mazz.pipeline.score import facts_for_lead
from mazz.schemas import Fact
from mazz.web.auth import current_user

router = APIRouter()

#: All statuses, in the order the table shows them. Deliberately includes
#: `no_angle` and `held` — the brief is explicit that hiding those states
#: would recreate the generic-filler problem the client is trying to escape.
ALL_STATUSES: tuple[LeadStatus, ...] = (
    LeadStatus.RESEARCHING,
    LeadStatus.READY,
    LeadStatus.NO_ANGLE,
    LeadStatus.HELD,
    LeadStatus.APPROVED,
    LeadStatus.SENT,
)


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _first_last(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# --------------------------------------------------------------------------
# Lead table
# --------------------------------------------------------------------------


@router.get("/leads", response_class=HTMLResponse)
async def list_leads(
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)

    status_filter: LeadStatus | None = None
    if status:
        try:
            status_filter = LeadStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")

    query = select(Lead).order_by(Lead.updated_at.desc(), Lead.id.desc())
    if status_filter is not None:
        query = query.where(Lead.status == status_filter)
    leads = list(session.execute(query).scalars().all())

    # One extra query for the "governing draft" per lead, so the table can show
    # a verification hint without the reviewer opening every row.
    drafts_by_lead: dict[int, Draft] = {}
    for lead in leads:
        draft = latest_draft(session, lead)
        if draft is not None:
            drafts_by_lead[lead.id] = draft

    return templates.TemplateResponse(
        request,
        "leads_list.html",
        {
            "leads": leads,
            "drafts_by_lead": drafts_by_lead,
            "all_statuses": ALL_STATUSES,
            "active_status": status_filter,
            "user": user,
        },
    )


# --------------------------------------------------------------------------
# Lead detail
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CitedFact:
    """One signal cited by the draft, paired with its source for the template."""

    id: str
    kind: str
    content: str
    source_url: str | None


def _cited_facts(draft: Draft | None, facts: list[Fact]) -> list[_CitedFact]:
    if draft is None:
        return []
    by_id = {f.id: f for f in facts}
    out: list[_CitedFact] = []
    for fact_id in draft.signals_used or []:
        fact = by_id.get(fact_id)
        if fact is None:
            out.append(_CitedFact(id=fact_id, kind="(missing)", content="", source_url=None))
        else:
            out.append(
                _CitedFact(id=fact.id, kind=fact.kind, content=fact.content, source_url=fact.source_url)
            )
    return out


def _detail_context(
    request: Request, session: Session, lead_id: int, user: str
) -> dict:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    draft = latest_draft(session, lead)
    facts = facts_for_lead(session, lead)
    score_result: ScoreResult = score_lead(session, lead)

    grounding: Grounding | None = None
    if draft is not None:
        grounding = build_grounding(lead, list(draft.signals_used or []), facts)

    return {
        "request": request,
        "lead": lead,
        "draft": draft,
        "cited": _cited_facts(draft, facts),
        "scored": score_result.scored,
        "score_result": score_result,
        "grounding": grounding,
        "can_approve": draft is not None and is_approvable(draft),
        "send_state": _send_state(session, lead),
        "user": user,
    }


@dataclass(frozen=True, slots=True)
class SendState:
    """What the reviewer is told about sending, before and after the click.

    The screen used to say "push to Smartlead" and then, on success, nothing at
    all -- the lead went `sent` and where it went from was Smartlead's business.
    Now it is ours, so the reviewer sees which of the client's mailboxes will
    carry the message, and afterwards which one did.
    """

    #: Which mailbox would send it, if one can right now.
    mailbox: Mailbox | None = None
    #: Set when every mailbox is capped or spacing: the message waits, and this
    #: says until when, so a queued lead does not read as a broken one.
    waiting_until: datetime | None = None
    #: Set when no mailbox can EVER serve this lead. Approve refuses on this.
    problem: str | None = None
    #: After the fact: the audit row for the message that actually left.
    sent: SentMessage | None = None

    @property
    def sent_from(self) -> str | None:
        return self.sent.from_address if self.sent is not None else None


def _send_state(session: Session, lead: Lead) -> SendState:
    if lead.status == LeadStatus.SENT:
        sent = session.execute(
            select(SentMessage)
            .where(SentMessage.lead_id == lead.id)
            .order_by(SentMessage.sent_at.desc())
            .limit(1)
        ).scalars().first()
        return SendState(sent=sent)

    try:
        return SendState(mailbox=select_mailbox(session, campaign_id=lead.campaign_id))
    except MailboxUnavailable as exc:
        return SendState(waiting_until=exc.retry_at)
    except NoMailboxConfigured as exc:
        return SendState(problem=str(exc))


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    context = _detail_context(request, session, lead_id, user)
    return templates.TemplateResponse(request, "lead_detail.html", context)


def _draft_panel_response(request: Request, session: Session, lead_id: int, user: str, *, status_code: int = 200):
    templates = _templates(request)
    context = _detail_context(request, session, lead_id, user)
    return templates.TemplateResponse(
        request, "partials/draft_panel.html", context, status_code=status_code
    )


# --------------------------------------------------------------------------
# Actions: approve / edit / reject
# --------------------------------------------------------------------------


@router.post("/leads/{lead_id}/approve", response_class=HTMLResponse)
async def approve_lead(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Approve exactly one lead's current draft and queue it for sending.

    Server-side gate: a draft whose `verification_status` is not PASSED is
    refused here with a 409, regardless of what the UI shows. This is the
    control the brief requires — not the hidden button.

    The lead goes to ``approved``, not ``sent``. It becomes ``sent`` when the
    message actually leaves, which the worker records against a real receipt.
    Marking it sent at click time would be a lie the client could not detect:
    a screen saying "sent" for an email still sitting behind a daily cap, or
    one that failed on a dead credential.
    """

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    if lead.status == LeadStatus.SENT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"lead {lead_id} has already been sent. Approving again would "
                "email the same person the same thing twice."
            ),
        )

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to approve")

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

    # Tell the reviewer now if nothing could ever send this. A mailbox that is
    # merely capped or spacing is fine — the job waits for it — but a roster
    # with no active mailbox at all would queue work that can only fail, and
    # the click would look like it worked.
    try:
        select_mailbox(session, campaign_id=lead.campaign_id)
    except NoMailboxConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MailboxUnavailable:
        pass

    lead.status = LeadStatus.APPROVED
    # enqueue_job is idempotent for an identical pending job, so a double-click
    # queues one send, not two.
    enqueue_job(session, lead.id, SEND_JOB_KIND)
    session.flush()

    return _draft_panel_response(request, session, lead_id, user)


@router.post("/leads/{lead_id}/reject", response_class=HTMLResponse)
async def reject_lead(
    request: Request,
    lead_id: int,
    reason: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """A human declines to send this draft. Holds the lead; never auto-anything.

    Reuses `held` / `verification_status=failed` rather than inventing a new
    lead status, so `is_approvable()` continues to be the single source of
    truth for "may this be sent" — a rejected draft is, correctly, not
    approvable until a new draft replaces it.
    """

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to reject")

    note = f"REJECTED by reviewer ({user}).\n" + (reason.strip() or "(no reason given)")
    draft.verification_status = VerificationStatus.FAILED
    draft.verification_notes = note
    lead.status = LeadStatus.HELD
    session.flush()

    return _draft_panel_response(request, session, lead_id, user)


@router.post("/leads/{lead_id}/edit", response_class=HTMLResponse)
async def edit_draft(
    request: Request,
    lead_id: int,
    subject: str = Form(...),
    body_text: str = Form(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """A human rewrites the draft's subject/body, then it is re-verified.

    Editing does not grant approvability by itself — the edited copy is run
    back through `verify_content` (the same compliance control drafting
    goes through) before the lead can move anywhere. Loosening or skipping
    that check on the "someone reviewed it" theory is exactly the softened
    control the brief warns against.
    """

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to edit")

    try:
        body = render_body(body_text, lead)
    except MissingSenderAddress as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    draft.subject = subject
    draft.body = body

    facts = facts_for_lead(session, lead)
    result = verify_content(
        lead=lead,
        subject=draft.subject,
        body=draft.body,
        signals_used=list(draft.signals_used or []),
        available_facts=facts,
    )
    apply_verification(session, lead, draft, result)

    return _draft_panel_response(request, session, lead_id, user)
