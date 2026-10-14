"""WP6: cost entries and cost per qualified conversation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import pipeline_helpers as h
from mazz import metrics as M
from mazz.models import AuditEvent, CostEntry, Reply, ReplyTag, SentMessage
from mazz.web.app import create_app
from mazz.web.routes.costs import parse_amount
from test_replies import env  # noqa: F401  (autouse settings fixture)
from test_sequence import law_lead, ssm_campaign
from test_web import ensure_account, login

OCT = date(2026, 10, 1)
IN_OCT = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
IN_SEP = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    h.reset_db()
    with h.db_session() as s:
        yield s
    h.reset_db()


def cost(session, ws, category, amount, month=OCT):
    session.add(CostEntry(workspace_id=ws, month=month, category=category,
                          amount=Decimal(amount), entered_by="maaz@example.test"))


def sent(session, lead, at, *, bounced=None, n=1):
    for _ in range(n):
        d = h.make_draft(session, lead)
        session.add(SentMessage(lead_id=lead.id, draft_id=d.id, from_address="s@x.com",
                                to_address=lead.email, subject="s", body="b", sent_at=at,
                                bounced_at=bounced))


def reply(session, lead, at, *, mid, cls="needs_review", tags=()):
    r = Reply(lead_id=lead.id, provider_message_id=mid, from_address=lead.email, subject="Re",
              body="x", received_at=at, classification=cls)
    session.add(r)
    session.flush()
    for tag, when in tags:
        session.add(ReplyTag(reply_id=r.id, tag=tag, tagged_by="maaz@example.test", tagged_at=when))
    return r


def test_cost_per_qualified_conversation_counts_by_the_date_tagged(session):
    campaign = ssm_campaign(session)
    ws = campaign.workspace_id
    a, b, c = (law_lead(session, campaign, email=f"{n}@firm{n}.com", domain=f"firm{n}.com")
               for n in "abc")
    for lead in (a, b, c):
        sent(session, lead, IN_OCT, n=2)
    sent(session, c, IN_OCT, bounced=IN_OCT)
    # a: replied in Sept, tagged qualified in Oct -> counts in October
    reply(session, a, IN_SEP, mid="a", tags=[("interested", IN_OCT), ("qualified", IN_OCT),
                                             ("meeting_booked", IN_OCT)])
    # b: replied in Oct, tagged qualified in Nov -> not October
    reply(session, b, IN_OCT, mid="b", tags=[("qualified", IN_OCT + timedelta(days=30))])
    # c: out-of-office is not a response
    reply(session, c, IN_OCT, mid="c", cls="ooo")
    cost(session, ws, "mailboxes", "100.00")
    cost(session, ws, "smartlead", "50.50")
    session.commit()

    m = M.month_metrics(session, OCT)
    assert (m.sent, m.contacted, m.bounces) == (7, 3, 1)
    assert m.unique_replies == 1  # b only: a's reply was in September, c's is an OOO
    assert m.stages == {"interested": 1, "qualified": 1, "meeting_booked": 1,
                        "meeting_showed": 0, "client_won": 0}
    assert m.cost_total == Decimal("150.50")
    assert m.cost_per_qualified == Decimal("150.50")
    assert m.cost_per_contacted == Decimal("150.50") / 3
    assert round(m.bounce_rate, 4) == round(1 / 7, 4)
    assert M.month_metrics(session, date(2026, 11, 1)).qualified == 1  # b, by tag date


def test_no_denominator_or_no_costs_is_undefined_never_zero(session):
    campaign = ssm_campaign(session)
    lead = law_lead(session, campaign)
    sent(session, lead, IN_OCT)
    session.commit()
    m = M.month_metrics(session, OCT)
    assert m.cost_per_qualified is None and m.cost_per_contacted is None  # no costs entered
    cost(session, campaign.workspace_id, "hosting", "20")
    session.commit()
    m = M.month_metrics(session, OCT)
    assert m.cost_per_qualified is None  # costs but nobody qualified: not "$0"
    assert m.cost_per_contacted == Decimal("20")
    assert M.month_metrics(session, date(2026, 8, 1)).reply_rate is None


def test_metrics_scope_to_one_workspace(session):
    mine = ssm_campaign(session)
    other = h.make_campaign(session, name="Other")
    theirs = h.make_lead(session, email="z@else.com", company_domain="else.com",
                         campaign_id=other.id)
    sent(session, law_lead(session, mine), IN_OCT)
    sent(session, theirs, IN_OCT, n=3)
    cost(session, mine.workspace_id, "hosting", "10")
    cost(session, other.workspace_id, "hosting", "90")
    session.commit()
    assert M.month_metrics(session, OCT).sent == 4
    only = M.month_metrics(session, OCT, workspace_id=mine.workspace_id)
    assert (only.sent, only.cost_total) == (1, Decimal("10"))


def test_month_helpers():
    assert M.parse_month("2026-10") == OCT
    assert M.parse_month("garbage", default=date(2026, 3, 9)) == date(2026, 3, 1)
    start, end = M.bounds(date(2026, 12, 1))
    assert (start.month, end.year, end.month) == (12, 2027, 1)


@pytest.mark.parametrize("raw,expected", [("70", "70.00"), ("$1,234.5", "1234.50"), ("0.005", "0.01")])
def test_amounts_parse_to_cents(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "abc", "-5", "NaN", "Infinity", "99999999"])
def test_bad_amounts_are_refused(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_the_database_refuses_negative_amounts_and_mid_month_dates(session):
    ws = ssm_campaign(session).workspace_id
    session.commit()
    for amount, month in ((Decimal("-1"), OCT), (Decimal("1"), date(2026, 10, 9))):
        session.add(CostEntry(workspace_id=ws, month=month, category="other", amount=amount,
                              entered_by="x"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_costs_screen_adds_attributes_audits_and_deletes(session):
    campaign = ssm_campaign(session)
    session.commit()
    ensure_account("maaz@example.test")
    ensure_account("sean@example.test", operator=False, workspace_ids=(campaign.workspace_id,))
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")

    def post(**kw):
        data = {"month": "2026-10", "workspace_id": campaign.workspace_id,
                "category": "smartlead", "amount": "97", **kw}
        return maaz.post("/costs", data=data, follow_redirects=False)

    assert post().status_code == 303
    assert post(amount="-3").status_code == 400
    assert post(category="lunch").status_code == 400
    assert post(workspace_id=999999).status_code == 400
    session.expire_all()
    entry = session.execute(select(CostEntry)).scalar_one()
    assert (entry.amount, entry.entered_by, entry.month) == (
        Decimal("97.00"), "maaz@example.test", OCT)
    assert "Smartlead subscription" in maaz.get("/costs?month=2026-10").text

    maaz.post(f"/costs/{entry.id}/delete")
    session.expire_all()
    assert session.execute(select(CostEntry)).first() is None
    actions = [a for (a,) in session.execute(select(AuditEvent.action).order_by(AuditEvent.id)).all()]
    assert actions == ["cost_add", "cost_delete"]

    sean = login(TestClient(create_app(), base_url="https://testserver"), "sean@example.test")
    assert sean.get("/costs").status_code == 403
    assert sean.post("/costs", data={"month": "2026-10", "workspace_id": campaign.workspace_id,
                                     "category": "other", "amount": "1"}).status_code == 403


def test_dashboard_leads_with_the_primary_metric_and_the_baseline(session):
    campaign = ssm_campaign(session)
    lead = law_lead(session, campaign)
    sent(session, lead, IN_OCT)
    reply(session, lead, IN_OCT, mid="q", tags=[("qualified", IN_OCT)])
    cost(session, campaign.workspace_id, "mailboxes", "200")
    session.commit()
    ensure_account("maaz@example.test")
    maaz = login(TestClient(create_app(), base_url="https://testserver"), "maaz@example.test")
    page = maaz.get("/dashboard?month=2026-10&workspace=").text  # "" = all workspaces
    assert page.index("Cost per qualified conversation") < page.index("Hold rate")
    assert "$200.00" in page and "223" in page and "0.5%" in page and "4.9%" in page
    assert maaz.get("/dashboard?month=nonsense").status_code == 200
    assert "no costs entered for this month" in maaz.get("/dashboard?month=2020-01").text
