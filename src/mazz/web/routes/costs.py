"""Cost entries: what the client actually spends, month by month.

Operator only (it is the client's money). Entries are append-and-delete, never
edited in place, and every add or delete is attributed in the audit trail.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.metrics import BASELINE, COST_CATEGORIES, month_metrics, month_start, parse_month
from mazz.models import CostEntry, Workspace
from mazz.web.auth import current_user, require_operator

router = APIRouter(dependencies=[Depends(require_operator)])

MAX_AMOUNT = Decimal("10000000")


def parse_amount(raw: str) -> Decimal:
    """Dollars and cents, finite, 0 <= x <= 10M. Raises ValueError otherwise."""

    try:
        value = Decimal((raw or "").strip().replace(",", "").lstrip("$"))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not an amount") from None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT:
        raise ValueError("amount must be between 0 and 10,000,000")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _render(request: Request, session: Session, month, *, error=None, status_code=200):
    entries = list(
        session.execute(
            select(CostEntry).where(CostEntry.month == month).order_by(CostEntry.id)
        ).scalars()
    )
    workspaces = list(session.execute(select(Workspace).order_by(Workspace.name)).scalars())
    names = {w.id: w.name for w in workspaces}
    return request.app.state.templates.TemplateResponse(
        request,
        "costs.html",
        {
            "month": month,
            "month_value": f"{month:%Y-%m}",
            "entries": entries,
            "workspaces": workspaces,
            "workspace_names": names,
            "categories": COST_CATEGORIES,
            "metrics": month_metrics(session, month),
            "baseline": BASELINE,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/costs", response_class=HTMLResponse)
async def costs(request: Request, month: str | None = None, session: Session = Depends(get_session)):
    return _render(request, session, parse_month(month))


@router.post("/costs", response_class=HTMLResponse)
async def add_cost(
    request: Request,
    month: str = Form(...),
    workspace_id: int = Form(...),
    category: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    first = month_start(parse_month(month))
    problems = []
    if category not in COST_CATEGORIES:
        problems.append(f"unknown category {category!r}")
    if session.get(Workspace, workspace_id) is None:
        problems.append("unknown workspace")
    try:
        value = parse_amount(amount)
    except ValueError as exc:
        problems.append(str(exc))
    if problems:
        return _render(request, session, first, error="; ".join(problems), status_code=400)

    entry = CostEntry(
        workspace_id=workspace_id, month=first, category=category, amount=value,
        note=note.strip() or None, entered_by=user,
    )
    session.add(entry)
    session.flush()
    audit.record(session, user, "cost_add", "cost_entry", entry.id,
                 month=f"{first:%Y-%m}", category=category, amount=str(value),
                 workspace_id=workspace_id)
    return RedirectResponse(url=f"/costs?month={first:%Y-%m}", status_code=303)


@router.post("/costs/{entry_id}/delete", response_class=HTMLResponse)
async def delete_cost(
    entry_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    entry = session.get(CostEntry, entry_id)
    month = entry.month if entry else month_start(parse_month(None))
    if entry is not None:
        audit.record(session, user, "cost_delete", "cost_entry", entry.id,
                     month=f"{entry.month:%Y-%m}", category=entry.category,
                     amount=str(entry.amount), entered_by=entry.entered_by)
        session.delete(entry)
    return RedirectResponse(url=f"/costs?month={month:%Y-%m}", status_code=303)
