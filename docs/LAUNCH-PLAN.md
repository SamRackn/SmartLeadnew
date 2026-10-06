# Launch plan — Search Site Marketing (V1), then ClientRise (staged)

Branch `launch-v1`, cut from `origin/phase2-verifier-provenance-routing`
(`bb7399b`, identical to `origin/worktree-w6-operations`). Client launch answers
of 2026-09-29 are the source of truth; `CLAUDE.md` still governs everything
they do not override.

This file is written in WP0 and updated as each work package lands. The
**status table** below reflects the branch *as found*; the per-WP sections say
what changed.

---

## Baseline test run (WP0)

Local Postgres 17 (Windows service `postgresql-x64-17`), test database
`mazz_test` (the suite refuses any database not named `*_test`). `pg_dump` /
`pg_restore` must be on `PATH` for the backup test.

```
$ PATH="/c/Program Files/PostgreSQL/17/bin:$PATH" \
  TEST_DATABASE_URL=postgresql+psycopg://postgres:***@localhost/mazz_test \
  .venv/Scripts/python.exe -m pytest -q
...
716 passed, 1 warning in 225.29s (0:03:45)
```

Two Windows-only problems found and fixed before that run:

* `pip install -e ".[dev,excel,google]"` — the `excel` and `google` extras were
  not installed in `.venv`; `tests/test_excel_import.py` failed to collect.
* `test_exactly_one_place_in_the_codebase_creates_a_send_job` read source files
  with the platform default codec (cp1252 on Windows) and crashed on a UTF-8
  byte. Now `read_text(encoding="utf-8")`.

`alembic upgrade head` runs cleanly (the suite's session fixture runs it; head
is `b3e1c9d4f2a7`).

---

## What the branch has and lacks, verified against code

"Claimed" = what the launch prompt said the branch appears to have.

### Baseline items (claimed present)

| Item | State | Where | Covering tests |
|---|---|---|---|
| Workspaces | **exists** (identity + name only; no membership, no isolation) | `models.Workspace`, `web/routes/campaigns.py` | `test_workspaces.py` |
| Campaigns: per-campaign qualification, offer line, proof points, opt-in generic fallback | **exists** | `models.Campaign`, `models.ProofPoint`, `pipeline/outreach.py`, `pipeline/qualify.py` | `test_outreach.py`, `test_qualify.py`, `test_web.py` |
| Campaign with zero proof points, no model call | **exists** — `offer_for_campaign` returns an offer with no claim; email 1 is `compose_outreach`, a pure template | `pipeline/outreach.py` | `test_outreach.py` |
| Mailboxes | **exists** | `models.Mailbox`, `web/routes/mailboxes.py`, `pipeline/mailbox.py` | `test_sending.py`, `test_web.py` |
| sent_messages | **exists** (records `provider_thread_id`, `rfc_message_id`; unused for threading) | `models.SentMessage` | `test_sending.py` |
| Gmail API sender, `gmail.send` only | **exists, but on the wrong credential model**: service account + domain-wide delegation (`GMAIL_SA_<REF>`). Must be replaced (WP5). | `integrations/email_sender.py` | `test_sending.py` |
| Rotation + daily caps + spacing | **exists** (UTC day, no send window, no row lock → one worker only) | `pipeline/mailbox.py` | `test_sending.py::test_rotation_*`, `test_a_capped_*` |
| At-most-once send | **exists** (`drafts.send_attempted_at` committed before the wire) | `pipeline/send.py`, `ops.resolve_send` | `test_sending.py::test_a_failure_after_the_wire_is_never_retried_into_a_second_send`, `test_ops.py` |
| Approve separate from send | **exists** (approve → `approved`; one send job per human click on /outbox) | `web/routes/leads.py`, `web/routes/outbox.py` | `test_sending.py::test_exactly_one_place_in_the_codebase_creates_a_send_job` |
| Approval audit trail | **partial** — `leads.approved_by` / `approved_at` only. Reject, edit, suppress, send-release are unattributed. `approved_by` is the single env login. | `models.Lead` | `test_web.py` |
| Two-pane review | **exists** | `web/routes/review.py` | `test_web.py::test_review_*` |
| Outbox | **exists** | `web/routes/outbox.py` | `test_sending.py`, `test_web.py` |
| Excel import | **exists** (browser + CLI; .xlsx/.xls) | `ingest/excel_import.py`, `web/routes/imports.py` | `test_excel_import.py` |
| Dashboard with cost | **partial** — reports cost per usable lead from AI batch spend, hold rate, queue health. No manual cost entries, no reply/qualified metrics. | `web/routes/dashboard.py` | `test_web.py` |
| Ops worker / health / backups | **exists** — but `scripts/launchd.sh` and `notify_macos` are macOS-only | `ops.py` | `test_ops.py` |
| Verifier | **exists** | `pipeline/verify.py` | `test_verify.py`, `test_outreach.py` |

### Items the prompt said appear missing (grep-confirmed)

