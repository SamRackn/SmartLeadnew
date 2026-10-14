# Project: Owned Outbound Platform (replacing Smartlead)

## What this is
Software the client owns and operates for outbound sales, replacing his
dependence on Smartlead. Smartlead is a **reference for required business
capabilities, not a thing to clone feature-for-feature.**

Supersedes the original brief, which scoped this as a research/personalization
layer feeding Smartlead as the sending rail. That brief is in git history
(commit 66fd0be) if the earlier reasoning is ever needed.

**Sending is ours; Smartlead is still paid for.** Approved mail goes out of
the client's own Google Workspace mailboxes over the Gmail API, authorised per
mailbox with OAuth (Zapmail Custom OAuth), not a service account. The Smartlead
subscription stays: it hosts warmup and the SmartSenders mailboxes, which
cannot be transferred. Report it in costs; it is not zero. Launch state,
client answers and the go-live gates live in `docs/LAUNCH-PLAN.md`.

## Primary metric: COST PER QUALIFIED CONVERSATION (client's choice, 2026-09-29)
Qualified = a genuine response from a decision-maker in the campaign's
vertical showing enough interest to discuss the services, tagged by a human.
Cost per qualified conversation = month's recorded costs ÷ conversations tagged
qualified that month (`mazz/metrics.py`, `/dashboard`). Cost per usable lead
stays as a secondary number. Cost is still the objective — not feature parity,
not eliminating subscriptions for their own sake — and "keep paying the third
party" remains a valid build-vs-buy answer.

### The cost hierarchy (measured, see docs/GAP-ANALYSIS.md)
At 1k / 10k / 100k leads per month:

| | 1k | 10k | 100k |
|---|---|---|---|
| **AI research + personalization** | **57%** | **70%** | **70%** |
| Mailboxes + domains + verification | 13% | 16% | 17% |
| Smartlead subscription (kept: warmup + SmartSenders) | 20% | 16% | 6% — re-measure |
| Compute, warmup | 10% | 8% | 7% |

**This table is now out of date and is kept for the reasoning, not the
numbers.** It was measured against event-based research feeding a per-lead
Opus draft. As of 2026-09-21 the cold path runs no model at all — email 1 is a
fixed template over a deterministic qualification (`docs/DECISIONS.md`) — so
the 57-70% AI line applies only to the deep-research path, which is now spent
on leads that REPLIED rather than on every cold list entry. Re-measure before
quoting a split. The reasoning stands: AI cost engineering outranks everything
else precisely because that is what made it worth removing from the cold path.
The Smartlead line is not zero: the subscription stays for warmup and the
SmartSenders mailboxes. Owning the rail adds compute and operational cost that
has not been measured yet. Re-measure in `docs/GAP-ANALYSIS.md` before quoting
a new split.

Cost per usable lead today: ~$0.19 at 1k, ~$0.11 at 10k, ~$0.064 at 100k.
Drive those down; report them honestly; never trade draft quality for them
without measuring the quality cost first.

## What we do NOT build, because buying is cheaper or the only option
- **Warmup** — a network effect requiring thousands of opted-in mailboxes.
  The client has five. Five mailboxes emailing each other is the pattern spam
  detectors exist to catch. Join or buy; never build.
- **Cheap SMTP for cold outreach does not exist.** Amazon SES at $0.10/1k
  prohibits cold outreach in its AUP and suspends accounts for it. Real sending
  runs on Google Workspace / M365 mailboxes on burner domains, rotated — the
  same mailboxes we'd buy either way.
- **Mailbox provisioning, email verification, deliverability/blocklist feeds** —
  commodity, no margin in self-supplying.

We DO build the orchestration on top: rotation, pacing, sequencing, inbox,
classification, routing, suppression, research, personalization.

