"""Migration-readiness audit of the web layer (WEB- findings).

Every test asserts the CORRECT behaviour, so a test that fails here is a
confirmed defect, and the test name says which one. Tests that pass are kept
as regression evidence for the controls that do hold (isolation matrix,
operator-only routes, cookie flags). Stubs only: no network, no real sender.

Run:  runtests.sh mazz_audit_web_test -- -q -p no:cacheprovider tests/test_audit_web.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import pipeline_helpers as h
from mazz import metrics as M
from mazz.config import reload_settings
from mazz.models import (
    AuditEvent,
    Base,
    CostEntry,
    Job,
    JobStatus,
    Lead,
    LeadStatus,
    ProofPoint,
    Reply,
    ReplyTag,
    SentMessage,
    SuppressionEntry,
    VerificationStatus,
)
from mazz.pipeline.queue import process_next
from mazz.schemas import Fact
from mazz.web import auth as web_auth
from mazz.web.app import app, create_app
from mazz.web.routes import auth as auth_routes
from test_web import (  # noqa: F401  (env is an autouse fixture)
    JOB_FACT,
    TEST_PASSWORD,
    _two_workspace_leads,
    ensure_account,
    env,
    login,
    make_mailbox,
    make_passed_lead,
    seed_research,
    send_jobs_for,
    session,
)

SESSION_COOKIE = "mazz_review_session"
OCT = date(2026, 10, 1)
IN_OCT = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
IN_SEP = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_throttle():
    auth_routes._failures.clear()
    yield
    auth_routes._failures.clear()


def as_user(handle: str, *, operator: bool, ws: tuple[int, ...] = (), **kw) -> TestClient:
    ensure_account(handle, operator=operator, workspace_ids=ws)
    c = TestClient(app, **kw)
    login(c, handle)
    return c


def operator(**kw) -> TestClient:
    return as_user("client", operator=True, **kw)


def add_reply(session, lead, *, mid="r1", body="Tell me more", cls="needs_review", at=None, subject="Re: x"):
    r = Reply(lead_id=lead.id, provider_message_id=mid, from_address=lead.email, subject=subject,
              body=body, received_at=at or h.utcnow(), classification=cls)
    session.add(r)
    session.commit()
    return r


# ==========================================================================
# 1. Workspace isolation
# ==========================================================================


def test_scoped_user_gets_404_on_every_object_route_of_another_workspace(session):
    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    reply_b = add_reply(session, lead_b, body="B-ONLY-REPLY-BODY")
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))

    probes = [
        ("get", f"/leads/{lead_b.id}", None),
        ("get", f"/review/{lead_b.id}/panel", None),
        ("get", f"/review?lead_id={lead_b.id}", None),
        ("post", f"/leads/{lead_b.id}/approve", None),
        ("post", f"/leads/{lead_b.id}/reject", {"reason": "x"}),
        ("post", f"/leads/{lead_b.id}/edit", {"subject": "s", "body_text": "b"}),
        ("post", f"/outbox/{lead_b.id}/send", None),
        ("post", f"/inbox/{reply_b.id}/tag", {"tag": "qualified"}),
        ("post", f"/inbox/{reply_b.id}/handled", None),
        ("post", f"/inbox/{reply_b.id}/confirm", None),
    ]
    for method, path, data in probes:
        resp = sean.request(method.upper(), path, data=data, follow_redirects=False)
        assert resp.status_code == 404, (method, path, resp.status_code)

    session.expire_all()
    assert session.get(Lead, lead_b.id).status == LeadStatus.READY
    assert session.execute(select(ReplyTag)).first() is None
    assert session.get(Reply, reply_b.id).handled_at is None
    assert session.execute(select(AuditEvent)).first() is None


def test_scoped_user_pages_never_contain_another_workspaces_data(session):
    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    lead_b.full_name, lead_b.company_name = "Zed Betaperson", "Betacorp Holdings"
    add_reply(session, lead_b, body="B-ONLY-REPLY-BODY")
    session.commit()
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))
    for path in ("/leads", "/review", "/review/list", "/inbox?show=all", "/outbox", "/removals"):
        body = sean.get(path).text
        for marker in ("Zed Betaperson", "Betacorp", "bob@northwind-logistics.com", "B-ONLY-REPLY-BODY"):
            assert marker not in body, (path, marker)


def _nav_counter(html: str, href: str) -> int:
    import re

    m = re.search(rf'href="{href}" class="nav-item"[^>]*>\s*<span>[^<]*</span>(?:<span class="counter"[^>]*>(\d+)</span>)?', html)
    assert m, f"nav item {href} not found"
    return int(m.group(1) or 0)


def test_sidebar_work_counts_are_scoped_to_the_users_workspaces(session):
    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    extra, _ = make_passed_lead(session, email="more@northwind-logistics.com")
    extra.campaign_id = lead_b.campaign_id
    extra.status = LeadStatus.APPROVED
    add_reply(session, lead_b, body="b")
    session.commit()
    html = as_user("sean@example.test", operator=False, ws=(ws_a,)).get("/leads").text
    assert (_nav_counter(html, "/review"), _nav_counter(html, "/outbox"), _nav_counter(html, "/inbox")) == (1, 0, 0)
    html_op = operator().get("/leads").text
    assert (_nav_counter(html_op, "/review"), _nav_counter(html_op, "/outbox"), _nav_counter(html_op, "/inbox")) == (2, 1, 1)


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


PUBLIC = {("GET", "/login"), ("POST", "/login"), ("GET", "/health"), ("GET", "/u/{token}"),
          ("POST", "/u/{token}"), ("GET", "/oauth/google/callback"),
          ("GET", "/about"), ("GET", "/privacy")}

_ROUTER_MODULES = ("auth", "campaigns", "costs", "dashboard", "imports", "inbox", "leads", "mailboxes",
                   "oauth", "outbox", "public", "removals", "review")


def _routes():
    """(method, path, requires_operator) for every route the app mounts.

    FastAPI 0.142 hides included routes behind lazy _IncludedRouter objects, so
    walk the source routers directly (all are included with no prefix)."""

    import importlib

    for name in _ROUTER_MODULES:
        router = importlib.import_module(f"mazz.web.routes.{name}").router
        router_op = any(d.dependency is web_auth.require_operator for d in router.dependencies)
        for r in router.routes:
            if isinstance(r, APIRoute):
                op = router_op or _depends_on(r.dependant, web_auth.require_operator)
                for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                    yield m, r.path, op


def _fill(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_non_public_route_requires_a_session():
    checked = 0
    anon = TestClient(app)
    for method, path, _op in _routes():
        if (method, path) in PUBLIC:
            continue
        resp = anon.request(method, _fill(path), follow_redirects=False)
        assert resp.status_code in (303, 401), (method, path, resp.status_code)
        checked += 1
    assert checked >= 40  # not vacuous


def test_every_require_operator_route_is_403_for_a_scoped_user(session):
    lead_a, _lead_b, ws_a = _two_workspace_leads(session)
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))
    seen = []
    for method, path, op in _routes():
        if (method, path) in PUBLIC or not op:
            continue
        resp = sean.request(method, _fill(path), follow_redirects=False)
        assert resp.status_code == 403, (method, path, resp.status_code)
        seen.append(path)
    for needed in ("/mailboxes", "/mailboxes/{mailbox_id}/connect", "/dashboard", "/costs",
                   "/costs/{entry_id}/delete", "/leads/import", "/workspaces", "/campaigns/{campaign_id}/delete",
                   "/inbox/{reply_id}/lift"):
        assert needed in seen, needed


def test_scoped_lead_detail_does_not_show_person_facts_about_other_people(session):
    """WEB-: lead_detail renders every cached fact at the company domain, including
    facts stamped subject_email=<someone else> (another workspace's prospect)."""

    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    secret = Fact.build(kind="linkedin_profile", content="SECRET-PERSON-FACT about bob",
                        source_url="https://linkedin.com/in/bob", retrieved_at=h.utcnow(),
                        subject_email=lead_b.email)
    seed_research(session, h.SHARED_DOMAIN, [JOB_FACT, secret])
    session.commit()
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))
    assert "SECRET-PERSON-FACT" not in sean.get(f"/leads/{lead_a.id}").text


