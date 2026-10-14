"""The Search Site Marketing V1 campaign: the client's approved copy, as data.

Run through ``python -m mazz.users seed-launch``. Idempotent: an existing
campaign keeps its row and its steps are rewritten to exactly this copy.
Text is verbatim from the client's launch answers (2026-09-29); change it
only with a new sign-off.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz.models import Campaign, Mailbox, MailboxStatus, SequenceStep, SuppressionEntry, Workspace

CAMPAIGN_NAME = "Search Site Marketing, law firms"
VERTICAL = "law firms"
OFFER_LINE = "generate qualified client opportunities through search marketing"
SIGNATURE = "Sean Gough\nSearch Site Marketing, Inc"

#: (step, wait_days after the previous SEND, subject, body). Blank subject on
#: a follow-up = "Re: Quick Question" in the same thread.
STEPS = (
    (1, 0, "Quick Question",
     "Hey {first_name},\n\n"
     "Quick question: is your firm currently looking to bring in more clients?\n\n"
     "We help {vertical} {offer_line}.\n\n"
     "Worth sending over a little more info?\n\n"
     "{signature}"),
    (2, 3, None,
     "Hey {first_name},\n\n"
     "Just following up on my last email. Worth sending over a little more info?\n\n"
     "{signature}"),
    (3, 4, None,
     "Hey {first_name},\n\n"
     "This is my last note. If you want more info later, just reply and I'll send it over.\n\n"
     "{signature}"),
)

#: The client's rule: confirmed from the site by any of these.
QUALIFICATION = {
    "vertical_terms": [
        "attorney", "attorneys", "law firm", "law firms", "law office", "law offices",
        "practice areas", "practice area", "llp", "pllc", "p.c.",
    ],
    # Two distinct terms, not one: "LLP" or "P.C." alone also fits an
    # accounting firm, and "We help law firms" to a CPA is a false premise
    # (audit M2, Sam's decision 2026-10-14).
    "min_vertical_matches": 2,
    # The copy's question presupposes nothing the site must show; the gate is
    # the vertical alone (client decision).
    "require_intake_signal": False,
    # Site blocked or unreachable: legal-services industry AND a law-firm name.
    "fallback_industry_terms": ["legal services", "law firms", "legal", "law practice"],
    "fallback_name_terms": [
        "law", "legal", "attorney", "attorneys", "lawyers", "llp", "pllc", "p.c.", "esq",
    ],
}

#: Sean's four sending mailboxes (confirmed by Maaz 2026-10-14). The cap is
#: the client's launch decision: 15/mailbox/day, 60/day in all, follow-ups
#: included. Raising it is a human edit on /mailboxes, never a re-seed.
MAILBOXES = (
    "sean.gough@getsearchsitemarketing.com", "gough@getsearchsitemarketing.com",
    "sean.gough@searchsitemarketinghub.com", "gough@searchsitemarketinghub.com",
)
DISPLAY_NAME = "Sean Gough"
DAILY_CAP = 15

#: Never contact: the senders' own domains (client decision).
OWN_DOMAINS = (
    "searchsitemarketing.com", "getsearchsitemarketing.com", "searchsitemarketinghub.com",
    "clientrise.io", "clientrisehq.com",
)


def ensure_launch_campaign(session: Session, workspace: Workspace) -> Campaign:
    campaign = session.execute(
        select(Campaign).where(
            Campaign.workspace_id == workspace.id, Campaign.name == CAMPAIGN_NAME
        )
    ).scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(workspace_id=workspace.id, name=CAMPAIGN_NAME)
        session.add(campaign)
    campaign.vertical = VERTICAL
    campaign.offer_line = OFFER_LINE
    campaign.signature = SIGNATURE
    campaign.qualification_config = QUALIFICATION
    campaign.generic_vertical_fallback = False
    session.flush()

    existing = {s.step: s for s in campaign.steps}
    for step, wait, subject, body in STEPS:
        row = existing.get(step) or SequenceStep(campaign_id=campaign.id, step=step)
        row.wait_days, row.subject, row.body_template, row.enabled = wait, subject, body, True
        session.add(row)

    for domain in OWN_DOMAINS:
        found = session.execute(
            select(SuppressionEntry.id).where(
                SuppressionEntry.domain == domain, SuppressionEntry.workspace_id.is_(None)
            )
        ).first()
        if found is None:
            session.add(SuppressionEntry(domain=domain, reason="own domain", added_by="seed-launch"))
    session.flush()
    return campaign


def ensure_launch_mailboxes(session: Session, campaign: Campaign) -> None:
    """Sean's four mailboxes, pinned to the campaign at the launch cap.

    Insert only: an existing row keeps whatever cap, pin and status a person
    gave it. Each starts ``needs_reconnect`` and is never picked to send
    until connected through /mailboxes/<id>/connect."""

    for address in MAILBOXES:
        if session.execute(select(Mailbox.id).where(Mailbox.email_address == address)).first() is None:
            session.add(Mailbox(
                email_address=address, display_name=DISPLAY_NAME, credential_ref="oauth",
                daily_cap=DAILY_CAP, campaign_id=campaign.id,
                # Never selected to send until the OAuth callback connects it.
                status=MailboxStatus.NEEDS_RECONNECT,
            ))
    session.flush()
