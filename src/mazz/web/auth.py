"""Session-cookie auth, one account per person.

Accounts live in the ``users`` table (``python -m mazz.users``). There is no
shared login: approvals must be attributable to a person. The hash is
PBKDF2-HMAC-SHA256 with a random per-hash salt, stdlib ``hashlib`` only.

Scoping: an operator account (``all_workspaces``) sees everything and the admin
screens; any other account sees only leads whose campaign belongs to one of its
workspaces, and anything else answers 404 — the same as a lead that does not
exist, so a guessed id reveals nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select, true
from sqlalchemy.orm import Session

from mazz.db import get_session, session_scope
from mazz.models import Campaign, Lead, User, UserWorkspace

__all__ = [
    "SESSION_USER_KEY",
    "Account",
    "AuthRequired",
    "Forbidden",
    "hash_password",
    "verify_password",
    "authenticate",
    "current_account",
    "current_user",
    "require_operator",
    "login_user",
    "logout_user",
]

SESSION_USER_KEY = "user"

_ALGORITHM = "sha256"
_ITERATIONS = 260_000
_SALT_BYTES = 16


class AuthRequired(Exception):
    """Raised by route dependencies when no valid session is present.

    Handled centrally in ``app.py``: browsers get redirected to ``/login``,
    everything else (HTMX partials, non-GET requests) gets a 401. Either way,
    the request never reaches a protected route without a session.
    """


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-contained ``algo$iterations$salt$digest`` hash string."""

    salt = salt if salt is not None else os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a hash from ``hash_password``."""

    if not encoded:
        return False
    try:
        algo_part, iterations_part, salt_hex, digest_hex = encoded.split("$")
        algorithm = algo_part.split("_", 1)[1]
        iterations = int(iterations_part)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, IndexError):
        return False
    actual = hashlib.pbkdf2_hmac(algorithm, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def authenticate(username: str, password: str) -> bool:
    """True iff ``username``/``password`` match an active account with a password."""

    handle = (username or "").strip().lower()
    if not handle:
        return False
    with session_scope() as session:
        user = session.execute(select(User).where(User.email == handle)).scalar_one_or_none()
        if user is None or not user.active or not user.password_hash:
            # No hash means the account was seeded but never given a password:
            # login can never succeed — fail closed, not "any password works".
            return False
        return verify_password(password, user.password_hash)


class Forbidden(Exception):
    """Logged in, but this account may not see or do this. Rendered as 403."""


@dataclass(frozen=True, slots=True)
class Account:
    """The logged-in person, resolved from the database on every request."""

    id: int
    email: str
    display_name: str
    all_workspaces: bool
    workspace_ids: frozenset[int]

    def sees_workspace(self, workspace_id: int | None) -> bool:
        if self.all_workspaces:
            return True
        return workspace_id is not None and workspace_id in self.workspace_ids

    def lead_filter(self):
        """A WHERE clause limiting ``Lead`` rows to this account's workspaces.

        An unrouted lead (no campaign) belongs to no workspace, so only the
        operator sees it.
        """

        if self.all_workspaces:
            return true()
        return Lead.campaign_id.in_(
            select(Campaign.id).where(Campaign.workspace_id.in_(sorted(self.workspace_ids)))
        )

    def can_see_lead(self, session: Session, lead: Lead | None) -> bool:
        if lead is None:
            return False
        if self.all_workspaces:
            return True
        if lead.campaign_id is None:
            return False
        campaign = session.get(Campaign, lead.campaign_id)
        return campaign is not None and campaign.workspace_id in self.workspace_ids


def current_account(request: Request, session: Session = Depends(get_session)) -> Account:
    """FastAPI dependency: the logged-in account, re-read from ``users``.

    Re-read every request so a deactivated account is locked out at once,
    not at cookie expiry.
    """

    handle = request.session.get(SESSION_USER_KEY)
    if not handle:
        raise AuthRequired("no active session")
    user = session.execute(select(User).where(User.email == handle)).scalar_one_or_none()
    if user is None or not user.active:
        request.session.pop(SESSION_USER_KEY, None)
        raise AuthRequired("account no longer active")
    workspace_ids = frozenset(
        session.execute(
            select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user.id)
        ).scalars()
    )
    return Account(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        all_workspaces=user.all_workspaces,
        workspace_ids=workspace_ids,
    )


def current_user(account: Account = Depends(current_account)) -> str:
    """FastAPI dependency: the logged-in user's login handle.

    Use as ``Depends(current_user)`` on every route except login. Every
    protected route in this app takes it — that is the whole control.
    """

    return account.email


def visible_lead(session: Session, account: Account, lead_id: int) -> Lead:
    """The lead, or 404 — for a missing lead AND for another workspace's lead.

    One answer for both so a guessed id tells a scoped account nothing.
    """

    from fastapi import HTTPException

    lead = session.get(Lead, lead_id)
    if not account.can_see_lead(session, lead):
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


def require_operator(account: Account = Depends(current_account)) -> Account:
    """The admin screens (workspaces, campaigns, mailboxes, imports): operator only."""

    if not account.all_workspaces:
        raise Forbidden("this screen is for the operator account only")
    return account


def login_user(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = username.strip().lower()


def logout_user(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "hash":
        print("usage: python -m mazz.web.auth hash <password>", file=sys.stderr)
        return 2
    print(hash_password(argv[2]))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main(sys.argv))