def test_removal_confirmation_does_not_count_other_workspaces_leads(session):
    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))
    resp = sean.post("/removals", data={"value": lead_b.email, "scope": "global"})
    assert resp.status_code == 200
    assert "1 lead(s) held" not in resp.text  # tells Sean ClientRise has a lead at that address


def test_outbox_roster_does_not_list_mailboxes_pinned_to_other_workspaces(session):
    lead_a, lead_b, ws_a = _two_workspace_leads(session)
    make_mailbox(session, email_address="clientrise-private@other.example", campaign_id=lead_b.campaign_id)
    sean = as_user("sean@example.test", operator=False, ws=(ws_a,))
    assert "clientrise-private@other.example" not in sean.get("/outbox").text


def test_a_garbage_scope_value_is_a_4xx_not_a_500(session):
    op = operator(raise_server_exceptions=False)
    resp = op.post("/removals", data={"value": "example.org", "scope": "²"})
    assert resp.status_code < 500


# ==========================================================================
# 2. Status guards on approve / edit / reject / release
# ==========================================================================


def test_reject_cannot_turn_a_sent_lead_into_a_held_one(session):
    lead, draft = make_passed_lead(session)
    lead.status = LeadStatus.SENT
    session.commit()
    resp = operator().post(f"/leads/{lead.id}/reject", data={"reason": "oops"})
    session.expire_all()
    assert resp.status_code == 409
    assert session.get(Lead, lead.id).status == LeadStatus.SENT
    assert session.get(type(draft), draft.id).verification_status == VerificationStatus.PASSED


