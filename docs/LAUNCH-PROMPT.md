# Claude Code prompt: launch the owned outbound platform for Search Site Marketing, then stage ClientRise

Paste everything below the line into Claude Code, run from the repo root.

---

You are taking the owned outbound platform from "built" to "sending for a real client". This prompt carries the client's launch answers (29 Sep 2026) and the current repo findings. CLAUDE.md and `.claude/agents/_shared-rules.md` still govern. If this prompt conflicts with CLAUDE.md, this prompt wins only on the items under "CLAUDE.md drift" and you do not edit CLAUDE.md yourself.

## Working rules

- Terse. No narration between tool calls. Explain reasoning only on non-obvious judgment calls.
- Branch `launch-v1`. One commit per work package. Paste real test output. Never claim green without running.
- This is multi-file and risky, so write the plan once (WP0), then keep going without asking, stopping only at the HUMAN GATES below.
- Do not rebuild what works. Use the six agents in `.claude/agents/` by file ownership. `compliance-auditor` gates every work package and never fixes.
- Do not invent: postal address, proof points, client names, call times, DNS results, credentials, claims. Missing means blocked.
- No real email leaves the system until the go-live gate passes. Add `SEND_ENABLED` (default false) as a global kill switch checked in the send path. When false, render MIME to `out/dryrun/` instead of sending.
- Secrets never enter git, logs or tests. Google OAuth client secret and API keys come from env or the host secret store. Per-mailbox refresh tokens are the one secret that lives in the DB, encrypted at rest with a key from env, never returned by any route or logged. Add a secret scan to pre-commit.
- Repo runs on Windows. `scripts/dev_db.sh` needs Git Bash, WSL or Docker. Pick whichever works and record it in README.
- Working tree shows CRLF-only diffs (`git diff --ignore-cr-at-eol --stat` is empty). Do not commit line-ending churn. Add `.gitattributes` with `* text=auto` if it is missing.

## Baseline: the real work is on an unmerged branch

`main` is behind. `origin/phase2-verifier-provenance-routing` (same tip as `origin/worktree-w6-operations`, 114 files) holds the sending rail. Start from it:

```
git fetch origin
git checkout -b launch-v1 origin/phase2-verifier-provenance-routing
```

Read `CLAUDE.md`, `docs/DECISIONS.md`, `docs/PHASE2.md`, `docs/CLIENT-MEETING.md`, `docs/CLIENT-REPLY-TEMPLATES.md` on that branch. Run `alembic upgrade head` and `pytest -q` against a local Postgres and paste the result.

What the branch appears to have (verify each against code and tests, do not trust this list):
workspaces, campaigns (per-campaign scoring, offer line, proof points, opt-in generic fallback), mailboxes, sent_messages, Gmail API sender with `gmail.send` only, rotation and daily caps, at-most-once send, approve separate from send, approval audit trail (`approved_by`), two-pane review, outbox, Excel import, dashboard with cost, ops worker/health/backups, verifier.

What it appears to lack (grep to confirm, then build): public `/u/<token>` unsubscribe route, follow-up steps, reply sync (`gmail.readonly`), reply classification and inbox UI, bounce DSN handling, OOO pause/resume, more than one user login, removal/never-contact admin page, `SEND_ENABLED` kill switch, cost per qualified conversation, Smartlead state import, DNS verification script.

The client was told the app already has an unsubscribe page, reply inbox and two-step follow-ups. If the code does not, say so plainly in `docs/LAUNCH-PLAN.md`.

## Client decisions (source of truth)

