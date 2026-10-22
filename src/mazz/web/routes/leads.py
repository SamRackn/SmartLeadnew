"""Lead table, lead detail, and the approve / edit / reject actions.

This module is the entire control surface for sending anything. Two rules
are enforced here, not just in the templates:

* No auto-send. There is no route that approves more than one lead, and no
  route that approves anything without a specific ``lead_id`` a human clicked
  into. Grep this file for "for lead in" if you doubt it — it is not here.
* A draft that is not ``is_approvable()`` (verification_status != PASSED)
  is rejected by ``approve()`` itself, with a 409, before a send job is ever
  created. The template hides the button too, but that is cosmetic; this
  check is the control.

Approve does NOT enqueue, and does not send. It records one human's judgement
that one draft is fit to go, and stops there. Creating the send job is a second,
separate human action on /outbox -- see `mazz.web.routes.outbox`.

That split is deliberate. When approve enqueued the send itself, the two
decisions "this copy is correct" and "put this on the wire now" were the same
click, which meant there was no moment at which a reviewer could approve a
batch of copy and then release it deliberately. Splitting them costs one extra
click and buys a gap in which an approval can still be revoked.

The message still leaves from the worker, which owns mailbox rotation, daily
caps and spacing, and which re-checks suppression at the moment of sending. A
route handler that talked to Gmail directly would either ignore the client's
daily caps or block an HTTP request on a provider call, and it would put the
last-moment compliance re-checks in the place least able to retry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.ingest.csv_import import SuppressionChecker, SuppressionUnavailable
from mazz.models import (
    Draft,
    Lead,
    LeadStatus,
    Mailbox,
    SentMessage,
    VerificationStatus,
)
from mazz.pipeline.draft import (
    MissingSenderAddress,
    render_body,
    split_body,
    unresolved_placeholders,
)
from mazz.pipeline.mailbox import MailboxUnavailable, NoMailboxConfigured
from mazz.pipeline.score import ScoredFact, ScoreResult, score_lead
from mazz.pipeline.send import unresolved_send_attempt
from mazz.pipeline.verify import (
    Grounding,
    apply_verification,
    build_grounding,
    is_approvable,
    latest_draft,
    verify_content,
    verify_draft,
)
from mazz.pipeline.score import facts_for_lead
from mazz.schemas import Fact
from mazz.web.auth import Account, current_account, current_user, visible_lead
# Only to WITHDRAW a release (delete a pending job). Creating a send job stays
# in outbox.release_one; this module imports neither the queue nor the kind.
from mazz.web.routes.outbox import cancel_pending_send, mailbox_for

router = APIRouter()

#: All statuses, in the order the table shows them. Deliberately includes
#: `no_angle` and `held` — the brief is explicit that hiding those states
#: would recreate the generic-filler problem the client is trying to escape.
ALL_STATUSES: tuple[LeadStatus, ...] = (
    LeadStatus.RESEARCHING,
    LeadStatus.READY,
    LeadStatus.NO_ANGLE,
    LeadStatus.HELD,
    LeadStatus.APPROVED,
    LeadStatus.SENT,
)


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _first_last(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# --------------------------------------------------------------------------
# Lead table
# --------------------------------------------------------------------------


@router.get("/leads", response_class=HTMLResponse)
async def list_leads(
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    templates = _templates(request)

    status_filter: LeadStatus | None = None
    if status:
        try:
            status_filter = LeadStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")

    query = (
        select(Lead)
        .where(account.lead_filter())
        .order_by(Lead.updated_at.desc(), Lead.id.desc())
    )
    if status_filter is not None:
        query = query.where(Lead.status == status_filter)
    leads = list(session.execute(query).scalars().all())

    # One extra query for the "governing draft" per lead, so the table can show
    # a verification hint without the reviewer opening every row.
    drafts_by_lead: dict[int, Draft] = {}
    for lead in leads:
        draft = latest_draft(session, lead)
        if draft is not None:
            drafts_by_lead[lead.id] = draft

    return templates.TemplateResponse(
        request,
        "leads_list.html",
        {
            "leads": leads,
            "drafts_by_lead": drafts_by_lead,
            "all_statuses": ALL_STATUSES,
            "active_status": status_filter,
            "user": user,
        },
    )


# --------------------------------------------------------------------------
# Lead detail
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CitedFact:
    """One signal cited by the draft, paired with its source for the template."""

    id: str
    kind: str
    content: str
    source_url: str | None


def _cited_facts(draft: Draft | None, facts: list[Fact]) -> list[_CitedFact]:
    if draft is None:
        return []
    by_id = {f.id: f for f in facts}
    out: list[_CitedFact] = []
    for fact_id in draft.signals_used or []:
        fact = by_id.get(fact_id)
        if fact is None:
            out.append(_CitedFact(id=fact_id, kind="(missing)", content="", source_url=None))
        else:
            out.append(
                _CitedFact(id=fact.id, kind=fact.kind, content=fact.content, source_url=fact.source_url)
            )
    return out


def _fact_visible_for(fact: Fact, lead: Lead) -> bool:
    """Company facts are shared across everyone at the domain (and across
    workspaces); a fact about a specific OTHER person is not this lead's to see."""

    subject = (fact.subject_email or "").strip().lower()
    return not subject or subject == (lead.email or "").strip().lower()


