"""Lead table, lead detail, and the approve / edit / reject actions.

This module is the entire control surface for sending anything. Two rules
are enforced here, not just in the templates:

* No auto-send. There is no route that approves more than one lead, and no
  route that approves anything without a specific ``lead_id`` a human clicked
  into. Grep this file for "for lead in" if you doubt it — it is not here.
* A draft that is not ``is_approvable()`` (verification_status != PASSED)
  is rejected by ``approve()`` itself, with a 409, before Smartlead is ever
  touched. The template hides the button too, but that is cosmetic; this
  check is the control.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.integrations.smartlead import SmartleadLead, get_smartlead_client
from mazz.models import Draft, Lead, LeadStatus, VerificationStatus
from mazz.pipeline.draft import MissingSenderAddress, render_body
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
        "user": user,
    }


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
    """Approve exactly one lead's current draft and push it to Smartlead.

    Server-side gate: a draft whose `verification_status` is not PASSED is
    refused here with a 409, regardless of what the UI shows. This is the
    control the brief requires — not the hidden button.
    """

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

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

    first_name, last_name = _first_last(lead.full_name)
    smartlead_lead = SmartleadLead(
        email=lead.email,
        first_name=first_name,
        last_name=last_name,
        company_name=lead.company_name or "",
        website=lead.website or "",
        location=lead.location or "",
        linkedin_profile=lead.linkedin_url or "",
        custom_fields={"subject": draft.subject, "body": draft.body},
    )

    client = get_smartlead_client()
    client.add_lead_to_campaign(smartlead_lead)

    lead.status = LeadStatus.SENT
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
