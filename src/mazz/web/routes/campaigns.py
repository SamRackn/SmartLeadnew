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
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.models import Campaign, Lead, ProofPoint, Workspace
from mazz.pipeline.qualify import qualification_config_from_json
from mazz.pipeline.score import scoring_config_from_json
from mazz.web.auth import current_user

router = APIRouter()


def _campaign_form(
    *, name="", segment="", scoring_config="{}", vertical="", offer_line="",
    qualification_config="{}", generic_vertical_fallback=False,
) -> dict:
    return {
        "name": name,
        "segment": segment,
        "scoring_config": scoring_config,
        "vertical": vertical,
        "offer_line": offer_line,
        "qualification_config": qualification_config,
        "generic_vertical_fallback": generic_vertical_fallback,
    }

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


_DIGIT_RE = re.compile(r"\d")


def _parse_qualification_config(raw_text: str) -> tuple[dict, str | None]:
    """Validate a qualification_config textarea through the worker's own loader.

    Same shape as `_parse_scoring_config` and for the same reason: a config
    this form accepts must be one the pipeline can load. It matters more here.
    A scoring config that fails to load falls back to a vocabulary that still
    writes emails; a qualification config that fails to load falls back to one
    that qualifies NOBODY, so a campaign would go quiet and look configured.
    """

    text = (raw_text or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"qualification_config is not valid JSON: {exc}"

    try:
        qualification_config_from_json(parsed)
    except (TypeError, ValueError) as exc:
        return {}, str(exc)
    return parsed, None


def _validate_offer(vertical: str, offer_line: str, generic_fallback: bool = False) -> str | None:
    """The two fields email 1's copy is built from.

    A digit in the offer line is refused here and in `outreach.offer_for_campaign`
    both — see that exception's docstring. The short version: that sentence
    reads as sender voice, so a number in it is never held to the prospect's
    evidence, which makes it the strongest claim in the message with nothing
    behind it. Statistics belong in a proof point, which demands a client name.
    """

    if offer_line and not vertical and not generic_fallback:
        return (
            "a campaign with an offer line needs a vertical too — the sentence is "
            '"We help <vertical> <offer line>". Tick the generic-vertical box if '
            "this offer really is vertical-agnostic."
        )
    if _DIGIT_RE.search(offer_line):
        return (
            "the offer line may not contain a number. Describe what you do here "
            "and add the result as a proof point, where it gets a client name "
            "and a date behind it."
        )
    return None


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


def _proof_points(session: Session, campaign_id: int) -> list[ProofPoint]:
    """Every proof point on a campaign, retired ones included.

    Retired claims are shown rather than hidden: the operator needs to see that
    a stat was pulled, not to wonder where it went, and a draft sent last month
    may still cite one. Ordered the way `outreach.select_proof_point` chooses —
    freshest first, undated last — so the row at the top is the one email 1
    will actually carry.
    """

    stmt = (
        select(ProofPoint)
        .where(ProofPoint.campaign_id == campaign_id)
        .order_by(
            ProofPoint.active.desc(),
            ProofPoint.occurred_on.desc().nullslast(),
            ProofPoint.id.desc(),
        )
    )
    return list(session.execute(stmt).scalars())


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
            "form": _campaign_form(),
            "proof_points": [],
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
    vertical: str = Form(""),
    offer_line: str = Form(""),
    qualification_config: str = Form("{}"),
    generic_vertical_fallback: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    name = (name or "").strip()
    segment_value = (segment or "").strip() or None
    vertical_value = (vertical or "").strip()
    offer_line_value = (offer_line or "").strip()
    parsed_config, config_error = _parse_scoring_config(scoring_config)
    parsed_qual, qual_error = _parse_qualification_config(qualification_config)
    generic_fallback = generic_vertical_fallback == "on"

    errors = [e for e in (
        "name is required" if not name else None,
        f"scoring_config: {config_error}" if config_error else None,
        f"qualification_config: {qual_error}" if qual_error else None,
        _validate_offer(vertical_value, offer_line_value, generic_fallback),
    ) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": workspace,
                "campaign": None,
                "error": "; ".join(errors),
                "form": _campaign_form(
                    name=name, segment=segment, scoring_config=scoring_config,
                    vertical=vertical, offer_line=offer_line,
                    qualification_config=qualification_config,
                    generic_vertical_fallback=generic_fallback,
                ),
                "proof_points": [],
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
        vertical=vertical_value or None,
        offer_line=offer_line_value or None,
        qualification_config=parsed_qual,
        generic_vertical_fallback=generic_fallback,
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
                "form": _campaign_form(
                    name=name, segment=segment, scoring_config=scoring_config,
                    vertical=vertical, offer_line=offer_line,
                    qualification_config=qualification_config,
                    generic_vertical_fallback=generic_fallback,
                ),
                "proof_points": [],
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
            "form": _campaign_form(
                name=campaign.name,
                segment=campaign.segment or "",
                scoring_config=json.dumps(
                    campaign.scoring_config or {}, indent=2, sort_keys=True
                ),
                vertical=campaign.vertical or "",
                offer_line=campaign.offer_line or "",
                qualification_config=json.dumps(
                    campaign.qualification_config or {}, indent=2, sort_keys=True
                ),
                generic_vertical_fallback=campaign.generic_vertical_fallback,
            ),
            "proof_points": _proof_points(session, campaign.id),
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
    vertical: str = Form(""),
    offer_line: str = Form(""),
    qualification_config: str = Form("{}"),
    generic_vertical_fallback: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    templates = _templates(request)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    name = (name or "").strip()
    segment_value = (segment or "").strip() or None
    vertical_value = (vertical or "").strip()
    offer_line_value = (offer_line or "").strip()
    parsed_config, config_error = _parse_scoring_config(scoring_config)
    parsed_qual, qual_error = _parse_qualification_config(qualification_config)
    generic_fallback = generic_vertical_fallback == "on"

    errors = [e for e in (
        "name is required" if not name else None,
        f"scoring_config: {config_error}" if config_error else None,
        f"qualification_config: {qual_error}" if qual_error else None,
        _validate_offer(vertical_value, offer_line_value, generic_fallback),
    ) if e]
    if errors:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            {
                "workspace": campaign.workspace,
                "campaign": campaign,
                "error": "; ".join(errors),
                "form": _campaign_form(
                    name=name, segment=segment, scoring_config=scoring_config,
                    vertical=vertical, offer_line=offer_line,
                    qualification_config=qualification_config,
                    generic_vertical_fallback=generic_fallback,
                ),
                "proof_points": _proof_points(session, campaign.id),
                "lead_count": _lead_count(session, campaign.id),
                "user": user,
            },
            status_code=400,
        )

    campaign.name = name
    campaign.segment = segment_value
    campaign.scoring_config = parsed_config
    campaign.vertical = vertical_value or None
    campaign.offer_line = offer_line_value or None
    campaign.qualification_config = parsed_qual
    campaign.generic_vertical_fallback = generic_fallback
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
                "form": _campaign_form(
                    name=name, segment=segment, scoring_config=scoring_config,
                    vertical=vertical, offer_line=offer_line,
                    qualification_config=qualification_config,
                    generic_vertical_fallback=generic_fallback,
                ),
                "proof_points": _proof_points(session, campaign_id),
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


# --------------------------------------------------------------------------
# Proof points
# --------------------------------------------------------------------------
#
# The sender's own evidence: the one sentence in email 1 that carries a number,
# and the only place in the system a digit may enter the copy from. Everything
# on this surface exists to stop an unattributed statistic being typed in --
# which is what the field it replaces (free text in `leads.notes`) allowed.
#
# Note what is NOT here: editing. A proof point that is wrong is RETIRED and a
# corrected one added, so the record of what a draft cited stays readable after
# the claim changes. Editing the text under a sent message would leave the
# audit trail describing a sentence nobody ever sent.


@router.post("/campaigns/{campaign_id}/proof-points", response_class=HTMLResponse)
async def create_proof_point(
    request: Request,
    campaign_id: int,
    claim_text: str = Form(...),
    client_name: str = Form(...),
    occurred_on: str = Form(""),
    source_note: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    claim = (claim_text or "").strip()
    client = (client_name or "").strip()
    note = (source_note or "").strip()
    occurred, date_error = _parse_occurred_on(occurred_on)

    errors = [e for e in (
        "the claim is required" if not claim else None,
        # Attribution is not optional. An unattributed statistic is the exact
        # shape of claim this table exists to prevent, and the column is NOT
        # NULL for the same reason.
        "name the client this result belongs to" if not client else None,
        date_error,
    ) if e]
    if errors:
        return _render_campaign_form(
            request, session, campaign, user,
            error="; ".join(errors),
            proof_form={
                "claim_text": claim_text,
                "client_name": client_name,
                "occurred_on": occurred_on,
                "source_note": source_note,
            },
            status_code=400,
        )

    session.add(
        ProofPoint(
            campaign_id=campaign.id,
            claim_text=claim,
            client_name=client,
            occurred_on=occurred,
            source_note=note or None,
        )
    )
    session.flush()
    return RedirectResponse(url=f"/campaigns/{campaign.id}/edit", status_code=303)


@router.post(
    "/campaigns/{campaign_id}/proof-points/{proof_point_id}/retire",
    response_class=HTMLResponse,
)
async def retire_proof_point(
    request: Request,
    campaign_id: int,
    proof_point_id: int,
    active: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Retire or restore a claim. Never deletes.

    Retiring takes effect immediately and retroactively: `verify` re-resolves
    proof points every time it runs, so a draft already written and waiting for
    review stops verifying the moment its claim is pulled. That is the point --
    a stat the client can no longer stand behind must not be one click from
    being sent.
    """

    proof = session.get(ProofPoint, proof_point_id)
    if proof is None or proof.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="proof point not found")

    proof.active = active == "true"
    session.flush()
    return RedirectResponse(url=f"/campaigns/{campaign_id}/edit", status_code=303)


def _parse_occurred_on(raw: str) -> tuple[date | None, str | None]:
    """An ISO date, or nothing. Blank is allowed; unparseable is not.

    Blank is honest ("I do not have the date to hand") and sorts the claim last
    when `outreach.select_proof_point` picks one, because an undated result is
    not evidence of recency. A date we misread would be worse than no date:
    the copy says "one RECENT client".
    """

    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, f"{text!r} is not a date (expected YYYY-MM-DD)"


def _render_campaign_form(
    request: Request,
    session: Session,
    campaign: Campaign,
    user: str,
    *,
    error: str | None = None,
    proof_form: dict | None = None,
    status_code: int = 200,
):
    return _templates(request).TemplateResponse(
        request,
        "campaign_form.html",
        {
            "workspace": campaign.workspace,
            "campaign": campaign,
            "error": error,
            "form": _campaign_form(
                name=campaign.name,
                segment=campaign.segment or "",
                scoring_config=json.dumps(
                    campaign.scoring_config or {}, indent=2, sort_keys=True
                ),
                vertical=campaign.vertical or "",
                offer_line=campaign.offer_line or "",
                qualification_config=json.dumps(
                    campaign.qualification_config or {}, indent=2, sort_keys=True
                ),
                generic_vertical_fallback=campaign.generic_vertical_fallback,
            ),
            "proof_points": _proof_points(session, campaign.id),
            "proof_form": proof_form or {},
            "lead_count": _lead_count(session, campaign.id),
            "user": user,
        },
        status_code=status_code,
    )
