"""Operator entry point for bulk drafting.

Two commands, deliberately separate, because a batch is submitted now and
lands up to 24 hours later and nothing should hold a database session open
across that:

    .venv/bin/python scripts/draft_batch_run.py submit
    .venv/bin/python scripts/draft_batch_run.py poll

`submit` claims due `draft` jobs, settles the no-angle ones for free, and sends
the rest as one Batch API request. `poll` lands any batch that has finished:
drafts are written `verification_status=pending` and chained to `verify`,
exactly as the synchronous path writes them. Run `poll` on a short cadence and
`submit` on whatever suits the volume.

    .venv/bin/python scripts/draft_batch_run.py check

`check` makes one cheap call to confirm the frozen system prompt is long enough
for the configured model to cache it at all. Below the model's minimum the API
creates no cache entry and reports no error -- you simply pay full price
forever. Worth running after any edit to SYSTEM_PROMPT or DRAFT_MODEL.

Nothing here can send or approve an email. A batch result is a draft awaiting
verification and then a human.
"""

from __future__ import annotations

import logging
import sys

from mazz.config import get_settings
from mazz.db import get_sessionmaker
from mazz.pipeline.compose_claude import assert_cacheable_prefix
from mazz.pipeline.draft_batch import poll_open_batches, run_once


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    command = argv[1] if len(argv) > 1 else "submit"

    if command == "check":
        tokens = assert_cacheable_prefix()
        print(f"system prefix is {tokens} tokens — cacheable on this model")
        return 0

    factory = get_sessionmaker()

    if command == "submit":
        with factory() as session:
            report = run_once(session, limit=get_settings().draft_batch_size)
            session.commit()
        if report.batch_id is None:
            print(f"nothing submitted ({report.no_angle} leads resolved no_angle, free)")
            return 0
        print(
            f"submitted {report.batch_id}: {report.submitted} requests, "
            f"{report.no_angle} no_angle resolved without a model call"
        )
        return 0

    if command == "poll":
        with factory() as session:
            reports = poll_open_batches(session)
            session.commit()
        for report in reports:
            if report.drafted or report.failed:
                print(
                    f"{report.batch_id}: {report.drafted} drafted, "
                    f"{report.failed} failed, "
                    f"{report.usage.cache_hit_rate:.0%} of prompt tokens cached, "
                    f"${report.cost_usd:.4f} (${report.cost_per_draft:.5f}/draft)"
                )
            else:
                print(f"{report.batch_id}: still processing")
        if not reports:
            print("no open batches")
        return 0

    print(f"unknown command {command!r}; expected submit, poll or check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
