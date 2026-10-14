"""Removal requests and the never-contact list, for the people who get them.

Maaz owns removal requests; Sean forwards anything he receives. Either can add
an email or a domain here and it takes effect on the next check -- there is
no batch, no delay, and no approval step between "remove me" and removed.

Scope: GLOBAL (every workspace) is the default and the only choice for an
email, because a person who asked to stop asked all of us. A domain may be
scoped to one of the user's own workspaces, which is how "Sean's current
clients" blocks Sean's campaigns without stopping ClientRise's.

"Delete this person" erases everything held about them (delete-by-person) and
leaves a suppression tombstone so the next import cannot bring them back.
Operator only: it is irreversible and crosses workspaces.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.ingest.csv_import import email_sha256
from mazz.models import Lead, LeadStatus, SuppressionEntry, Workspace
from mazz.pipeline.sequence import LIVE_STATES
from mazz.privacy import delete_person
from mazz.web.auth import Account, current_account, current_user

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.IGNORECASE)
_STOPPABLE = (LeadStatus.RESEARCHING, LeadStatus.READY, LeadStatus.APPROVED)


def _workspaces(session: Session, account: Account) -> list[Workspace]:
    query = select(Workspace).order_by(Workspace.name)
    if not account.all_workspaces:
        query = query.where(Workspace.id.in_(sorted(account.workspace_ids)))
    return list(session.execute(query).scalars())


def _entries(session: Session, account: Account) -> list[SuppressionEntry]:
    query = select(SuppressionEntry).order_by(SuppressionEntry.added_at.desc()).limit(200)
    if not account.all_workspaces:
        # Global rows hold other workspaces' unsubscribers; a scoped user sees
        # only his own workspace's entries (and can still add global ones).
        query = query.where(SuppressionEntry.workspace_id.in_(sorted(account.workspace_ids)))
    return list(session.execute(query).scalars())


def _render(request, session, account, user, *, message=None, error=None, status_code=200):
    return request.app.state.templates.TemplateResponse(
        request,
        "removals.html",
        {
            "entries": _entries(session, account),
            "workspaces": _workspaces(session, account),
            "can_delete": account.all_workspaces,
            "message": message,
            "error": error,
            "user": user,
        },
        status_code=status_code,
    )


@router.get("/removals", response_class=HTMLResponse)
def removals(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    return _render(request, session, account, user)


def stop_matching_leads(
    session: Session, *, email: str | None, domain: str | None, actor: str
) -> int:
    """Pull every not-yet-sent lead a new GLOBAL entry covers out of the send
    path, and stop the sequence of any it covers mid-sequence."""

    query = select(Lead).where(
        or_(Lead.status.in_(_STOPPABLE), Lead.sequence_state.in_(LIVE_STATES))
    )
    if email:
        query = query.where(Lead.email == email)
    else:
        query = query.where(
            or_(
                Lead.company_domain == domain,
                Lead.company_domain.like(f"%.{domain}"),
                Lead.email.like(f"%@{domain}"),
                Lead.email.like(f"%.{domain}"),
            )
        )
    held = 0
    for lead in session.execute(query).scalars():
        if lead.status in _STOPPABLE:
            lead.status = LeadStatus.HELD
        if lead.sequence_state in LIVE_STATES:
            lead.sequence_state, lead.next_step_due_at = "stopped", None
        audit.record(session, actor, "hold_suppressed", "lead", lead.id)
        held += 1
    return held


@router.post("/removals", response_class=HTMLResponse)
def add_removal(
    request: Request,
    value: str = Form(...),
    scope: str = Form("global"),
    reason: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    raw = value.strip().lower()
    email = raw if _EMAIL_RE.match(raw) else None
    domain = None
    if email is None:
        domain = raw.removeprefix("@").removeprefix("www.")
        if not _DOMAIN_RE.match(domain):
            return _render(request, session, account, user, status_code=400,
                           error=f"{value!r} is neither an email address nor a domain")

    workspace_id = None
    if scope != "global":
        if email is not None:
            return _render(request, session, account, user, status_code=400,
                           error="an email removal is always global: the person asked all of us to stop")
        workspace_id = int(scope) if scope.isdigit() else -1
        if not account.sees_workspace(workspace_id):
            return _render(request, session, account, user, status_code=403,
                           error="you can only scope a block to your own workspace")

    if domain and workspace_id is None and not account.all_workspaces:
        return _render(request, session, account, user, status_code=403,
                       error="a global domain block stops every workspace; scope it to "
                             "your workspace or ask an operator")

    entry = SuppressionEntry(
        email=email,
        email_sha256=email_sha256(email) if email else None,
        domain=domain,
        workspace_id=workspace_id,
        reason=reason.strip() or "removal request",
        added_by=user,
    )
    session.add(entry)
    session.flush()
    # A workspace-scoped block is enforced at every check (ingest, release,
    # send); only a global entry is broad enough to hold leads wholesale here.
    held = (
        stop_matching_leads(session, email=email, domain=domain, actor=user)
        if workspace_id is None
        else 0
    )
    audit.record(
        session, user, "suppress", "suppression", entry.id,
        scope="global" if workspace_id is None else f"workspace:{workspace_id}",
        leads_held=held,
    )
    return _render(request, session, account, user,
                   message=f"Added {email or domain}. {held} lead(s) held or stopped.")


@router.post("/removals/delete-person", response_class=HTMLResponse)
def delete_person_route(
    request: Request,
    email: str = Form(...),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    if not account.all_workspaces:
        return _render(request, session, account, user, status_code=403,
                       error="deleting a person is operator-only; add a removal instead")
    email = email.strip().lower()
    if confirm.strip() != "DELETE":
        return _render(request, session, account, user, status_code=400,
                       error="type DELETE in the confirmation box to erase this person")
    report = delete_person(email, session=session, added_by=user)
    audit.record(
        session, user, "delete_person", "suppression", None,
        email_sha256=email_sha256(email), leads_deleted=report.leads_deleted,
    )
    return _render(request, session, account, user, message=report.summary())
