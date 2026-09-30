"""FastAPI application: session middleware, Jinja2 templates, routes.

Server-rendered, HTMX for the approve/edit/reject partials. No SPA, no
bundler — see the module docstring in ``mazz/web/routes/leads.py`` and the
review-app-engineer brief for why.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from mazz.config import get_settings
from mazz.web.auth import AuthRequired, Forbidden
from mazz.web.routes import auth as auth_routes
from mazz.web.routes import campaigns as campaigns_routes
from mazz.web.routes import dashboard as dashboard_routes
from mazz.web.routes import imports as imports_routes
from mazz.web.routes import leads as leads_routes
from mazz.web.routes import mailboxes as mailboxes_routes
from mazz.web.routes import outbox as outbox_routes
from mazz.web.routes import review as review_routes

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="MazzSmartLead Review")

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="mazz_review_session",
        same_site="lax",
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.state.templates = templates

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
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

    @app.get("/")
    async def root():
        return RedirectResponse(url="/leads", status_code=303)

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
