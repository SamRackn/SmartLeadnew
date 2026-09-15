---
description: Reset the dev database, load the sample leads, and walk them through every pipeline stage, printing the rows produced at each step.
---

The end-to-end proof. Runnable any time; it is how we demonstrate the pipe works.

1. Drop and recreate the `mazz_dev` schema, then `alembic upgrade head`.
2. Seed `suppression_list` with the fixture entries.
3. Ingest `tests/fixtures/sample_leads.csv`.
4. Run the queue worker until the queue drains.
5. Print, per stage: leads by status, `research_cache` rows by domain, drafts with
   their `verification_status` and cited fact ids, and the contents of
   `smartlead_outbox.jsonl`.

Call out each of these explicitly, since they are the behaviors that matter:
- the suppressed lead was **rejected at ingest** and has no `jobs` row
- two leads at the same company produced **one** cache row and **one** provider call
- the weak-signal lead is `no_angle` with **no draft**
- the unverifiable draft is `failed` / `held`

Report what actually happened. If a stage errored, paste the error rather than
summarizing it.
