"""Check SPF, DKIM and DMARC for the sending domains before anything is sent.

    python scripts/verify_dns.py getsearchsitemarketing.com searchsitemarketinghub.com
    python scripts/verify_dns.py getsearchsitemarketing.com --selector zmail

* SPF   exactly one ``v=spf1`` record, and it includes ``_spf.google.com``.
* DKIM  a ``v=DKIM1`` key with a non-empty ``p=`` at ``google._domainkey``; any
        ``--selector`` is checked and reported too (Zapmail may use its own),
        and the domain passes if at least one selector has a key.
* DMARC a ``v=DMARC1`` record at ``_dmarc``; its policy is printed.

Exits 1 if any domain fails. Shells out to ``nslookup -type=TXT`` rather than
adding a DNS library (ponytail: one subprocess per name; dnspython if this ever
needs to run somewhere without nslookup).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Callable

_TEXT = re.compile(r"\btext\s*=")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_txt(output: str) -> list[str]:
    """TXT records from ``nslookup -type=TXT`` output (Windows or Linux).
    A record split into several quoted strings is joined, as DNS joins it."""

    return ["".join(_QUOTED.findall(chunk)) for chunk in _TEXT.split(output)[1:]]


def nslookup(name: str) -> list[str]:
    try:
        out = subprocess.run(["nslookup", "-type=TXT", name], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_txt(out)


def _tag(record: str, tag: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{tag}\s*=\s*([^;]*)", record, re.I)
    return m.group(1).strip() if m else None


def check_spf(records: list[str]) -> tuple[bool, str]:
    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if len(spf) != 1:
        return False, f"{len(spf)} SPF records (need exactly one)"
    if "include:_spf.google.com" not in spf[0].lower().split():
        return False, f"does not include _spf.google.com: {spf[0]}"
    return True, spf[0]


def check_dkim(records: list[str]) -> tuple[bool, str]:
    key = next((r for r in records if r.replace(" ", "").lower().startswith("v=dkim1")), None)
    if key is None:
        return False, "no v=DKIM1 record"
    if not _tag(key, "p"):
        return False, "key is empty (p=): revoked"
    return True, f"key present ({len(_tag(key, 'p'))} chars)"


def check_dmarc(records: list[str]) -> tuple[bool, str]:
    rec = next((r for r in records if r.replace(" ", "").lower().startswith("v=dmarc1")), None)
    if rec is None:
        return False, "no v=DMARC1 record"
    return True, f"policy p={_tag(rec, 'p') or '(missing)'}: {rec}"


def verify(domain: str, selectors: list[str], lookup: Callable[[str], list[str]] = nslookup) -> bool:
    print(domain)
    ok_spf, spf = check_spf(lookup(domain))
    print(f"  SPF   {'pass' if ok_spf else 'FAIL'}  {spf}")
    dkim_ok = False
    for sel in selectors:
        ok, why = check_dkim(lookup(f"{sel}._domainkey.{domain}"))
        dkim_ok |= ok
        print(f"  DKIM  {'pass' if ok else 'FAIL'}  selector {sel}: {why}")
    ok_dmarc, dmarc = check_dmarc(lookup(f"_dmarc.{domain}"))
    print(f"  DMARC {'pass' if ok_dmarc else 'FAIL'}  {dmarc}")
    return ok_spf and dkim_ok and ok_dmarc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("domains", nargs="+")
    parser.add_argument("--selector", action="append", default=[],
                        help="extra DKIM selector to check (repeatable)")
    args = parser.parse_args(argv)
    selectors = ["google", *[s for s in args.selector if s != "google"]]
    results = [verify(d.strip().lower(), selectors) for d in args.domains]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
