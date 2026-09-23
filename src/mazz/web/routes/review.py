"""The two-pane draft-review screen: `GET /review`.

This is the screen the client actually lives in -- a searchable lead list on
the left, the full draft (and the evidence it rests on) on the right, worked
down one lead at a time. `/leads` stays as it is for bulk oversight; this is
its Smartlead-equivalent workspace rhythm.

Nothing here re-implements a control. Approve, reject and edit still live in
`mazz.web.routes.leads` -- the module that owns "no auto-send" and "a failed
draft can never be approved" -- and this screen's right pane posts straight
to those routes. This module only owns getting the right lead onto the
screen: search, the status facet, and picking the panel to show.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.models import Draft, Lead, LeadStatus, VerificationStatus
from mazz.web.auth import current_user
from mazz.web.routes.leads import ALL_STATUSES, _detail_context

router = APIRouter()

#: Hard cap on the left pane. `leads.py`'s `/leads` query has no LIMIT at
#: all, which means every page load walks the entire leads table; this
#: route exists in part to not repeat that.
LIST_LIMIT = 100


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _parse_status(raw: str | None) -> LeadStatus | None:
    """An unrecognised or blank status is treated as "no filter", not an
    error -- this value round-trips through query params and hidden form
    fields the operator never types by hand, so there is no case where a
    junk value here should surface as a 400 the way `/leads`' does for a
    hand-typed URL.
    """

    if not raw:
        return None
    try:
        return LeadStatus(raw)
    except ValueError:
        return None


def _initials(full_name: str) -> str:
    """CSS-drawn avatar initials -- no image, no avatar service."""

    parts = [p for p in (full_name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@dataclass(frozen=True, slots=True)
class _Row:
    lead: Lead
    verification: VerificationStatus | None


def _search_rows(
    session: Session,
    *,
    q: str | None,
    status: LeadStatus | None,
    limit: int | None = None,
) -> tuple[list[_Row], int]:
    """One query for the left pane: every lead plus its governing draft's
    verification status, no per-row round trip.

    `leads.py`'s `/leads` list has two problems this deliberately does not
    repeat: no LIMIT (a full table scan on every load) and an N+1
    (`latest_draft()` called once per lead in a loop). Here "the newest
    draft per lead" is a single window-function subquery joined once, and
    the LIMIT is on the same statement; the total match count is one cheap
    extra aggregate so the search box can say how many leads matched
    without loading them all.
    """

    if limit is None:
        limit = LIST_LIMIT

    latest_rank = (
        select(
            Draft.lead_id,
            Draft.verification_status,
            func.row_number()
            .over(
                partition_by=Draft.lead_id,
                order_by=(Draft.created_at.desc(), Draft.id.desc()),
            )
            .label("rn"),
        )
        .subquery()
    )
    latest = select(latest_rank).where(latest_rank.c.rn == 1).subquery()

    base = select(Lead, latest.c.verification_status).outerjoin(
        latest, latest.c.lead_id == Lead.id
    )

    if status is not None:
        base = base.where(Lead.status == status)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        base = base.where(
            or_(
                Lead.full_name.ilike(pattern),
                Lead.email.ilike(pattern),
                Lead.company_name.ilike(pattern),
            )
        )

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    rows_query = base.order_by(Lead.updated_at.desc(), Lead.id.desc()).limit(limit)
    rows = [
        _Row(lead=lead, verification=verification)
        for lead, verification in session.execute(rows_query).all()
    ]
    return rows, total


def _empty_panel_context() -> dict:
    """What the right pane gets when there is no lead to select at all --
    an empty database, or a search with zero matches and nothing already
    selected. Shaped the same as `_detail_context`'s return so the template
    never has to special-case a missing key.
    """

    return {
        "lead": None,
        "draft": None,
        "cited": [],
        "can_approve": False,
        "send_state": None,
        "editable_body": "",
    }


@router.get("/review", response_class=HTMLResponse)
async def review(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    lead_id: int | None = None,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    status_filter = _parse_status(status)
    rows, total = _search_rows(session, q=q, status=status_filter)

    selected_id = lead_id
    if selected_id is None and rows:
        selected_id = rows[0].lead.id

    if selected_id is not None:
        panel_context = _detail_context(request, session, selected_id, user)
    else:
        panel_context = _empty_panel_context()
    panel_context["error"] = None

    context = {
        "request": request,
        "rows": rows,
        "total": total,
        "limit": LIST_LIMIT,
        "q": q or "",
        "all_statuses": ALL_STATUSES,
        "active_status": status_filter,
        "selected_lead_id": selected_id,
        "initials": _initials,
        "user": user,
        **panel_context,
    }
    return templates.TemplateResponse(request, "review.html", context)


@router.get("/review/list", response_class=HTMLResponse)
async def review_list(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    lead_id: int | None = None,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """HTMX partial: just the left-pane status facet + rows.

    Driven by the search box (`hx-trigger="keyup changed delay:300ms"`) and
    by the status facet links -- both target the same fragment so the active
    status highlight and the row list can never go out of sync with each
    other.
    """

    templates = _templates(request)
    status_filter = _parse_status(status)
    rows, total = _search_rows(session, q=q, status=status_filter)
    return templates.TemplateResponse(
        request,
        "partials/review_rows.html",
        {
            "rows": rows,
            "total": total,
            "limit": LIST_LIMIT,
            "q": q or "",
            "all_statuses": ALL_STATUSES,
            "active_status": status_filter,
            "selected_lead_id": lead_id,
            "initials": _initials,
        },
    )


@router.get("/review/{lead_id}/panel", response_class=HTMLResponse)
async def review_panel(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """HTMX partial: just the right pane, for a row click."""

    templates = _templates(request)
    context = _detail_context(request, session, lead_id, user)
    context["error"] = None
    return templates.TemplateResponse(request, "partials/review_panel.html", context)
