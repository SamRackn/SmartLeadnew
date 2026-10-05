"""Deliverability rehearsal: one real message from each connected mailbox to a
seed inbox, then the receiving side's ``Authentication-Results``.

    python scripts/rehearsal.py --seeds maaz@clientrise.io,sean.gough@searchsitemarketinghub.com

Refuses unless ``SEND_ENABLED=true``, ``EMAIL_SENDER=google`` and ``--seeds``
are given. It never touches a lead: no draft, no job, no ``sent_messages`` row
(so these few messages do not count toward the daily cap). Seeds are spread
over the mailboxes, never a mailbox to itself. A seed that is itself a
connected mailbox is read back through the Gmail reader; for any other seed
the script says what to look for in the received headers.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone


def refusal(settings, seeds: list[str]) -> str | None:
    if not settings.send_enabled:
        return "SEND_ENABLED is not true: a rehearsal sends real mail, so it needs the switch on"
    if settings.email_sender != "google":
        return f"EMAIL_SENDER is {settings.email_sender!r}, not 'google': nothing would leave"
    if not seeds:
        return "no --seeds given: name the inboxes Maaz and Sean will check"
    return None


def pairs(mailboxes: list[str], seeds: list[str]) -> list[tuple[str, str]]:
    """Each mailbox -> one seed, round-robin, skipping itself."""

    out = []
    for i, box in enumerate(mailboxes):
        others = [s for s in seeds if s != box] or seeds
        out.append((box, others[i % len(others)]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="", help="comma-separated seed addresses")
    parser.add_argument("--wait", type=int, default=120, help="seconds to wait for delivery")
    args = parser.parse_args(argv)
    seeds = [s.strip().lower() for s in args.seeds.split(",") if s.strip()]

    from mazz.config import get_settings

    reason = refusal(get_settings(), seeds)
    if reason:
        print(f"refused: {reason}", file=sys.stderr)
        return 2

    from sqlalchemy import select

    from mazz.db import session_scope
    from mazz.integrations import gmail_reader as gr
    from mazz.integrations.email_sender import OutgoingMessage, get_email_sender
    from mazz.integrations.google_oauth import service_for_mailbox
    from mazz.models import Mailbox, MailboxStatus

    with session_scope() as session:
        boxes = {
            m.email_address.lower(): (m.display_name, m.credential_ref)
            for m in session.execute(select(Mailbox).where(
                Mailbox.status == MailboxStatus.ACTIVE,
                Mailbox.oauth_refresh_token_enc.is_not(None),
            ).order_by(Mailbox.id)).scalars()
        }
    if not boxes:
        print("refused: no active, connected mailbox (gate 3)", file=sys.stderr)
        return 2

    sender = get_email_sender()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sent = []
    for box, seed in pairs(list(boxes), seeds):
        name, ref = boxes[box]
        receipt = sender.send(OutgoingMessage(
            from_address=box, from_name=name, to_address=seed,
            subject=f"Deliverability rehearsal {stamp}",
            body=f"Rehearsal from {box}. No action needed.",
        ), credential_ref=ref)
        print(f"sent {box} -> {seed}  {receipt.rfc_message_id}")
        sent.append((box, seed, receipt.rfc_message_id))

    deadline = time.monotonic() + args.wait
    for box, seed, mid in sent:
        if seed not in boxes:
            print(f"{box} -> {seed}: open it in {seed}, 'Show original', and check "
                  "spf=pass, dkim=pass, dmarc=pass")
            continue
        svc = service_for_mailbox(seed)
        found = None
        while found is None and time.monotonic() < deadline:
            hits = svc.users().messages().list(userId="me", q=f"rfc822msgid:{mid}").execute()
            if hits.get("messages"):
                found = gr.get_message(svc, hits["messages"][0]["id"])
            else:
                time.sleep(10)
        results = found.headers.get("authentication-results") if found else None
        print(f"{box} -> {seed}: {results or 'not delivered within --wait (check spam, re-run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
