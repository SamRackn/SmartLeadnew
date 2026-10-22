"""The mailbox roster: what the client sends from, and how fast.

`mailboxes` landed as a table with nothing that writes a row, which would have
left an operator adding the client's five accounts by hand-written SQL. This is
that screen: list, add, edit, pause and remove, plus today's usage against each
mailbox's cap so the person deciding whether to approve twenty more leads can
see whether there is anywhere for them to go.

Three things this module is NOT, on purpose:

* **Not a send surface.** No route here sends, approves, or retries a send.
  `leads.py` owns approval, one lead at a time, and the worker owns sending.
  A roster screen is exactly where a convenient "flush the queue" button would
  appear later; it does not appear here.
* **Not a credential store.** The form takes a ``credential_ref`` — a NAME —
  and the row holds only that. A field that accepted a service-account key
  would put Google credentials in the database, in a form post, and in this
  page's HTML. The key stays in the environment.
* **Not a warmup tool.** Pausing a mailbox stops us handing it work; it does
  not warm it. Warmup is a network effect that needs thousands of opted-in
  mailboxes and the client has five — CLAUDE.md is explicit that we buy it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mazz import audit
from mazz.config import get_settings
from mazz.db import get_session
from mazz.models import Campaign, Mailbox, MailboxStatus, Workspace
from mazz.pipeline.mailbox import headroom, next_eligible_at, sent_today
from mazz.web.auth import current_user, require_operator

# Operator only: Sean's account sees leads, review, outbox and inbox for his
# workspace, never the roster or other workspaces' setup.
router = APIRouter(dependencies=[Depends(require_operator)])

#: Deliberately loose — we are not the authority on what a valid address is,
#: the provider is. This only catches the typo that would otherwise become a
#: row nobody notices until a send fails.
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@dataclass(frozen=True, slots=True)
class MailboxRow:
    """A mailbox plus the numbers that decide whether it can send right now."""

    mailbox: Mailbox
    sent_today: int
    headroom: int
    eligible_at: object  # datetime; the template only formats it


def _rows(session: Session) -> list[MailboxRow]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mailboxes = (
        session.execute(select(Mailbox).order_by(Mailbox.email_address))
        .scalars()
        .all()
    )
    return [
        MailboxRow(
            mailbox=m,
            sent_today=sent_today(session, m, now=now),
            headroom=headroom(session, m, now=now),
            eligible_at=next_eligible_at(m, now=now),
        )
        for m in mailboxes
    ]


def _campaign_choices(session: Session) -> list[tuple[int, str]]:
    rows = session.execute(
        select(Campaign.id, Workspace.name, Campaign.name)
        .join(Workspace, Campaign.workspace_id == Workspace.id)
        .order_by(Workspace.name, Campaign.name)
    ).all()
    return [(cid, f"{ws} / {cname}") for cid, ws, cname in rows]


def _form_defaults() -> dict:
    settings = get_settings()
    return {
        "email_address": "",
        "display_name": "",
        "credential_ref": "",
        "daily_cap": settings.default_daily_cap,
        "min_spacing_seconds": settings.default_min_spacing_seconds,
        "campaign_id": "",
        "notes": "",
    }


def _validate(
    *,
    email_address: str,
    display_name: str,
    credential_ref: str,
    daily_cap: int,
    min_spacing_seconds: int,
) -> list[str]:
    errors: list[str] = []
    if not _ADDRESS_RE.match((email_address or "").strip()):
        errors.append(f"{email_address!r} is not an email address")
    if not (display_name or "").strip():
        errors.append("display name is required — it is the From name recipients see")
    if not (credential_ref or "").strip():
        errors.append(
            "credential ref is required (a label; the mailbox's own OAuth "
            "authorisation is what it sends with)"
        )
    if daily_cap < 0:
        errors.append("daily cap cannot be negative")
    if min_spacing_seconds < 0:
        errors.append("spacing cannot be negative")
    return errors


def _int_field(raw: str, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@router.get("/mailboxes", response_class=HTMLResponse)
async def list_mailboxes(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "mailboxes_list.html",
        {
            "rows": _rows(session),
            "sender": get_settings().email_sender,
            "user": user,
        },
    )


@router.get("/mailboxes/new", response_class=HTMLResponse)
async def new_mailbox_form(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "mailbox_form.html",
        {
            "mailbox": None,
            "form": _form_defaults(),
            "campaigns": _campaign_choices(session),
            "error": None,
            "user": user,
        },
    )


@router.post("/mailboxes", response_class=HTMLResponse)
async def create_mailbox(
    request: Request,
    email_address: str = Form(...),
    display_name: str = Form(...),
    credential_ref: str = Form(...),
    daily_cap: str = Form(""),
    min_spacing_seconds: str = Form(""),
    campaign_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    settings = get_settings()
    cap = _int_field(daily_cap, settings.default_daily_cap)
    spacing = _int_field(min_spacing_seconds, settings.default_min_spacing_seconds)
    form = {
        "email_address": email_address,
        "display_name": display_name,
        "credential_ref": credential_ref,
        "daily_cap": cap,
        "min_spacing_seconds": spacing,
        "campaign_id": campaign_id,
        "notes": notes,
    }

    errors = _validate(
        email_address=email_address,
        display_name=display_name,
        credential_ref=credential_ref,
        daily_cap=cap,
        min_spacing_seconds=spacing,
    )
    if errors:
        return templates.TemplateResponse(
            request,
            "mailbox_form.html",
            {
                "mailbox": None,
                "form": form,
                "campaigns": _campaign_choices(session),
                "error": "; ".join(errors),
                "user": user,
            },
            status_code=400,
        )

    mailbox = Mailbox(
        email_address=email_address.strip().lower(),
        display_name=display_name.strip(),
        credential_ref=credential_ref.strip(),
        daily_cap=cap,
        min_spacing_seconds=spacing,
        campaign_id=int(campaign_id) if campaign_id.strip() else None,
        notes=notes.strip() or None,
    )
    session.add(mailbox)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "mailbox_form.html",
            {
                "mailbox": None,
                "form": form,
                "campaigns": _campaign_choices(session),
                "error": (
                    f"{email_address.strip().lower()!r} is already on the roster. "
                    "Two rows for one account would double its real daily cap "
                    "while each row believed it was obeying."
                ),
                "user": user,
            },
            status_code=409,
        )

    audit.record(session, user, "mailbox_create", "mailbox", mailbox.id,
                 email=mailbox.email_address, daily_cap=cap, min_spacing_seconds=spacing)
    return RedirectResponse(url="/mailboxes", status_code=303)


@router.get("/mailboxes/{mailbox_id}/edit", response_class=HTMLResponse)
async def edit_mailbox_form(
    request: Request,
    mailbox_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")
    return templates.TemplateResponse(
        request,
        "mailbox_form.html",
        {
            "mailbox": mailbox,
            "form": {
                "email_address": mailbox.email_address,
                "display_name": mailbox.display_name,
                "credential_ref": mailbox.credential_ref,
                "daily_cap": mailbox.daily_cap,
                "min_spacing_seconds": mailbox.min_spacing_seconds,
                "campaign_id": str(mailbox.campaign_id or ""),
                "notes": mailbox.notes or "",
            },
            "campaigns": _campaign_choices(session),
            "error": None,
            "user": user,
        },
    )


@router.post("/mailboxes/{mailbox_id}/edit", response_class=HTMLResponse)
async def edit_mailbox(
    request: Request,
    mailbox_id: int,
    email_address: str = Form(...),
    display_name: str = Form(...),
    credential_ref: str = Form(...),
    daily_cap: str = Form(""),
    min_spacing_seconds: str = Form(""),
    campaign_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")

    settings = get_settings()
    cap = _int_field(daily_cap, settings.default_daily_cap)
    spacing = _int_field(min_spacing_seconds, settings.default_min_spacing_seconds)
    errors = _validate(
        email_address=email_address,
        display_name=display_name,
        credential_ref=credential_ref,
        daily_cap=cap,
        min_spacing_seconds=spacing,
    )
    if errors:
        return templates.TemplateResponse(
            request,
            "mailbox_form.html",
            {
                "mailbox": mailbox,
                "form": {
                    "email_address": email_address,
                    "display_name": display_name,
                    "credential_ref": credential_ref,
                    "daily_cap": cap,
                    "min_spacing_seconds": spacing,
                    "campaign_id": campaign_id,
                    "notes": notes,
                },
                "campaigns": _campaign_choices(session),
                "error": "; ".join(errors),
                "user": user,
            },
            status_code=400,
        )

    # A cap raise is the compliance-relevant edit: record old and new.
    audit.record(
        session, user, "mailbox_edit", "mailbox", mailbox.id,
        email=mailbox.email_address,
        daily_cap=f"{mailbox.daily_cap} -> {cap}",
        min_spacing_seconds=f"{mailbox.min_spacing_seconds} -> {spacing}",
    )
    mailbox.email_address = email_address.strip().lower()
    mailbox.display_name = display_name.strip()
    mailbox.credential_ref = credential_ref.strip()
    mailbox.daily_cap = cap
    mailbox.min_spacing_seconds = spacing
    mailbox.campaign_id = int(campaign_id) if campaign_id.strip() else None
    mailbox.notes = notes.strip() or None
    session.flush()
    return RedirectResponse(url="/mailboxes", status_code=303)


@router.get("/mailboxes/{mailbox_id}/connect", response_class=HTMLResponse)
async def connect_mailbox(
    request: Request,
    mailbox_id: int,
    session: Session = Depends(get_session),
):
    """The per-mailbox Google authorisation link, and whether it is connected.

    The link carries a single-use ``state``; opening this page does not use it
    up. Zapmail's Custom OAuth takes this link (see docs/LAUNCH-PLAN.md).
    """

    from mazz.integrations import google_oauth

    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")
    now = datetime.now(timezone.utc)
    if not mailbox.oauth_state or (
        mailbox.oauth_state_at is None or now - mailbox.oauth_state_at > google_oauth.STATE_TTL
    ):
        mailbox.oauth_state = google_oauth.new_state()
        mailbox.oauth_state_at = now
        session.flush()
    link, problem = None, None
    try:
        link = google_oauth.authorization_url(mailbox.email_address, mailbox.oauth_state)
    except google_oauth.OAuthNotConfigured as exc:
        problem = f"cannot build a link yet: {exc}"
    return _templates(request).TemplateResponse(
        request,
        "mailbox_connect.html",
        {
            "mailbox": mailbox,
            "link": link,
            "problem": problem,
            "redirect_uri": None if problem else google_oauth.redirect_uri(),
            "connected": bool(mailbox.oauth_refresh_token_enc)
            and mailbox.status is not MailboxStatus.NEEDS_RECONNECT,
            "scopes": google_oauth.SCOPES,
        },
    )


@router.post("/mailboxes/{mailbox_id}/connect/new", response_class=HTMLResponse)
async def new_connect_link(
    mailbox_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Replace the link's state. The old link stops working at once."""

    from mazz.integrations import google_oauth

    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")
    mailbox.oauth_state = google_oauth.new_state()
    mailbox.oauth_state_at = datetime.now(timezone.utc)
    audit.record(session, user, "oauth_link_regenerated", "mailbox", mailbox.id)
    return RedirectResponse(url=f"/mailboxes/{mailbox_id}/connect", status_code=303)


