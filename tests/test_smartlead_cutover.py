"""WP7: cutover from Smartlead. Synthetic exports (no real one has been seen;
the assumed headers are `smartlead.COLUMNS`); Gmail is faked."""

from __future__ import annotations

import re
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.ingest import smartlead as S
from mazz.models import (
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    Reply,
    ResearchCache,
    SentMessage,
    SuppressionEntry,
)
from mazz.pipeline import replies as R
from mazz.pipeline.draft import handle_draft
from mazz.pipeline.queue import JobContext, PermanentJobError, enqueue_job
from mazz.pipeline.send import send_lead
from mazz.pipeline.sequence import due_after
from mazz.schemas import Fact
from test_replies import Call, FakeGmail, gmsg
from test_sequence import ET, TUE, Recording, mailbox, run_jobs, ssm_campaign

LAST = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)  # a Thursday


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    reload_settings()
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


def row(email, status, sent="", replied="0"):
    first, domain = email.split("@")
    return {"First Name": first.title(), "Last Name": "Lawyer", "Email": email,
            "Company Name": f"{first.title()} Law PLLC", "Website": domain,
            "Status": status, "Last Email Sequence Sent": sent, "Reply Count": replied,
            "Last Sent Time": LAST.isoformat()}


def researched(session, domain):
    fact = Fact.build(kind="homepage", content="Law PLLC. Our attorneys fight for injured clients.",
                      source_url=f"https://{domain}/", retrieved_at=h.utcnow())
    session.add(ResearchCache(company_domain=domain, facts=[fact.model_dump(mode="json")],
                              fetched_at=h.utcnow()))
    session.flush()


def lead(session, email):
    return session.execute(select(Lead).where(Lead.email == email)).scalar_one_or_none()


EXPORT = [
    row("new@fresh-law.com", "NOT_STARTED"),
    row("one@one-law.com", "INPROGRESS", sent="1"),
    row("two@two-law.com", "INPROGRESS", sent="2"),
    row("three@three-law.com", "COMPLETED", sent="3"),
    row("replied@r-law.com", "INPROGRESS", sent="1", replied="1"),
    row("blocked@b-law.com", "BLOCKED", sent="1"),
    row("unsub@u-law.com", "INPROGRESS", sent="1"),
    row("unsubnew@u2-law.com", "NOT_STARTED"),
    row("mystery@m-law.com", "WEIRD"),
]


