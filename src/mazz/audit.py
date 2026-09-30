"""One call to record who changed what. Every human state change goes through it.

``actor`` is a login handle. The system acting on its own (a bounce that
suppresses, an OOO that pauses a sequence) records ``SYSTEM`` so "nobody did
this" is never confused with "we forgot to say who".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz.models import AuditEvent, User

SYSTEM = "system"


def record(
    session: Session,
    actor: str,
    action: str,
    target_type: str,
    target_id: Any = None,
    **detail: Any,
) -> AuditEvent:
    email = (actor or "").strip().lower()
    if not email:
        raise ValueError("an audit event needs an actor; use audit.SYSTEM for the system")
    user_id = (
        None
        if email == SYSTEM
        else session.execute(select(User.id).where(User.email == email)).scalar_one_or_none()
    )
    event = AuditEvent(
        user_id=user_id,
        user_email=email,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        detail={k: v for k, v in detail.items() if v is not None},
    )
    session.add(event)
    session.flush()
    return event
