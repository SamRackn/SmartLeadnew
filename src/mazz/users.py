"""Accounts: create, set a password, grant a workspace, list.

    python -m mazz.users seed-launch                  # Maaz + Sean (no passwords), V1 campaign
    python -m mazz.users set-password maaz@clientrise.io   # prompts, never echoes
    python -m mazz.users add someone@x.com "Some One" [--operator]
    python -m mazz.users grant someone@x.com <workspace-slug>
    python -m mazz.users list

Passwords are never taken on the command line (they would land in shell
history) and never invented: a seeded account cannot log in until its owner
sets one.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from mazz.db import session_scope
from mazz.models import User, UserWorkspace, Workspace
from mazz.web.auth import hash_password

#: The two launch accounts (client decision 2026-09-29). Sean is scoped to his
#: own workspace; Maaz runs outreach for every workspace.
LAUNCH_WORKSPACE_SLUG = "search-site-marketing"
LAUNCH_WORKSPACE_NAME = "Search Site Marketing, Inc"
LAUNCH_USERS = (
    ("maaz@clientrise.io", "Maaz Nadeem", True, ()),
    ("sean@searchsitemarketing.com", "Sean Gough", False, (LAUNCH_WORKSPACE_SLUG,)),
)

MIN_PASSWORD_LENGTH = 12


def ensure_user(
    session: Session, email: str, display_name: str, *, operator: bool = False
) -> User:
    email = email.strip().lower()
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=display_name, all_workspaces=operator)
        session.add(user)
        session.flush()
    return user


def grant(session: Session, user: User, workspace: Workspace) -> None:
    if session.get(UserWorkspace, (user.id, workspace.id)) is None:
        session.add(UserWorkspace(user_id=user.id, workspace_id=workspace.id))
        session.flush()


def ensure_workspace(session: Session, slug: str, name: str) -> Workspace:
    ws = session.execute(select(Workspace).where(Workspace.slug == slug)).scalar_one_or_none()
    if ws is None:
        ws = Workspace(slug=slug, name=name)
        session.add(ws)
        session.flush()
    return ws


def seed_launch(session: Session) -> list[User]:
    from mazz.launch_copy import ensure_launch_campaign, ensure_launch_mailboxes

    ws = ensure_workspace(session, LAUNCH_WORKSPACE_SLUG, LAUNCH_WORKSPACE_NAME)
    ensure_launch_mailboxes(session, ensure_launch_campaign(session, ws))
    users = []
    for email, name, operator, slugs in LAUNCH_USERS:
        user = ensure_user(session, email, name, operator=operator)
        for slug in slugs:
            ws = session.execute(select(Workspace).where(Workspace.slug == slug)).scalar_one()
            grant(session, user, ws)
        users.append(user)
    return users


def set_password(session: Session, email: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    user = session.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None:
        raise LookupError(f"no account {email!r}; create it first")
    user.password_hash = hash_password(password)
    session.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mazz.users")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-launch")
    sub.add_parser("list")
    p = sub.add_parser("set-password")
    p.add_argument("email")
    p = sub.add_parser("add")
    p.add_argument("email")
    p.add_argument("display_name")
    p.add_argument("--operator", action="store_true")
    p = sub.add_parser("grant")
    p.add_argument("email")
    p.add_argument("workspace_slug")
    args = parser.parse_args(argv)

    with session_scope() as session:
        if args.cmd == "seed-launch":
            for u in seed_launch(session):
                state = "password set" if u.password_hash else "NO PASSWORD — run set-password"
                print(f"{u.email}: {state}")
        elif args.cmd == "list":
            for u in session.execute(select(User).order_by(User.email)).scalars():
                ws = session.execute(
                    select(Workspace.slug)
                    .join(UserWorkspace, UserWorkspace.workspace_id == Workspace.id)
                    .where(UserWorkspace.user_id == u.id)
                ).scalars().all()
                scope = "ALL workspaces" if u.all_workspaces else ", ".join(ws) or "(none)"
                print(f"{u.email}\t{u.display_name}\t{scope}\tactive={u.active}\tpassword={'yes' if u.password_hash else 'no'}")
        elif args.cmd == "set-password":
            first = getpass.getpass("new password: ")
            if first != getpass.getpass("again: "):
                print("passwords differ", file=sys.stderr)
                return 1
            set_password(session, args.email, first)
            print("password set")
        elif args.cmd == "add":
            ensure_user(session, args.email, args.display_name, operator=args.operator)
            print("added")
        elif args.cmd == "grant":
            user = session.execute(
                select(User).where(User.email == args.email.lower())
            ).scalar_one()
            ws = session.execute(
                select(Workspace).where(Workspace.slug == args.workspace_slug)
            ).scalar_one()
            grant(session, user, ws)
            print("granted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
