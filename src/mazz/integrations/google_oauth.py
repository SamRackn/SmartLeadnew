"""Per-mailbox Google OAuth: the credential path for sending and reading.

Replaces the service-account / domain-wide-delegation path. The mailboxes are
Zapmail-provisioned Workspace accounts in tenants we do not administer, so
each one is authorised individually: we build a per-mailbox authorization
link (our client id, our redirect URI, a single-use ``state``), Zapmail's
Custom OAuth carries the mailbox through consent, and Google redirects to our
callback with a code. We exchange it, verify WHICH mailbox consented, and
keep only the refresh token, Fernet-encrypted.

Scopes are exactly ``gmail.send`` and ``gmail.readonly``. Zapmail's own
example link requests ``https://mail.google.com/``; we do not.

The client id/secret come from the environment (``GOOGLE_OAUTH_CLIENT_ID`` /
``GOOGLE_OAUTH_CLIENT_SECRET``), the encryption key from ``OAUTH_TOKEN_KEY``
(``python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"``).
Nothing here logs, returns or renders a token.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select

from mazz.integrations.base import setting
from mazz.integrations.email_sender import MailboxNeedsReconnect

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = (GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth/google/callback"
#: A link older than this is refused at the callback.
STATE_TTL = timedelta(days=7)


class OAuthNotConfigured(RuntimeError):
    """A required setting (client id/secret, token key) is missing."""


class OAuthExchangeError(RuntimeError):
    """Google refused the code, or the consenting account is the wrong one."""


# --------------------------------------------------------------------------
# Settings and encryption
# --------------------------------------------------------------------------


def _need(name: str) -> str:
    value = setting(name)
    if not value:
        raise OAuthNotConfigured(f"{name} is not set")
    return str(value)


def client_credentials() -> tuple[str, str]:
    return _need("GOOGLE_OAUTH_CLIENT_ID"), _need("GOOGLE_OAUTH_CLIENT_SECRET")


def redirect_uri() -> str:
    return _need("APP_BASE_URL").rstrip("/") + CALLBACK_PATH


def _fernet():
    from cryptography.fernet import Fernet

    try:
        return Fernet(_need("OAUTH_TOKEN_KEY").encode())
    except ValueError as exc:
        raise OAuthNotConfigured(f"OAUTH_TOKEN_KEY is not a valid Fernet key: {exc}") from exc


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise OAuthNotConfigured(
            "a stored refresh token cannot be decrypted with OAUTH_TOKEN_KEY "
            "(key changed?); reconnect the mailbox"
        ) from exc


# --------------------------------------------------------------------------
# The authorization link and the code exchange
# --------------------------------------------------------------------------


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(mailbox_address: str, state: str) -> str:
    client_id, _ = client_credentials()
    return AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "access_type": "offline",
            "client_id": client_id,
            "login_hint": mailbox_address,
            "prompt": "consent",
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https URL
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = {}
        return {"error": body.get("error", f"http_{exc.code}")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # DNS, reset, timeout, or a non-JSON 200: Google never answered usefully.
        # Surface it as an exchange failure (the callback shows "not connected").
        raise OAuthExchangeError(
            f"the token endpoint could not be reached ({exc.__class__.__name__})"
        ) from exc


def exchange_code(code: str, *, post: Callable[..., dict] = _post_form) -> dict[str, Any]:
    """Authorization code -> token response. Raises on any refusal."""

    client_id, client_secret = client_credentials()
    result = post(
        TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        },
    )
    if "error" in result or not result.get("refresh_token"):
        # No refresh token = the grant was not offline/consented. Useless to us.
        raise OAuthExchangeError(
            "Google did not return a refresh token: "
            f"{result.get('error', 'no refresh_token in response')}"
        )
    granted = set(str(result.get("scope", "")).split())
    if not set(SCOPES) <= granted:
        raise OAuthExchangeError(
            f"the grant is missing required scopes (got {sorted(granted)})"
        )
    return result


def credentials_for(refresh_token: str):
    """google-auth credentials that refresh their own access token on demand."""

    from google.oauth2.credentials import Credentials  # lazy: the google extra

    client_id, client_secret = client_credentials()
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(SCOPES),
    )


def mailbox_identity(refresh_token: str) -> str:
    """Which Google account this refresh token belongs to (Gmail's own answer,
    so no scope beyond gmail.readonly is needed)."""

    from googleapiclient.discovery import build  # lazy: the google extra

    service = build("gmail", "v1", credentials=credentials_for(refresh_token), cache_discovery=False)
    return str(service.users().getProfile(userId="me").execute()["emailAddress"]).strip().lower()


def is_revoked(exc: BaseException) -> bool:
    """A refresh that Google refused permanently: revoked, expired, or the
    app is still in Testing and the 7-day token lapsed."""

    # Only Google's own "invalid_grant". A RefreshError can also be a token
    # endpoint 5xx or a network failure: that is retryable, and must not flag
    # every mailbox needs_reconnect during a Google outage.
    return "invalid_grant" in f"{exc.__class__.__name__} {exc}".lower()


# --------------------------------------------------------------------------
# A Gmail service for one mailbox
# --------------------------------------------------------------------------


def service_for_mailbox(address: str):
    """Build a Gmail client for the mailbox, from its stored refresh token.

    Raises `MailboxNeedsReconnect` when it has none or has been flagged.
    """

    from googleapiclient.discovery import build  # lazy: the google extra

    from mazz.db import session_scope
    from mazz.models import Mailbox, MailboxStatus

    with session_scope() as session:
        mailbox = session.execute(
            select(Mailbox).where(Mailbox.email_address == address)
        ).scalar_one_or_none()
        encrypted = mailbox.oauth_refresh_token_enc if mailbox else None
        flagged = mailbox is not None and mailbox.status is MailboxStatus.NEEDS_RECONNECT
    if not encrypted or flagged:
        raise MailboxNeedsReconnect(
            f"{address} has no live OAuth authorisation; reconnect it on /mailboxes",
            provider="google",
            target=address,
        )
    creds = credentials_for(decrypt_token(encrypted))
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