## Required capabilities and current state
Built: lead import/management (CSV, Excel, ZoomInfo headers) · suppression,
removals page and public `/u/<token>` unsubscribe · domain-keyed research
caching · own company research engine (homepage, services, contact — 2
fetches/domain, no AI) · mailbox management (roster, caps, spacing, domain
rotation, bounce auto-pause, per-mailbox OAuth) · owned sending (Gmail API,
one human-approved message at a time, `SEND_ENABLED` kill switch) ·
three-step sequences (per-campaign `sequence_steps`, same thread, business
days, Mon–Fri 9–17 send window in the lead's timezone, per-company cap) ·
reply sync, deterministic classification (bounce, OOO, unsubscribe, negative)
and the `/inbox` with human tags · per-user logins and `audit_events` ·
workspaces with scoped users and scoped suppression · cost entries and cost
per qualified conversation on `/dashboard` · Smartlead cutover import ·
`ops preflight` go-live check.

Partial: company research (paid provider arm under evaluation) · email 1
(fixed per-campaign template, zero AI) · multi-client workspaces (scoping is
built; ClientRise's own workspace is staged, not launched).

Missing: canned replies · lead distribution · APIs/webhooks · holiday
calendar in the send window.

## Pipeline
```
ingest → enrich → qualify → draft → verify → human review → send
```
A lead arrives complete (name, company, site, title, email, headcount, revenue,
location, industry, LinkedIn URL, notes). No discovery step — enrichment only.

## Non-negotiable guardrails
These are compliance controls. They fail closed, and they are not softened for
throughput. All are built and audited.

1. **Suppression is checked before a lead enters the queue** — email and domain,
   subdomains included. A read error rejects the lead rather than admitting it.
2. **An unconfirmed premise means no email.** The copy calls the recipient
   something ("We help *law firms* …"), and that word has to be earned from
   the prospect's own homepage, services or contact page. A lead whose
   vertical `pipeline.qualify` cannot confirm lands `no_angle` with no draft
   written, as does one its own site disqualifies. There is no fallback to
   generic filler and no writing the sentence with the noun left vague — that
   is the exact failure the client is paying to escape.
   `DEFAULT_QUALIFICATION` ships with an EMPTY vertical vocabulary, so an
   unconfigured campaign writes to nobody rather than to everybody.
   **One documented exception**, opt-in per campaign and off by default:
   `campaigns.generic_vertical_fallback` sends "We help *businesses like
   yours* …" when no vertical is set. The phrase is vacuous rather than false
   — it asserts nothing about the recipient — so it waives the vertical gate
   and NOTHING else: intake evidence and disqualifiers still apply, and the
   review panel says plainly that no vertical was confirmed. A blank vertical
   without the flag still refuses to draft, loudly, because it is normally a
   setup gap and the error is what gets it filled in.
   For template campaigns the gate is vertical confirmation on the prospect's
   own pages (`min_vertical_matches` distinct terms; 2 for the launch law-firm
   campaign, so "LLP" alone does not make an accounting firm a law firm).
   When the site is **blocked or unreachable**, and only then, the lead may
   qualify from its own record if its industry is legal services AND its
   company name is law-firm-like (`fallback_industry_terms` +
   `fallback_name_terms`, cited as `lead_record`). An outage retries; it never
   falls back. Unconfirmed = `no_angle`, `skip_reason=vertical_unconfirmed`,
   counted and listed. Generic fallback stays OFF for Search Site Marketing.
   (Blocked-site fallback ratified 2026-10-14.)
   (Restated 2026-09-21. It previously read "weak signals mean no email",
   scoped to event research; against a list of nine-person law firms that gate
   refused to write to the entire addressable market. `docs/DECISIONS.md`.)
3. **Unverifiable claims are blocked, not flagged.** A claim that does not trace
   to a cited fact id, a `proof_points` row, or the lead's notes fails
   verification, holds the lead, and is refused server-side at the approve
   route. Hallucinated claims about another company in cold outreach are legal
   exposure, not a quality nit.
   Numbers have exactly one door into the copy: a `proof_points` row, which
   requires a named client. A digit in `campaigns.offer_line` is refused —
   that sentence reads as sender voice, so the verifier never holds its
   figures to anything, which would make a statistic typed there the strongest
   claim in the message with nothing behind it.
4. **No auto-send.** Every email is human-approved individually, and approving
   is not sending. No approve-all, no send-all, no scheduled approval, no retry
   path that approves. Now that we own the sending rail this is structural, and
   it is enforced in two places:
   - The pipeline chain stops at `verify`. Never add `enqueue_next("send")` to
     a pipeline stage.
   - The ONLY thing that creates a `send` job is one human pressing send on one
     message in `web/routes/outbox.py`. **Exactly one call site exists in the
     whole codebase and a test counts them**
     (`test_exactly_one_place_in_the_codebase_creates_a_send_job`). A second
     one appearing anywhere is the auto-sender arriving by increments, which
     is the only way it ever would.
   (Restated 2026-09-23. Approve used to enqueue the send itself, which fused
   "this copy is correct" and "put this on the wire now" into one click and
   left no instant in which an approval could be reconsidered. `web/routes/
   leads.py` no longer imports the queue or the send-job kind at all, so
   restoring the old behaviour means restoring an import — a visible line in a
   diff rather than one word changed inside an existing call.)
   The send step re-checks the approval, the verification verdict and
   suppression at the moment of sending, because all three can have changed
   since the click — and the release action re-checks them eagerly too, so the
   operator learns before the job runs rather than after it fails.
5. **Delete-by-person is complete** — leads, drafts, jobs, and person-scoped facts
   pruned from the shared company cache by `subject_email`.
6. **Footer carries a physical mailing address and one-click unsubscribe**, added
   outside the composer so no model can omit it. The address is per workspace
   and renders only once a named person has confirmed it.
7. **Kill switch.** `SEND_ENABLED` (default false) is checked in the send path
   before any sender exists; false renders the MIME to `out/dryrun/` and
   nothing leaves. Live sending (`SEND_ENABLED=true`, `EMAIL_SENDER=google`)
   refuses to boot without an https unsubscribe host isolated by
   `UNSUBSCRIBE_HOST`, an `UNSUBSCRIBE_SECRET`, and the OAuth settings
   (`config.deployment_problems`). Run `python -m mazz.ops preflight` before
   flipping it.

### Known gap in the guardrails
Verification proves a claim traces to retrieved evidence. It does **not** prove
the evidence is true. A scraper that misidentifies a filter control as a job req
puts a false fact in the cache, and the draft citing it passes verification
correctly. Research provenance needs a check at cache-ingest, not at draft time.

## Research and cost discipline
- Cache by **company domain**, never by person. 50 prospects at one employer must
  cost one company research operation. Proven: 2 colleagues → 1 row, 1 fetch.
- Prefer, in order: existing client data → company websites and permitted public
  sources → authorized APIs → licensed data. Paid per-record providers only where
  they measurably beat the free baseline.
- **Never** build anything to bypass CAPTCHAs, access controls, anti-bot measures
  or platform restrictions. Respect robots.txt, rate-limit per domain, identify
  honestly. A site that blocks us is recorded as blocked; we move on.
- A provider failure must **raise**, never return an empty list. Empty legitimately
  means `no_angle`, so a swallowed error silently tells the client there was no
  reason to reach out.

## Campaign routing
Client's workflow: **one queue, system determines campaign, segment and mailbox.**
Implement as a swappable policy, not a hardwired topology:
```
lead → RoutingPolicy.route(lead) → (campaign, segment, mailbox)
```
`SystemRoutingPolicy` is the default — segment from lead attributes, mailbox by
health, daily-cap headroom and rotation. One-campaign-per-mailbox must remain
possible without a migration.

## Data model
`leads` (+ indexed `company_domain`, status enum) · `research_cache` (PK company
domain, JSONB facts with stable ids, `subject_email` on person facts) · `drafts`
(`signals_used`, `verification_status`, `verification_notes`) ·
`suppression_list` · `jobs` (table-as-queue, `FOR UPDATE SKIP LOCKED`) ·
`workspaces` · `campaigns` (`vertical`, `offer_line`, `qualification_config`,
and the legacy `scoring_config`) · `proof_points` (the sender's own evidence:
`claim_text` verbatim, `client_name` NOT NULL, `occurred_on`, `source_note`,
`active` — the only source of a digit in the copy) · `mailboxes`
(the client's sending accounts, `daily_cap`, `min_spacing_seconds`,
`credential_ref` now vestigial; the Fernet-encrypted OAuth refresh token is
the one secret in the database) · `sent_messages` (one row per email that
actually left; the daily-cap denominator and the thread key replies match on)
· `sequence_steps` · `replies` · `users`, `user_workspaces`, `audit_events` ·
`cost_entries`.

## Stack
Postgres · Python · FastAPI + Jinja2 + HTMX (server-rendered; no SPA, no bundler)
· Claude via ANTHROPIC_API_KEY · Gmail API via per-mailbox OAuth (`.[google]`
extra; `gmail.send` + `gmail.readonly`, nothing broader), refresh tokens
encrypted at rest with `OAUTH_TOKEN_KEY` · Alembic · pytest · Render (one web,
one worker, managed Postgres; `render.yaml`).

**Sending is off by default.** `SEND_ENABLED=false` writes dry-run MIME and
sends nothing. `EMAIL_SENDER=stub` writes to `sent_outbox.jsonl` and touches
no network. Only `SEND_ENABLED=true` with `EMAIL_SENDER=google` sends, and a
credential appearing in the environment must never flip either — same rule as
`DRAFT_COMPOSER`, sharper consequence.

**Claude API specifics** (verified, several changed in 2025–26):
`claude-opus-5` default; structured output via `output_config.format` (the
`output_format` param is deprecated); adaptive thinking with `output_config.effort`
(`budget_tokens` returns 400 on Opus 5); no assistant prefill; handle
`stop_reason == "refusal"`. Use the **Batch API** for drafting — nothing is
latency-sensitive and it is 50% cheaper. Put the frozen system prompt first so
prompt caching actually hits; verify with `usage.cache_read_input_tokens > 0`.

## Agent team
Six agents in `.claude/agents/`, each owning a file set: `db-architect`,
`pipeline-engineer`, `drafting-engineer`, `integrations-engineer`,
`review-app-engineer`, and `compliance-auditor` (read-only gate, reports but
never fixes). Commands: `/phase1`, `/seed`, `/audit`.

Do not rebuild working systems. Do not duplicate another agent's files. Report
honestly — paste real test output, never claim green without running it.

## Work order
V1 sends fixed templates with no model calls (`DRAFT_COMPOSER=template`).
Launch readiness, reply handling and suppression lead; AI cost work returns
if and when a model returns to the cold path. Launch status:
`docs/LAUNCH-PLAN.md`.

1. **AI cost engineering** — largely DONE for the cold path, by deleting the
   spend rather than optimising it: email 1 costs zero tokens, and research
   dropped from six-to-eight fetches per domain to two. What remains is the
   deep-research path on replies (batching, prompt caching, model selection)
   and maximising the research cache hit rate.
2. **Verifier precision + research provenance** — still item 2, and the
   remaining work is now clearer: the cold path's claim surface is small and
   exactly known, so provenance-at-cache-ingest is the open piece. The
   guardrail gap above is unchanged by the cutover — a scraper that misreads a
   heading as a practice area puts a false fact in the cache, and the draft
   citing it passes verification correctly.
3. **Own research engine over paid providers** wherever the free baseline suffices.
4. **Sending platform** — mailboxes, rotation, caps, pacing and the send path
   are BUILT, and so are follow-ups, the inbox, reply classification and the
   send window (launch-v1). Deliverability is now our liability: warmup stays
   bought (in Smartlead), and SPF/DKIM/DMARC per burner domain is a standing
   obligation (`scripts/verify_dns.py`).
5. Multi-tenancy, APIs/webhooks, analytics — as the business actually requires.