@router.post("/mailboxes/{mailbox_id}/status", response_class=HTMLResponse)
async def set_mailbox_status(
    request: Request,
    mailbox_id: int,
    status: str = Form(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Pause, resume, or disable a mailbox.

    Resuming a `disabled` mailbox is allowed and deliberate: `disabled` is set
    by the send path when authentication fails, and the fix (re-granting
    domain-wide delegation) happens in the Google admin console, outside this
    system. Someone has to be able to say "I fixed it" — and the failure
    counter is reset here so a stale count does not keep the mailbox at the
    back of the rotation forever.
    """

    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")
    try:
        new_status = MailboxStatus(status)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown mailbox status {status!r}; expected one of "
                f"{[s.value for s in MailboxStatus]}"
            ),
        ) from None

    if mailbox.status is MailboxStatus.NEEDS_RECONNECT and new_status is MailboxStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail="this mailbox's Google authorisation is gone; reconnect it "
            "(the connect link), which re-activates it once Google confirms.",
        )
    audit.record(session, user, "mailbox_status", "mailbox", mailbox.id,
                 email=mailbox.email_address, status=f"{mailbox.status.value} -> {new_status.value}")
    mailbox.status = new_status
    if new_status == MailboxStatus.ACTIVE:
        mailbox.consecutive_failures = 0
        mailbox.last_error = None
    session.flush()
    return RedirectResponse(url="/mailboxes", status_code=303)


@router.post("/mailboxes/{mailbox_id}/delete", response_class=HTMLResponse)
async def delete_mailbox(
    request: Request,
    mailbox_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Take a mailbox off the roster.

    `sent_messages.mailbox_id` is ON DELETE SET NULL, so the record of what
    this mailbox sent survives it — removing an account must not quietly erase
    the evidence of the email it sent while it was on the roster.
    """

    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="mailbox not found")
    audit.record(session, user, "mailbox_delete", "mailbox", mailbox.id,
                 email=mailbox.email_address, daily_cap=mailbox.daily_cap)
    session.delete(mailbox)
    session.flush()
    return RedirectResponse(url="/mailboxes", status_code=303)
