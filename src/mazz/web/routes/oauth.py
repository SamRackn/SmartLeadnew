"""The Google OAuth callback.

Public by necessity: Google redirects the browser here, and that browser (a
Zapmail-driven consent, or an operator's) may have no session with us. It is
authenticated instead by the single-use ``state`` we minted for ONE mailbox,
and it checks which Google account actually consented before storing anything:
a token for the wrong account is refused, not saved.

Nothing on this page echoes a code, a token or a secret.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.integrations import google_oauth
from mazz.models import Mailbox, MailboxStatus

router = APIRouter()


def _page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>{title}</title><body style='font-family:sans-serif;"
        f"max-width:32rem;margin:4rem auto'><h1>{title}</h1><p>{message}</p></body>",
        status_code=status_code,
    )


@router.get(google_oauth.CALLBACK_PATH, response_class=HTMLResponse)
def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    mailbox = (
        session.execute(select(Mailbox).where(Mailbox.oauth_state == state)).scalar_one_or_none()
        if state
        else None
    )
    if mailbox is None:
        return _page("Link not recognised", "This authorisation link is unknown or already used.", 400)
    started = mailbox.oauth_state_at
    if started is None or datetime.now(timezone.utc) - started > google_oauth.STATE_TTL:
        return _page("Link expired", "Ask for a new authorisation link.", 400)
    if error or not code:
        return _page("Not authorised", "Google did not grant access, so nothing was connected.", 400)

    try:
        tokens = google_oauth.exchange_code(code)
        refresh = tokens["refresh_token"]
        try:
            who = google_oauth.mailbox_identity(refresh)
        except Exception as exc:  # noqa: BLE001 - HttpError / RefreshError / network
            raise google_oauth.OAuthExchangeError(
                f"could not confirm the account ({exc.__class__.__name__})"
            ) from exc
        if who != mailbox.email_address.strip().lower():
            raise google_oauth.OAuthExchangeError(
                f"consented as a different account ({who})"
            )
        encrypted = google_oauth.encrypt_token(refresh)
    except (google_oauth.OAuthExchangeError, google_oauth.OAuthNotConfigured) as exc:
        audit.record(session, audit.SYSTEM, "oauth_failed", "mailbox", mailbox.id, reason=str(exc)[:200])
        return _page("Not connected", "The authorisation could not be completed. Nothing was saved.", 400)

    mailbox.oauth_refresh_token_enc = encrypted
    mailbox.oauth_connected_at = datetime.now(timezone.utc)
    mailbox.oauth_state = None  # single use
    mailbox.oauth_state_at = None
    if mailbox.status is MailboxStatus.NEEDS_RECONNECT:
        mailbox.status = MailboxStatus.ACTIVE
        mailbox.consecutive_failures = 0
        mailbox.last_error = None
    audit.record(session, audit.SYSTEM, "oauth_connected", "mailbox", mailbox.id)
    return _page("Connected", f"{mailbox.email_address} is connected. You can close this tab.")