| Topic | Decision |
|---|---|
| Operator | Maaz Nadeem (ClientRise) runs outreach. Sean Gough (Search Site Marketing, Inc) is the first sender. |
| ICP and offer | Law firms. Offer line, exact: "We help law firms generate qualified client opportunities through search marketing." |
| Proof points | None in V1. Needs a named client plus permission. Client quoted 32 leads/month at about $70 CPA and 41 leads incl. PI. Live Smartlead copy says "50+" and "42 leads at $70 CPA". Figures disagree and none are named, so none are used. |
| Mailboxes | sean.gough@ and gough@ on getsearchsitemarketing.com; the same two on searchsitemarketinghub.com. Display name "Sean Gough" on all four. Two separate Google Workspace tenants (Zapmail via Smartlead SmartSenders). Domains bought 2026-09-02 and 2026-09-03. |
| Volume | Start 15/mailbox/day, 60/day total, Mon to Fri. Raise only by explicit config change. |
| Leads | ZoomInfo CSV/XLSX. Required: first name, last name, email. Keep company, website, title, employee count, location, industry. 864 in the live Smartlead campaign, another batch from other states coming. |
| Sequence | Email 1, Follow-up 1 at +3 days, Follow-up 2 at +4 days. Same thread. Every email individually approved. |
| Stop rules | Any genuine reply stops the sequence. Out-of-office pauses and resumes after the return date. Bounce and unsubscribe suppress. |
| Vertical unconfirmed | Skip the lead. Generic "businesses like yours" fallback stays OFF. |
| Replies | Maaz handles inbox and qualification. Target 2 business hours, same business day at the latest. Sean takes the qualified meeting. Humans decide "interested". OOO, unsubscribe and obvious negatives may be auto-categorized. |
| Logins | Maaz: maaz@clientrise.io. Sean: sean@searchsitemarketing.com. Separate accounts so approvals are attributable. |
| Success metric | Cost per qualified conversation. Qualified = genuine response from a law-firm decision-maker showing enough interest to discuss the services. Also track meetings booked, meetings showed, clients won. |
| Removal requests | Maaz owns them: maaz@clientrise.io. Sean forwards anything he gets. Suppress immediately. |
| Never contact | Every Smartlead unsubscribe and bounce, Sean's current clients, any company he names, plus own domains: searchsitemarketing.com, getsearchsitemarketing.com, searchsitemarketinghub.com, clientrise.io, clientrisehq.com. |
| Warmup | Done in Smartlead before launch and must keep running. We do not build warmup. |
| Mailbox auth | Gmail OAuth per mailbox, connected through Zapmail's Custom OAuth (Zapmail documents this for connecting its Google mailboxes to outside apps, using our own Google OAuth app/client ID and a per-mailbox authorization link). NOT service-account JSON with domain-wide delegation: Zapmail does not document that, and it is not confirmed as permitted. Do not build on `GMAIL_SA_*`. |
| Reply identity | Maaz handles replies but introduces himself as working with Sean / Search Site Marketing. He does not pose as Sean. |
| Hosting | Always-on host. `/u/*` public. App login for Maaz and Sean only. Unsubscribe host `outreach.getsearchsitemarketing.com` if DNS allows, else a dedicated subdomain we control. |
| Postal address | TBD per client. Never invent. The live Smartlead emails currently show "Search Site Marketing, Inc, 137 Erie St W, Blauvelt, NY 10913". Treat that as a candidate that Maaz or Sean must confirm, not as config. |

## CLAUDE.md drift (put a proposed patch in `docs/LAUNCH-PLAN.md`, do not apply it)