def test_import_maps_state_and_no_legacy_lead_can_reach_email_1(session):
    campaign = ssm_campaign(session)
    S.import_suppression(session, [{"Email": "unsub@u-law.com"}, {"Email": "unsubnew@u2-law.com"}])
    report = S.import_leads(session, EXPORT, campaign, now=TUE)

    assert {k: report.counts[k] for k in ("total", "contacted", "not_started", "in_progress",
                                          "completed", "blocked", "unknown_status", "suppressed",
                                          "refused", "new_suppressed")} == {
        "total": 9, "contacted": 7, "not_started": 2, "in_progress": 4, "completed": 1,
        "blocked": 1, "unknown_status": 1, "suppressed": 1, "refused": 1, "new_suppressed": 1}
    assert set(session.execute(select(Lead.email)).scalars()) == {
        "new@fresh-law.com", "one@one-law.com", "two@two-law.com", "three@three-law.com",
        "replied@r-law.com", "blocked@b-law.com"}

    # Only the not-started lead is queued; no legacy lead has a job.
    jobs = session.execute(select(Job.lead_id, Job.kind)).all()
    assert jobs == [(lead(session, "new@fresh-law.com").id, "enrich")]

    one, two = lead(session, "one@one-law.com"), lead(session, "two@two-law.com")
    assert (one.sequence_state, one.sequence_step, one.status) == ("active", 1, LeadStatus.SENT)
    assert one.next_step_due_at == due_after(LAST, 3, ET)  # FU1 at +3
    assert (two.sequence_state, two.sequence_step) == ("active", 2)
    assert two.next_step_due_at == due_after(LAST, 4, ET)  # FU2 at +4
    assert lead(session, "three@three-law.com").sequence_state == "complete"
    assert lead(session, "replied@r-law.com").sequence_state == "stopped"
    assert lead(session, "blocked@b-law.com").sequence_state == "stopped"

    # Zero legacy-contacted leads can reach Email 1, whatever queues a draft.
    for email in ("one@one-law.com", "two@two-law.com", "three@three-law.com",
                  "replied@r-law.com", "blocked@b-law.com"):
        legacy = lead(session, email)
        with pytest.raises(PermanentJobError, match="already been emailed"):
            handle_draft(JobContext(session=session, job=Job(lead_id=legacy.id, kind="draft"),
                                    lead=legacy, handlers={}))

    # Re-running is idempotent: same leads, same sent rows, same jobs.
    counts = lambda: (session.execute(select(func.count(Lead.id))).scalar_one(),  # noqa: E731
                      session.execute(select(func.count(SentMessage.id))).scalar_one(),
                      session.execute(select(func.count(Job.id))).scalar_one(),
                      session.execute(select(func.count(SuppressionEntry.id))).scalar_one())
    before = counts()
    again = S.import_leads(session, EXPORT, campaign, now=TUE)
    S.import_suppression(session, [{"Email": "unsub@u-law.com"}])
    # 5 legacy + the not-started lead, whose replay no longer re-admits it.
    assert counts() == before and again.counts["already"] == 6


def test_exclusions_are_scoped_to_the_workspace(session):
    campaign = ssm_campaign(session)
    report = S.import_exclusions(session, [{"Domain": "client-law.com"}, {"Company": "No Domain LLP"}],
                                 campaign.workspace)
    assert (report.counts["added"], report.counts["no_domain"]) == (1, 1)
    scoped = select(SuppressionEntry).where(SuppressionEntry.workspace_id.is_not(None))
    entry = session.execute(scoped).scalar_one()  # the seed's own domains are global
    assert (entry.domain, entry.workspace_id) == ("client-law.com", campaign.workspace_id)
    S.import_exclusions(session, [{"Domain": "client-law.com"}], campaign.workspace)
    assert len(session.execute(scoped).all()) == 1


def test_a_legacy_followup_with_no_adopted_thread_is_held_not_sent(session, tmp_path):
    campaign = ssm_campaign(session)
    mailbox(session)
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    researched(session, "one-law.com")
    one = lead(session, "one@one-law.com")
    # Whatever queued it (adoption never does for an unadopted lead), the
    # follow-up may be written and approved, but it may not leave.
    enqueue_job(session, one.id, "followup", run_after=h.utcnow() - timedelta(minutes=1))
    run_jobs(session)
    one = lead(session, "one@one-law.com")
    assert one.status is LeadStatus.READY
    one.status = LeadStatus.APPROVED
    session.commit()

    sender = Recording(tmp_path)
    with pytest.raises(PermanentJobError, match="no Gmail thread"):
        send_lead(session, one, sender=sender, now=TUE)
    session.expire_all()
    assert lead(session, "one@one-law.com").status is LeadStatus.HELD and sender.sent == []


class SentSearch(FakeGmail):
    """FakeGmail plus `messages.list(q='in:sent to:<addr> ...')`."""

    def __init__(self, address, by_recipient, **kw):
        super().__init__(address, **kw)
        self.by_recipient = by_recipient

    def messages(self):
        def _list(userId, q, maxResults):
            to = re.search(r"to:(\S+)", q).group(1)
            return Call({"messages": [{"id": i} for i in self.by_recipient.get(to, [])]})

        return types.SimpleNamespace(get=lambda userId, id, format: Call(self.msgs[id]), list=_list)