def test_approve_refuses_a_lead_held_because_it_is_suppressed(session):
    lead, _ = make_passed_lead(session)
    lead.status = LeadStatus.HELD
    h.suppress(session, email=lead.email, reason="unsubscribed")
    session.commit()
    resp = operator().post(f"/leads/{lead.id}/approve")
    session.expire_all()
    assert resp.status_code == 409
    assert session.get(Lead, lead.id).status == LeadStatus.HELD


def test_approve_refuses_a_lead_whose_sequence_was_stopped(session):
    lead, _ = make_passed_lead(session)
    lead.sequence_state = "stopped"  # a reply, bounce or unsubscribe landed
    session.commit()
    resp = operator().post(f"/leads/{lead.id}/approve")
    session.expire_all()
    assert resp.status_code == 409
    assert session.get(Lead, lead.id).status != LeadStatus.APPROVED


def test_editing_a_suppressed_leads_draft_does_not_release_the_hold(session):
    lead, draft = make_passed_lead(session)
    lead.status = LeadStatus.HELD
    h.suppress(session, email=lead.email, reason="unsubscribed")
    session.commit()
    operator().post(f"/leads/{lead.id}/edit", data={"subject": draft.subject, "body_text": "Dana, same text."})
    session.expire_all()
    assert session.get(Lead, lead.id).status == LeadStatus.HELD


def test_approving_twice_keeps_the_first_approvers_stamp(session):
    lead, _ = make_passed_lead(session)
    first = operator()
    assert first.post(f"/leads/{lead.id}/approve").status_code == 200
    session.expire_all()
    stamp = session.get(Lead, lead.id).approved_at
    second = as_user("second@example.test", operator=True)
    second.post(f"/leads/{lead.id}/approve")
    session.expire_all()
    lead = session.get(Lead, lead.id)
    assert (lead.approved_by, lead.approved_at) == ("client", stamp)


def test_reject_cancels_the_pending_send_job_so_an_edit_cannot_ride_the_old_release(session):
    """release -> reject -> edit -> approve must need a NEW release click."""

    lead, draft = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 303
    assert op.post(f"/leads/{lead.id}/reject", data={"reason": "wrong tone"}).status_code == 200
    edit = op.post(f"/leads/{lead.id}/edit", data={
        "subject": draft.subject,
        "body_text": "Dana - saw Northwind Logistics is hiring a Revenue Operations Manager to own the CRM.",
    })
    assert edit.status_code == 200
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200

    while process_next(kinds=("send",)) is not None:
        pass
    session.expire_all()
    assert session.execute(select(SentMessage)).first() is None, (
        "the edited copy was sent on the release click given to the earlier version"
    )


