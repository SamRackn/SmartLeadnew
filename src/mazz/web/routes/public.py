"""The only routes that answer without a login: unsubscribe and health.

``/u/<token>`` is the link in every footer and the target of the
``List-Unsubscribe`` header. GET shows a one-button confirm page (a link
scanner or preview pane prefetching the URL must not unsubscribe anyone);
POST suppresses immediately — that is the RFC 8058 one-click path mail
clients call with ``List-Unsubscribe=One-Click``, and the button's target.

The suppression is GLOBAL (every workspace): someone who asked one of our
senders to stop has asked us to stop.

These routes are also the whole surface of the dedicated unsubscribe host;
`app.py` 404s everything else arriving on it.
"""

from __future__ import annotations

import hmac
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.ingest.csv_import import email_sha256
from mazz.models import Lead, LeadStatus, SuppressionEntry
from mazz.pipeline.draft import unsubscribe_token
from mazz.pipeline.sequence import LIVE_STATES

router = APIRouter()

#: Statuses an unsubscribe pulls back out of the send path. A lead already
#: `sent` keeps its status (it records what happened); its sequence is
#: stopped, and the follow-up and send steps re-check suppression anyway.
_STOPPABLE = (LeadStatus.RESEARCHING, LeadStatus.READY, LeadStatus.APPROVED)

_DEAD_LINK = (
    "<h1>This link is no longer active</h1><p>If you are still receiving "
    "email from us, reply with the word <strong>unsubscribe</strong> and "
    "we will remove you.</p>"
)


def lead_for_token(session: Session, token: str) -> Lead | None:
    """The lead whose unsubscribe token this is, or None.

    ponytail: one HMAC per lead per click, O(n). Fine at thousands of leads;
    store the token in an indexed column if the list reaches ~100k.
    """

    if not token or len(token) > 64:
        return None
    for lead_id, email in session.execute(select(Lead.id, Lead.email)):
        if hmac.compare_digest(unsubscribe_token(email), token):
            return session.get(Lead, lead_id)
    return None


def suppress_email(
    session: Session, email: str, *, reason: str, actor: str
) -> SuppressionEntry:
    """Add a global email suppression unless one already exists. Idempotent."""

    email = email.strip().lower()
    digest = email_sha256(email)
    existing = session.execute(
        select(SuppressionEntry).where(
            SuppressionEntry.workspace_id.is_(None),
            (SuppressionEntry.email == email) | (SuppressionEntry.email_sha256 == digest),
        )
    ).scalars().first()
    if existing is not None:
        return existing
    entry = SuppressionEntry(email=email, email_sha256=digest, reason=reason, added_by=actor)
    session.add(entry)
    session.flush()
    return entry


def _page(title: str, body: str) -> HTMLResponse:
    # Self-contained: the unsubscribe host serves nothing else, not even /static.
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>"
        "body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;color:#222;background:#fff}"
        "button{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:6px;"
        "background:#222;color:#fff;cursor:pointer}"
        "</style></head><body>" + body + "</body></html>"
    )


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, session: Session = Depends(get_session)):
    lead = lead_for_token(session, token)
    if lead is None:
        return _page("Unsubscribe", _DEAD_LINK)
    return _page(
        "Unsubscribe",
        f"<h1>Unsubscribe</h1><p>Stop all email to "
        f"<strong>{escape(_mask(lead.email))}</strong>?</p>"
        f"<form method='post' action='/u/{escape(token)}'>"
        "<button type='submit'>Unsubscribe</button></form>",
    )


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, session: Session = Depends(get_session)):
    lead = lead_for_token(session, token)
    if lead is None:
        return _page("Unsubscribe", _DEAD_LINK)
    entry = suppress_email(session, lead.email, reason="unsubscribe link", actor=audit.SYSTEM)
    if lead.status in _STOPPABLE:
        lead.status = LeadStatus.HELD
    if lead.sequence_state in LIVE_STATES:
        lead.sequence_state, lead.next_step_due_at = "stopped", None
    audit.record(
        session, audit.SYSTEM, "unsubscribe", "lead", lead.id,
        source="recipient_link", suppression_id=entry.id,
    )
    return _page(
        "Unsubscribed",
        "<h1>You're unsubscribed</h1><p>You will not receive any more email from us.</p>",
    )


@router.get("/health")
def health(session: Session = Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health reports, it does not raise
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}
