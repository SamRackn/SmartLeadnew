"""Login / logout. The only routes that do not require a session."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from mazz.web.auth import authenticate, login_user, logout_user

router = APIRouter()

#: Failed logins allowed per (username, client IP) inside the window before
#: 429. Keyed on the pair so a stranger guessing Maaz's password locks out
#: their own address, not Maaz.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
# ponytail: per-process memory, reset on restart. Right for the one web
# instance render.yaml runs; move to a DB table if web ever scales out.
_failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _throttled(key: tuple[str, str], now: float) -> bool:
    recent = _failures.get(key)
    if recent is None:
        return False
    while recent and recent[0] < now - FAILURE_WINDOW_SECONDS:
        recent.popleft()
    if not recent:
        del _failures[key]  # no unbounded growth from one-off guesses
        return False
    return len(recent) >= MAX_FAILURES


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login")
async def login_form(request: Request):
    templates = _templates(request)
    if request.session.get("user"):
        return RedirectResponse(url="/leads", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    templates = _templates(request)
    # request.client is the real client: uvicorn runs with --proxy-headers.
    ip = request.client.host if request.client else ""
    key, now = (username.strip().lower(), ip), time.monotonic()
    if _throttled(key, now):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )
    if not authenticate(username, password):
        _failures[key].append(now)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password."},
            status_code=401,
        )
    _failures.pop(key, None)
    login_user(request, username)
    return RedirectResponse(url="/leads", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
