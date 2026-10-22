"""Migration-readiness audit (DB-*): Smartlead cutover importer and adoption.

Each test asserts the CORRECT business behaviour, so a failing test is a
defect that still exists. Synthetic rows only: no real Smartlead export has
been seen, so every header and status value here is either one the importer
already assumes (`smartlead.COLUMNS` / `STATUSES`) or labelled hypothetical.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

import pipeline_helpers as h
from mazz.config import reload_settings
from mazz.ingest import smartlead as S
from mazz.ingest.csv_import import SuppressionChecker
from mazz.ingest.validate import validate_row
from mazz.models import AuditEvent, Job, JobStatus, Lead, LeadStatus, Mailbox, SentMessage, SuppressionEntry
from mazz.pipeline.sequence import due_after
from test_replies import gmsg
from test_sequence import ET, TUE, mailbox, ssm_campaign
from test_smartlead_cutover import LAST, SentSearch, lead, row


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    reload_settings()
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


def _connected_box(session):
    mailbox(session)
    box = session.execute(select(Mailbox)).scalar_one()
    box.oauth_refresh_token_enc = "encrypted-elsewhere"
    return box


# --------------------------------------------------------------------------
# State mapping: the source -> target table (printed with -s), and its invariant
# --------------------------------------------------------------------------

MAPPING_CASES = [
    # (label, status, sent, replied)
    ("not started", "not started", "", "0"),
    ("Not Started", "Not Started", "", "0"),
    ("NOT_STARTED + ws", "NOT_STARTED ", "", "0"),
    ("not started, sent 0", "NOT_STARTED", "0", "0"),
    ("NOT_YET_CONTACTED", "NOT_YET_CONTACTED", "", "0"),
    ("contacted, 1", "CONTACTED", "1", "0"),
    ("INPROGRESS 1", "INPROGRESS", "1", "0"),
    ("IN_PROGRESS 2", "IN_PROGRESS", "2", "0"),
    ("in progress, no count", "INPROGRESS", "", "0"),
    ("PAUSED 1", "PAUSED", "1", "0"),
    ("PAUSED 0", "PAUSED", "0", "0"),
    ("COMPLETED 3", "COMPLETED", "3", "0"),
    ("COMPLETED 4", "COMPLETED", "4", "0"),
    ("COMPLETED 1", "COMPLETED", "1", "0"),
    ("BLOCKED 0", "BLOCKED", "", "0"),
    ("BLOCKED 1", "BLOCKED", "1", "0"),
    ("STOPPED 1", "STOPPED", "1", "0"),
    ("BOUNCED 1", "BOUNCED", "1", "0"),
    ("UNSUBSCRIBED 1", "UNSUBSCRIBED", "1", "0"),
    ("REPLIED (status) 1", "REPLIED", "1", "0"),
    ("INPROGRESS replied 1", "INPROGRESS", "1", "1"),
    ("OUT_OF_OFFICE 1", "OUT_OF_OFFICE", "1", "0"),
    ("MANUALLY_EXCLUDED 0", "MANUALLY_EXCLUDED", "", "0"),
    ("STARTED 0", "STARTED", "", "0"),
    ("blank status", "", "", "0"),
]


def test_print_mapping_table_and_no_contacted_row_is_fresh(session):
    campaign = ssm_campaign(session)
    rows = [row(f"r{i}@m{i}-law.com", st, sent=sn, replied=rp)
            for i, (_, st, sn, rp) in enumerate(MAPPING_CASES)]
    S.import_leads(session, rows, campaign, now=TUE)
    print("\n| source | status | sent | replied | lead? | status | seq_state | step | job |")
    fresh_labels = set()
    for i, (label, st, sn, rp) in enumerate(MAPPING_CASES):
        ld = lead(session, f"r{i}@m{i}-law.com")
        job = (session.execute(select(Job.kind).where(Job.lead_id == ld.id)).scalars().all()
               if ld else [])
        if job == ["enrich"]:
            fresh_labels.add(label)
        print(f"| {label} | {st!r} | {sn!r} | {rp} | {bool(ld)} | "
              f"{ld.status.value if ld else '-'} | {ld.sequence_state if ld else '-'} | "
              f"{ld.sequence_step if ld else '-'} | {job} |")
    # Only an unambiguous not-started row with no sent count may reach Email 1.
    assert fresh_labels == {"not started", "Not Started", "NOT_STARTED + ws",
                            "not started, sent 0", "NOT_YET_CONTACTED"}


# --------------------------------------------------------------------------
# DB-01: a Smartlead status that ENDED the sequence is resumed as active
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["COMPLETED", "UNSUBSCRIBED", "BOUNCED", "REPLIED"])
def test_a_sequence_smartlead_ended_is_never_resumed(session, status):
    campaign = ssm_campaign(session)
    S.import_leads(session, [row("x@x-law.com", status, sent="1")], campaign, now=TUE)
    x = lead(session, "x@x-law.com")
    assert x.sequence_state != "active", (
        f"Smartlead status {status} with 1 send was imported as an active sequence: "
        f"adopt will queue FU1 (next_step_due_at={x.next_step_due_at})")


@pytest.mark.parametrize("extra", [{"Is Unsubscribed": "true"}, {"Is Bounced": "true"}])
def test_unsubscribed_or_bounced_flag_columns_stop_the_lead(session, extra):
    # HYPOTHETICAL header names: the point is that no flag column is read at all.
    campaign = ssm_campaign(session)
    S.import_leads(session, [{**row("x@x-law.com", "INPROGRESS", sent="1"), **extra}],
                   campaign, now=TUE)
    x = lead(session, "x@x-law.com")
    suppressed = SuppressionChecker(session).check("x@x-law.com", "x-law.com") is not None
    assert x is None or x.sequence_state != "active" or suppressed


# --------------------------------------------------------------------------
# DB-02: file A / file C header and shape
# --------------------------------------------------------------------------


def test_suppression_export_with_an_email_address_header_is_not_silently_empty(session):
    # "Email Address" is an alias the lead importer already accepts.
    report = S.import_suppression(session, [{"Email Address": "gone@u-law.com"}])
    suppressed = SuppressionChecker(session).check("gone@u-law.com", "u-law.com") is not None
    assert suppressed or S.blockers(report), (
        f"nobody suppressed and --commit would succeed: {dict(report.counts)}")


def test_an_exclusion_row_with_email_and_domain_blocks_the_whole_company(session):
    campaign = ssm_campaign(session)
    S.import_exclusions(session, [{"Email": "ceo@client-law.com", "Website": "client-law.com"}],
                        campaign.workspace)
    match = SuppressionChecker(session).check(
        "partner@client-law.com", "client-law.com", workspace_id=campaign.workspace_id)
    assert match is not None, "an existing client's colleague is still emailable"


# --------------------------------------------------------------------------
# DB-03: order dependence (leads before suppression)
# --------------------------------------------------------------------------


def test_leads_imported_before_suppression_leave_no_live_work_for_the_suppressed(session):
    campaign = ssm_campaign(session)
    S.import_leads(session, [row("unsub@u-law.com", "NOT_STARTED")], campaign, now=TUE)
    S.import_suppression(session, [{"Email": "unsub@u-law.com"}])
    x = lead(session, "unsub@u-law.com")
    pending = session.execute(select(func.count(Job.id)).where(
        Job.lead_id == x.id, Job.status == JobStatus.PENDING)).scalar_one()
    assert pending == 0, "a suppressed person still has an enrich job; Email 1 gets drafted for review"


# --------------------------------------------------------------------------
# DB-04: reconciliation and idempotent replay
# --------------------------------------------------------------------------

#: One terminal bucket per source row (the counters the CLI prints today,
#: plus a duplicate bucket the reconciliation needs).
OUTCOMES = ("awaiting_adoption", "complete", "stopped_replied", "stopped_blocked", "already",
            "conflict", "suppressed", "refused", "MUST_FIX", "new_queued", "new_invalid",
            "new_suppressed", "new_suppression_unavailable", "new_error", "duplicate",
            "held_unclear")


def test_every_source_row_has_exactly_one_reported_outcome(session):
    campaign = ssm_campaign(session)
    rows = [row("dup@d-law.com", "INPROGRESS", sent="1"),
            row("dup@d-law.com", "NOT_STARTED"),       # same person, other row
            row("new@n-law.com", "NOT_STARTED"),
            row("new@n-law.com", "NOT_STARTED")]       # exact duplicate
    report = S.import_leads(session, rows, campaign, now=TUE)
    enrich = session.execute(select(func.count(Job.id)).where(Job.kind == "enrich")).scalar_one()
    outcomes = {k: report.counts[k] for k in OUTCOMES if report.counts.get(k)}
    assert report.counts["new_queued"] == enrich, f"new_queued over-counts: {outcomes}, jobs={enrich}"
    assert sum(outcomes.values()) == report.counts["total"], outcomes


def test_replaying_the_leads_import_does_not_reset_pipeline_state(session):
    campaign = ssm_campaign(session)
    S.import_leads(session, [row("new@n-law.com", "NOT_STARTED")], campaign, now=TUE)
    x = lead(session, "new@n-law.com")
    # The pipeline has since run and a reviewer held the Email 1 draft.
    for job in session.execute(select(Job)).scalars():
        job.status = JobStatus.DONE
    x.status = LeadStatus.HELD
    session.flush()
    S.import_leads(session, [row("new@n-law.com", "NOT_STARTED")], campaign, now=TUE)
    x = lead(session, "new@n-law.com")
    pending = session.execute(select(func.count(Job.id)).where(
        Job.status == JobStatus.PENDING)).scalar_one()
    assert (x.status, pending) == (LeadStatus.HELD, 0)


def test_dry_run_cli_leaves_nothing_behind(session, tmp_path):
    campaign = ssm_campaign(session)
    session.commit()
    csv = tmp_path / "b.csv"
    header = list(row("a@a-law.com", "NOT_STARTED"))
    lines = [",".join(header)] + [",".join(str(r[k]) for k in header) for r in (
        row("a@a-law.com", "NOT_STARTED"), row("b@b-law.com", "INPROGRESS", sent="1"))]
    csv.write_text("﻿" + "\n".join(lines), encoding="utf-8")
    before = [session.execute(select(func.count(m.id))).scalar_one()
              for m in (Lead, Job, SentMessage, AuditEvent, SuppressionEntry)]
    S.main(["leads", str(csv), "--campaign", str(campaign.id)])
    session.expire_all()
    after = [session.execute(select(func.count(m.id))).scalar_one()
             for m in (Lead, Job, SentMessage, AuditEvent, SuppressionEntry)]
    assert after == before


# --------------------------------------------------------------------------
# DB-05: header aliasing can hijack the lead's email (generic validate.py)
# --------------------------------------------------------------------------


def test_an_unknown_header_with_a_known_prefix_does_not_override_the_real_email(session):
    # HYPOTHETICAL header: any "Email <something>" column carrying an address.
    raw = {"First Name": "Ann", "Last Name": "Lee",
           "Email Account": "sean.gough@getsearchsitemarketing.com",
           "Email": "ann@lee-law.com", "Website": "lee-law.com"}
    assert validate_row(raw).email == "ann@lee-law.com"


# --------------------------------------------------------------------------
# DB-06/07: adoption
# --------------------------------------------------------------------------


def test_adopt_replay_retries_a_lead_held_because_its_mailbox_was_not_yet_readable(session):
    campaign = ssm_campaign(session)
    box = _connected_box(session)
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    S.adopt(session, service_for=lambda a: SentSearch(box.email_address, {}), now=TUE)
    assert lead(session, "one@one-law.com").status is LeadStatus.HELD

    s1 = gmsg("s1", "t1", box.email_address, "Quick Question", "Hey One",
              headers={"Message-ID": "<s1@smartlead>"}, ts=LAST)
    svc = SentSearch(box.email_address, {"one@one-law.com": ["s1"]}, messages=[s1],
                     threads={"t1": [s1]})
    report = S.adopt(session, service_for=lambda a: svc, now=TUE)
    assert report.counts["adopted"] == 1, dict(report.counts)


def test_adopt_reconciles_the_step_with_what_the_thread_shows(session):
    """Export says 1 sent; the mailbox shows Smartlead sent 2 in the thread
    (export taken before the pause). The next email must be step 3, not FU1."""

    campaign = ssm_campaign(session)
    box = _connected_box(session)
    S.import_leads(session, [row("one@one-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    s1 = gmsg("s1", "t1", box.email_address, "Quick Question", "Hey One",
              headers={"Message-ID": "<s1@smartlead>"}, ts=LAST)
    s2 = gmsg("s2", "t1", box.email_address, "Re: Quick Question", "Bumping this",
              headers={"Message-ID": "<s2@smartlead>"}, ts=LAST + timedelta(days=2))
    svc = SentSearch(box.email_address, {"one@one-law.com": ["s1", "s2"]}, messages=[s1, s2],
                     threads={"t1": [s1, s2]})
    S.adopt(session, service_for=lambda a: svc, now=TUE)
    one = lead(session, "one@one-law.com")
    assert one.sequence_step == 2
    assert one.next_step_due_at == due_after(LAST + timedelta(days=2), 4, ET)


def test_overdue_followups_are_all_due_at_once_after_adoption(session):
    """Quantifies the cutover burst: N legacy leads last emailed 20 days ago
    all get run_after in the past; nothing staggers them. Passes = burst exists."""

    campaign = ssm_campaign(session)
    box = _connected_box(session)
    old = TUE - timedelta(days=20)
    n = 6
    emails = [f"l{i}@l{i}-law.com" for i in range(n)]
    S.import_leads(session, [row(e, "INPROGRESS", sent="1") for e in emails], campaign, now=TUE)
    msgs = {e: gmsg(f"s{i}", f"t{i}", box.email_address, "Quick Question", "Hi",
                    headers={"Message-ID": f"<s{i}@sl>"}, ts=old) for i, e in enumerate(emails)}
    svc = SentSearch(box.email_address, {e: [m["id"]] for e, m in msgs.items()},
                     messages=list(msgs.values()),
                     threads={m["threadId"]: [m] for m in msgs.values()})
    S.adopt(session, service_for=lambda a: svc, now=TUE)
    due = session.execute(select(Job.run_after).where(Job.kind == "followup")).scalars().all()
    assert len(due) == n and all(d <= TUE for d in due)


# --------------------------------------------------------------------------
# Regressions for the 2026-10-16 fixes
# --------------------------------------------------------------------------


def test_an_unreadable_suppression_or_exclusion_row_blocks_commit(session):
    campaign = ssm_campaign(session)
    # "Address" is no email alias: the row is someone we failed to suppress.
    assert "invalid: 1" in S.blockers(S.import_suppression(session, [{"Address": "gone@u-law.com"}]))
    assert "no_domain: 1" in S.blockers(
        S.import_exclusions(session, [{"Company": "No Domain LLP"}], campaign.workspace))


def test_an_exclusion_imported_after_the_leads_holds_the_colleague(session):
    campaign = ssm_campaign(session)
    S.import_leads(session, [row("partner@client-law.com", "NOT_STARTED")], campaign, now=TUE)
    S.import_exclusions(session, [{"Email": "ceo@client-law.com", "Website": "client-law.com"}],
                        campaign.workspace)
    x = lead(session, "partner@client-law.com")
    pending = session.execute(select(func.count(Job.id)).where(
        Job.lead_id == x.id, Job.status == JobStatus.PENDING)).scalar_one()
    assert (x.status, pending) == (LeadStatus.HELD, 0)


def _csv(tmp_path, rows):
    header = list(rows[0])
    path = tmp_path / "b.csv"
    path.write_text("\n".join([",".join(header)] + [",".join(str(r[k]) for k in header)
                                                     for r in rows]), encoding="utf-8")
    return path


def test_accept_mismatch_waives_totals_but_never_a_row_blocker(session, tmp_path):
    campaign = ssm_campaign(session)
    session.commit()
    bad = {**row("bad@b-law.com", "INPROGRESS", sent="1"), "First Name": ""}  # MUST_FIX
    argv = ["--campaign", str(campaign.id), "--commit", "--accept-mismatch"]
    assert S.main(["leads", str(_csv(tmp_path, [row("a@a-law.com", "NOT_STARTED"), bad]))] + argv) == 2
    session.expire_all()
    assert session.execute(select(func.count(Lead.id))).scalar_one() == 0
    # Totals alone (a 1-row file against the client's 864) are waivable.
    S.main(["leads", str(_csv(tmp_path, [row("a@a-law.com", "NOT_STARTED")]))] + argv)
    session.expire_all()
    assert lead(session, "a@a-law.com") is not None


def test_the_cli_refuses_a_file_with_a_duplicated_column(session, tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("Email,Email\nkeep@a-law.com,other@b-law.com\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        S.main(["suppression", str(path), "--commit"])
    assert S.duplicate_headers("Email,Notes,Notes,Email\n") == ["Email", "Notes"]


def test_two_different_emails_on_one_row_are_rejected_not_guessed(session):
    from mazz.ingest.validate import RowValidationError

    with pytest.raises(RowValidationError, match="two different email"):
        validate_row({"First Name": "A", "Last Name": "B", "Email": "a@x-law.com",
                      "Work Email": "b@y-law.com"})


def test_an_unreadable_last_sent_time_is_reported(session):
    campaign = ssm_campaign(session)
    report = S.import_leads(session, [{**row("x@x-law.com", "INPROGRESS", sent="1"),
                                       "Last Sent Time": "last Tuesday"}], campaign, now=TUE)
    assert report.counts["bad_last_sent"] == 1


def test_a_lead_in_another_workspace_is_not_moved_by_the_import(session):
    campaign = ssm_campaign(session)
    other = h.make_campaign(session)
    h.make_lead(session, email="x@x-law.com", campaign_id=other.id)
    report = S.import_leads(session, [row("x@x-law.com", "INPROGRESS", sent="1")], campaign, now=TUE)
    assert "refused: 1" in S.blockers(report)
    assert lead(session, "x@x-law.com").campaign_id == other.id


def test_a_finished_legacy_lead_is_adopted_for_its_replies_and_never_queued(session):
    campaign = ssm_campaign(session)
    box = _connected_box(session)
    S.import_leads(session, [row("done@d-law.com", "COMPLETED", sent="3"),
                             row("lost@l-law.com", "COMPLETED", sent="3")], campaign, now=TUE)
    s1 = gmsg("s1", "t1", box.email_address, "Quick Question", "Hi",
              headers={"Message-ID": "<s1@sl>"}, ts=LAST)
    svc = SentSearch(box.email_address, {"done@d-law.com": ["s1"]}, messages=[s1],
                     threads={"t1": [s1]})
    report = S.adopt(session, service_for=lambda a: svc, now=TUE)
    assert (report.counts["adopted_history"], report.counts["history_not_found"]) == (1, 1)
    done = session.execute(select(SentMessage).where(
        SentMessage.lead_id == lead(session, "done@d-law.com").id)).scalar_one()
    assert done.provider_thread_id == "t1"  # reply sync now reads this thread
    assert lead(session, "lost@l-law.com").status is LeadStatus.SENT  # nothing to hold
    assert session.execute(select(func.count(Job.id))).scalar_one() == 0


def _downgrade(revision):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from mazz.db import get_engine

    path = next((Path(__file__).resolve().parents[1] / "migrations" / "versions").glob(f"*{revision}*.py"))
    spec = importlib.util.spec_from_file_location(f"rev_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with get_engine().connect() as conn:
        trans = conn.begin()  # transactional DDL: whatever happens is rolled back
        try:
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
        finally:
            trans.rollback()


@pytest.mark.parametrize("revision", ["c0a7e8f9b1d2", "d5b2f3a4c6e7"])
def test_a_downgrade_refuses_to_delete_protected_rows(session, revision):
    from sqlalchemy.exc import DBAPIError

    from mazz.ingest.csv_import import email_sha256

    if revision == "c0a7e8f9b1d2":  # a legacy send: the Email 1 block
        S.import_leads(session, [row("x@x-law.com", "INPROGRESS", sent="1")],
                       ssm_campaign(session), now=TUE)
    else:  # an erased person's tombstone
        session.add(SuppressionEntry(email_sha256=email_sha256("gone@x-law.com"), reason="erased"))
    session.commit()
    with pytest.raises(DBAPIError, match="refusing to downgrade"):
        _downgrade(revision)