| Item | State | Evidence |
|---|---|---|
| Public `/u/<token>` unsubscribe route | **missing.** The footer of every rendered draft carries `…/u/<token>` and the `List-Unsubscribe` header points at it, but no route serves it, and `APP_BASE_URL` defaults to `app.mazzsmartlead.example`. **An email sent today would carry a dead unsubscribe link.** | no `/u/` route in `web/` |
| Follow-up steps | **missing** | no sequence/step model or code |
| Reply sync (`gmail.readonly`) | **missing** | only `gmail.send` scope anywhere |
| Reply classification + inbox UI | **missing** | — |
| Bounce DSN handling | **missing** | — |
| OOO pause/resume | **missing** | — |
| More than one login | **missing** — single `REVIEW_USER` from env | `web/auth.py` |
| Removal / never-contact admin page | **missing** — suppression is CSV/CLI only | — |
| `SEND_ENABLED` kill switch | **missing** — `EMAIL_SENDER=stub|google` is the only switch | — |
| Cost per qualified conversation | **missing** | — |
| Smartlead state import | **missing** | — |
| DNS verification script | **missing** | — |

### Further gaps found while auditing (not on the prompt's list)

1. **Delete-by-person leaves no suppression entry.** `privacy.delete_person`
   keeps an *existing* suppression row but never creates one, so an erased
   person re-imported from the next ZoomInfo pull walks straight back in.
2. **Postal address is one global env var** (`SENDER_POSTAL_ADDRESS`), with no
   record of who confirmed it. The client needs it per workspace.
3. **Footer text does not match the client's copy** — it lacks "If this isn't
   relevant, just let me know and I won't follow up."
4. **Email 1 wording does not match the approved V1 copy.** The branch renders
   "Quick question — is {company} currently looking…" with no signature; the
   approved copy is "Quick question: is your firm currently looking…" signed
   "Sean Gough / Search Site Marketing, Inc".
5. **Suppression has no workspace scope.** Every row is global; "Sean's current
   clients" must not suppress those companies for ClientRise.
6. **Sending is paced on the UTC day, with no business-hours window.**
7. **Enrichment that is blocked by the site holds the lead** (retries, then
   `held`). The client's rule is a fallback: legal-services industry + law-firm
   name → qualified; otherwise skipped.

### What the client was told vs what exists

The client was told the app already has an **unsubscribe page, a reply inbox and
two-step follow-ups. None of the three existed on this branch.** They are built
in WP2 (unsubscribe) and WP3/WP5 (follow-ups, inbox); see the status at the end
of this file for what is actually live.

---

## Proposed CLAUDE.md patch (NOT applied — Sam decides)

```diff
-## Primary constraint: COST PER USABLE LEAD
-Cost is the stated primary objective — not feature parity, not eliminating
-subscriptions for their own sake. Every build-vs-buy call gets made on measured
-cost per usable lead, and "keep paying the third party" is a valid answer.
+## Primary metric: COST PER QUALIFIED CONVERSATION (client's choice, 2026-09-29)
+Qualified = a genuine response from a decision-maker in the campaign's
+vertical showing enough interest to discuss the services, tagged by a human.
+Cost per qualified conversation = month's recorded costs ÷ conversations tagged
+qualified that month. Cost per usable lead stays as a secondary number.
+"Keep paying the third party" remains a valid build-vs-buy answer.

-**Sending is ours now.** As of 2026-09-21 the Smartlead client is deleted, ...
+**Sending is ours; Smartlead is still paid for.** Approved mail goes out of
+the client's own Google Workspace mailboxes over the Gmail API, authorised per
+mailbox with OAuth (Zapmail Custom OAuth), not a service account. The Smartlead
+subscription stays: it hosts warmup and the SmartSenders mailboxes, which
+cannot be transferred. Report it in costs; it is not zero.

-| ~~Smartlead subscription~~ (removed 2026-09-21) | ...
+| Smartlead subscription (kept: warmup + SmartSenders) | … re-measure … |

 ## Non-negotiable guardrails
-2. **An unconfirmed premise means no email.** ...
+2. **An unconfirmed premise means no email.** For template campaigns the gate
+   is vertical confirmation (prospect's own site, or — when the site is
+   blocked/unreachable — legal-services industry AND a law-firm company name),
+   not signal strength. Unconfirmed = `no_angle` with skip reason
+   `vertical_unconfirmed`, counted and listed. Generic fallback stays OFF for
+   Search Site Marketing.
+7. **Kill switch.** `SEND_ENABLED` (default false) is checked in the send path;
+   false renders MIME to `out/dryrun/` and nothing leaves.

 ## Work order
-1. **AI cost engineering** — ...
+V1 sends fixed templates with no model calls (`DRAFT_COMPOSER=template`).
+AI cost engineering is deferred. Order now: 1. launch readiness (auth,
+unsubscribe, suppression, kill switch), 2. reply handling (sync,
+classification, inbox, stop rules), 3. follow-ups and pacing, 4. metric,
+5. AI cost work if and when a model returns to the cold path.

-Missing: multi-step campaigns · lead distribution · activity-based
-stop/continue · unified inbox · reply classification · APIs/webhooks ·
-multi-client workspaces.
+(Refresh the Built/Partial/Missing lists from the status table in
+docs/LAUNCH-PLAN.md once launch-v1 merges.)

 ## Stack
-· Gmail API via a service account with domain-wide delegation (`.[google]`
-extra, `gmail.send` scope only)
+· Gmail API via per-mailbox OAuth (`gmail.send` + `gmail.readonly`), refresh
+tokens encrypted at rest with a key from env
```

