"""FastAPI application: session middleware, Jinja2 templates, routes.

Server-rendered, HTMX for the approve/edit/reject partials. No SPA, no
bundler — see the module docstring in ``mazz/web/routes/leads.py`` and the
review-app-engineer brief for why.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from contextlib import asynccontextmanager

from sqlalchemy import func, select

from mazz.config import deployment_problems, get_settings
from mazz.db import session_scope
from mazz.models import Lead, LeadStatus, Reply
from mazz.web.auth import SESSION_USER_KEY, AuthRequired, Forbidden, load_account
from mazz.web.routes import auth as auth_routes
from mazz.web.routes import campaigns as campaigns_routes
from mazz.web.routes import dashboard as dashboard_routes
from mazz.web.routes import imports as imports_routes
from mazz.web.routes import leads as leads_routes
from mazz.web.routes import mailboxes as mailboxes_routes
from mazz.web.routes import outbox as outbox_routes
from mazz.web.routes import public as public_routes
from mazz.web.routes import oauth as oauth_routes
from mazz.web.routes import inbox as inbox_routes
from mazz.web.routes import costs as costs_routes
from mazz.web.routes import removals as removals_routes
from mazz.web.routes import review as review_routes

BASE_DIR = Path(__file__).resolve().parent


def nav_state(request: Request) -> dict | None:
    """What the sidebar needs: who is looking, whether they see the operator
    screens, and how much waits on the three daily screens. None when nobody
    is logged in. Counts use the same scoping as the pages they link to."""

    handle = request.session.get(SESSION_USER_KEY)
    if not handle:
        return None
    with session_scope() as session:
        account = load_account(session, handle)
        if account is None:
            return None
        leads = select(func.count(Lead.id)).where(account.lead_filter())
        replies = (
            select(func.count(Reply.id))
            .join(Lead, Lead.id == Reply.lead_id)
            .where(account.lead_filter(), Reply.handled_at.is_(None), Reply.classification != "ooo")
        )
        return {
            "operator": account.all_workspaces,
            "name": account.display_name or account.email,
            "email": account.email,
            "review": session.scalar(leads.where(Lead.status == LeadStatus.READY)),
            "outbox": session.scalar(leads.where(Lead.status == LeadStatus.APPROVED)),
            "inbox": session.scalar(replies),
        }


@asynccontextmanager
async def _refuse_unsafe_boot(app: FastAPI):
    # At server start, not at import: a public session secret would let
    # anyone forge an operator login on a host the internet can reach.
    problems = deployment_problems(get_settings())
    if problems:
        raise RuntimeError("refusing to start: " + "; ".join(problems))
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="MazzSmartLead Review", lifespan=_refuse_unsafe_boot)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="mazz_review_session",
        same_site="lax",
        # Secure cookie whenever the app is served over https (always, live).
        https_only=settings.app_base_url.startswith("https://"),
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["nav_state"] = nav_state
    app.state.templates = templates

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    # Busts browser caches whenever the stylesheet changes, so a deploy never
    # pairs new markup with yesterday's CSS.
    templates.env.globals["css_version"] = int((static_dir / "style.css").stat().st_mtime)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(AuthRequired)
    async def _auth_required(request: Request, exc: AuthRequired):
        # Browsers navigating a page get sent to the login form; anything else
        # (HTMX partial calls, non-GET requests) gets a plain 401. Either way
        # the protected route body never runs without a session — this is the
        # ONLY way in, there is no second, laxer path.
        if request.method == "GET":
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.middleware("http")
    async def _unsubscribe_host_isolation(request: Request, call_next):
        # The public unsubscribe host exposes /u/* and /health and NOTHING
        # else — not the login page, not static files. Everything else on it
        # is a plain 404, so the host a stranger's email links to reveals no
        # app behind it.
        public_host = (get_settings().unsubscribe_host or "").strip().lower()
        path = request.url.path
        if (
            public_host
            and (request.url.hostname or "").lower() == public_host
            and not (path.startswith("/u/") or path == "/health")
        ):
            return PlainTextResponse("Not Found", status_code=404)
        return await call_next(request)

    @app.get("/")
    async def root():
        return RedirectResponse(url="/leads", status_code=303)

    app.include_router(public_routes.router)
    app.include_router(oauth_routes.router)
    app.include_router(inbox_routes.router)
    app.include_router(costs_routes.router)
    app.include_router(removals_routes.router)
    app.include_router(auth_routes.router)
    # Before leads_routes: /leads/import would otherwise be matched by
    # /leads/{lead_id} and fail to parse "import" as an integer.
    app.include_router(imports_routes.router)
    app.include_router(leads_routes.router)
    app.include_router(campaigns_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(mailboxes_routes.router)
    app.include_router(outbox_routes.router)
    app.include_router(review_routes.router)

    return app


app = create_app()
