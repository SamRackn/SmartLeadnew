"""Login / logout. The only routes that do not require a session."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from mazz.web.auth import authenticate, login_user, logout_user

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login")
async def login_form(request: Request):
    templates = _templates(request)
    if request.session.get("user"):
        return RedirectResponse(url="/leads", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    templates = _templates(request)
    if not authenticate(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password."},
            status_code=401,
        )
    login_user(request, username)
    return RedirectResponse(url="/leads", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