---

## Pacing (WP4) — the numbers the dashboard shows

* Cap: 15 sends / mailbox / business day × 4 mailboxes = **60 sends/day**,
  **300/week** (Mon–Fri). Caps apply to every send, follow-ups included.
* A lead consumes up to **3 sends** (Email 1 + FU1 + FU2). Steady-state intake
  at 60/day is therefore **~100 new leads/week**, not 300.
* 300 new leads/week needs up to **180 sends/day**: 45/mailbox on four
  mailboxes (not advisable for burner domains), or **12 mailboxes at 15/day**,
  or **9 at 20/day**. Caps are NOT raised to hit 300; that is a buying
  decision (more mailboxes), not a config change.
* 864-lead list, 641 not started (2026-10-06, and falling while Smartlead
  still sends): at ~100 new/week that is **~6.4 weeks** to start everyone,
  plus ~1.5 weeks for the last follow-ups to drain.
  The dashboard computes "days to drain" live from the queue.

---

## Work packages (adjusted to what exists)

Ownership follows `.claude/agents/`. Implementation was done in one session
rather than by six concurrently-running agents: every agent's tests truncate a
shared Postgres (`_shared-rules.md` rule 3) and the work packages touch
overlapping files (models, send path, templates), so parallel agents would
have corrupted each other's runs. `compliance-auditor` is run as the
independent gate (WP9).

* **WP1 Users and attribution** — `users`, `user_workspaces`, `audit_events`;
  per-user login; workspace scoping on every lead-bearing route.
* **WP2 Compliance plumbing** — per-workspace postal address + confirmation;
  footer line; `/u/<token>` GET/POST; host isolation; placeholder guard at
  approve; removal page; delete leaves a suppression tombstone (email +
  sha256); scoped suppression rows; `SEND_ENABLED` kill switch with dry-run
  MIME.
* **WP3 Campaign gate and sequence** — per-campaign step templates (the
  approved V1 copy), vertical gate with blocked-site fallback and
  `skip_reason`, ZoomInfo mapping + required first/last/email, per-company
  cap, follow-up scheduling on business days in the lead's timezone, send
  window.
* **WP4 Pacing** — caps on all sends, bounce auto-pause, domain alternation,
  drain maths on the dashboard.
* **WP5 Reply sync and inbox** — OAuth credential provider (encrypted refresh
  tokens), connect screen, reply sync, deterministic classification, stop /
  OOO rules, inbox UI with tags and age timer.
* **WP6 Metric** — `cost_entries`, cost per qualified conversation.
* **WP7 Cutover** — Smartlead CSV imports (suppression, campaign state,
  exclusions), `legacy_contacted` mapping.
* **WP8 Deploy** — host recommendation, `verify_dns.py`, `rehearsal.py`.
* **WP9 Audit gate.**

---

## Client answers (launch checklist, returned 2026-10-06)

Settled, and matching what is built:
* **Postal address:** "Search Site Marketing, Inc, 6 Rockford Drive, West
  Nyack, NY 10994" — Sean's new address, NOT the Blauvelt one on the live
  Smartlead mail. Confirmed by Maaz Nadeem. Enter it on the workspace screen
  after deploy (the confirmation is recorded against whoever is logged in, so
  Maaz should press confirm himself).
* **Hosting** under ClientRise. DNS record for
  `outreach.getsearchsitemarketing.com`: yes, Maaz adds it.
* **Pacing:** 15/mailbox/day × 4 = 60/day, max 2 per company, Mon–Fri
  business hours in the lead's timezone, Eastern if unknown. Don't raise caps.
* **Proof points:** none at launch. 32 / 41 / 42@$70 / 50+ / "1–3 clients a
  month" are all barred until Sean verifies one.
* **Reply rules** approved exactly as built: Maaz owns the inbox and the
  software never marks anyone interested; a genuine reply stops the sequence;
  OOO pauses until the return date, else +7 days; bounce and unsubscribe
  suppress at once. Removals go to maaz@clientrise.io, and Sean forwards any
  he gets.
* **Logins:** maaz@clientrise.io (all workspaces), sean@searchsitemarketing.com
  (Search Site Marketing only) — matches `users.LAUNCH_USERS`.
* **Never-contact domains:** the five own domains, nothing more.
* **Smartlead, verified 2026-10-06, campaign still ACTIVE:** 864 total, 223
  contacted, 641 not started, 120 in progress, 101 completed, 2 blocked, 3
  bounced, 11 unique replies, 0 positive, 606 of 3,420 sends (≈4 steps per
  lead in Smartlead; ours is 3, and 3+ received maps to complete). Now in
  `smartlead.EXPECTED` and `metrics.BASELINE`; both move until the pause.

