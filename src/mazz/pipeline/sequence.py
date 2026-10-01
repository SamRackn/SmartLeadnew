"""Sequences: follow-ups, business days, the send window, the per-company cap.

A sent step schedules the next one as a ``followup`` job with ``run_after`` at
its due date. That job only WRITES a draft and chains to ``verify``: the lead
lands back in the review queue as ``ready``, and a human approves and releases
it exactly like Email 1. Nothing here creates a send job (guardrail 4).

Due dates and the send window are judged in the lead's local time (from its
US state), Eastern when the state is unknown.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from mazz.models import Campaign, Draft, Lead, LeadStatus, SentMessage, SequenceStep

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from mazz.pipeline.queue import JobContext

log = logging.getLogger(__name__)

FOLLOWUP_JOB_KIND = "followup"
EASTERN = ZoneInfo("America/New_York")
WINDOW_START, WINDOW_END = time(9, 0), time(17, 0)

#: Sequence states that occupy one of a company's concurrent slots.
LIVE_STATES = ("active", "paused")

_ET, _CT, _MT, _PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
# ponytail: one zone per state; split states (TX west, FL panhandle, ...) use
# the majority zone. Fine for a 9-17 window.
_STATE_TZ = {
    "AL": _CT, "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": _CT, "CA": _PT,
    "CO": _MT, "CT": _ET, "DE": _ET, "DC": _ET, "FL": _ET, "GA": _ET, "HI": "Pacific/Honolulu",
    "ID": _MT, "IL": _CT, "IN": _ET, "IA": _CT, "KS": _CT, "KY": _ET, "LA": _CT, "ME": _ET,
    "MD": _ET, "MA": _ET, "MI": _ET, "MN": _CT, "MS": _CT, "MO": _CT, "MT": _MT, "NE": _CT,
    "NV": _PT, "NH": _ET, "NJ": _ET, "NM": _MT, "NY": _ET, "NC": _ET, "ND": _CT, "OH": _ET,
    "OK": _CT, "OR": _PT, "PA": _ET, "RI": _ET, "SC": _ET, "SD": _CT, "TN": _CT, "TX": _CT,
    "UT": _MT, "VT": _ET, "VA": _ET, "WA": _PT, "WV": _ET, "WI": _CT, "WY": _MT, "PR": "America/Puerto_Rico",
}
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def _state_code(text: str | None) -> str | None:
    raw = (text or "").strip().strip(".").lower()
    if not raw:
        return None
    if raw.upper() in _STATE_TZ:
        return raw.upper()
    return _STATE_NAMES.get(raw)


def lead_timezone(lead: Lead) -> ZoneInfo:
    """From ``state``, else the last state-looking piece of ``location``
    ("Austin, TX, United States"), else Eastern."""

    code = _state_code(lead.state)
    if code is None:
        for piece in reversed((lead.location or "").split(",")):
            code = _state_code(piece)
            if code:
                break
    return ZoneInfo(_STATE_TZ[code]) if code else EASTERN


def _skip_weekend(local: datetime) -> datetime:
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local


def due_after(sent_at: datetime, wait_days: int, tz: ZoneInfo) -> datetime:
    """``wait_days`` calendar days after the send, rolled to the next business
    day if that lands on a weekend. Returned in UTC."""

    local = _skip_weekend(sent_at.astimezone(tz) + timedelta(days=wait_days))
    return local.astimezone(timezone.utc)


def next_window_open(now: datetime, tz: ZoneInfo) -> datetime | None:
    """None when ``now`` is inside Mon-Fri 9:00-17:00 local; else the next
    opening, in UTC. Holidays are not modelled."""

    local = now.astimezone(tz)
    if local.weekday() < 5 and WINDOW_START <= local.time() < WINDOW_END:
        return None
    day = local.date() if local.time() < WINDOW_START else local.date() + timedelta(days=1)
    opening = _skip_weekend(datetime.combine(day, WINDOW_START, tzinfo=tz))
    return opening.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def step_for(session: "Session", campaign_id: int | None, step: int) -> SequenceStep | None:
    if campaign_id is None:
        return None
    return session.execute(
        select(SequenceStep).where(
            SequenceStep.campaign_id == campaign_id,
            SequenceStep.step == step,
            SequenceStep.enabled.is_(True),
        )
    ).scalar_one_or_none()


def first_name_for(lead: Lead) -> str:
    if (lead.first_name or "").strip():
        return lead.first_name.strip()
    parts = (lead.full_name or "").split()
    return parts[0] if parts else "there"


def render_template(template: str, lead: Lead, campaign: Campaign, *, vertical: str = "") -> str:
    """Fill the known fields. Unknown ``{...}`` stays put, so the placeholder
    guard refuses it rather than a KeyError hiding which field was wrong."""

    values = {
        "first_name": first_name_for(lead),
        "company_name": (lead.company_name or "").strip(),
        "vertical": vertical,
        "offer_line": " ".join((campaign.offer_line or "").split()),
        "signature": (campaign.signature or "").strip(),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out.strip()


def thread_parent(session: "Session", lead: Lead) -> SentMessage | None:
    """The most recent message actually sent to this lead: what a follow-up
    replies to, and the mailbox it must leave from."""

    return session.execute(
        select(SentMessage)
        .where(SentMessage.lead_id == lead.id)
        .order_by(SentMessage.sent_at.desc(), SentMessage.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def thread_root(session: "Session", lead: Lead) -> SentMessage | None:
    return session.execute(
        select(SentMessage)
        .where(SentMessage.lead_id == lead.id)
        .order_by(SentMessage.sent_at, SentMessage.id)
        .limit(1)
    ).scalar_one_or_none()


def schedule_next_step(session: "Session", lead: Lead, draft: Draft, sent_at: datetime) -> None:
    """After a send: queue the next step at its due date, or finish."""

    from mazz.pipeline.queue import enqueue_job

    lead.sequence_state = lead.sequence_state or "active"
    nxt = step_for(session, lead.campaign_id, (draft.step or 1) + 1)
    if nxt is None:
        lead.sequence_state = "complete"
        lead.next_step_due_at = None
        return
    due = due_after(sent_at, nxt.wait_days, lead_timezone(lead))
    lead.next_step_due_at = due
    enqueue_job(session, lead.id, FOLLOWUP_JOB_KIND, run_after=due)


def handle_followup(ctx: "JobContext") -> None:
    """Write the due follow-up draft and chain to verify. A no-op when the
    sequence has been stopped, paused or finished since it was scheduled."""

    from mazz.pipeline.draft import FOOTER_SEPARATOR, render_body
    from mazz.pipeline.outreach import effective_vertical
    from mazz.pipeline.qualify import qualify_lead

    session, lead = ctx.session, ctx.lead
    if lead.status != LeadStatus.SENT or lead.sequence_state != "active":
        log.info("lead %s: follow-up skipped (status=%s, sequence=%s)",
                 lead.id, lead.status.value, lead.sequence_state)
        return

    step_no = (lead.sequence_step or 1) + 1
    campaign = session.get(Campaign, lead.campaign_id) if lead.campaign_id else None
    step = step_for(session, lead.campaign_id, step_no)
    parent = thread_parent(session, lead)
    if campaign is None or step is None or parent is None:
        lead.sequence_state = "complete"
        lead.next_step_due_at = None
        return

    # Same premise as Email 1: if the lead no longer qualifies, stop.
    result = qualify_lead(session, lead)
    if not result.qualified:
        lead.sequence_state = "stopped"
        lead.skip_reason = f"followup_{result.verdict.value}"
        lead.next_step_due_at = None
        log.info("lead %s: sequence stopped, no longer qualifies: %s", lead.id, result.rationale)
        return

    root = thread_root(session, lead)
    base_subject = (root.subject if root else parent.subject) or ""
    subject = (step.subject or "").strip() or (
        base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
    )
    message = render_template(
        step.body_template, lead, campaign, vertical=effective_vertical(campaign) or ""
    )
    if FOOTER_SEPARATOR in message:
        raise ValueError(f"step {step_no} template contains the footer separator")
    session.add(
        Draft(
            lead_id=lead.id,
            step=step_no,
            subject=subject,
            body=render_body(message, lead),
            signals_used=result.cited_ids or ["lead_record"],
        )
    )
    lead.sequence_step = step_no
    lead.next_step_due_at = None
    lead.status = LeadStatus.RESEARCHING
    session.flush()
    ctx.enqueue_next("verify")


# --------------------------------------------------------------------------
# Per-company cap
# --------------------------------------------------------------------------


def active_at_company(session: "Session", lead: Lead) -> int:
    """Other leads at this company_domain with a live sequence."""

    if not lead.company_domain:
        return 0
    return int(
        session.execute(
            select(func.count(Lead.id)).where(
                Lead.company_domain == lead.company_domain,
                Lead.id != lead.id,
                Lead.sequence_state.in_(LIVE_STATES),
                Lead.status.notin_((LeadStatus.HELD, LeadStatus.NO_ANGLE)),
            )
        ).scalar_one()
    )


COMPANY_CAP_MARKER = "company cap"


def check_company_cap(session: "Session", lead: Lead, *, now: datetime) -> None:
    """Raise `JobDeferred` (a day out) if starting this lead would exceed its
    campaign's ``max_active_per_company``. Only Email 1 is capped; a running
    sequence is never cut off by a colleague arriving later."""

    from mazz.pipeline.queue import JobDeferred

    if lead.sequence_state is not None or lead.campaign is None:
        return
    cap = lead.campaign.max_active_per_company
    # ponytail: counted, not locked; one worker means no race. Lock the
    # domain row if workers ever run concurrently.
    if cap > 0 and active_at_company(session, lead) >= cap:
        raise JobDeferred(
            f"{COMPANY_CAP_MARKER}: {cap} sequences already live at {lead.company_domain}",
            retry_at=now + timedelta(days=1),
        )
