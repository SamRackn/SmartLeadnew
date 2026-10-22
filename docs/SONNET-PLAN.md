# Execution plan for a Sonnet session: finish launch-v1 (WP6 → WP9)

Paste everything below the line into Claude Code (Sonnet) from the repo root.

---

You are finishing branch `launch-v1` of the owned outbound platform. WP0–WP5
are committed. WP6 is built but uncommitted. WP7, WP8 and WP9 are not started.
`CLAUDE.md`, `docs/LAUNCH-PROMPT.md` (the client's decisions; it wins where it
says so) and `docs/LAUNCH-PLAN.md` (what is built, with audit notes) are the
spec. Read all three before touching code. Do not edit `CLAUDE.md`.

## Rules

- One task at a time, in order. One commit per task. Don't start a task
  until the previous one is green and committed.
- Before claiming a task is green, run the full suite and paste the real
  last lines of its output. Baseline at the start of this plan: see Task 0.
- Reuse before writing. Before every new function, grep for an existing one:
  suppression writes (`ingest/csv_import.py` suppression import,
  `web/routes/public.py::suppress_email`; there's no single helper, so
  don't add an eleventh variant), `audit.record`,
  `privacy.delete_person`, `sequence.thread_parent`,
  `mailbox.record_bounce`, `replies` classification and the Gmail reader.
  If something already does 80% of the job, extend it.
- No new dependencies without asking. `dnspython` is NOT installed. Ask
  before adding it (Task 3).
- Never invent a postal address, a proof point, a client name, a credential,
  a DNS result or a CSV column name. If you need one, it's missing: stop
  and say so.
- Never add a second call site that creates a `send` job. Never add
  `enqueue_next("send")`. `test_exactly_one_place_in_the_codebase_creates_a_send_job`
  must keep passing.
- Tests never touch the network. Do not weaken the autouse stub fixture in
  `tests/conftest.py`.
- Windows. Use Git Bash or PowerShell. Don't commit CRLF churn: check
  `git diff --ignore-cr-at-eol --stat` before each commit.
- **STOP and ask** when a task says STOP, when a test fails twice for a reason you
  don't understand, or when a fix would touch a guardrail in `CLAUDE.md`.

## Task 0: test environment (no code)

The suite only runs against a database whose name ends in `_test`. Take the
credentials from `DATABASE_URL` in `.env` and swap the database name:

```powershell
$db = ((Select-String .env -Pattern '^DATABASE_URL=').Line -replace '^DATABASE_URL=','').Trim('"')
$env:TEST_DATABASE_URL = [regex]::Replace($db, '/[^/?]+(\?|$)', '/mazz_test$1')
$env:PATH = "C:\Program Files\PostgreSQL\17\bin;$env:PATH"   # pg_dump for the backup test
.venv\Scripts\python.exe -m pytest -q -p no:warnings
```

Expected: `828 passed` in about 7 minutes (measured 2026-10-05, with WP6
uncommitted in the tree). If not, STOP and report. Run the suite in the
foreground and never start a second run while one is going. Two runs on
`mazz_test` truncate each other's data and give false `uq_leads_email`
failures. A backgrounded PowerShell run once died with exit 255 and left
python running. Also run `alembic upgrade head` on the dev database. It is
on an old revision (`a7f31c0b9e42`) and has no `workspaces`, `mailboxes`,
`sent_messages`, `replies` or `cost_entries` tables, so the app can't
serve `/costs` or `/dashboard` against it until it's migrated.

## Task 1: commit WP6 (metric and dashboard)

Already written and tested (`tests/test_metrics.py`, 16 passed):
`models.CostEntry`, migration `b9f6d7e8a0c1_cost_entries`, `mazz/metrics.py`,
`/costs` route and template, the dashboard metrics panel, and the nav link.

1. Read the diff (`git diff --ignore-cr-at-eol` plus the untracked files).
   Check against WP6 in `LAUNCH-PROMPT.md`: costs per workspace/month/category,
   cost per qualified conversation by the date tagged, the secondary metrics, the
   Smartlead baseline, and no open/click tracking.
2. `/costs` and `/dashboard` are both `require_operator`, so Sean can't
   reach either. Keep it that way: no workspace-scoped user sees costs.
3. Do not commit `docs/LAUNCH-PROMPT.md` or `docs/SONNET-PLAN.md`. Ask Sam
   first.
4. Append a "WP6 (done)" section to `docs/LAUNCH-PLAN.md`: what was built and
   which tests cover it.
5. Commit `WP6: cost entries and cost per qualified conversation`.

## Task 2: WP7, cutover from Smartlead

Goal: no lead gets Email 1 twice, no suppressed or replied lead re-enters, and
no mailbox is driven by both systems.

**Read first.** `ingest/csv_import.py` (it already maps ZoomInfo and dedupes
on email), the suppression CSV import, `pipeline/sequence.py`
(`thread_parent`, `LIVE_STATES`), the guard in `pipeline/draft.py` around
line 664 (`sequence_step > 1 or thread_parent(...)` refuses Email 1), and
`pipeline/replies.py` (sync stores only mail on threads in `sent_messages`).

**The trap to design around.** Contacted leads were emailed by Smartlead
from the same four Google mailboxes, so we have no `sent_messages` row for
them. That causes two problems:
- (a) Follow-ups can't thread: `send.py` takes the Gmail `threadId` from
  `sent_messages`.
- (b) Reply sync ignores their replies: the thread isn't "ours". After
  cutover, a reply to a Smartlead email would be seen by neither system, and
  we would still send them FU1.

Required behaviour:
1. **Importer** (`python -m mazz.ingest.smartlead`, CLI only, no web route):
   three inputs, matching the three Smartlead exports in WP7. Column names come
   from the real exports. **STOP if you don't have a sample file**: build
   against a header map in one dict at the top of the module, write tests with
   synthetic CSVs, and list the headers you assumed in `LAUNCH-PLAN.md` as
   "unconfirmed until the real export arrives".
   - Unsubscribes and bounces → global suppression, through the existing
     suppression path.
   - Sean's clients and named companies → **workspace-scoped** suppression
     rows (Sean's workspace only).
   - Campaign leads: suppressed → skip. Replied in Smartlead →
     `sequence_state="stopped"`. Received N emails → `sequence_step = N + 1`
     (so the draft guard refuses Email 1), with `next_step_due_at` taken from the
     last-sent date and the step's `wait_days`. Received ≥ 3 →
     `sequence_state="complete"`. Not started → normal import.
   - Put the received→next-step mapping in one dict so Maaz can change it.
   - Print the sanity counts and compare them with the expected
     864 / 133 / 731 / 124 / 7 / 2. A mismatch prints loudly but doesn't
     abort.
2. **Thread adoption** before any legacy follow-up can be released. For each
   live legacy lead, search the sending mailbox read-only
   (`in:sent to:<email> subject:"Quick Question"` through the existing Gmail
   reader, `gmail.readonly` only). Record the found message as a
   `sent_messages` row marked legacy (nullable flag or `provider="smartlead"`,
   migration if needed). Then follow-ups thread and reply sync sees replies.
   **If no thread is found, the lead is held with a reason. Never send an
   unthreaded "Re: Quick Question".**
3. `--dry-run` is the default. It writes nothing and prints what it would
   do. `--commit` writes.

Tests (synthetic data):
- zero legacy-contacted leads can reach Email 1;
- zero suppressed or replied leads enter the queue;
- a legacy follow-up with no adopted thread is held, not sent;
- a reply on an adopted thread is stored and stops the sequence;
- re-running the import is idempotent.

Then run `compliance-auditor` on the diff, fix what it finds, add the
WP7 section to `LAUNCH-PLAN.md`, and commit.

## Task 3: WP8, deploy and verification scripts

1. `scripts/verify_dns.py <domain>...` checks SPF (includes
   `_spf.google.com`), DKIM (selector `google`; report any other selector
   given with `--selector`), and DMARC (record exists, print the policy). Exits
   non-zero on any failure. **Ask before adding `dnspython`.** The fallback
   is shelling out to `nslookup -type=TXT`, which is uglier but has no
   dependency. Test the parsing on recorded TXT strings, not live DNS.
2. `scripts/rehearsal.py --seeds a@x,b@y` refuses unless
   `SEND_ENABLED=true` and seeds are given. It sends one message per active
   connected mailbox, then reads each seed's copy through the reader and prints
   `Authentication-Results`. It can't run until gates 2 and 3 are met, so
   test the refusal paths only.
3. Hosting: write one short section in `LAUNCH-PLAN.md`. Recommend Render
   or Fly.io (one web process, one worker running `python -m mazz.ops`, managed
   Postgres), confirm the worker entrypoint against `ops.py`, and give the
   start commands. Replace `scripts/launchd.sh` only with what that host needs
   (a `Procfile` or `render.yaml`). Don't spend more than this on it.
4. Commit.

## Task 4: close the open audit findings that are compliance issues

These are from `LAUNCH-PLAN.md` "Audit of WP5", "Open". Do these three only:
1. An `Auto-Submitted` header outranks "remove me" (it's classed OOO). An
   unsubscribe request inside an auto-reply must still suppress. Fix the
   precedence in `pipeline/replies.py` and add one test.
2. There's no reversal for a false-positive auto-unsubscribe. Add an
   operator-only "lift suppression" action on the inbox item that was
   auto-suppressed. It's attributed through `audit.record`, needs a
   confirmation step, and works only on rows created by auto-classification
   (never on ones a human or an import added). Add one test.
3. `/outbox` and release don't show a stopped or paused lead as blocked
   before the click. Add it to the existing eager re-check in release.
   The send step already refuses; this only moves the message earlier.

Skip the rest: outbox mailbox header, `sent_outbox.jsonl` erase, and the
attempt-to-wire race. They're in the plan as known lows.

## Task 5: WP9, the audit gate

Run `/audit` (`compliance-auditor`, read-only). It must pass everything listed
under WP9 in `LAUNCH-PROMPT.md`. Fix findings in the owning files and re-run
until there are no critical or high findings. Paste the auditor's final summary
into `LAUNCH-PLAN.md` and commit.

## Out of scope: do not build

- Canned replies (WP5 optional). Not asked for at launch.
- The ClientRise workspace, `sender_claims` attestation, or ClientRise copy.
  Every input is missing.
- Applying the proposed `CLAUDE.md` patch. Sam decides.
- Any model call on the cold path, open or click tracking, or warmup.

## Hand-off format

Per task: files changed, exact pytest tail, auditor findings with what
you fixed. At the end, a go-live table with the 7 human gates from
`LAUNCH-PROMPT.md`, each marked met or unmet with its owner. With what's
in the repo today, all 7 are unmet.