def _detail_context(
    request: Request, session: Session, lead_id: int, user: str
) -> dict:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    draft = latest_draft(session, lead)
    facts = [f for f in facts_for_lead(session, lead) if _fact_visible_for(f, lead)]
    score_result: ScoreResult = score_lead(session, lead)
    scored = [s for s in score_result.scored if _fact_visible_for(s.fact, lead)]

    grounding: Grounding | None = None
    if draft is not None:
        grounding = build_grounding(lead, list(draft.signals_used or []), facts)

    return {
        "request": request,
        "lead": lead,
        "draft": draft,
        "cited": _cited_facts(draft, facts),
        "scored": scored,
        "score_result": score_result,
        "grounding": grounding,
        "can_approve": draft is not None and is_approvable(draft),
        "send_state": _send_state(session, lead),
        "user": user,
        # The body with the compliance footer stripped, for the edit textarea.
        # Computed with `split_body()` -- the same helper `render_body()` /
        # `verify` use -- rather than a hardcoded copy of `FOOTER_SEPARATOR`,
        # so the textarea can never drift from what actually delimits the
        # footer. See the module docstring's bug note.
        "editable_body": split_body(draft.body)[0] if draft is not None else "",
    }


@dataclass(frozen=True, slots=True)
class SendState:
    """What the reviewer is told about sending, before and after the click.

    The screen used to say "push to Smartlead" and then, on success, nothing at
    all -- the lead went `sent` and where it went from was Smartlead's business.
    Now it is ours, so the reviewer sees which of the client's mailboxes will
    carry the message, and afterwards which one did.
    """

    #: Which mailbox would send it, if one can right now.
    mailbox: Mailbox | None = None
    #: Set when every mailbox is capped or spacing: the message waits, and this
    #: says until when, so a queued lead does not read as a broken one.
    waiting_until: datetime | None = None
    #: Set when no mailbox can EVER serve this lead. Approve refuses on this.
    problem: str | None = None
    #: After the fact: the audit row for the message that actually left.
    sent: SentMessage | None = None

    @property
    def sent_from(self) -> str | None:
        return self.sent.from_address if self.sent is not None else None


def _send_state(session: Session, lead: Lead) -> SendState:
    if lead.status == LeadStatus.SENT:
        sent = session.execute(
            select(SentMessage)
            .where(SentMessage.lead_id == lead.id)
            .order_by(SentMessage.sent_at.desc())
            .limit(1)
        ).scalars().first()
        return SendState(sent=sent)

    try:
        return SendState(mailbox=mailbox_for(session, lead))
    except MailboxUnavailable as exc:
        return SendState(waiting_until=exc.retry_at)
    except NoMailboxConfigured as exc:
        return SendState(problem=str(exc))


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    visible_lead(session, account, lead_id)
    templates = _templates(request)
    context = _detail_context(request, session, lead_id, user)
    return templates.TemplateResponse(request, "lead_detail.html", context)


def _panel_template_name(request: Request) -> str:
    """Which partial renders the draft + actions, for this request.

    Approve/reject/edit are one set of routes shared by two screens: the
    original single-column `/leads/{id}` page (target `#draft-panel`) and the
    two-pane `/review` screen (target `#review-panel`). htmx sends the
    target element's id as the `HX-Target` request header, so the same route
    can answer with the partial that actually matches what asked for it,
    without either screen needing its own copy of this business logic.
    """

    if request.headers.get("HX-Target") == "review-panel":
        return "partials/review_panel.html"
    return "partials/draft_panel.html"


def _draft_panel_response(
    request: Request,
    session: Session,
    lead_id: int,
    user: str,
    *,
    status_code: int = 200,
    error: str | None = None,
):
    """Render the panel for one lead, optionally carrying a visible error.

    ``error`` is how a refused action is surfaced. htmx 1.9 does not swap a
    non-2xx response into the DOM, so a route that raised `HTTPException` for
    an ordinary, expected refusal (already sent, not approvable) rendered
    NOTHING in the browser -- the operator clicked Approve and saw no
    feedback at all. Returning the panel itself, with ``status_code`` still
    set to the refusal code and an error banner explaining why, keeps the
    refusal visible without weakening it: the status code is still there for
    anything that inspects the API contract (tests included), and the
    server-side check that produced it has not moved or softened.
    """

    templates = _templates(request)
    context = _detail_context(request, session, lead_id, user)
    context["error"] = error
    return templates.TemplateResponse(
        request, _panel_template_name(request), context, status_code=status_code
    )


