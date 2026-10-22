"""Audit (SND-*): sending, sequencing and queue defects, proved against the
real Postgres. Each test asserts the CORRECT behaviour, so it fails while the
defect exists. Stub sender only; no network."""

from __future__ import annotations

import pathlib
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz.config import get_settings, reload_settings
from mazz.integrations.email_sender import StubEmailSender
from mazz.launch_copy import ensure_launch_campaign
from mazz.models import (
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    MailboxStatus,
    SentMessage,
    VerificationStatus,
    Workspace,
)
from mazz.pipeline.draft import RELEVANCE_LINE
from mazz.pipeline.mailbox import MailboxUnavailable, NoMailboxConfigured, select_mailbox
from mazz.pipeline.queue import JobDeferred, enqueue_job, process_next
from mazz.pipeline.send import send_lead
from mazz.pipeline.sequence import next_window_open

BODY = (
    "Hello.\n\n-- \nMazz Ltd, 1 Test Street, Testville\n"
    f"{RELEVANCE_LINE}\nUnsubscribe (one click): https://example.test/u/abc"
)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "Mazz Ltd, 1 Test Street, Testville")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    reload_settings()
    import mazz.pipeline.send as send_module

    monkeypatch.setattr(
        send_module, "get_email_sender",
        lambda: StubEmailSender(outbox_path=tmp_path / "outbox.jsonl"),
    )
    yield
    reload_settings()


@pytest.fixture
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s


def mailbox(session, **kw) -> Mailbox:
    d = dict(email_address="dana@outreach-northwind.com", display_name="Dana",
             credential_ref="x", daily_cap=30, min_spacing_seconds=0)
    d.update(kw)
    m = Mailbox(**d)
    session.add(m)
    session.flush()
    return m


def approved(session, **kw) -> Lead:
    lead = h.make_lead(session, **kw)
    lead.status = LeadStatus.APPROVED
    h.make_draft(session, lead, verification_status=VerificationStatus.PASSED, body=BODY)
    session.flush()
    return lead


# --------------------------------------------------------------------------
# SND-01: a send refused in the worker leaves the lead `approved`
# --------------------------------------------------------------------------


def test_snd01_suppressed_after_approval_is_held_through_the_worker(session):
    """send_lead flushes HELD then raises; the job transaction rolls it back and
    fail_job never holds an APPROVED lead. Through the real worker the lead stays
    approved and in the outbox."""

    mailbox(session)
    lead = approved(session)
    h.suppress(session, email=lead.email)
    enqueue_job(session, lead.id, "send")
    lead_id = lead.id
    session.commit()

    process_next()

    session.expire_all()
    assert session.execute(select(func.count(SentMessage.id))).scalar_one() == 0
    assert session.get(Lead, lead_id).status is LeadStatus.HELD


def test_snd01_stopped_sequence_is_held_through_the_worker(session):
    mailbox(session)
    lead = approved(session)
    lead.sequence_state = "stopped"
    enqueue_job(session, lead.id, "send")
    lead_id = lead.id
    session.commit()

    process_next()

    session.expire_all()
    assert session.get(Lead, lead_id).status is LeadStatus.HELD


# --------------------------------------------------------------------------
# SND-02: the per-company cap is only checked when Email 1 is DRAFTED
# --------------------------------------------------------------------------


def test_snd02_company_cap_holds_at_send_for_colleagues_approved_together(session):
    ws = Workspace(slug="ssm", name="SSM", **h.CONFIRMED_ADDRESS)
    session.add(ws)
    session.flush()
    campaign = ensure_launch_campaign(session, ws)
    assert campaign.max_active_per_company == 2
    mailbox(session)
    leads = [
        approved(session, email=f"{n}@damelegal.com", company_domain="damelegal.com",
                 campaign_id=campaign.id, state="NY")
        for n in "abc"
    ]
    session.commit()

    for lead in leads[:2]:
        send_lead(session, lead)
        session.commit()
    assert {l.sequence_state for l in leads[:2]} == {"active"}

    with pytest.raises(JobDeferred, match="company cap"):
        send_lead(session, leads[2])


# --------------------------------------------------------------------------
# SND-03: "daily" is the UTC day, so the cap resets inside a US business day
# --------------------------------------------------------------------------


def test_snd03_cap_is_per_business_day_not_per_utc_day(session):
    la = ZoneInfo("America/Los_Angeles")
    box = mailbox(session, daily_cap=1)
    other = approved(session, email="earlier@elsewhere.example")
    fri_noon_pt = datetime(2026, 1, 16, 20, 0, tzinfo=timezone.utc)  # Fri 12:00 PST
    session.add(SentMessage(lead_id=other.id, draft_id=None, mailbox_id=box.id,
                            from_address=box.email_address, to_address=other.email,
                            subject="s", body="b", sent_at=fri_noon_pt))
    session.flush()

    fri_1630_pt = datetime(2026, 1, 17, 0, 30, tzinfo=timezone.utc)  # Fri 16:30 PST
    assert next_window_open(fri_1630_pt, la) is None  # a California lead's window is open
    with pytest.raises(MailboxUnavailable):
        select_mailbox(session, now=fri_1630_pt)  # the mailbox already sent its 1 today


