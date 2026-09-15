"""Session-cookie auth for the single reviewer.

There is exactly one account: ``REVIEW_USER`` / ``REVIEW_PASSWORD_HASH`` from
the environment (see ``.env.example``). The hash is PBKDF2-HMAC-SHA256 with a
random per-hash salt, computed with stdlib ``hashlib`` only — no new
dependency, per the review-app-engineer brief.

Generate a hash for ``.env`` with::

    python -m mazz.web.auth hash <password>

which is exactly the command ``.env.example`` tells the client to run.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys

from fastapi import Request

from mazz.config import get_settings

__all__ = [
    "SESSION_USER_KEY",
    "AuthRequired",
    "hash_password",
    "verify_password",
    "authenticate",
    "current_user",
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
    """True iff ``username``/``password`` match the single configured account."""

    settings = get_settings()
    if not username or username != settings.review_user:
        return False
    if not settings.review_password_hash:
        # No hash configured means login can never succeed — fail closed, not
        # "any password works".
        return False
    return verify_password(password, settings.review_password_hash)


def current_user(request: Request) -> str:
    """FastAPI dependency: the logged-in username, or raise ``AuthRequired``.

    Use as ``Depends(current_user)`` on every route except login. Every
    protected route in this app takes it — that is the whole control.
    """

    user = request.session.get(SESSION_USER_KEY)
    if not user:
        raise AuthRequired("no active session")
    return user


def login_user(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = username


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