def _attempt_unresolved(request: Request, session: Session, lead_id: int, user: str):
    # The copy was handed to the sender and no result was booked: it may be in
    # the recipient's inbox. Rewriting or failing it would falsify that record.
    return _draft_panel_response(
        request, session, lead_id, user, status_code=409,
        error=(
            f"lead {lead_id}'s email may already have been sent (no result was "
            "booked). Resolve it first: python -m mazz.ops resolve-send "
            f"{lead_id} --sent or --not-sent --by <your email>."
        ),
    )


# --------------------------------------------------------------------------
# Actions: approve / edit / reject
# --------------------------------------------------------------------------


@router.post("/leads/{lead_id}/approve", response_class=HTMLResponse)
async def approve_lead(
    request: Request,
    lead_id: int,
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """Approve exactly one lead's current draft. Queues nothing.

    Server-side gate: a draft whose `verification_status` is not PASSED is
    refused here with a 409, regardless of what the UI shows. This is the
    control the brief requires — not the hidden button.

    The lead goes to ``approved``, not ``sent``, and no send job is created.
    Releasing it is a separate human action on /outbox. It becomes ``sent``
    when the message actually leaves, which the worker records against a real
    receipt. Marking it sent at click time would be a lie the client could not
    detect: a screen saying "sent" for an email still sitting behind a daily
    cap, or one that failed on a dead credential.
    """

    lead = visible_lead(session, account, lead_id)

    if lead.status == LeadStatus.SENT:
        # Refused, not silently ignored -- but not a bare HTTPException either.
        # See `_draft_panel_response`'s docstring: htmx will not swap a
        # non-2xx response, so the refusal has to come back as the panel
        # itself (still carrying the 409) or the operator sees nothing.
        return _draft_panel_response(
            request,
            session,
            lead_id,
            user,
            status_code=409,
            error=(
                f"lead {lead_id} has already been sent. Approving again would "
                "email the same person the same thing twice."
            ),
        )

    if lead.status == LeadStatus.APPROVED:
        # Idempotent: a second click (or a second reviewer) must not restamp
        # who approved it, or when.
        return _draft_panel_response(request, session, lead_id, user)

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to approve")

    if not is_approvable(draft):
        # THE control (see module docstring): a draft that is not PASSED is
        # refused here, in the route, regardless of what the button looked
        # like. Visible, not just server-enforced -- see the docstring above.
        return _draft_panel_response(
            request,
            session,
            lead_id,
            user,
            status_code=409,
            error=(
                f"draft {draft.id} is not approvable "
                f"(verification_status={draft.verification_status.value}); "
                "a failed or pending draft can never be sent. "
                f"verification_notes: {draft.verification_notes or '(none)'}"
            ),
        )

    holes = unresolved_placeholders(draft.subject, draft.body)
    if holes:
        # A template rendered with a hole in it -- `{day}` nobody filled, or
        # no subject at all. Refused here, server-side, like a failed draft.
        return _draft_panel_response(
            request,
            session,
            lead_id,
            user,
            status_code=409,
            error=(
                f"draft {draft.id} cannot be approved: unfilled merge fields or "
                f"no subject {holes}. Edit the draft to fill them first."
            ),
        )

    if lead.status != LeadStatus.READY:
        # Only a draft that finished verification and is waiting for review can
        # be approved. `held` (suppressed, stopped, rejected, failed), `no_angle`
        # and `researching` are not an approver's to override.
        return _draft_panel_response(
            request,
            session,
            lead_id,
            user,
            status_code=409,
            error=(
                f"lead {lead_id} is {lead.status.value}, not ready for review. "
                "Only a lead whose draft passed verification can be approved."
            ),
        )

    if lead.sequence_state == "stopped":
        return _draft_panel_response(
            request, session, lead_id, user, status_code=409,
            error=(
                f"lead {lead_id}'s sequence was stopped (a reply, bounce or "
                "unsubscribe), so this email will not go out. Not approved."
            ),
        )
    try:
        match = SuppressionChecker(session).check_lead(lead)
    except SuppressionUnavailable as exc:
        # Fails closed, like every other suppression read.
        return _draft_panel_response(
            request, session, lead_id, user, status_code=503,
            error=f"suppression list unreadable, refusing to approve: {exc}",
        )
    if match is not None:
        return _draft_panel_response(
            request, session, lead_id, user, status_code=409,
            error=f"{lead.email} is suppressed ({match.describe()}). Not approved.",
        )

    lead.status = LeadStatus.APPROVED
    # The database clock, like `jobs.created_at`: the send step refuses a
    # release older than the approval it is about to act on.
    lead.approved_at = func.now()
    lead.approved_by = user
    # A send job left over from an earlier release belongs to earlier copy.
    cancel_pending_send(session, lead.id)
    session.flush()
    audit.record(session, user, "approve", "lead", lead.id, draft_id=draft.id)

    # NOTE FOR ANYONE EDITING THIS FUNCTION
    # Do not queue a send here. A send job used to be created on this very line,
    # and moving it out is the entire point of the split: exactly one human
    # pressing send on exactly one message creates one, in
    # `mazz.web.routes.outbox`, and nothing else in the codebase does.
    # This module no longer imports the queue or the send-job kind at all, so
    # putting one back means adding an import too -- which is the point. Two
    # tests hold the line: one asserts approving creates no job row, one reads
    # this file's source (tests/test_sending.py).
    #
    # The roster is deliberately NOT checked here either. Whether a mailbox is
    # free is a question about sending, and approving is no longer sending; a
    # 503 at this point would block the reviewer from recording a judgement
    # about copy because of an operational problem with the wire. /outbox
    # reports the roster state, and the send action enforces it.

    return _draft_panel_response(request, session, lead_id, user)


@router.post("/leads/{lead_id}/reject", response_class=HTMLResponse)
async def reject_lead(
    request: Request,
    lead_id: int,
    reason: str = Form(""),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """A human declines to send this draft. Holds the lead; never auto-anything.

    Reuses `held` / `verification_status=failed` rather than inventing a new
    lead status, so `is_approvable()` continues to be the single source of
    truth for "may this be sent" — a rejected draft is, correctly, not
    approvable until a new draft replaces it.
    """

    lead = visible_lead(session, account, lead_id)

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to reject")

    already_sent = session.execute(
        select(SentMessage.id).where(SentMessage.draft_id == draft.id)
    ).first()
    if lead.status == LeadStatus.SENT or already_sent is not None:
        # Sent copy is history. Rejecting it would turn a sent lead into a held
        # one and mark the email that actually left as failed verification.
        return _draft_panel_response(
            request, session, lead_id, user, status_code=409,
            error=f"lead {lead_id}'s email has already been sent; it can no longer be rejected.",
        )
    if unresolved_send_attempt(session, lead) is not None:
        return _attempt_unresolved(request, session, lead_id, user)

    note = f"REJECTED by reviewer ({user}).\n" + (reason.strip() or "(no reason given)")
    draft.verification_status = VerificationStatus.FAILED
    draft.verification_notes = note
    lead.status = LeadStatus.HELD
    # Withdraw a release already on the queue: a later edit and re-approval
    # needs a fresh release click of its own.
    cancel_pending_send(session, lead.id)
    session.flush()
    audit.record(
        session, user, "reject", "lead", lead.id, draft_id=draft.id, reason=reason.strip() or None
    )

    return _draft_panel_response(request, session, lead_id, user)


@router.post("/leads/{lead_id}/edit", response_class=HTMLResponse)
async def edit_draft(
    request: Request,
    lead_id: int,
    subject: str = Form(...),
    body_text: str = Form(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
    account: Account = Depends(current_account),
):
    """A human rewrites the draft's subject/body, then it is re-verified.

    Editing does not grant approvability by itself — the edited copy is run
    back through `verify_content` (the same compliance control drafting
    goes through) before the lead can move anywhere. Loosening or skipping
    that check on the "someone reviewed it" theory is exactly the softened
    control the brief warns against.
    """

    lead = visible_lead(session, account, lead_id)

    draft = latest_draft(session, lead)
    if draft is None:
        raise HTTPException(status_code=404, detail="lead has no draft to edit")
    if lead.status in (LeadStatus.SENT, LeadStatus.APPROVED):
        # Sent copy is history; approved copy is un-approved by rejecting first.
        return _draft_panel_response(
            request, session, lead_id, user, status_code=409,
            error=f"this lead is {lead.status.value}; its draft can no longer be edited",
        )
    if unresolved_send_attempt(session, lead) is not None:
        return _attempt_unresolved(request, session, lead_id, user)

    try:
        body = render_body(body_text, lead)
    except MissingSenderAddress as exc:
        return _draft_panel_response(
            request, session, lead_id, user, status_code=400, error=str(exc)
        )

    draft.subject = subject
    draft.body = body
    cancel_pending_send(session, lead.id)

    # The same verification drafting goes through, campaign copy and proof
    # points included -- a bare `verify_content` here would re-check an edited
    # campaign email without the evidence for its vertical noun and hold it.
    result = verify_draft(session, lead, draft)
    apply_verification(session, lead, draft, result)
    audit.record(
        session, user, "edit", "lead", lead.id, draft_id=draft.id,
        verification=result.status.value,
    )

    return _draft_panel_response(request, session, lead_id, user)
