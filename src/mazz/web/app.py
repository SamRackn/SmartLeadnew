"""FastAPI application: session middleware, Jinja2 templates, routes.

Server-rendered, HTMX for the approve/edit/reject partials. No SPA, no
bundler — see the module docstring in ``mazz/web/routes/leads.py`` and the
review-app-engineer brief for why.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

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

SESSION_MAX_AGE_SECONDS = 8 * 3600

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Public by design: mail clients POST the one-click unsubscribe with no cookie
#: and no Origin we could match.
_CSRF_EXEMPT_PREFIXES = ("/u/", "/health")


def _cross_site(request: Request) -> bool:
    """True when a state-changing request plainly came from another site.

    SameSite=Lax alone is not enough: sibling subdomains of the app's own
    registrable domain are "same-site". Browsers always send Sec-Fetch-Site and
    Origin on unsafe methods; a request with neither (curl, tests, an old
    client) carries no ambient browser authority to ride, so it passes.
    """

    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower()
    return False


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

    # No /docs, /redoc or /openapi.json: they are public and list every route.
    app = FastAPI(
        title="MazzSmartLead Review",
        lifespan=_refuse_unsafe_boot,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="mazz_review_session",
        same_site="lax",
        # A working day, not the 14-day default: a cookie is a standing right
        # to approve and release mail.
        max_age=SESSION_MAX_AGE_SECONDS,
        # Secure cookie whenever the app is served over https (always, live).
        https_only=settings.app_base_url.startswith("https://"),
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["nav_state"] = nav_state
    # Imported or scraped URLs become hrefs; autoescape does not stop "javascript:".
    templates.env.filters["safe_url"] = lambda v: (
        v.strip() if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")) else ""
    )
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
        # A trailing dot is the same DNS name ("host." == "host"); without the
        # strip it walks straight past this check.
        public_host = (get_settings().unsubscribe_host or "").strip().lower().rstrip(".")
        path = request.url.path
        if (
            public_host
            and (request.url.hostname or "").lower().rstrip(".") == public_host
            and not (path.startswith("/u/") or path == "/health")
        ):
            return PlainTextResponse("Not Found", status_code=404)
        return await call_next(request)

    @app.middleware("http")
    async def _refuse_cross_site_writes(request: Request, call_next):
        path = request.url.path
        if (
            request.method in _UNSAFE_METHODS
            and not path.startswith(_CSRF_EXEMPT_PREFIXES)
            and _cross_site(request)
        ):
            return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        path = request.url.path
        if not path.startswith(("/static", "/u/")):
            # Lead names, drafts and replies: not for a shared cache or the back button.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

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