Still open:
* Zapmail written confirmation (not asked yet).
* Google OAuth app — nothing created yet. Steps 1–4 (project, External
  consent screen, the two scopes, In production) do not need the redirect
  URI; only the client ID does, and Google lets it be added later.
* Render account (ClientRise's card) → deploy → redirect URI
  `<APP_BASE_URL>/oauth/google/callback` and the DNS record go to Maaz.
* Where DNS for getsearchsitemarketing.com is managed.
* Smartlead files A, B, C; Sean's client / never-contact list.
* Sean's test inbox (Maaz's: maaz@clientrise.io); Sean's final copy approval.
* Monthly costs — client says enter nothing until billed amounts are
  verified. Dashboard shows "—" meanwhile.
* No cutover date until authorization, test sends, auth checks and import
  validation pass. Replacement warmup must exist before Smartlead warmup
  is ever turned off.
* ClientRise: blocked until Search Site Marketing is live.

---

## Status as built

### WP2 leftovers (done)
* `scripts/secret_scan.py` + `.githooks/pre-commit`. Enable per clone:
  `git config core.hooksPath .githooks`.
* `.env.example` lists `SEND_ENABLED`, `UNSUBSCRIBE_BASE_URL`, `UNSUBSCRIBE_HOST`.

### WP3 campaign gate and sequence (done)
* `python -m mazz.users seed-launch` now also creates the campaign
  "Search Site Marketing, law firms" (`mazz/launch_copy.py`): the exact V1
  Email 1 / FU1 (+3 days) / FU2 (+4 days) as `sequence_steps`, zero proof
  points, generic fallback off, and the own domains on the global never-contact list.
* Vertical gate: confirmed by attorney / law firm / practice areas / LLP /
  PLLC / P.C. on the prospect's own pages. Blocked (robots, 401/403)
  unreachable on the last retry → qualify from the lead row only if industry
  is legal AND the company name is law-firm-like; cited as `lead_record`.
  Otherwise `no_angle` with `skip_reason=vertical_unconfirmed`, counted on
  the dashboard and listed at `/review?status=no_angle`.
* Import: first name, last name, email required (a full name is split if
  that is all there is). ZoomInfo headers and state mapped. Dedupe on email.
  A re-import of anyone with a sequence started queues nothing.
* Email 1 at most once: the draft stage refuses any lead already emailed.
* Per-company cap: `campaigns.max_active_per_company` (default 2). Email 1
  for a third colleague is deferred a day; the dashboard shows how many are
  held and flags it above 10% of the list.
* Follow-ups: a send queues a `followup` job at its due date (business-day
  rolled, lead's timezone). It writes a draft and returns the lead to review;
  approval and release stay one human click per email. Same mailbox, same
  Gmail thread, `In-Reply-To` / `References`, subject "Re: Quick Question".
* Send window: Mon–Fri 9:00–17:00 in the lead's timezone (state, else
  Eastern). Outside it a send is deferred, not failed. Holidays not modelled.
* Follow-ups sort ahead of new leads in review and the outbox. Caps are not
  reserved for them (both share the 15/mailbox/day).

### WP4 pacing (done)
* Caps count every send from `sent_messages`, follow-ups included; a
  follow-up is pinned to the mailbox that owns its thread and waits on that
  mailbox's cap.
* `mailbox.record_bounce` (called by reply sync, WP5): hard bounce →
  global suppression (email + sha256), sequence stopped; mailbox auto-paused
  on any 5.7.x, or above 3% hard bounces over its last 50 sends (denominator
  fixed at 50, so the 2nd bounce pauses). A 5.7.x at submission pauses too.
  Un-pausing is a human action on /mailboxes. Dead credentials still disable.
* Rotation alternates the two sending domains.
* Dashboard: "Days to drain the list" = remaining sends (3 per unstarted
  lead, the rest of each live sequence) ÷ daily capacity, plus new
  leads/week the caps sustain. Caps were not raised.

### Audit of WP1–WP3 (compliance-auditor, 2026-10-01)
Fixed: a sent draft can no longer be edited, re-approved or sent again (HIGH);
every drafting path (incl. the Batch runner, now off unless
`DRAFT_COMPOSER=claude`) refuses Email 1 for an already-emailed lead; the
blocked-site fallback applies to `ProviderBlocked` only, an outage still
retries and holds; Sean no longer sees global suppression rows nor adds
global domain blocks; delete-by-person covers `sent_messages` and erases
dry-run `.eml` files; credential-shaped JSON files are gitignored.
Open (low): outbox header lists every workspace's mailboxes to scoped users;
outbox advisory text omits the placeholder check (send still refuses);
`sent_outbox.jsonl` (stub sender, dev only) is not erased on delete.

### WP5 reply sync and inbox (done, unverified against live Google)
Built and tested against a faked Gmail. Nothing here has touched Google or
Zapmail: gates 2 and 3 are what prove it.

