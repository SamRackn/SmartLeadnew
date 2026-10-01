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
* 864-lead list, 731 not started: at ~100 new/week that is **~7.3 weeks** to
  start everyone, plus ~1.5 weeks for the last follow-ups to drain.
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

## Inputs still needed (not guessed)

* Postal address for Search Site Marketing — candidate "Search Site Marketing,
  Inc, 137 Erie St W, Blauvelt, NY 10913" from live Smartlead mail, **needs
  Maaz or Sean to confirm**; and for ClientRise.
* Google OAuth client ID and secret (Maaz creates the app).
* Zapmail per-mailbox authorization for the four mailboxes.
* The three Smartlead exports (global unsubscribes + bounces; campaign lead
  export with status/step; Sean's clients and named companies).
* Sean's client list and named exclusions.
* Hosting account and DNS access for the unsubscribe host.
* Decision on volume vs intake (see pacing).
* A named client plus permission for any proof point, with one reconciled
  figure (32 / 41 / 42 / 50+ currently disagree).
* Monthly costs for the dashboard.
* ClientRise mailboxes, domains, subject line, postal address, claim
  attestation.
* A plan for warmup if Smartlead is ever cancelled.

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