1. Primary metric is now cost per qualified conversation (client's choice). Cost per usable lead stays as a secondary number.
2. V1 sends fixed templates with no model calls (`DRAFT_COMPOSER=template`). The AI cost engineering work order is deferred. Launch readiness, reply handling and suppression lead.
3. Guardrail 2 for template campaigns: the gate is vertical confirmation, not signal strength. It still fails closed and the generic fallback stays off for this campaign.
4. The Smartlead subscription stays for now. It hosts warmup and the SmartSenders mailboxes, which cannot be transferred. "Keep paying the third party" is the right answer here. Report it honestly in costs.
5. The "Missing" list in CLAUDE.md is out of date against the branch.

## Copy: Search Site Marketing, V1 (exact)

Fields: `{first_name}` from the lead. Subject on Email 1 is "Quick Question". Follow-ups thread under it as "Re: Quick Question" using `In-Reply-To`/`References` and the Gmail `threadId`.

Email 1:
```
Hey {first_name},

Quick question: is your firm currently looking to bring in more clients?

We help law firms generate qualified client opportunities through search marketing.

Worth sending over a little more info?

Sean Gough
Search Site Marketing, Inc
```
No number, no proof sentence. (The live Smartlead Email 1 adds "We recently helped one client generate 50+ potential clients in a single month." Drop it.)

Follow-up 1 (+3 days):
```
Hey {first_name},

Just following up on my last email. Worth sending over a little more info?

Sean Gough
Search Site Marketing, Inc
```

Follow-up 2 (+4 days after FU1):
```
Hey {first_name},

This is my last note. If you want more info later, just reply and I'll send it over.

Sean Gough
Search Site Marketing, Inc
```

Footer, appended outside the composer on every email: postal address, the line "If this isn't relevant, just let me know and I won't follow up.", one-click unsubscribe link. Also send `List-Unsubscribe` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`.

The Smartlead screenshots also show a 4-step sequence (waits 2, 2, 3 days) and other bodies ("Just making sure this didn't get buried", "I'll assume bringing in more clients isn't a priority right now", and one with "42 leads in a month at a $70 CPA"). Reference only. Not approved for V1.

## Work packages

### WP0. Audit and plan (all agents, read only)
Produce `docs/LAUNCH-PLAN.md`: exists/partial/missing table for every item in "What it appears to lack" and every baseline item, each with file path and covering test; pytest output; proposed CLAUDE.md patch; the WPs below adjusted to reality. Then continue.

### WP1. Users and attribution (db-architect, review-app-engineer)
- `users` table and per-user login replacing the single `REVIEW_USER`. Seed Maaz (all workspaces) and Sean (Search Site Marketing workspace only). No shared login.
- `approved_by` already exists on leads. Extend attribution to reject, suppress, reply classification, qualified tag, cost entries (audit table with user_id, action, target, timestamp).
- Test: Sean cannot open another workspace's leads; every state change has a user.

### WP2. Compliance plumbing (pipeline-engineer, drafting-engineer, review-app-engineer)
- Postal address per workspace. Empty, placeholder-like (TBD, example, lorem, "123") or not marked confirmed makes the send path raise. Record who confirmed and when.
- Public routes: `GET /u/<token>` shows a confirm page, `POST /u/<token>` (one-click) suppresses immediately. Unsubscribe host serves only `/u/*` and `/health`; every other path returns 404 on that host.
- Placeholder guard: an unresolved `{...}` token or empty subject blocks approve server-side.
- Removal page (Maaz and Sean): add an email or domain to never-contact, immediately. Optional "delete this person" that calls the existing delete-by-person, with confirmation. Deleting a person must leave a suppression entry (normalized email plus sha256) so a re-import cannot resurrect them. Auditor tests this.
- Suppression is global (email and domain, subdomains included). Client-specific domain blocks are scoped rows. Read errors reject the lead.

### WP3. Campaign gate and sequence (pipeline-engineer, drafting-engineer)
- Campaign "Search Site Marketing, law firms": template composer, offer line above, zero proof points, generic fallback off. Confirm the code allows a campaign with no proof points and no model call.
- Vertical gate: confirmed if the company website shows a law firm (attorney, law firm, practice areas, LLP, PLLC, P.C.). If the site is unreachable or blocked, accept only when the lead's industry is legal services AND the company name matches a law-firm pattern. Otherwise `skipped_vertical_unconfirmed`, counted and listed in the report. Respect robots.txt, rate-limit per domain, record blocked sites, never bypass.
- Import validation rejects rows missing first name, last name or email. Map ZoomInfo columns. Dedupe on email.
- Per-company cap: default 2 concurrent active sequences per `company_domain`, configurable. Report how many leads it holds back; flag if above 10% of the list.
- Sequence steps: add a steps table (campaign, step, wait_days, template). Follow-ups enter the review queue when due. Due dates roll to the next business day. Send window Mon to Fri, business hours in the lead's timezone (from state), Eastern as fallback.
- Follow-ups schedule ahead of new leads. Approval stays per email; no approve-all, no scheduled approval.

### WP4. Pacing check (pipeline-engineer)
Caps apply to all sends, follow-ups included. Math to put in the plan and in the dashboard: 60 sends/day is 300/week. A lead uses up to 3 sends, so intake sustains about 100 new leads/week, not 300. 300 new/week needs up to 180 sends/day: 45 per mailbox on four mailboxes, or 12 mailboxes at 15/day, 9 at 20/day. Do not raise caps to hit 300. Surface "days to drain the list at current caps" on the dashboard.
- Auto-pause a mailbox on hard-bounce rate above 3% over its last 50 sends, or on any 5.7.x rejection. Already-dead mailboxes stay disabled (existing behavior).
- Rotation alternates the two domains.

### WP5. Reply sync and inbox (integrations-engineer, pipeline-engineer, review-app-engineer)
- Replace the service-account credential path in `email_sender.py` (`GMAIL_SA_<REF>`, `gmail.send` only) with a credential provider that uses per-mailbox OAuth: `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in env, refresh token per mailbox encrypted in the DB (`mailboxes` column or table, migration), access tokens refreshed on demand. Keep the `EmailSender` interface and the stub untouched so tests stay valid. Scopes: `gmail.send` and `gmail.readonly`, nothing broader.
- Connection flow: read Zapmail's current Custom OAuth docs first and implement their exact contract (app name, client ID, mailbox IDs, authorization URLs, redirect URI). Do not guess the endpoint shape. Add an authenticated admin screen per mailbox showing connected / needs reconnect, with the authorization link.
- Token failure (`invalid_grant`, revoked, expired): mark the mailbox `needs_reconnect`, stop sending from it, surface it on `/mailboxes`. Existing rule that a dead mailbox stays disabled applies.
- Check the Google OAuth app's publishing status. An External app left in "Testing" issues refresh tokens that expire after 7 days. It must be "In production" (unverified is acceptable at 4 mailboxes, with the consent warning, but confirm against Google's current rules and say what you found).
- Incremental sync with `users.history.list`; fall back to `threadId` listing.
- Deterministic classification, no model calls: `bounce` (mailer-daemon DSN, hard bounce suppresses), `ooo` (headers `Auto-Submitted`, `X-Autoreply`, `Precedence: auto_reply`, subject patterns), `unsubscribe_request` (keywords like "unsubscribe", "remove me", "stop emailing": suppress immediately and flag for human confirmation), `negative` (clear "not interested": stops the sequence), everything else `needs_review`. Human tags `interested`. The system never sets `interested` on its own.
- OOO: pause the sequence, parse a return date when present, resume the next business day after it. No date: resume after 7 days. Max 2 OOO pauses per lead.
- Inbox UI for Maaz: threads, classification, tag buttons (interested, qualified conversation, meeting booked, meeting showed, client won), age timer that highlights interested replies unanswered past 2 business hours.
- Optional and last: canned replies from `docs/CLIENT-REPLY-TEMPLATES.md` (slot fields DAY, TIME, TIMEZONE filled by a human, sent by a human click). Replies go out of the "Sean Gough" mailboxes. Decided: Maaz introduces himself as working with Sean / Search Site Marketing and never poses as Sean. Canned replies for this workspace open with a one-line intro to that effect ("I work with Sean at Search Site Marketing"), signed "Maaz Nadeem" with the client's company line, and the footer still applies. Adapt the PDF templates' "Founder, CLIENT NAME" wording accordingly.

### WP6. Metric and dashboard (review-app-engineer, db-architect)
- `cost_entries` (workspace, month, category, amount, note, entered_by). Categories: mailboxes/SmartSenders, Google Workspace seats, domains, Smartlead subscription, ZoomInfo allocation, hosting, other.
- Dashboard leads with cost per qualified conversation = month cost / qualified conversations (by date tagged). Also: sent, reply rate, bounce rate, qualified, booked, showed, won, skipped-unconfirmed count, cost per contacted lead (secondary).
- Baseline to show for comparison, from the client's Smartlead screenshot: 133 contacted, 6 unique replies (about 4.5%), 0 tagged positive, 3 bounces (0.9%), 0 deals. Open tracking is off in Smartlead (0 opens). Do not build open or click tracking.

### WP7. Cutover from Smartlead (integrations-engineer, pipeline-engineer)
No lead may get Email 1 twice, and no mailbox may be driven by both systems.
- Import from client CSV exports, not the Smartlead API: (a) global unsubscribes and bounces, (b) campaign lead export with status and whatever step/last-sent data it carries, (c) Sean's clients and named companies. Sanity counts to report: 864 leads, 133 contacted, 731 not started, 124 in progress, 7 completed, 2 blocked.
- Import contacted leads as `legacy_contacted` with steps received and last sent date. Mapping: received 1 email, next is FU1 once 3 days have passed; received 2, next is FU2; received 3 or more, sequence complete. Anyone who replied in Smartlead is stopped. Make the mapping a config so Maaz can change it.
- Test: zero `legacy_contacted` leads can reach Email 1. Zero suppressed or replied leads enter the queue.
- Human step for Maaz at cutover: detach the four mailboxes from the Smartlead campaign and pause it. Leave Smartlead warmup ON.

### WP8. Deploy and verification (integrations-engineer, pipeline-engineer)
- Hosting: the app is FastAPI plus Postgres plus a long-running worker (ops.py). Vercel serverless is a poor fit for the worker. Recommend Fly.io or Render (one web, one worker, managed Postgres) and confirm against the code. Replace `scripts/launchd.sh` with the host's process manager. Decide and document; do not spend a day on it.
- `scripts/verify_dns.py`: for both domains check SPF (includes Google), DKIM (try selector `google`, report others found), DMARC (exists, policy). Exit nonzero on failure. Zapmail may use a different selector; if so report it.
- `scripts/rehearsal.py`: sends one message per mailbox to seed inboxes Maaz and Sean name, prints `Authentication-Results` from received headers so spf, dkim and dmarc pass can be seen. Runs only when `SEND_ENABLED=true` and a `--seeds` list is given.
- Register both domains in Google Postmaster Tools (human step).

### WP9. Audit gate (compliance-auditor)
Run `/audit`. Must pass: suppression before queue with fail-closed reads, unverifiable claims blocked at approve, no auto-send path, delete-by-person complete and suppression preserved, footer outside composer, placeholder guard, kill switch, `/u/*` isolation, Sean scoped to his workspace.

## HUMAN GATES (stop and report; do not work around)

1. Postal address confirmed by Maaz or Sean, recorded in the workspace row.
2. Maaz creates a Google Cloud OAuth app (scopes `gmail.send`, `gmail.readonly`, publishing status In production) and shares the client ID and secret via password manager or restricted share only. Smartlead says SmartSenders mailboxes cannot be transferred away, so confirm with Zapmail support that running OAuth sends from our app alongside Smartlead warmup is fine.
3. All four mailboxes authorized through Zapmail's per-mailbox Custom OAuth link, each showing `connected` on `/mailboxes`, and one test send plus one read per mailbox succeeds.
4. The three Smartlead exports in WP7.
5. `verify_dns.py` passes and rehearsal headers show pass.
6. Mailboxes detached from the Smartlead campaign.
7. Sam says go. Then set `SEND_ENABLED=true` and start at 15/mailbox/day.

## Staged, not launched: ClientRise workspace (after Sean is live)

Add workspace "ClientRise", vertical "marketing agencies", reply owner Maaz. Load the copy below as a template set, status `blocked`. This is config, not new code, if WP1 to WP3 are done right.

Email 1 is the client's "Follow Up #2" from `Master PR Template.pdf`, sent first. Verbatim:
```
Hey {first_name}, we have been helping marketing agencies gain 1 - 3 more clients every month through project research and lead generation.

If we have the blueprint to help you do the same, are you open for a quick 15-min call on {day} at {time} {timezone} time to discuss this?

Best,

Maaz Nadeem
ClientRise
```
Proposed, not approved, seeded disabled: Email 2 = PDF "Follow Up #1", Email 3 = PDF "Follow Up #3". Maaz confirms order.

It stays blocked because:
- The claim "1 - 3 more clients every month" is an unnamed results claim, the same kind the proof-point rule blocks. Put it in a `sender_claims` record that requires an attestation (user, date, evidence note) before any email referencing it can be approved. Do not reword the client's copy.
- Subject line not provided. Empty subject blocks approve.
- `{day}`, `{time}`, `{timezone}` are filled by the reviewer per email from a real availability. Never auto-fill.
- No ClientRise sending mailboxes, sending domains or postal address were provided. Sean's four mailboxes cannot send as Maaz.

## Inputs still needed (list in the plan, do not guess)

Postal address (Search Site Marketing and ClientRise) · Google OAuth client ID and secret · Zapmail per-mailbox authorization for four mailboxes · Smartlead exports · Sean's client list and named exclusions · hosting account and DNS access for the unsubscribe host · decision on volume vs intake (WP4) · named client and permission for a proof point, with one reconciled figure · monthly costs for the dashboard · ClientRise mailboxes, domains, subject, claim attestation · a plan for warmup if Smartlead is ever cancelled.

## Report format

Per work package: what changed, files, exact pytest output, open gates. Final: go-live checklist with each gate marked met or unmet and who owns it.