# --------------------------------------------------------------------------
# SND-04: pinning falls through to unpinned mailboxes when the pins are paused
# --------------------------------------------------------------------------


def test_snd04_paused_pinned_mailboxes_do_not_release_the_campaign_to_unpinned(session):
    campaign = h.make_campaign(session)
    mailbox(session, email_address="pinned@a.example", campaign_id=campaign.id,
            status=MailboxStatus.PAUSED)
    mailbox(session, email_address="loose@b.example")
    with pytest.raises((NoMailboxConfigured, MailboxUnavailable)):
        picked = select_mailbox(session, campaign_id=campaign.id)
        pytest.fail(f"campaign pinned to a paused mailbox was sent from {picked.email_address}")


# --------------------------------------------------------------------------
# SND-05: two workers break the daily cap (counted, not locked)
# --------------------------------------------------------------------------


def test_snd05_two_concurrent_workers_cannot_exceed_a_mailbox_cap(session, monkeypatch, tmp_path):
    import mazz.pipeline.send as send_module

    mailbox(session, daily_cap=1)
    a = approved(session, email="a@one.example", company_domain="one.example")
    b = approved(session, email="b@two.example", company_domain="two.example")
    enqueue_job(session, a.id, "send")
    enqueue_job(session, b.id, "send")
    session.commit()

    barrier = threading.Barrier(2, timeout=5)

    class Rendezvous(StubEmailSender):
        def send(self, message, *, credential_ref):
            try:  # both workers on the wire together; a fixed build reaches here once
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return super().send(message, credential_ref=credential_ref)

    monkeypatch.setattr(send_module, "get_email_sender",
                        lambda: Rendezvous(outbox_path=tmp_path / "o.jsonl"))
    workers = [threading.Thread(target=process_next) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(30)

    session.expire_all()
    assert session.execute(select(func.count(SentMessage.id))).scalar_one() <= 1


# --------------------------------------------------------------------------
# SND-06 / SND-07: preflight blind spots
# --------------------------------------------------------------------------


def test_snd06_preflight_flags_a_running_send_job(session):
    from mazz.ops import check_preflight

    lead = approved(session)
    session.add(Job(lead_id=lead.id, kind="send", status=JobStatus.RUNNING,
                    locked_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    session.flush()
    alerts = check_preflight(session)
    assert any(str(lead.id) in a.message and "send" in a.code for a in alerts), alerts


def test_snd07_preflight_flags_the_send_window_switched_off(session):
    from mazz.ops import check_preflight

    settings = get_settings().model_copy(update={"send_window": False})
    alerts = check_preflight(session, settings=settings)
    assert any("window" in (a.code + a.message).lower() for a in alerts), alerts


# --------------------------------------------------------------------------
# Hardening (passes today): a wider net than the one-send-site count test
# --------------------------------------------------------------------------


def test_no_send_job_or_direct_send_outside_the_known_sites():
    """AST, not regex: catches `"send"` literals, `Job(kind=...)`, `enqueue_next`,
    raw SQL inserts and scripts/, which the existing count test does not, and
    ignores docstrings that merely mention them."""

    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    files = [*(root / "src" / "mazz").rglob("*.py"), *(root / "scripts").rglob("*.py")]

    def is_send(node) -> bool:
        return (isinstance(node, ast.Constant) and node.value == "send") or (
            isinstance(node, (ast.Name, ast.Attribute))
            and (getattr(node, "id", None) or getattr(node, "attr", None)) == "SEND_JOB_KIND"
        )

    def name(func) -> str:
        return getattr(func, "id", None) or getattr(func, "attr", None) or ""

    creators, senders = set(), set()
    for path in files:
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(
                r"insert\s+into\s+jobs", node.value, re.I
            ):
                creators.add(rel)
            if not isinstance(node, ast.Call):
                continue
            args = [*node.args, *(k.value for k in node.keywords)]
            if name(node.func) in ("enqueue_job", "enqueue_next") and any(map(is_send, args)):
                creators.add(rel)
            if name(node.func) == "Job" and any(
                k.arg == "kind" and is_send(k.value) for k in node.keywords
            ):
                creators.add(rel)
            if name(node.func) == "send" and isinstance(node.func, ast.Attribute):
                senders.add(rel)
    assert sorted(creators) == ["src/mazz/web/routes/outbox.py"], creators
    assert sorted(senders) == [
        "scripts/rehearsal.py",
        "src/mazz/integrations/email_sender.py",
        # httpx `Client.send` of a research GET (the SSRF-safe fetcher): no mail.
        "src/mazz/integrations/safe_http.py",
        "src/mazz/pipeline/send.py",
    ], senders