def test_edit_rejects_a_subject_with_a_line_break(session):
    lead, draft = make_passed_lead(session)
    lead.status = LeadStatus.READY
    session.commit()
    op = operator()
    op.post(f"/leads/{lead.id}/edit", data={"subject": "Hello\r\nBcc: someone@else.example",
                                            "body_text": "Dana - saw the hiring post."})
    resp = op.post(f"/leads/{lead.id}/approve")
    session.expire_all()
    assert resp.status_code == 409, "a CR/LF subject was approved; it fails only after the send attempt marker"


def test_release_refuses_a_draft_with_unfilled_merge_fields(session):
    lead, draft = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    draft.subject = "Hi {first_name}"  # damaged after approval by any other path
    session.commit()
    resp = op.post(f"/outbox/{lead.id}/send", follow_redirects=False)
    assert resp.status_code == 409
    assert send_jobs_for(session, lead.id) == []


def test_release_refuses_a_paused_sequence(session):
    lead, _ = make_passed_lead(session)
    make_mailbox(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    lead.sequence_state = "paused"
    lead.next_step_due_at = h.utcnow() + timedelta(days=3)
    session.commit()
    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 409
    assert send_jobs_for(session, lead.id) == []


def test_a_failed_draft_cannot_be_approved_or_released(session):
    from test_web import make_held_lead

    lead, _ = make_held_lead(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 409
    assert op.post(f"/outbox/{lead.id}/send", follow_redirects=False).status_code == 409


# ==========================================================================
# 3. Sessions, CSRF, exposure
# ==========================================================================


def test_logout_invalidates_the_session_cookie_server_side(session):
    c = operator()
    stolen = c.cookies.get(SESSION_COOKIE)
    c.post("/logout")
    replay = TestClient(app)
    replay.cookies.set(SESSION_COOKIE, stolen)
    assert replay.get("/leads", follow_redirects=False).status_code == 303


def test_cross_site_post_with_a_valid_cookie_is_refused(session):
    lead, _ = make_passed_lead(session)
    c = operator()
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    resp = c.post(f"/leads/{lead.id}/reject", data={"reason": "csrf"}, headers=evil)
    session.expire_all()
    assert resp.status_code == 403
    assert session.get(Lead, lead.id).status == LeadStatus.READY


def test_openapi_and_docs_are_not_public():
    anon = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path, follow_redirects=False).status_code in (303, 401, 403, 404), path


def test_session_cookie_is_secure_httponly_samesite_over_https(monkeypatch, session):
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    reload_settings()
    ensure_account("client")
    c = TestClient(create_app(), base_url="https://app.example.test")
    resp = c.post("/login", data={"username": "client", "password": TEST_PASSWORD}, follow_redirects=False)
    cookie = resp.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_session_cookie_lifetime_is_no_longer_than_a_working_day(session):
    ensure_account("client")
    c = TestClient(app)
    resp = c.post("/login", data={"username": "client", "password": TEST_PASSWORD}, follow_redirects=False)
    cookie = resp.headers["set-cookie"].lower()
    import re

    m = re.search(r"max-age=(\d+)", cookie)
    assert m and int(m.group(1)) <= 12 * 3600, f"cookie lives {m and int(m.group(1)) / 86400:.0f} days, no idle timeout"


def test_login_throttle_ignores_username_case_and_whitespace(session):
    ensure_account("client")
    c = TestClient(app)
    for variant in ("client", "CLIENT", " Client ", "client", "cLiEnT"):
        assert c.post("/login", data={"username": variant, "password": "wrong"}).status_code == 401
    assert c.post("/login", data={"username": "client ", "password": TEST_PASSWORD}).status_code == 429


def test_password_hash_uses_a_current_iteration_count():
    # OWASP 2023: PBKDF2-HMAC-SHA256 needs >= 600k iterations.
    assert int(web_auth.hash_password("x" * 12).split("$")[1]) >= 600_000


def test_app_pages_forbid_framing(session):
    lead, _ = make_passed_lead(session)
    h_ = {k.lower(): v for k, v in operator().get(f"/leads/{lead.id}").headers.items()}
    assert "x-frame-options" in h_ or "frame-ancestors" in h_.get("content-security-policy", "")


def test_the_default_inbox_still_shows_an_old_unhandled_reply_after_500_newer_handled_ones(session):
    lead_a, _lead_b, _ws = _two_workspace_leads(session)
    old = Reply(lead_id=lead_a.id, provider_message_id="old", from_address=lead_a.email, subject="Re",
                body="OLD-UNHANDLED-REPLY", received_at=h.utcnow() - timedelta(days=90), classification="needs_review")
    session.add(old)
    for i in range(501):
        session.add(Reply(lead_id=lead_a.id, provider_message_id=f"n{i}", from_address=lead_a.email, subject="Re",
                          body="done", received_at=h.utcnow() - timedelta(minutes=i), classification="needs_review",
                          handled_at=h.utcnow(), handled_by="client"))
    session.commit()
    html = operator().get("/inbox").text
    assert "OLD-UNHANDLED-REPLY" in html  # sidebar counts it (unlimited query), the page must too


# ==========================================================================
# 4. Unsubscribe host / endpoint
# ==========================================================================

UNSUB_HOST = "outreach.getsearchsitemarketing.com"


@pytest.mark.parametrize("host", [UNSUB_HOST, UNSUB_HOST.upper(), UNSUB_HOST + ":443", UNSUB_HOST + "."])
@pytest.mark.parametrize("path", ["/login", "/leads", "/static/style.css", "/docs", "/openapi.json",
                                  "/oauth/google/callback", "/removals", "/", "/logout"])
def test_unsubscribe_host_answers_404_for_everything_but_u_and_health(monkeypatch, host, path):
    monkeypatch.setenv("UNSUBSCRIBE_HOST", UNSUB_HOST)
    reload_settings()
    resp = TestClient(app).get(path, headers={"host": host}, follow_redirects=False)
    assert resp.status_code == 404, (host, path, resp.status_code)


def _routed_lead(session, email="ada@firm-law.com"):
    camp = h.make_campaign(session)
    lead = h.make_lead(session, email=email, company_domain="firm-law.com", campaign_id=camp.id,
                       status=LeadStatus.APPROVED, sequence_state=None)
    session.commit()
    return lead


def test_one_click_post_with_a_body_suppresses_and_stops_the_sequence(session):
    from mazz.pipeline.draft import unsubscribe_token

    lead = _routed_lead(session)
    lead.status, lead.sequence_state = LeadStatus.SENT, "active"
    session.commit()
    token = unsubscribe_token(lead)
    anon = TestClient(app)
    assert anon.get(f"/u/{token}").status_code == 200
    session.expire_all()
    assert session.execute(select(SuppressionEntry)).first() is None  # GET only confirms
    r = anon.post(f"/u/{token}", content="List-Unsubscribe=One-Click",
                  headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    session.expire_all()
    assert session.execute(select(SuppressionEntry)).scalar_one().email == lead.email
    assert session.get(Lead, lead.id).sequence_state == "stopped"


def test_one_click_post_with_an_unknown_token_is_not_reported_as_success(session):
    """RFC 8058 clients treat any 2xx to the POST as 'unsubscribed'. A token that
    matched nobody (rotated secret, pre-secret draft) must not look like that."""

    resp = TestClient(app).post("/u/" + "0" * 32, content="List-Unsubscribe=One-Click",
                                headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code >= 400


def test_reimport_after_unsubscribe_stays_suppressed(session):
    from mazz.ingest.csv_import import import_rows
    from mazz.pipeline.draft import unsubscribe_token

    lead = _routed_lead(session)
    token = unsubscribe_token(lead)
    TestClient(app).post(f"/u/{token}")
    session.delete(session.get(Lead, lead.id))  # e.g. list purged, then re-pulled from ZoomInfo
    session.commit()
    report = import_rows(session, [{"full_name": "Ada Law", "email": "ADA@firm-law.com",
                                    "company": "Firm Law", "website": "https://firm-law.com"}])
    assert not report.accepted and report.rejected[0].kind == "suppressed"


# ==========================================================================
# 5. XSS / unsafe rendering
# ==========================================================================

XSS = "<script>alert(1)</script><img src=x onerror=alert(2)>"


def test_hostile_lead_reply_draft_and_fact_text_is_escaped_everywhere(session):
    lead, draft = make_passed_lead(session)
    evil = Fact.build(kind="homepage", content=XSS, source_url="https://northwind-logistics.com/x",
                      retrieved_at=h.utcnow())
    seed_research(session, h.SHARED_DOMAIN, [JOB_FACT, evil])
    lead.full_name, lead.company_name, lead.job_title, lead.notes = XSS, XSS, XSS, XSS
    draft.signals_used = [JOB_FACT.id, evil.id]
    draft.subject = XSS
    session.commit()
    add_reply(session, lead, body=XSS, subject=XSS)
    op = operator()
    for path in ("/leads", f"/leads/{lead.id}", f"/review?lead_id={lead.id}", "/inbox?show=all", "/outbox"):
        html = op.get(path).text
        assert "<script>alert(1)" not in html and "<img src=x onerror" not in html, path


def test_lead_urls_are_never_rendered_as_javascript_hrefs(session):
    lead, _ = make_passed_lead(session)
    lead.linkedin_url = "javascript:alert(document.cookie)"
    session.commit()
    assert 'href="javascript:' not in operator().get(f"/leads/{lead.id}").text.lower()


# ==========================================================================
# 6. UI labels vs behaviour
# ==========================================================================


def test_approved_panels_do_not_claim_the_lead_is_queued_to_send(session):
    lead, _ = make_passed_lead(session)
    op = operator()
    assert op.post(f"/leads/{lead.id}/approve").status_code == 200
    for path in (f"/leads/{lead.id}", f"/review/{lead.id}/panel"):
        assert "queued to send" not in op.get(path).text.lower(), path
    assert send_jobs_for(session, lead.id) == []  # because nothing is queued


def test_ready_draft_page_does_not_say_approve_and_send(session):
    lead, _ = make_passed_lead(session)
    html = operator().get(f"/leads/{lead.id}").text
    assert "Approve &amp; send" not in html and "Approving queues" not in html


def test_no_angle_page_does_not_blame_the_retired_scorer(session):
    lead = h.make_lead(session, email="n2@paperkite.io", company_domain=h.WEAK_DOMAIN, status=LeadStatus.NO_ANGLE)
    session.commit()
    assert "scorer" not in operator().get(f"/leads/{lead.id}").text.lower()


# ==========================================================================
# 7. Metrics / dashboard
# ==========================================================================


def _tag(session, reply, tag, when):
    session.add(ReplyTag(reply_id=reply.id, tag=tag, tagged_by="maaz@example.test", tagged_at=when))
    session.commit()


def _sent(session, lead, at, *, legacy=False, bounced=None):
    d = h.make_draft(session, lead)
    session.add(SentMessage(lead_id=lead.id, draft_id=d.id, from_address="s@x.com", to_address=lead.email,
                            subject="s", body="b", sent_at=at, legacy=legacy, bounced_at=bounced))
    session.commit()


def test_dashboard_numbers_reconcile_with_the_source_rows(session):
    c1, c2 = h.make_campaign(session, name="One"), h.make_campaign(session, name="Two")
    mk = lambda e, c: h.make_lead(session, email=e, company_domain=e.split("@")[1], campaign_id=c.id)  # noqa: E731
    l1, l2, l3 = (mk(f"a{i}@one{i}.com", c1) for i in (1, 2, 3))
    l4 = mk("d@two4.com", c2)
    _sent(session, l1, IN_OCT); _sent(session, l1, IN_OCT + timedelta(hours=1)); _sent(session, l2, IN_OCT)  # noqa: E702
    _sent(session, l3, IN_SEP); _sent(session, l3, IN_OCT, legacy=True)  # noqa: E702
    _sent(session, l4, IN_OCT, bounced=IN_OCT)
    r1a = add_reply(session, l1, mid="1a", at=IN_OCT); r1b = add_reply(session, l1, mid="1b", at=IN_OCT)  # noqa: E702
    add_reply(session, l2, mid="2", at=IN_OCT, cls="ooo")
    r4 = add_reply(session, l4, mid="4", at=IN_OCT)
    r2q = add_reply(session, l2, mid="2q", at=IN_SEP)
    for r in (r1a, r1b):
        _tag(session, r, "qualified", IN_OCT)  # two replies, one conversation
    _tag(session, r4, "qualified", IN_OCT)
    _tag(session, r2q, "qualified", IN_SEP)  # September tag: not October
    for ws, cat, amt, month in ((c1.workspace_id, "mailboxes", "100.00", OCT), (c1.workspace_id, "smartlead", "50.50", OCT),
                                (c2.workspace_id, "hosting", "20.00", OCT), (c1.workspace_id, "other", "999", date(2026, 9, 1))):
        session.add(CostEntry(workspace_id=ws, month=month, category=cat, amount=Decimal(amt), entered_by="m@x.test"))
    session.commit()

    allm = M.month_metrics(session, OCT)
    one = M.month_metrics(session, OCT, workspace_id=c1.workspace_id)
    two = M.month_metrics(session, OCT, workspace_id=c2.workspace_id)
    assert (allm.sent, allm.contacted, allm.bounces, allm.qualified) == (4, 3, 1, 2)
    assert (one.sent, one.contacted, one.qualified) == (3, 2, 1)
    assert (two.sent, two.contacted, two.bounces, two.qualified) == (1, 1, 1, 1)
    assert (allm.cost_total, one.cost_total, two.cost_total) == (Decimal("170.50"), Decimal("150.50"), Decimal("20.00"))
    assert allm.cost_per_qualified == Decimal("85.25") and two.cost_per_qualified == Decimal("20.00")
    html = operator().get("/dashboard?month=2026-10").text
    assert "85.25" in html and "170.50" in html
    assert "20.00" in operator().get(f"/dashboard?month=2026-10&workspace={c2.workspace_id}").text


def test_reply_rate_can_never_exceed_100_percent(session):
    """A lead contacted in September whose reply lands in October is a reply in
    October's numerator with no October denominator."""

    camp = h.make_campaign(session)
    a = h.make_lead(session, email="a@oct1.com", company_domain="oct1.com", campaign_id=camp.id)
    b = h.make_lead(session, email="b@sep1.com", company_domain="sep1.com", campaign_id=camp.id)
    _sent(session, a, IN_OCT)
    _sent(session, b, IN_SEP)
    add_reply(session, a, mid="a", at=IN_OCT)
    add_reply(session, b, mid="b", at=IN_OCT)
    m = M.month_metrics(session, OCT)
    assert m.contacted == 1 and m.reply_rate <= 1.0, f"reply rate {m.reply_rate:.0%}"


def test_a_zero_dollar_cost_entry_does_not_produce_a_zero_cost_per_conversation(session):
    camp = h.make_campaign(session)
    lead = h.make_lead(session, email="q@qual1.com", company_domain="qual1.com", campaign_id=camp.id)
    _tag(session, add_reply(session, lead, mid="q", at=IN_OCT), "qualified", IN_OCT)
    session.add(CostEntry(workspace_id=camp.workspace_id, month=OCT, category="other", amount=Decimal("0"),
                          entered_by="m@x.test"))
    session.commit()
    assert M.month_metrics(session, OCT).cost_per_qualified is None  # '$0.00 per conversation' means 'not measured'


def test_skipped_unconfirmed_is_scoped_to_the_month_shown(session):
    camp = h.make_campaign(session)
    h.make_lead(session, email="s@skip1.com", company_domain="skip1.com", campaign_id=camp.id,
                skip_reason="vertical_unconfirmed", status=LeadStatus.NO_ANGLE)
    session.commit()
    assert M.month_metrics(session, date(2020, 1, 1)).skipped_unconfirmed == 0


@pytest.mark.parametrize("path", ["/dashboard?month=9999-12", "/costs?month=9999-12",
                                  "/dashboard?workspace=²", "/dashboard?workspace=99999999999999999999"])
def test_dashboard_and_costs_survive_hostile_query_parameters(session, path):
    resp = operator(raise_server_exceptions=False).get(path)
    assert resp.status_code < 500, path


# ==========================================================================
# 8. Audit attribution
# ==========================================================================


def _admin_actions(session):
    camp = h.make_campaign(session, name="Aud")
    box = make_mailbox(session)
    proof = session.execute(select(ProofPoint).where(ProofPoint.campaign_id == camp.id)).scalars().first()
    session.commit()
    ws, mb = camp.workspace_id, box.id
    return {
        "mailbox_create": (f"/mailboxes", dict(email_address="new@box.example", display_name="N", credential_ref="r")),
        "mailbox_edit_cap": (f"/mailboxes/{mb}/edit", dict(email_address=box.email_address, display_name="D",
                                                           credential_ref="r", daily_cap="99", min_spacing_seconds="0")),
        "mailbox_status": (f"/mailboxes/{mb}/status", {"status": "paused"}),
        "mailbox_delete": (f"/mailboxes/{mb}/delete", {}),
        "workspace_create": ("/workspaces", {"slug": "new-ws", "name": "New"}),
        "workspace_rename": (f"/workspaces/{ws}/edit", {"slug": "renamed", "name": "Renamed"}),
        "campaign_create": (f"/workspaces/{ws}/campaigns", {"name": "Second"}),
        "campaign_edit": (f"/campaigns/{camp.id}/edit", {"name": "Renamed", "vertical": "logistics companies",
                                                         "offer_line": "fill empty lanes"}),
        "proof_point_create": (f"/campaigns/{camp.id}/proof-points", {"claim_text": "We did X", "client_name": "Acme"}),
        "proof_point_retire": (f"/campaigns/{camp.id}/proof-points/{proof.id}/retire", {"active": ""}),
        "campaign_delete": (f"/campaigns/{camp.id}/delete", {}),
    }


@pytest.mark.parametrize("name", ["mailbox_create", "mailbox_edit_cap", "mailbox_status", "mailbox_delete",
                                  "workspace_create", "workspace_rename", "campaign_create", "campaign_edit",
                                  "proof_point_create", "proof_point_retire", "campaign_delete"])
def test_every_admin_state_change_writes_an_audit_event_with_the_actor(session, name):
    path, data = _admin_actions(session)[name]
    resp = operator().post(path, data=data, follow_redirects=False)
    assert resp.status_code in (200, 303), (name, resp.status_code, resp.text[:300])
    session.expire_all()
    events = session.execute(select(AuditEvent)).scalars().all()
    assert events and all(e.user_email == "client" for e in events), f"{name} left no audit trail"


def test_a_lead_import_is_audited(session):
    csv = b"Full Name,Email,Company,Website\nAda Law,ada@auditfirm.com,Audit Firm,https://auditfirm.com\n"
    resp = operator().post("/leads/import", files={"file": ("leads.csv", csv, "text/csv")})
    assert resp.status_code == 200
    session.expire_all()
    assert session.execute(select(AuditEvent)).first() is not None, "import of leads left no audit trail"


# ==========================================================================
# delete-by-person invoked from the web
# ==========================================================================


def test_delete_person_route_leaves_the_address_only_in_the_suppression_tombstone(session):
    lead, draft = make_passed_lead(session, email="erase.me@northwind-logistics.com")
    make_mailbox(session)
    person = Fact.build(kind="linkedin_profile", content="Erase Me works at Northwind",
                        source_url="https://linkedin.com/in/erase", retrieved_at=h.utcnow(),
                        subject_email=lead.email)
    seed_research(session, h.SHARED_DOMAIN, [JOB_FACT, person])
    session.commit()
    op = operator()
    op.post(f"/leads/{lead.id}/approve")
    op.post(f"/outbox/{lead.id}/send", follow_redirects=False)
    r = add_reply(session, lead, mid="e1", body="Please call me")
    _tag(session, r, "interested", h.utcnow())
    _sent(session, lead, h.utcnow())
    op.post(f"/leads/{lead.id}/reject", data={"reason": "why erase.me@northwind-logistics.com is odd"})

    resp = op.post("/removals/delete-person", data={"email": "erase.me@northwind-logistics.com", "confirm": "DELETE"})
    assert resp.status_code == 200
    leftovers = []
    for table in Base.metadata.sorted_tables:
        if table.name == "suppression_list":
            continue
        n = session.execute(
            text(f'SELECT count(*) FROM "{table.name}" t WHERE t::text ILIKE :p'),
            {"p": "%erase.me@northwind-logistics.com%"},
        ).scalar_one()
        if n:
            leftovers.append((table.name, n))
    assert leftovers == []