**Credentials.** The service-account / `GMAIL_SA_*` path is gone. Each mailbox
has an encrypted OAuth refresh token (`OAUTH_TOKEN_KEY`, Fernet) on its row;
the Gmail client refreshes access tokens itself. Scopes: `gmail.send` +
`gmail.readonly` only. `EmailSender`, the stub and `service_factory` are
unchanged. A revoked/expired grant (`invalid_grant`) marks the mailbox
`needs_reconnect`, stops it sending and reading, and shows on `/mailboxes`;
it can only be revived by actually reconnecting (the status button refuses).

**Zapmail Custom OAuth (read from docs.zapmail.ai/custom-oauth, not guessed).**
`POST https://api.zapmail.ai/api/v2/mailboxes/custom-oauth`, header
`x-auth-zapmail`. Body: `{"google": {"appName", "clientId",
"mailboxesPerDomain": {<domainId>: [{"mailboxId": <Zapmail UUID>,
"oauthLink": <URL>}]}}}`. WE generate each `oauthLink` (our client id,
redirect URI, per-mailbox single-use `state`); Zapmail carries the mailbox
through consent and Google redirects to OUR callback, where we exchange the
code and verify which account consented. `/mailboxes/<id>/connect` shows each
mailbox's link. Zapmail rate-limits the call to 3 per mailbox per 7 days.
Their example link requests `https://mail.google.com/`; ours does not.
Still needed from a human: Zapmail API key, each mailbox's Zapmail UUID and
domain id, the `appName` registered with Google, and Zapmail's confirmation
(gate 2) that OAuth sends alongside Smartlead warmup are permitted.

