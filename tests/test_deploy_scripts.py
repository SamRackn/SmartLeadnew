"""WP8: scripts/verify_dns.py parsing (recorded nslookup output, never live
DNS) and scripts/rehearsal.py's refusals (it cannot run before gates 2-3)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mazz.config import reload_settings

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dns = load("verify_dns")
rehearsal = load("rehearsal")

# Recorded from Windows nslookup on 2026-10-05.
WINDOWS = """Server:  cdns01.comcast.net
Address:  2001:558:feed::1

getsearchsitemarketing.com\ttext =

\t"google-site-verification=zO530CjeQBmIyBk99nFXcrw4a1ZupNbhvC_E9DZ9Yek"
getsearchsitemarketing.com\ttext =

\t"v=spf1 include:_spf.google.com ~all"
"""
# Linux style, a DKIM key split across two strings as long keys are.
LINUX_DKIM = ('google._domainkey.example.com\ttext = "v=DKIM1; k=rsa; p=MIIBIjAN" "BgkqhkiG9w0"\n')


def test_parse_txt_reads_both_platforms_and_joins_split_strings():
    assert dns.parse_txt(WINDOWS) == [
        "google-site-verification=zO530CjeQBmIyBk99nFXcrw4a1ZupNbhvC_E9DZ9Yek",
        "v=spf1 include:_spf.google.com ~all",
    ]
    assert dns.parse_txt(LINUX_DKIM) == ["v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0"]
    assert dns.parse_txt("** server can't find _dmarc.nope.example: NXDOMAIN") == []


def test_checks_fail_on_the_misconfigurations_that_matter():
    assert dns.check_spf(dns.parse_txt(WINDOWS))[0]
    assert not dns.check_spf(["v=spf1 include:sendgrid.net ~all"])[0]
    assert not dns.check_spf(["v=spf1 include:_spf.google.com ~all", "v=spf1 -all"])[0]
    assert dns.check_dkim(dns.parse_txt(LINUX_DKIM))[0]
    assert not dns.check_dkim(["v=DKIM1; k=rsa; p="])[0]
    ok, why = dns.check_dmarc(["v=DMARC1; p=reject; rua=mailto:x@example.com"])
    assert ok and "p=reject" in why
    assert not dns.check_dmarc([])[0]


def test_verify_passes_when_any_selector_has_a_key(capsys):
    records = {
        "example.com": ["v=spf1 include:_spf.google.com ~all"],
        "zmail._domainkey.example.com": ["v=DKIM1; p=MIIB"],
        "_dmarc.example.com": ["v=DMARC1; p=none"],
    }
    lookup = lambda name: records.get(name, [])  # noqa: E731
    assert dns.verify("example.com", ["google", "zmail"], lookup)
    out = capsys.readouterr().out
    assert "selector google: no v=DKIM1 record" in out and "selector zmail: key present" in out
    assert not dns.verify("example.com", ["google"], lookup)


@pytest.mark.parametrize("env, seeds, why", [
    ({"SEND_ENABLED": "false"}, "a@x.com", "SEND_ENABLED"),
    ({}, "a@x.com", "EMAIL_SENDER"),  # conftest pins the stub
    ({"EMAIL_SENDER": "google"}, "", "--seeds"),
])
def test_rehearsal_refuses_without_the_switch_a_live_sender_and_seeds(monkeypatch, capsys, env, seeds, why):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reload_settings()
    assert rehearsal.main(["--seeds", seeds]) == 2
    assert why in capsys.readouterr().err


def test_a_host_supplied_database_url_uses_psycopg3():
    from mazz.config import Settings

    for url in ("postgres://u:p@h/db", "postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"):
        assert Settings(database_url=url).database_url == "postgresql+psycopg://u:p@h/db"


STRONG = "s" * 40


@pytest.mark.parametrize("secret, sending, env, expected", [
    ("changeme", False, {}, ["SESSION_SECRET"]),
    ("", False, {}, ["SESSION_SECRET"]),
    ("short-but-not-default", False, {}, ["SESSION_SECRET"]),
    (STRONG, False, {}, []),
    (STRONG, True, {"SEND_ENABLED": "true", "EMAIL_SENDER": "stub"}, ["EMAIL_SENDER"]),
    (STRONG, True, {"SEND_ENABLED": "false", "EMAIL_SENDER": "stub"}, []),
    # Live sending's own settings are covered in test_launch_readiness.py.
    # SEND_WINDOW on, as live sending requires (conftest turns it off).
    (STRONG, True, {"SEND_ENABLED": "true", "EMAIL_SENDER": "google", "SEND_WINDOW": "true",
                    "UNSUBSCRIBE_BASE_URL": "https://outreach.example.com",
                    "UNSUBSCRIBE_HOST": "outreach.example.com", "UNSUBSCRIBE_SECRET": STRONG,
                    "APP_BASE_URL": "https://app.example.com", "GOOGLE_OAUTH_CLIENT_ID": "i",
                    "GOOGLE_OAUTH_CLIENT_SECRET": "s", "OAUTH_TOKEN_KEY": "k"}, []),
])
def test_deployment_problems(monkeypatch, secret, sending, env, expected):
    from mazz.config import Settings, deployment_problems

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    problems = deployment_problems(Settings(session_secret=secret), sending=sending)
    assert [next(w for w in ("SESSION_SECRET", "EMAIL_SENDER") if w in p) for p in problems] == expected


def test_the_app_and_worker_refuse_to_start_unsafe(monkeypatch):
    from fastapi.testclient import TestClient

    from mazz import ops
    from mazz.web.app import create_app

    monkeypatch.setenv("SESSION_SECRET", "changeme")
    reload_settings()
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        with TestClient(create_app()):
            pass
    # conftest runs SEND_ENABLED=true on the stub: fine for tests, fatal for a worker.
    monkeypatch.setenv("SESSION_SECRET", STRONG)
    reload_settings()
    assert ops.main(["worker"]) == 2


def test_rehearsal_never_sends_a_mailbox_to_itself():
    assert rehearsal.pairs(["a@x.com", "b@y.com"], ["a@x.com", "s@z.com"]) == [
        ("a@x.com", "s@z.com"), ("b@y.com", "s@z.com")]
