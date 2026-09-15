# Project: Owned Outbound Platform (replacing Smartlead)

## What this is
Software the client owns and operates for outbound sales, replacing his
dependence on Smartlead. Smartlead is a **reference for required business
capabilities, not a thing to clone feature-for-feature.**

Supersedes the original brief, which scoped this as a research/personalization
layer feeding Smartlead as the sending rail. That brief is in git history
(commit 66fd0be) if the earlier reasoning is ever needed.

## Primary constraint: COST PER USABLE LEAD
Cost is the stated primary objective — not feature parity, not eliminating
subscriptions for their own sake. Every build-vs-buy call gets made on measured
cost per usable lead, and "keep paying the third party" is a valid answer.

### The cost hierarchy (measured, see docs/GAP-ANALYSIS.md)
At 1k / 10k / 100k leads per month:

| | 1k | 10k | 100k |
|---|---|---|---|
| **AI research + personalization** | **57%** | **70%** | **70%** |
| Mailboxes + domains + verification | 13% | 16% | 17% |
| Smartlead subscription | 20% | 16% | 6% |
| Compute, warmup | 10% | 8% | 7% |

**This table sets the work order.** AI is the dominant cost at every scale, so
AI cost engineering outranks everything else. Eliminating the Smartlead
subscription is a 6–20% line item and does not lead.

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
Built: lead import/management · suppression & unsubscribe · domain-keyed
research caching · own company research engine (40% standalone signal coverage).

Partial: scheduling/pacing (queue primitive only) · AI personalization
(`Composer` seam, deterministic template behind it) · company research (live
adapter built; paid provider arm under evaluation).

Missing: mailbox management · multi-step campaigns · lead distribution ·
activity-based stop/continue · unified inbox · reply classification ·
performance tracking · APIs/webhooks · multi-client workspaces.

## Pipeline
```
ingest → enrich → score → draft → verify → human review → send
```
A lead arrives complete (name, company, site, title, email, headcount, revenue,
location, industry, LinkedIn URL, notes). No discovery step — enrichment only.

## Non-negotiable guardrails
These are compliance controls. They fail closed, and they are not softened for
throughput. All are built and audited.

1. **Suppression is checked before a lead enters the queue** — email and domain,
   subdomains included. A read error rejects the lead rather than admitting it.
2. **Weak signals mean no email.** Funding-only leads land `no_angle` with no
   draft written. There is no fallback to generic filler — that is the exact
   failure the client is paying to escape.
3. **Unverifiable claims are blocked, not flagged.** A claim that does not trace
   to a cited fact id or the lead's notes fails verification, holds the lead, and
   is refused server-side at the approve route. Hallucinated claims about another
   company in cold outreach are legal exposure, not a quality nit.
4. **No auto-send.** Every email is human-approved individually. No approve-all,
   no scheduled approval, no retry path that approves.
5. **Delete-by-person is complete** — leads, drafts, jobs, and person-scoped facts
   pruned from the shared company cache by `subject_email`.
6. **Footer carries a physical mailing address and one-click unsubscribe**, added
   outside the composer so no model can omit it.

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
`suppression_list` · `jobs` (table-as-queue, `FOR UPDATE SKIP LOCKED`).
To come: `mailboxes`, `campaigns`, `sequences`, `replies`, `workspaces`.

## Stack
Postgres · Python · FastAPI + Jinja2 + HTMX (server-rendered; no SPA, no bundler)
· Claude via ANTHROPIC_API_KEY · Alembic · pytest.

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

## Work order (cost-primary)
1. **AI cost engineering** — batching, prompt caching, model selection per task,
   maximise research cache hit rate. Largest line item, mostly already-owned code.
2. **Verifier precision + research provenance** — gates every AI-written email and
   closes the guardrail gap above.
3. **Own research engine over paid providers** wherever the free baseline suffices.
4. **Sending platform** (mailboxes, sequences, pacing, inbox, classification) —
   built for control and scale margin, on a slower clock, because it is a 6–20%
   cost line.
5. Multi-tenancy, APIs/webhooks, analytics — as the business actually requires.
