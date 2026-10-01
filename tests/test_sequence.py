"""WP3: the V1 copy and its follow-ups, the vertical gate's blocked-site
fallback, ZoomInfo names, the per-company cap and the send window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.ingest.validate import RowValidationError, validate_row
from mazz.integrations.base import ProviderBlocked
from mazz.integrations.email_sender import StubEmailSender
from mazz.launch_copy import ensure_launch_campaign
from mazz.models import (
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    Mailbox,
    ResearchCache,
    SentMessage,
    VerificationStatus,
    Workspace,
)
from mazz.pipeline.draft import split_body
from mazz.pipeline.queue import JobDeferred, PermanentJobError, enqueue_job, process_next
from mazz.pipeline.qualify import qualify_lead
from mazz.pipeline.send import send_lead
from mazz.pipeline.sequence import (
    check_company_cap,
    due_after,
    lead_timezone,
    next_window_open,
)
from mazz.pipeline.verify import latest_draft
from mazz.schemas import Fact

ET = ZoneInfo("America/New_York")
TUE = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)  # 11:00 Eastern

EMAIL_1 = (
    "Hey Louis,\n\n"
    "Quick question: is your firm currently looking to bring in more clients?\n\n"
    "We help law firms generate qualified client opportunities through search marketing.\n\n"
    "Worth sending over a little more info?\n\n"
    "Sean Gough\nSearch Site Marketing, Inc"
)
FU_1 = (
    "Hey Louis,\n\n"
    "Just following up on my last email. Worth sending over a little more info?\n\n"
    "Sean Gough\nSearch Site Marketing, Inc"
)
FU_2 = (
    "Hey Louis,\n\n"
    "This is my last note. If you want more info later, just reply and I'll send it over.\n\n"
    "Sean Gough\nSearch Site Marketing, Inc"
)


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    reload_settings()
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


class Recording(StubEmailSender):
    def __init__(self, tmp_path):
        super().__init__(outbox_path=tmp_path / "outbox.jsonl")
        self.sent = []

    def send(self, message, *, credential_ref):
        self.sent.append(message)
        return super().send(message, credential_ref=credential_ref)


def ssm_campaign(session):
    ws = Workspace(slug="search-site-marketing", name="Search Site Marketing, Inc",
                   **h.CONFIRMED_ADDRESS)
    session.add(ws)
    session.flush()
    return ensure_launch_campaign(session, ws)


def mailbox(session):
    session.add(Mailbox(email_address="sean.gough@getsearchsitemarketing.com",
                        display_name="Sean Gough", credential_ref="m1", min_spacing_seconds=0))


def law_lead(session, campaign, *, email="louis@damelegal.com", domain="damelegal.com",
             facts=True, **kw):
    if facts and session.get(ResearchCache, domain) is None:
        fact = Fact.build(
            kind="homepage",
            content="DAME Legal PLLC. Our attorneys fight for injured clients.",
            source_url=f"https://{domain}/",
            retrieved_at=h.utcnow(),
        )
        session.add(ResearchCache(company_domain=domain, facts=[fact.model_dump(mode="json")],
                                  fetched_at=h.utcnow()))
    defaults = dict(full_name="Louis Dame", first_name="Louis", last_name="Dame",
                    company_name="DAME Legal PLLC", website=f"https://{domain}",
                    company_domain=domain, campaign_id=campaign.id, state="NY",
                    status=LeadStatus.RESEARCHING)
    defaults.update(kw)
    return h.make_lead(session, email=email, **defaults)


def run_jobs(session):
    session.commit()
    while process_next() is not None:
        pass
    session.expire_all()


def drafted_and_approved(session, campaign, **kw):
    lead = law_lead(session, campaign, **kw)
    enqueue_job(session, lead.id, "draft")
    run_jobs(session)
    lead = session.get(Lead, lead.id)
    lead.status = LeadStatus.APPROVED
    session.commit()
    return lead


def test_v1_sequence_end_to_end_exact_copy_same_thread(session, tmp_path):
    campaign = ssm_campaign(session)
    assert campaign.proof_points == [] and not campaign.generic_vertical_fallback
    lead = law_lead(session, campaign)
    mailbox(session)
    enqueue_job(session, lead.id, "draft")
    run_jobs(session)

    lead = session.get(Lead, lead.id)
    d1 = latest_draft(session, lead)
    assert lead.status is LeadStatus.READY, d1.verification_notes
    assert (d1.step, d1.subject, split_body(d1.body)[0]) == (1, "Quick Question", EMAIL_1)
    assert d1.verification_status is VerificationStatus.PASSED

    sender = Recording(tmp_path)
    for step, wait, expected in ((2, 3, FU_1), (3, 4, FU_2)):
        lead.status = LeadStatus.APPROVED
        session.commit()
        sent = send_lead(session, lead, sender=sender, now=TUE)
        session.commit()
        job = session.execute(select(Job).where(
            Job.kind == "followup", Job.status == JobStatus.PENDING)).scalar_one()
        assert job.run_after == due_after(sent.sent_at, wait, ET) == lead.next_step_due_at
        job.run_after = h.utcnow() - timedelta(minutes=1)  # it is due
        run_jobs(session)
        lead = session.get(Lead, lead.id)
        d = latest_draft(session, lead)
        assert lead.status is LeadStatus.READY, d.verification_notes
        assert (d.step, d.subject, split_body(d.body)[0]) == (step, "Re: Quick Question", expected)

    lead.status = LeadStatus.APPROVED
    session.commit()
    send_lead(session, lead, sender=sender, now=TUE)
    session.commit()

    first, fu1, fu2 = session.execute(select(SentMessage).order_by(SentMessage.id)).scalars().all()
    assert first.mailbox_id == fu1.mailbox_id == fu2.mailbox_id
    assert "In-Reply-To" not in sender.sent[0].headers
    assert sender.sent[1].headers["In-Reply-To"] == first.rfc_message_id
    assert sender.sent[2].headers["References"] == f"{first.rfc_message_id} {fu1.rfc_message_id}"
    assert sender.sent[2].thread_id == fu1.provider_thread_id
    assert lead.sequence_state == "complete"
    assert session.execute(select(Job).where(
        Job.kind == "followup", Job.status == JobStatus.PENDING)).first() is None


def test_email_1_is_never_written_twice(session, tmp_path):
    from mazz.ingest.csv_import import import_rows
    from mazz.pipeline.draft import handle_draft
    from mazz.pipeline.queue import JobContext

    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)
    send_lead(session, lead, sender=Recording(tmp_path), now=TUE)
    session.commit()

    with pytest.raises(PermanentJobError, match="already been emailed"):
        handle_draft(JobContext(session=session, job=Job(lead_id=lead.id, kind="draft"),
                                lead=lead, handlers={}))

    # A re-import of someone mid-sequence refreshes details and queues nothing.
    lead.status = LeadStatus.READY  # e.g. a follow-up sitting in review
    session.commit()
    report = import_rows(session, [{"first_name": "Louis", "last_name": "Dame",
                                    "email": lead.email, "website": "damelegal.com"}])
    assert [o.job_id for o in report.accepted] == [None]


def test_blocked_site_falls_back_to_legal_industry_and_law_firm_name(session, monkeypatch):
    campaign = ssm_campaign(session)
    ok = law_lead(session, campaign, facts=False, email="a@smithlaw.com", domain="smithlaw.com",
                  company_name="Smith Law Group PLLC", industry_primary="Legal Services")
    wrong_name = law_lead(session, campaign, facts=False, email="b@acme.com", domain="acme.com",
                          company_name="Acme Holdings", industry_primary="Legal Services")
    wrong_industry = law_lead(session, campaign, facts=False, email="c@jones.com",
                              domain="jones.com", company_name="Jones Law",
                              industry_primary="Retail")

    result = qualify_lead(session, ok)
    assert result.qualified and result.cited_ids == ["lead_record"]
    assert not qualify_lead(session, wrong_name).qualified
    assert not qualify_lead(session, wrong_industry).qualified

    # Enrich meets a blocked site: recorded, not retried, handed to qualify.
    import mazz.pipeline.enrich as enrich

    def blocked(*a, **k):
        raise ProviderBlocked("robots.txt disallows", provider="company_site", target="x")

    monkeypatch.setattr(enrich, "enrich_lead", blocked)
    for lead in (ok, wrong_name):
        enqueue_job(session, lead.id, "enrich")
    run_jobs(session)

    ok, wrong_name = session.get(Lead, ok.id), session.get(Lead, wrong_name.id)
    draft = latest_draft(session, ok)
    assert ok.status is LeadStatus.READY, draft and draft.verification_notes
    assert split_body(draft.body)[0].startswith("Hey Louis,")
    assert (wrong_name.status, wrong_name.skip_reason) == (
        LeadStatus.NO_ANGLE, "vertical_unconfirmed")
    assert session.get(ResearchCache, "acme.com").last_error.startswith("ProviderBlocked")


def test_company_cap_holds_back_a_third_colleague(session):
    campaign = ssm_campaign(session)
    a, b, c = (law_lead(session, campaign, email=f"{n}@damelegal.com") for n in "abc")
    a.sequence_state = b.sequence_state = "active"
    a.status = b.status = LeadStatus.SENT
    session.flush()
    with pytest.raises(JobDeferred, match="company cap"):
        check_company_cap(session, c, now=TUE)
    b.status = LeadStatus.HELD  # rejected in review: frees the slot
    session.flush()
    check_company_cap(session, c, now=TUE)


def test_due_dates_roll_to_business_days_and_the_window_is_local():
    fri = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    assert due_after(fri, 3, ET).astimezone(ET).date().isoformat() == "2026-10-12"  # Mon
    assert due_after(fri, 1, ET).astimezone(ET).date().isoformat() == "2026-10-12"  # Sat -> Mon
    assert due_after(fri, 4, ET).astimezone(ET).date().isoformat() == "2026-10-13"

    assert next_window_open(TUE, ET) is None
    sat = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    assert next_window_open(sat, ET) == datetime(2026, 10, 12, 9, 0, tzinfo=ET)
    pt = ZoneInfo("America/Los_Angeles")
    early = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)  # 07:00 Pacific
    assert next_window_open(early, pt) == datetime(2026, 10, 6, 9, 0, tzinfo=pt)
    late = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)  # 18:00 Eastern
    assert next_window_open(late, ET) == datetime(2026, 10, 7, 9, 0, tzinfo=ET)

    assert lead_timezone(Lead(location="Austin, TX, United States")).key == "America/Chicago"
    assert lead_timezone(Lead(state="california")).key == "America/Los_Angeles"
    assert lead_timezone(Lead(location="London, United Kingdom")).key == "America/New_York"


def test_a_send_outside_the_window_is_deferred_not_failed(session, monkeypatch, tmp_path):
    monkeypatch.setenv("SEND_WINDOW", "true")
    reload_settings()
    campaign = ssm_campaign(session)
    mailbox(session)
    lead = drafted_and_approved(session, campaign)
    sat = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(JobDeferred) as exc:
        send_lead(session, lead, sender=Recording(tmp_path), now=sat)
    assert exc.value.retry_at == datetime(2026, 10, 12, 9, 0, tzinfo=ET)
    assert session.execute(select(SentMessage)).first() is None


def test_zoominfo_rows_need_first_last_and_email():
    row = validate_row({"First Name": "Mary Ann", "Last Name": "Smith",
                        "Email Address": " MARY@SmithLaw.com ", "Person State": "TX",
                        "Company Name": "Smith Law"})
    assert (row.lead.first_name, row.lead.last_name, row.lead.full_name, row.lead.state) == (
        "Mary Ann", "Smith", "Mary Ann Smith", "TX")
    assert row.lead.email == "mary@smithlaw.com"
    assert validate_row({"Full Name": "Jane Doe", "Email": "j@x.com"}).lead.first_name == "Jane"
    with pytest.raises(RowValidationError, match="last_name"):
        validate_row({"First Name": "Cher", "Email": "cher@x.com"})
    with pytest.raises(RowValidationError, match="email"):
        validate_row({"First Name": "A", "Last Name": "B"})
