"""The only routes that answer without a login: unsubscribe, health, and the
public /about and /privacy pages Google's OAuth branding requires.

``/u/<token>`` is the link in every footer and the target of the
``List-Unsubscribe`` header. GET shows a one-button confirm page (a link
scanner or preview pane prefetching the URL must not unsubscribe anyone);
POST suppresses immediately — that is the RFC 8058 one-click path mail
clients call with ``List-Unsubscribe=One-Click``, and the button's target.

The suppression is GLOBAL (every workspace): someone who asked one of our
senders to stop has asked us to stop.

These routes are also the whole surface of the dedicated unsubscribe host;
`app.py` 404s everything else arriving on it.
"""

from __future__ import annotations

import hmac
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mazz import audit
from mazz.db import get_session
from mazz.ingest.csv_import import email_sha256
from mazz.models import Lead, LeadStatus, SuppressionEntry
from mazz.pipeline.draft import unsubscribe_token
from mazz.pipeline.sequence import LIVE_STATES

router = APIRouter()

#: Statuses an unsubscribe pulls back out of the send path. A lead already
#: `sent` keeps its status (it records what happened); its sequence is
#: stopped, and the follow-up and send steps re-check suppression anyway.
_STOPPABLE = (LeadStatus.RESEARCHING, LeadStatus.READY, LeadStatus.APPROVED)

_DEAD_LINK = (
    "<h1>This link is no longer active</h1><p>If you are still receiving "
    "email from us, reply with the word <strong>unsubscribe</strong> and "
    "we will remove you.</p>"
)


def lead_for_token(session: Session, token: str) -> Lead | None:
    """The lead whose unsubscribe token this is, or None.

    ponytail: one HMAC per lead per click, O(n). Fine at thousands of leads;
    store the token in an indexed column if the list reaches ~100k.
    """

    if not token or len(token) > 64:
        return None
    for lead_id, email in session.execute(select(Lead.id, Lead.email)):
        if hmac.compare_digest(unsubscribe_token(email), token):
            return session.get(Lead, lead_id)
    return None


def suppress_email(
    session: Session, email: str, *, reason: str, actor: str
) -> SuppressionEntry:
    """Add a global email suppression unless one already exists. Idempotent."""

    email = email.strip().lower()
    digest = email_sha256(email)
    existing = session.execute(
        select(SuppressionEntry).where(
            SuppressionEntry.workspace_id.is_(None),
            (SuppressionEntry.email == email) | (SuppressionEntry.email_sha256 == digest),
        )
    ).scalars().first()
    if existing is not None:
        return existing
    entry = SuppressionEntry(email=email, email_sha256=digest, reason=reason, added_by=actor)
    session.add(entry)
    session.flush()
    return entry


def _page(title: str, body: str) -> HTMLResponse:
    # Self-contained: the unsubscribe host serves nothing else, not even /static.
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>"
        "body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;color:#222;background:#fff}"
        "button{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:6px;"
        "background:#222;color:#fff;cursor:pointer}"
        "</style></head><body>" + body + "</body></html>"
    )


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, session: Session = Depends(get_session)):
    lead = lead_for_token(session, token)
    if lead is None:
        return _page("Unsubscribe", _DEAD_LINK)
    return _page(
        "Unsubscribe",
        f"<h1>Unsubscribe</h1><p>Stop all email to "
        f"<strong>{escape(_mask(lead.email))}</strong>?</p>"
        f"<form method='post' action='/u/{escape(token)}'>"
        "<button type='submit'>Unsubscribe</button></form>",
    )


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, session: Session = Depends(get_session)):
    lead = lead_for_token(session, token)
    if lead is None:
        # Not 200: RFC 8058 clients read any 2xx as "unsubscribed", and this
        # link suppressed nobody.
        page = _page("Unsubscribe", _DEAD_LINK)
        page.status_code = 404
        return page
    entry = suppress_email(session, lead.email, reason="unsubscribe link", actor=audit.SYSTEM)
    if lead.status in _STOPPABLE:
        lead.status = LeadStatus.HELD
    if lead.sequence_state in LIVE_STATES:
        lead.sequence_state, lead.next_step_due_at = "stopped", None
    audit.record(
        session, audit.SYSTEM, "unsubscribe", "lead", lead.id,
        source="recipient_link", suppression_id=entry.id,
    )
    return _page(
        "Unsubscribed",
        "<h1>You're unsubscribed</h1><p>You will not receive any more email from us.</p>",
    )