def test_adoption_threads_followups_holds_the_lost_and_sees_replies(session, tmp_path):
    campaign = ssm_campaign(session)
    mailbox(session)
    box = session.execute(select(Mailbox)).scalar_one()
    box.oauth_refresh_token_enc = "encrypted-elsewhere"  # adopt is handed a service
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1"),
                             row("lost@lost-law.com", "INPROGRESS", sent="1"),
                             row("early@e-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    researched(session, "one-law.com")

    s1 = gmsg("s1", "t1", box.email_address, "Quick Question", "Hey One",
              headers={"Message-ID": "<s1@smartlead>"}, ts=LAST)
    s2 = gmsg("s2", "t2", box.email_address, "Quick Question", "Hey Early",
              headers={"Message-ID": "<s2@smartlead>"}, ts=LAST)
    r2 = gmsg("r2", "t2", "early@e-law.com", "Re: Quick Question", "Tell me more",
              ts=LAST + timedelta(days=1))
    svc = SentSearch(box.email_address, {"one@one-law.com": ["s1"], "early@e-law.com": ["s2"]},
                     messages=[s1, s2], threads={"t1": [s1], "t2": [s2, r2]})

    report = S.adopt(session, service_for=lambda address: svc, now=TUE)
    assert (report.counts["adopted"], report.counts["legacy_thread_not_found"],
            report.counts["adopted_replied"]) == (1, 1, 1)

    lost = lead(session, "lost@lost-law.com")
    assert (lost.status, lost.skip_reason) == (LeadStatus.HELD, "legacy_thread_not_found")
    early = lead(session, "early@e-law.com")
    assert early.sequence_state == "stopped"
    assert session.execute(select(Reply.body).where(Reply.lead_id == early.id)).scalar_one() == "Tell me more"

    one = lead(session, "one@one-law.com")
    adopted = session.execute(select(SentMessage).where(SentMessage.lead_id == one.id)).scalar_one()
    assert (adopted.provider_thread_id, adopted.rfc_message_id, adopted.mailbox_id, adopted.legacy) == (
        "t1", "<s1@smartlead>", box.id, True)
    job = session.execute(select(Job).where(Job.kind == "followup")).scalar_one()
    assert job.lead_id == one.id and job.run_after == due_after(LAST, 3, ET)
    # Only one live lead was adopted, so only one follow-up is queued.
    assert session.execute(select(func.count(Job.id)).where(
        Job.status == JobStatus.PENDING)).scalar_one() == 1

    # FU1 threads under the Smartlead message, from the same mailbox.
    job.run_after = h.utcnow() - timedelta(minutes=1)
    run_jobs(session)
    one = lead(session, "one@one-law.com")
    assert one.status is LeadStatus.READY
    one.status = LeadStatus.APPROVED
    session.commit()
    sender = Recording(tmp_path)
    sent = send_lead(session, one, sender=sender, now=TUE)
    session.commit()
    msg = sender.sent[0]
    assert (msg.subject, msg.thread_id, msg.headers["In-Reply-To"]) == (
        "Re: Quick Question", "t1", "<s1@smartlead>")
    assert sent.mailbox_id == box.id

    # A reply on the adopted thread is stored and stops the sequence.
    reply = gmsg("r1", "t1", "one@one-law.com", "Re: Quick Question", "Sure, send it",
                 ts=TUE + timedelta(hours=1))
    from mazz.integrations.gmail_reader import parse_gmail_message

    R.process_message(session, box, parse_gmail_message(reply), R.SyncReport())
    session.flush()
    one = lead(session, "one@one-law.com")
    assert one.sequence_state == "stopped"
    assert session.execute(select(Reply.lead_id).where(Reply.provider_message_id == "r1")).scalar_one() == one.id


def test_an_unresearched_legacy_followup_is_researched_and_fails_closed(session):
    campaign = ssm_campaign(session)
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    one = lead(session, "one@one-law.com")
    enqueue_job(session, one.id, "followup", run_after=h.utcnow() - timedelta(minutes=1))
    run_jobs(session)
    # The stub has no pages for this domain: researched, unconfirmed, stopped.
    assert session.get(ResearchCache, "one-law.com") is not None
    one = lead(session, "one@one-law.com")
    assert one.sequence_state == "stopped" and one.skip_reason.startswith("followup_")


def test_ambiguous_status_or_missing_headers_never_look_fresh(session):
    campaign = ssm_campaign(session)
    report = S.import_leads(session, [row("started@s-law.com", "STARTED"),
                                      row("counted@c-law.com", "STARTED", sent="1")], campaign, now=TUE)
    # "Started" with no count is refused; with a count it is legacy, held and
    # never resumed (an unknown status cannot say the sequence may go on).
    # Neither is queued.
    assert lead(session, "started@s-law.com") is None
    counted = lead(session, "counted@c-law.com")
    assert (counted.status, counted.sequence_state) == (LeadStatus.HELD, "stopped")
    assert session.execute(select(func.count(Job.id))).scalar_one() == 0
    assert S.blockers(report) == ["refused: 1", "unknown_status: 2", "held_unclear: 1"]

    no_count = [{k: v for k, v in row("x@x-law.com", "NOT_STARTED").items()
                 if k != "Last Email Sequence Sent"}]
    with pytest.raises(ValueError, match="emails_sent"):
        S.import_leads(session, no_count, campaign, now=TUE)


def test_the_cli_refuses_to_commit_a_mismatch(session):
    report = S.Report()
    report.note("total")
    assert "MISMATCH total: got 1, the client's figure is 864" in S.blockers(report, S.EXPECTED)
    report = S.Report()
    report.note("MUST_FIX", "row 2: CONTACTED but not importable")
    assert S.blockers(report) == ["MUST_FIX: 1"]


def test_a_workspace_block_keeps_a_contacted_lead_out(session):
    campaign = ssm_campaign(session)
    S.import_exclusions(session, [{"Domain": "one-law.com"}], campaign.workspace)
    report = S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    assert report.counts["suppressed"] == 1 and lead(session, "one@one-law.com") is None


def test_suppressed_after_import_is_stopped_at_adoption_not_queued(session):
    campaign = ssm_campaign(session)
    mailbox(session)
    box = session.execute(select(Mailbox)).scalar_one()
    box.oauth_refresh_token_enc = "encrypted-elsewhere"
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    S.import_suppression(session, [{"Email": "one@one-law.com"}])
    svc = SentSearch(box.email_address, {})
    report = S.adopt(session, service_for=lambda address: svc, now=TUE)
    assert report.counts["suppressed"] == 1
    assert lead(session, "one@one-law.com").sequence_state == "stopped"
    assert session.execute(select(func.count(Job.id))).scalar_one() == 0


def test_an_old_email_1_draft_cannot_be_sent_once_smartlead_says_contacted(session, tmp_path):
    from test_sequence import drafted_and_approved

    campaign = ssm_campaign(session)
    mailbox(session)
    louis = drafted_and_approved(session, campaign)  # approved Email 1, never sent
    S.import_leads(session, [row(louis.email, "INPROGRESS", sent="1")], campaign, now=TUE)
    louis = lead(session, louis.email)
    louis.status = LeadStatus.APPROVED  # e.g. held, then re-approved by hand
    session.commit()
    sender = Recording(tmp_path)
    with pytest.raises(PermanentJobError, match="already been emailed"):
        send_lead(session, louis, sender=sender, now=TUE)
    session.expire_all()
    assert lead(session, louis.email).status is LeadStatus.HELD and sender.sent == []


def test_legacy_sends_are_not_counted_as_ours(session):
    from mazz.metrics import month_metrics

    campaign = ssm_campaign(session)
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    assert month_metrics(session, LAST.date().replace(day=1)).sent == 0
