"""Live measurement spike for `LiveCompanyAdapter`'s job-posting provenance.

This is the W3/provenance-at-ingest measurement, made reproducible. It was
previously a scratchpad-only script and a one-off ten-domain list that got
lost between sessions -- rerun it from here instead of reconstructing the
domain list from prose in a report.

    .venv/bin/python scripts/measure_job_posting_provenance.py

Hits each domain's real site over the network (robots.txt respected, one
request in flight per domain, `LiveCompanyAdapter`'s own rate limiter
applies) and reports, per domain and in aggregate:

  * how many `JOB_POSTING` facts were admitted (STRONG tier, i.e. NOT
    demoted to `job_posting_unconfirmed`)
  * how many of those are false positives, judged by hand against the
    printed URL + title (this script cannot verify "is this a real req"
    automatically -- that's the entire point of the provenance gap)

The false-positive column in the printed table is left for a human to fill
in by eyeballing the printed candidates; this script's job is to make the
run repeatable and the raw JSON inspectable, not to grade itself.

Uses real network access and is therefore never run by pytest/CI --
`tests/conftest.py` pins every provider to stub. Run it by hand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The ten-domain measurement set from the original W3 spike (git history
# 66fd0be-era) and the href_keyword provenance re-run. Keep this list here,
# not in a commit message or a report, so it survives.
DOMAINS = [
    "baremetrics.com",
    "basecamp.com",
    "close.com",
    "chartmogul.com",
    "customer.io",
    "zapier.com",
    "asana.com",
    "calendly.com",
    "buffer.com",
    "ghost.org",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "scratch_measurements"


def main() -> int:
    from mazz.integrations.base import ProviderBlocked, ProviderUnavailable
    from mazz.integrations.company import LiveCompanyAdapter

    OUT_DIR.mkdir(exist_ok=True)
    adapter = LiveCompanyAdapter(rate_limit_seconds=1.0)
    total_admitted = 0
    total_domains_with_admitted = 0
    rows = []
    try:
        for domain in DOMAINS:
            print(f"--- {domain} ---", file=sys.stderr)
            try:
                result = adapter.fetch_company(domain)
            except (ProviderBlocked, ProviderUnavailable) as exc:
                print(f"  BLOCKED/UNAVAILABLE: {exc}", file=sys.stderr)
                rows.append({"domain": domain, "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 - report, don't hide, per CLAUDE.md
                print(f"  ERROR: {exc!r}", file=sys.stderr)
                rows.append({"domain": domain, "error": repr(exc)})
                continue

            admitted = [f for f in result.facts if f.kind == "job_posting"]
            demoted = [f for f in result.facts if f.kind == "job_posting_unconfirmed"]
            if admitted:
                total_domains_with_admitted += 1
            total_admitted += len(admitted)

            for f in admitted:
                print(
                    f"  JOB_POSTING  [{f.metadata.get('identification_method')}"
                    f"/{f.metadata.get('confidence')}] {f.content!r} -> {f.source_url}",
                    file=sys.stderr,
                )
            for f in demoted:
                print(
                    f"  (demoted)    [{f.metadata.get('identification_method')}"
                    f"/{f.metadata.get('confidence')}] {f.content!r} -> {f.source_url}",
                    file=sys.stderr,
                )

            rows.append(
                {
                    "domain": domain,
                    "admitted": [
                        {
                            "content": f.content,
                            "source_url": f.source_url,
                            "identification_method": f.metadata.get("identification_method"),
                            "confidence": f.metadata.get("confidence"),
                        }
                        for f in admitted
                    ],
                    "demoted": [
                        {
                            "content": f.content,
                            "source_url": f.source_url,
                            "identification_method": f.metadata.get("identification_method"),
                            "confidence": f.metadata.get("confidence"),
                        }
                        for f in demoted
                    ],
                }
            )
    finally:
        adapter.close()

    out_path = OUT_DIR / "job_posting_provenance.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print("\n=== summary ===")
    print(f"domains measured           : {len(DOMAINS)}")
    print(f"domains with JOB_POSTING   : {total_domains_with_admitted}/{len(DOMAINS)}")
    print(f"JOB_POSTING facts admitted : {total_admitted}")
    print(f"raw output                 : {out_path}")
    print(
        "\nFalse positives are not auto-graded -- eyeball each admitted item's "
        "content/source_url above (or in the JSON) against what the URL actually "
        "is, same as the original measurement."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