# Google's OAuth branding requires a public homepage and privacy policy on the
# authorized domain. Served by the app host; the unsubscribe host 404s them.
CONTACT_EMAIL = "sean.gough@getsearchsitemarketing.com"

_ABOUT = f"""
<h1>Search Site Marketing Outreach</h1>
<p>Search Site Marketing Outreach is the internal email outreach software used
by Search Site Marketing to contact businesses about its marketing services.</p>
<p>It connects to Search Site Marketing's own Google Workspace mailboxes to:</p>
<ul>
<li>send outreach emails that a person at Search Site Marketing has reviewed and
approved one at a time, and</li>
<li>read replies to those emails so they can be answered and so anyone who asks
to stop is removed.</li>
</ul>
<p>The software is used only by Search Site Marketing staff. It is not offered
to the public and there is no sign-up.</p>
<p><a href="/privacy">Privacy policy</a> &middot; Contact:
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></p>
"""

_PRIVACY = f"""
<h1>Privacy policy</h1>
<p><em>Search Site Marketing Outreach &middot; effective 22 October 2026</em></p>

<h2>Who we are</h2>
<p>Search Site Marketing Outreach is internal software operated by Search Site
Marketing. Questions about this policy:
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>

<h2>Google user data we access</h2>
<p>The software connects only to mailboxes belonging to Search Site Marketing,
with the mailbox owner's consent through Google sign-in. It requests two Gmail
permissions and no others:</p>
<ul>
<li><strong>Send email (gmail.send)</strong>: to send outreach emails that a person
has individually approved.</li>
<li><strong>Read email (gmail.readonly)</strong>: to find replies, bounces and
unsubscribe requests on the conversations the software started.</li>
</ul>

<h2>How we use it</h2>
<ul>
<li>We store only messages in conversations the software itself started. Other
mail in the mailbox is not stored.</li>
<li>Stored replies are used to show them to Search Site Marketing staff, to
classify them (for example out-of-office or unsubscribe), and to stop emailing
anyone who asks.</li>
<li>Google sign-in tokens are encrypted at rest and used only to send and read
mail as described above.</li>
</ul>

<h2>What we do not do</h2>
<ul>
<li>We do not sell Google user data or any other personal data.</li>
<li>We do not use Google user data for advertising.</li>
<li>We do not use Google user data to train AI or machine-learning models.</li>
<li>We do not share Google user data with third parties, except the hosting
provider that runs the software, or where required by law.</li>
</ul>
<p>Search Site Marketing Outreach's use and transfer of information received
from Google APIs adheres to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google
API Services User Data Policy</a>, including the Limited Use requirements.</p>

<h2>People we email</h2>
<p>We hold business contact details (name, company, work email, job title) for
the people we contact. Every email has an unsubscribe link; using it, or
replying to ask us to stop, removes you from all future outreach. To have your
details deleted, email <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>

<h2>Keeping and deleting data</h2>
<p>Data is kept while a mailbox is connected and outreach is active. A mailbox
owner can revoke access at any time at
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>;
the software then stops sending from and reading that mailbox. On request we
delete stored tokens and messages.</p>

<h2>Security</h2>
<p>Data is stored on a private server, sign-in tokens are encrypted, and the
software is accessible only to named Search Site Marketing staff with
individual logins.</p>

<h2>Changes</h2>
<p>We will update the date above when this policy changes.</p>
"""


@router.get("/about", response_class=HTMLResponse)
def about():
    return _page("Search Site Marketing Outreach", _ABOUT)


@router.get("/privacy", response_class=HTMLResponse)
def privacy():
    return _page("Privacy policy", _PRIVACY)


@router.get("/health")
def health(session: Session = Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health reports, it does not raise
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}
