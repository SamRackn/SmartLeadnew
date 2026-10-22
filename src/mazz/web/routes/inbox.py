"""The inbox: replies to our emails, classified, for a human to tag.

The system classifies mechanically (ooo, unsubscribe, negative, needs_review)
and stops sequences on its own. It NEVER decides "interested": that and every
later stage (qualified conversation, meeting booked, meeting showed, client
won) is a button a person presses, attributed and dated. Cost per qualified
conversation (WP6) counts these rows, by the month they were tagged.

Scoped like every lead screen: a workspace-scoped user sees only his own
workspace's replies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mazz import audit
from mazz.db import get_session
from mazz.models import AuditEvent, Lead, Reply, ReplyTag, SuppressionEntry
from mazz.pipeline.sequence import business_hours_between
from mazz.web.auth import Account, current_account, current_user, require_operator, visible_lead

router = APIRouter()

#: The human stages, in order.
TAGS = ("interested", "qualified", "meeting_booked", "meeting_showed", "client_won")
#: An interested reply nobody has dealt with after this many business hours is
#: highlighted (client rule: respond within 2 business hours).
OVERDUE_BUSINESS_HOURS = 2.0


@dataclass(slots=True)
class InboxRow:
    reply: Reply
    lead: Lead
    tags: set[str]
    age_hours: float
    overdue: bool


def _rows(session: Session, account: Account, *, show_all: bool, now: datetime) -> list[InboxRow]:
    query = (
        select(Reply, Lead)
        .join(Lead, Lead.id == Reply.lead_id)
        .where(account.lead_filter())
        .options(selectinload(Reply.tags))
        .order_by(Reply.received_at.desc())
    )
    if not show_all:
        # In SQL, before the limit: filtering after it let 500 newer handled
        # replies push an old unhandled one off the default screen.
        query = query.where(Reply.handled_at.is_(None), Reply.classification != "ooo")
    query = query.limit(500)
    rows = []
    for reply, lead in session.execute(query).all():
        tags = {t.tag for t in reply.tags}
        overdue = (
            "interested" in tags
            and reply.handled_at is None
            and business_hours_between(reply.received_at, now) > OVERDUE_BUSINESS_HOURS
        )
        rows.append(
            InboxRow(
                reply=reply,
                lead=lead,
                tags=tags,
                age_hours=(now - reply.received_at).total_seconds() / 3600,
                overdue=overdue,
            )
        )
    rows.sort(key=lambda r: (not r.overdue,))  # stable: overdue first, newest within
    return rows


@router.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request,
    show: str = "attention",
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    now = datetime.now(timezone.utc)
    rows = _rows(session, account, show_all=show == "all", now=now)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "user": user,
            "rows": rows,
            "tags": TAGS,
            "show": show,
            "overdue_count": sum(r.overdue for r in rows),
            "overdue_hours": OVERDUE_BUSINESS_HOURS,
            "operator": account.all_workspaces,
        },
    )


def _reply_for(session: Session, account: Account, reply_id: int) -> Reply:
    reply = session.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="reply not found")
    visible_lead(session, account, reply.lead_id)  # 404s for another workspace's
    return reply


@router.post("/inbox/{reply_id}/tag")
async def tag_reply(
    reply_id: int,
    tag: str = Form(...),
    on: str = Form("1"),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    if tag not in TAGS:
        raise HTTPException(status_code=400, detail=f"unknown tag {tag!r}; expected one of {TAGS}")
    reply = _reply_for(session, account, reply_id)
    existing = session.execute(
        select(ReplyTag).where(ReplyTag.reply_id == reply.id, ReplyTag.tag == tag)
    ).scalar_one_or_none()
    if on == "1" and existing is None:
        session.add(ReplyTag(reply_id=reply.id, tag=tag, tagged_by=user))
        audit.record(session, user, "tag", "reply", reply.id, tag=tag, lead_id=reply.lead_id)
    elif on != "1" and existing is not None:
        session.delete(existing)
        audit.record(session, user, "untag", "reply", reply.id, tag=tag, lead_id=reply.lead_id)
    return RedirectResponse(url="/inbox", status_code=303)


@router.post("/inbox/{reply_id}/handled")
async def mark_handled(
    reply_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    reply = _reply_for(session, account, reply_id)
    if reply.handled_at is None:
        reply.handled_at = datetime.now(timezone.utc)
        reply.handled_by = user
        audit.record(session, user, "reply_handled", "reply", reply.id, lead_id=reply.lead_id)
    return RedirectResponse(url="/inbox", status_code=303)


@router.post("/inbox/{reply_id}/confirm")
async def confirm_automatic_action(
    reply_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """A person confirms the automatic unsubscribe suppression was right."""

    reply = _reply_for(session, account, reply_id)
    if reply.needs_confirmation:
        reply.needs_confirmation = False
        audit.record(session, user, "reply_confirmed", "reply", reply.id, lead_id=reply.lead_id)
    return RedirectResponse(url="/inbox", status_code=303)


@router.post("/inbox/{reply_id}/lift")
async def lift_automatic_suppression(
    reply_id: int,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(require_operator),
):
    """Undo a false-positive auto-unsubscribe ("please don't stop emailing me").

    Operator only, typed confirmation, and ONLY the global row reply sync
    itself created from this reply's wording. A suppression a person, an
    import, a bounce or the recipient's own unsubscribe link put there is
    never lifted here; nor is this one if the person also used the link.
    The sequence stays stopped: they replied, so a human answers.
    """

    from mazz.pipeline.replies import AUTO_SUPPRESSED_BY, UNSUBSCRIBE

    reply = _reply_for(session, account, reply_id)
    if confirm.strip().upper() != "LIFT":
        raise HTTPException(status_code=400, detail="type LIFT to confirm lifting the suppression")
    if reply.classification != UNSUBSCRIBE:
        raise HTTPException(status_code=409, detail="this reply did not suppress anyone")
    lead = session.get(Lead, reply.lead_id)
    entry = session.execute(
        select(SuppressionEntry).where(
            SuppressionEntry.email == lead.email.strip().lower(),
            SuppressionEntry.workspace_id.is_(None),
            SuppressionEntry.added_by == AUTO_SUPPRESSED_BY,
        )
    ).scalars().first()
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail="no automatic suppression to lift: whatever suppresses this address "
                   "was added by a person, an import, a bounce or the unsubscribe link",
        )
    used_link = session.execute(
        select(AuditEvent.id).where(
            AuditEvent.action == "unsubscribe", AuditEvent.target_type == "lead",
            AuditEvent.target_id == str(lead.id),
        )
    ).first()
    if used_link:
        raise HTTPException(status_code=409, detail="they also used the unsubscribe link; it stays")

    audit.record(session, user, "lift_suppression", "suppression", entry.id,
                 reply_id=reply.id, lead_id=lead.id, was=entry.reason)
    session.delete(entry)
    reply.needs_confirmation = False
    return RedirectResponse(url="/inbox", status_code=303)