**Google OAuth app status (checked against Google's help page).** An External
app in "Testing" issues refresh tokens that expire **7 days** after consent, so
it must be set to **In production**. An unverified app shows the "unverified
app" screen and is capped at **100 new users in total, for the life of the
project** (we need 4). That page does not say which scope is "sensitive" vs
"restricted"; my understanding is gmail.send is sensitive and gmail.readonly
restricted, which only matters if the app is ever opened to more than the cap
— confirm before relying on it. A Workspace admin can also block third-party
apps outright, which is a Zapmail-tenant question (gate 2).

**Sync.** `python -m mazz.pipeline.replies` or the worker (every 5 min, only
when `EMAIL_SENDER=google`). Incremental via `users.history.list` (INBOX,
messageAdded); first sync or an expired cursor (404) falls back to scanning
threads sent in the last 14 days plus recent DSNs. Only mail on threads we
started is stored. A token that reads as a different account is refused.

**Rules (all deterministic, no model).** Bounce (DSN): matched to the sent
message by Message-ID, else failed recipient; hard (5.x.x, not 5.2.2) →
`record_bounce`. ooo (Auto-Submitted / X-Autoreply / Precedence: auto_reply /
subject) → pause, resume 9:00 local the business day after the stated return
date, else +7 days; max 2 pauses per lead. unsubscribe_request → global
suppression at once, sequence stopped, flagged for a person to confirm.
negative (short, clear "not interested") and everything else → sequence
stopped; a follow-up in review/outbox is held. Quoted history and our own
footer are stripped first, otherwise every reply "contains unsubscribe". The
send step re-checks the sequence (stopped → held; paused → deferred) after
approval.

**Inbox** (`/inbox`, scoped to the user's workspaces): classification, age,
human tags interested / qualified / meeting booked / meeting showed / client
won (attributed, dated, reversible), handled, confirm-unsubscribe. An
interested reply unhandled for >2 business hours (Mon–Fri 9–17 Eastern) is
highlighted. The system never sets `interested`.

**Not built.** Canned replies (the optional last item). Lifting a mistaken
auto-unsubscribe (today: ask, or delete the suppression row by hand).
Attachment-borne DSN parts are not fetched (Gmail's inline text + headers are
enough for Google-hosted senders; a DSN we cannot match is logged, not
suppressed). `/outbox` and `release` do not yet show a stopped/paused lead as
blocked before the click; the send step refuses either way.

### Audit of WP5 (compliance-auditor, 2026-10-01)
No critical or high findings. Fixed: reply text is capped at 50 KB before any
pattern runs and the attribution pattern is single-line bounded (a hostile
multi-megabyte reply was a poison message that would have stalled the worker
every cycle); only Google's own `invalid_grant` counts as a revoked token (a
token-endpoint outage no longer flags every mailbox `needs_reconnect`); the
send step locks and re-reads the lead so a reply committed by the sync is
seen; delete-by-person also erases replies the person wrote on someone else's
thread, and bounce audit entries no longer carry the address; the OAuth
callback turns a failure to confirm the account into a refusal, not a 500.
Open: a reply landing in the milliseconds between the attempt marker commit
and the wire still leaves (closing it means holding a transaction across the
Gmail call); a false-positive unsubscribe/negative has no reversal route (the
keyword rule is broad, e.g. "please don't stop emailing me" suppresses);
an `Auto-Submitted` header outranks "remove me" (classed OOO).

### WP5 audit items closed (2026-10-05)
* An opt-out outranks auto-reply headers: "remove me" from a client that
  stamps `Auto-Submitted` is classed `unsubscribe_request` and suppresses.
* Lifting a false-positive auto-unsubscribe: operator only, on the inbox
  item, typed `LIFT` to confirm, audited as `lift_suppression`. It removes
  only the global row reply sync made from that reply's wording (`added_by =
  system:reply-sync`); a row a person, an import, a bounce or the unsubscribe
  link added is never lifted, nor this one if the person also used the link.
  The sequence stays stopped.
* `/outbox` shows a stopped or paused lead as blocked, and release refuses it
  (stopped: held; paused: left approved until the pause ends).
* Still open (low, accepted): outbox mailbox header visible to scoped users,
  `sent_outbox.jsonl` not erased on delete, the attempt-to-wire race.

### WP6 metric and dashboard (done)
* `cost_entries` (migration `b9f6d7e8a0c1`): workspace, month (first day,
  DB-checked), category, amount (≥ 0, DB-checked), note, `entered_by`.
  Categories: mailboxes/SmartSenders, Google Workspace seats, domains,
  Smartlead subscription, ZoomInfo allocation, hosting, other.
* `/costs` (operator only): add / delete entries per month, both attributed
  in `audit_events`. Amounts parsed to cents, 0–10M.
* `mazz/metrics.py`: cost per qualified conversation = month's costs ÷ leads
  tagged `qualified` in that month (by tag date). Secondary: sent, contacted,
  reply rate (OOO excluded; DSNs are never replies), bounce rate, qualified,
  booked, showed, won, skipped-unconfirmed, cost per contacted lead. A
  missing denominator or no costs entered prints "—", never $0.
* `/dashboard` (operator only) leads with that panel, filterable by month and
  workspace, beside the client's Smartlead baseline (133 contacted, 6 replies,
  3 bounces, 0 positive, 0 deals). No open or click tracking.
* Workspace-scoped users (Sean) reach neither `/costs` nor `/dashboard`.
* Tests: `tests/test_metrics.py` (by-tag-date counting, undefined-not-zero,
  workspace scope, month and amount parsing, DB constraints, the costs screen
  incl. audit, dashboard rendering).

### WP7 cutover from Smartlead (built; not run against real exports)
`python -m mazz.ingest.smartlead` (CLI only). Every command is a dry run
unless given `--commit`; it does the work in a transaction and rolls it back.

| Command | Export | Effect |
|---|---|---|
| `suppression <csv>` | (a) unsubscribes + bounces | global email suppression (`public.suppress_email`) |
| `exclusions <csv> --workspace <slug>` | (c) Sean's clients, named companies | suppression scoped to that workspace; a row with a company name but no domain/email is listed, not guessed |
| `leads <csv> --campaign <id>` | (b) campaign lead export | see below; prints counts, flags a mismatch with `smartlead.EXPECTED` (864 / 223 / 641 / 120 / 101 / 2 as of 2026-10-06) |
| `adopt` | — | adopts each live legacy lead's Gmail thread (read-only search), then queues its follow-up |

**Run order at cutover:** Maaz **pauses the Smartlead campaign first** →
exports file B and reads the counts off the paused campaign (update
`EXPECTED`) → `suppression` → `exclusions` → `leads` (each dry, then
`--commit`) → Maaz detaches the mailboxes from the campaign (gate 6) →
mailboxes connected (gate 3) → `adopt --commit`.

Pause before export, not after. The campaign was still sending on
2026-10-06 (contacted went 133 → 223 in a week). Anyone Smartlead emails
between the export and the pause is imported as "not started" and gets
Email 1 again, and `adopt` schedules from the step in the export, not from
what is in the thread. Use an early file B for a dry run only.

**Lead mapping** (`smartlead.NEXT_STEP_AFTER`, one dict Maaz can edit):
not started → the normal import path (suppression gate, routing pinned to the
campaign, enrich job). Contacted → lead at `sent` with a `legacy`
`sent_messages` row and **no job**: received 1 → next is FU1 (+3 days from the
last send), 2 → FU2 (+4 days), 3+ → `complete`; replied or blocked →
`stopped`; contacted with no sent count → `stopped`, `legacy_step_unknown`.
Suppressed (global or workspace) → not imported. Unknown status with no sent
count → not imported (it may have been emailed). A contacted row that fails
validation is printed as `MUST_FIX`: a later ZoomInfo import could otherwise
give that person Email 1. Re-running is idempotent.

Deviation from the plan: the plan said `sequence_step = N + 1` to make the
draft guard refuse Email 1. The code's meaning of `sequence_step` is "step of
the newest email", and `handle_followup` adds one, so N + 1 would skip a
step. It is set to the step already sent; the guard is the legacy sent row,
which `draft.persist_draft` already refuses on (`thread_parent`).

**Threads.** Smartlead sent from our four mailboxes, so we hold no Gmail ids.
`adopt` searches each connected mailbox `in:sent to:<email>
subject:"Quick Question"` with `gmail.readonly`, records every message of the
newest thread as legacy `sent_messages` (thread id, Message-ID, mailbox),
reads the thread once so a reply that landed before cutover stops the
sequence, then queues the follow-up at its due date. Not found, or found in
two mailboxes → `held` with `skip_reason` `legacy_thread_not_found` /
`legacy_thread_ambiguous`. Independently, `send.send_lead` refuses and holds
any follow-up whose parent has no thread id or Message-ID, so no unthreaded
"Re: Quick Question" can leave by any path. After adoption, reply sync sees
replies on those threads like any other.

**Research.** Legacy leads were never enriched. `handle_followup` now
researches the company first when it has no cache row (an outage retries; a
blocked site falls to the lead-record fallback), so the vertical gate applies
to follow-ups exactly as before.

Legacy sends are excluded from `metrics` (they are Smartlead's baseline).

**Unconfirmed until the real exports arrive** (`smartlead.COLUMNS`, one dict):
status `status` / `lead_status`; sent count `last_email_sequence_sent` /
`emails_sent` / `sequence_step` / `sent_count`; last sent `last_sent_time` /
`last_email_sent_at` / `last_sent_at` / `last_sent` (ISO 8601; adoption
replaces it with Gmail's date); replied `reply_count` / `replied` /
`is_replied` / `replies`; exclusion domain `domain` / `company_domain` /
`website` / `company_url`. Status values assumed: `NOT_STARTED` / `NOT_YET_CONTACTED`
(the only "fresh" ones), `INPROGRESS`/`PAUSED`, `COMPLETED`,
`BLOCKED`/`STOPPED`; anything else is legacy with a sent count, refused
without one. Lead fields (name,
email, company, website) go through the existing alias table. Run each
command dry against the real file first; a mismatch in the counts is the
signal a header is wrong.

**Audit of WP7 (compliance-auditor, 2026-10-05).** No critical. Fixed:
(HIGH) an ambiguous status ("Started") was treated as not started, and the
CLI committed regardless of mismatches. Now only an unambiguous "not
started" is fresh; an export with no recognisable status or sent-count
column aborts; `--commit` refuses (exit 2) on any count mismatch,
`MUST_FIX`, `refused` or `unknown_status` row unless `--accept-mismatch` is
given. (MEDIUM) the send step now refuses Email 1 for any lead with a sent
row on record, so an Email 1 drafted before the import can't leave; `adopt`
re-checks suppression and stops a lead suppressed since import instead of
queueing it; a contacted row that fails validation blocks the commit.
(LOW) the suppression read is in a savepoint; an out-of-office found at
adoption pauses and resumes instead of stopping; a blocked site found on a
follow-up is recorded on the cache row; Smartlead sends and replies to them
are kept out of our sent / reply-rate / dashboard numbers; the held message
names `ops resolve-send` rows too.
Open (low): `NEXT_STEP_AFTER` is a dict in code, as the plan asked, not a
DB setting.

Tests: `tests/test_smartlead_cutover.py` (state mapping, zero legacy leads
reach Email 1, suppressed/replied never queued, idempotent re-run, scoped
exclusions, an unadopted follow-up is held not sent, adoption threads FU1
under the Smartlead Message-ID from the same mailbox, holds the lost, stops
on a pre-cutover reply, a reply on an adopted thread stops the sequence,
an unresearched legacy follow-up is researched and fails closed, legacy sends
not counted in metrics, "Started" / missing headers never look fresh, the
commit gate, a workspace block keeps a contacted lead out, suppression after
import stops at adoption, an old Email 1 draft cannot be sent).

### WP8 deploy and verification (built)
**DNS** — `python scripts/verify_dns.py <domain>... [--selector X]`: exactly one
SPF record including `_spf.google.com`; a DKIM key at selector `google` (or
any `--selector`; reported per selector); a DMARC record, policy printed.
Exit 1 on any failure. Uses `nslookup -type=TXT`, no new dependency
(`dnspython` not added). **Run 2026-10-05: both getsearchsitemarketing.com
and searchsitemarketinghub.com pass** — SPF `include:_spf.google.com ~all`,
DKIM `google` key present, DMARC `p=reject` (aspf/adkim relaxed). That is
half of gate 5; the rehearsal headers are the other half.

**Rehearsal** — `python scripts/rehearsal.py --seeds a@x,b@y [--wait 120]`.
Refuses unless `SEND_ENABLED=true`, `EMAIL_SENDER=google` and seeds are
given. Sends one message per active connected mailbox (seeds round-robin,
never a mailbox to itself), then reads each copy back through the Gmail
reader and prints `Authentication-Results` when the seed is one of our
connected mailboxes; for an outside seed it says what to check by hand. No
lead, draft, job or `sent_messages` row is touched (these few messages are not
counted against the cap). Only the refusals are tested; it cannot run before
gates 2 and 3.

**Hosting: Render** (Fly.io would work the same way: two process groups in
`fly.toml`). The app is one FastAPI web process plus one long-running worker;
Vercel-style serverless cannot host the worker. `render.yaml` replaces
`scripts/launchd.sh` (deleted):

| Service | Start command |
|---|---|
| web | `uvicorn mazz.web.app:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"`, pre-deploy `alembic upgrade head`, health check `/health` |
| worker | `python -m mazz.ops worker` (confirmed in `ops.py`: SIGTERM finishes the current job; reply sync runs inside it only when `EMAIL_SENDER=google`) — exactly ONE instance |
| database | Render managed Postgres; `DATABASE_URL` from the database (`config.py` now maps `postgres://` to the psycopg 3 driver) |

Secrets are `sync: false` and entered in the Render dashboard. Sending ships
off (`SEND_ENABLED=false`, `EMAIL_SENDER=stub`). Point
`outreach.getsearchsitemarketing.com` at the web service and set
`UNSUBSCRIBE_HOST` to it (that host then serves only `/u/*` and `/health`);
`APP_BASE_URL` is the app's own host, and the Google OAuth redirect URI is
`<APP_BASE_URL>/oauth/google/callback`.
Not carried over from launchd: `ops backup` writes `pg_dump` to local disk,
which is ephemeral on Render. Use Render's managed Postgres backups instead,
and run `ops health --no-backup-check`. `health --notify` is a macOS
notification, so on a host the alerts have no channel yet (open: wire it to
email when it matters). Not verified against a Render account (no hosting
account yet — an input still needed).

Tests: `tests/test_deploy_scripts.py` (TXT parsing on recorded Windows and
Linux output, split DKIM strings, SPF/DKIM/DMARC failure cases, any-selector
pass, rehearsal refusals, no self-send, host database URL mapping).

### WP9 audit gate (compliance-auditor, 2026-10-05)
First pass (static read of the whole tree at `16fae95`): 0 critical, 1 high,
7 medium, 12 low. Fixed in this commit: **H1** — `SESSION_SECRET` defaulted to
the public "changeme" with nothing stopping boot (a forged cookie = operator);
the web app (lifespan) and the worker now refuse to start on a blank, default
or <32-character secret. **M6** — `SEND_ENABLED=true` on the stub sender
would book leads as sent and follow them up; the worker now refuses to start
unless `EMAIL_SENDER=google` (`config.deployment_problems`). Re-check: both
closed for every `render.yaml` entrypoint; residual L16 below.

Auditor's final summary, verbatim:

> No critical or high findings remain. All six CLAUDE.md guardrails and every
> WP9 item pass on the code. Suppression precedes the queue and fails closed.
> Unverifiable claims, unfilled placeholders and an empty subject are refused
> server-side at approve and again at send. Exactly one call site creates a
> send job, and the pipeline stops at verify. `SEND_ENABLED` defaults false
> and is checked before any sender exists. Delete-by-person is complete and
> leaves a tombstone. `/u/*` is isolated. Sean is scoped on every lead-bearing
> route. The rehearsal script is operator-run, gated and touches no lead, so it
> is not an auto-send path. The web app and the worker now refuse to start
> with a blank, default or short `SESSION_SECRET`, and the worker also refuses
> `SEND_ENABLED=true` on the stub sender. Six MEDIUMs remain: suppression added
> after ingest is enforced only at send time, because pipeline stages and
> follow-ups never re-check it and unsubscribe or removal does not stop a sent
> lead's sequence (M1); the "law firms" vocabulary confirms from a single weak
> term such as P.C. or LLP (M2); the blocked-site fallback needs Sam's
> ratification in CLAUDE.md (M3); unsubscribe tokens are tied to the session
> secret, so rotation kills links already sent (M4); host isolation depends on
> a blank `UNSUBSCRIBE_HOST` that nothing requires (M5); and the mailbox pool
> falls through to unpinned mailboxes and the login has no throttle or Secure
> cookie flag (M7). Thirteen LOWs are listed in the audit report. Recommended
> before go-live: fix M1 and M5 or accept them explicitly. The human gates
> remain open.

**Open, not fixed here (decision needed):** M1–M5, M7 as above. Lows: L1
rehearsal is an uncounted second `sender.send` site and does not check seeds
against suppression; L2 the one-send-site test misses a `"send"` literal,
`Job(kind=…)` and scripts; L3 release does not pre-check placeholders/footer;
L4 cross-workspace leaks to Sean (outbox mailbox roster, "N held" count on a
removal, person facts at a shared domain); L5 approve/reject have no status
guards; L6 placeholder regex misses >40-char or multi-line braces, a CR/LF
subject fails after the attempt marker; L7 opt-out keyword gaps ("delete my
data", "leave me alone"); L8 delete-by-person residue in audit free text,
`research_cache.last_error`, the Gmail folders, `sent_outbox.jsonl`; L9 the
Smartlead `replied` column is optional; L10 send jobs released while
`SEND_ENABLED=false` go live when it flips — **go-live checklist: no pending
send jobs before flipping**; L11 `/docs` public, O(n) `/u/` lookup, no CSRF
token, htmx without SRI; L12 "Approved — queued to send" banner and "one
click" footer wording; L13 dev seed scripts bypass the gate; L14 edited drafts
skip the entailment judge; L15 `render.yaml` Postgres IP allow-list
unverified; L16 `send_lead` itself has no stub+enabled refusal (left out on
purpose: the test suite runs that combination by design; the worker guard
covers production). Standing: the attempt-to-wire race; `NEXT_STEP_AFTER` in
code.

Tests: `tests/test_deploy_scripts.py::test_deployment_problems`,
`::test_the_app_and_worker_refuse_to_start_unsafe`.
