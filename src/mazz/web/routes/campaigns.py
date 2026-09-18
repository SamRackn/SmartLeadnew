"""Workspaces and campaigns: where an operator encodes what a client is buying.

``workspaces`` and ``campaigns`` (plus ``campaigns.scoring_config``) landed as
tables and a loader (``pipeline.score.scoring_config_from_json``) with nothing
that writes a row — an operator could only populate them with hand-written
SQL. This module is the UI fix: list, create and edit both, and edit a
campaign's ``segment`` and ``scoring_config``.

Two things this module is NOT, on purpose:

* **Not a compliance gate.** Nothing here touches a lead's send path. There is
  no bulk action over leads, no approve, no send — `leads.py` owns that
  surface entirely, per the brief's "no auto-send, ever". Deleting a campaign
  only ever clears ``leads.campaign_id`` (``ON DELETE SET NULL`` on the FK),
  never a lead, a draft or a job.
* **Not the scoring engine.** ``scoring_config`` is the single biggest lever
  on cost per usable lead, and this module does not reimplement any of its
  rules — it calls `mazz.pipeline.score.scoring_config_from_json` and refuses
  to save whatever that refuses, verbatim. A config that is only caught at
  scoring time, inside a worker, as a failed job, is the worst outcome: it
  looks saved and silently isn't.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.models import Campaign, Lead, Workspace
from mazz.pipeline.score import scoring_config_from_json
from mazz.web.auth import current_user

router = APIRouter()

_SLUG_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _normalize_slug(raw: str) -> tuple[str, str | None]:
    """Lower-case a workspace slug and collapse punctuation to single hyphens.

    Same normalisation shape as `pipeline.routing.segment_slug` (not reused
    directly — that function is for the lead/campaign segment match, a
    different concept), so an operator typing "Acme Digital!" gets the
    predictable, human-stable handle "acme-digital" rather than a rejection.
    """

    slug = _SLUG_PUNCT_RE.sub("-", (raw or "").strip().lower()).strip("-")
    if not slug:
        return slug, "slug is required"
    return slug, None


def _parse_scoring_config(raw_text: str) -> tuple[dict, str | None]:
    """Parse + validate a scoring_config textarea submission before it saves.

    Round-trips the value through `scoring_config_from_json` — the same
    function `pipeline.score` uses at scoring time — so a config this UI
    accepts is a config the worker can load. ``{}`` (blank textarea) means
    "shipped defaults unchanged" and is never an error.
    """

    text = (raw_text or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"scoring_config is not valid JSON: {exc}"

    try:
        scoring_config_from_json(parsed)
    except (TypeError, ValueError) as exc:
        # TypeError -> not a JSON object; ValueError -> unknown key, a bad
        # role-term list, or a config that breaks guardrail 2
        # (funding >= threshold). Surface the real message either way —
        # that message names the exact key/value the operator has to fix.
        return parsed if isinstance(parsed, dict) else {}, str(exc)

    return parsed, None


def _lead_count(session: Session, campaign_id: int) -> int:
    return session.execute(
        select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id)
    ).scalar_one()


# --------------------------------------------------------------------------
# Workspaces: list + create + edit
# --------------------------------------------------------------------------


@router.get("/workspaces", response_class=HTMLResponse)
async def list_workspaces(
    request: Request,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspaces = list(
        session.execute(select(Workspace).order_by(Workspace.name)).scalars().all()
    )
    lead_counts = dict(
        session.execute(
            select(Campaign.id, func.count(Lead.id))
            .join(Lead, Lead.campaign_id == Campaign.id, isouter=True)
            .group_by(Campaign.id)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "workspaces_list.html",
        {
            "workspaces": workspaces,
            "lead_counts": lead_counts,
            "user": user,
        },
    )


@router.get("/workspaces/new", response_class=HTMLResponse)
async def new_workspace_form(
    request: Request,
    user: str = Depends(current_user),
):
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "workspace_form.html",
        {
            "workspace": None,
            "error": None,
            "form": {"slug": "", "name": ""},
            "user": user,
        },
    )


@router.post("/workspaces", response_class=HTMLResponse)
async def create_workspace(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    normalized_slug, slug_error = _normalize_slug(slug)
    name = (name or "").strip()

    errors = [e for e in (slug_error, "name is required" if not name else None) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "workspace_form.html",
            {
                "workspace": None,
                "error": "; ".join(errors),
                "form": {"slug": slug, "name": name},
                "user": user,
            },
            status_code=400,
        )

    workspace = Workspace(slug=normalized_slug, name=name)
    session.add(workspace)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "workspace_form.html",
            {
                "workspace": None,
                "error": f"a workspace with slug {normalized_slug!r} already exists",
                "form": {"slug": slug, "name": name},
                "user": user,
            },
            status_code=409,
        )

    return RedirectResponse(url="/workspaces", status_code=303)


@router.get("/workspaces/{workspace_id}/edit", response_class=HTMLResponse)
async def edit_workspace_form(
    request: Request,
    workspace_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return templates.TemplateResponse(
        request,
        "workspace_form.html",
        {
            "workspace": workspace,
            "error": None,
            "form": {"slug": workspace.slug, "name": workspace.name},
            "user": user,
        },
    )


@router.post("/workspaces/{workspace_id}/edit", response_class=HTMLResponse)
async def update_workspace(
    request: Request,
    workspace_id: int,
    slug: str = Form(...),
    name: str = Form(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    normalized_slug, slug_error = _normalize_slug(slug)
    name = (name or "").strip()

    errors = [e for e in (slug_error, "name is required" if not name else None) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "workspace_form.html",
            {
                "workspace": workspace,
                "error": "; ".join(errors),
                "form": {"slug": slug, "name": name},
                "user": user,
            },
            status_code=400,
        )

    workspace.slug = normalized_slug
    workspace.name = name
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "workspace_form.html",
            {
                "workspace": workspace,
                "error": f"a workspace with slug {normalized_slug!r} already exists",
                "form": {"slug": slug, "name": name},
                "user": user,
            },
            status_code=409,
        )

    return RedirectResponse(url="/workspaces", status_code=303)


# --------------------------------------------------------------------------
# Campaigns: create + edit + delete
# --------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/campaigns/new", response_class=HTMLResponse)
async def new_campaign_form(
    request: Request,
    workspace_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return templates.TemplateResponse(
        request,
        "campaign_form.html",
        {
            "workspace": workspace,
            "campaign": None,
            "error": None,
            "form": {"name": "", "segment": "", "scoring_config": "{}"},
            "lead_count": None,
            "user": user,
        },
    )


@router.post("/workspaces/{workspace_id}/campaigns", response_class=HTMLResponse)
async def create_campaign(
    request: Request,
    workspace_id: int,
    name: str = Form(...),
    segment: str = Form(""),
    scoring_config: str = Form("{}"),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    name = (name or "").strip()
    segment_value = (segment or "").strip() or None
    parsed_config, config_error = _parse_scoring_config(scoring_config)

    errors = [e for e in (
        "name is required" if not name else None,
        f"scoring_config: {config_error}" if config_error else None,
    ) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": workspace,
                "campaign": None,
                "error": "; ".join(errors),
                "form": {"name": name, "segment": segment, "scoring_config": scoring_config},
                "lead_count": None,
                "user": user,
            },
            status_code=400,
        )

    campaign = Campaign(
        workspace_id=workspace.id,
        name=name,
        segment=segment_value,
        scoring_config=parsed_config,
    )
    session.add(campaign)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": workspace,
                "campaign": None,
                "error": f"a campaign named {name!r} already exists in this workspace",
                "form": {"name": name, "segment": segment, "scoring_config": scoring_config},
                "lead_count": None,
                "user": user,
            },
            status_code=409,
        )

    return RedirectResponse(url="/workspaces", status_code=303)


@router.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
async def edit_campaign_form(
    request: Request,
    campaign_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return templates.TemplateResponse(
        request,
        "campaign_form.html",
        {
            "workspace": campaign.workspace,
            "campaign": campaign,
            "error": None,
            "form": {
                "name": campaign.name,
                "segment": campaign.segment or "",
                "scoring_config": json.dumps(
                    campaign.scoring_config or {}, indent=2, sort_keys=True
                ),
            },
            "lead_count": _lead_count(session, campaign.id),
            "user": user,
        },
    )


@router.post("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
async def update_campaign(
    request: Request,
    campaign_id: int,
    name: str = Form(...),
    segment: str = Form(""),
    scoring_config: str = Form("{}"),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    name = (name or "").strip()
    segment_value = (segment or "").strip() or None
    parsed_config, config_error = _parse_scoring_config(scoring_config)

    errors = [e for e in (
        "name is required" if not name else None,
        f"scoring_config: {config_error}" if config_error else None,
    ) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": campaign.workspace,
                "campaign": campaign,
                "error": "; ".join(errors),
                "form": {"name": name, "segment": segment, "scoring_config": scoring_config},
                "lead_count": _lead_count(session, campaign.id),
                "user": user,
            },
            status_code=400,
        )

    campaign.name = name
    campaign.segment = segment_value
    campaign.scoring_config = parsed_config
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": campaign.workspace,
                "campaign": campaign,
                "error": f"a campaign named {name!r} already exists in this workspace",
                "form": {"name": name, "segment": segment, "scoring_config": scoring_config},
                "lead_count": _lead_count(session, campaign_id),
                "user": user,
            },
            status_code=409,
        )

    return RedirectResponse(url="/workspaces", status_code=303)


@router.post("/campaigns/{campaign_id}/delete", response_class=HTMLResponse)
async def delete_campaign(
    request: Request,
    campaign_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Retire a campaign. Never touches a lead, a draft or a job.

    ``leads.campaign_id`` is ``ON DELETE SET NULL`` by deliberate design (see
    ``mazz.models.Lead.campaign_id``), so every lead currently routed here
    falls back to the shipped scoring defaults instead of being deleted,
    orphaned, or silently kept on a retired config.
    """

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    session.delete(campaign)
    session.flush()

    return RedirectResponse(url="/workspaces", status_code=303)
