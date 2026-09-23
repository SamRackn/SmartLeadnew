# Project: Owned Outbound Platform (replacing Smartlead)

## What this is
Software the client owns and operates for outbound sales, replacing his
dependence on Smartlead. Smartlead is a **reference for required business
capabilities, not a thing to clone feature-for-feature.**

Supersedes the original brief, which scoped this as a research/personalization
layer feeding Smartlead as the sending rail. That brief is in git history
(commit 66fd0be) if the earlier reasoning is ever needed.

**Sending is ours now.** As of 2026-09-21 the Smartlead client is deleted, not
flagged off, and approved mail goes out of the client's own Google Workspace
mailboxes over the Gmail API (`docs/DECISIONS.md`). Smartlead remains a
reference for capabilities we have not built yet — sequences, unified inbox,
reply classification — and nothing else.

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
| ~~Smartlead subscription~~ (removed 2026-09-21) | ~~20%~~ | ~~16%~~ | ~~6%~~ |
| Compute, warmup | 10% | 8% | 7% |

**This table is now out of date and is kept for the reasoning, not the
numbers.** It was measured against event-based research feeding a per-lead
Opus draft. As of 2026-09-21 the cold path runs no model at all — email 1 is a
fixed template over a deterministic qualification (`docs/DECISIONS.md`) — so
the 57-70% AI line applies only to the deep-research path, which is now spent
on leads that REPLIED rather than on every cold list entry. Re-measure before
quoting a split. The reasoning stands: AI cost engineering outranks everything
else precisely because that is what made it worth removing from the cold path. The Smartlead line is now zero —
the client sends from his own mailboxes — which makes AI a LARGER share of
what remains, not a smaller problem. The measured percentages above are the
pre-removal ones and are deliberately not re-derived here: owning the rail
adds compute and operational cost that has not been measured yet. Re-measure
in `docs/GAP-ANALYSIS.md` before quoting a new split.

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
research caching · own company research engine (qualification pages: homepage,
services, contact — 2 fetches/domain, no AI)
· mailbox management (roster, daily caps, spacing, rotation, health) · owned
sending (Gmail API, one human-approved message at a time).

Partial: scheduling/pacing (per-mailbox caps and spacing enforced at send;
no send-window or time-of-day shaping) · company research (live adapter built;
paid provider arm under evaluation) · email 1 (fixed template, zero AI; a
per-campaign body template is the next step and needs the verifier to treat an
operator-written sentence as an untrusted claim) · performance tracking (`/dashboard` reports
cost per usable lead, hold rate, cache effectiveness and queue health).

Missing: multi-step campaigns · lead distribution · activity-based
stop/continue · unified inbox · reply classification · APIs/webhooks ·
multi-client workspaces.

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
4. **No auto-send.** Every email is human-approved individually. No approve-all,
   no scheduled approval, no retry path that approves. Now that we own the
   sending rail this is structural: the pipeline chain stops at `verify`, and
   the ONLY thing that creates a `send` job is one human pressing approve on
   one lead. Never add `enqueue_next("send")` to a pipeline stage. The send
   step re-checks the approval, the verification verdict and suppression at the
   moment of sending, because all three can have changed since the click.
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
`suppression_list` · `jobs` (table-as-queue, `FOR UPDATE SKIP LOCKED`) ·
`workspaces` · `campaigns` (`vertical`, `offer_line`, `qualification_config`,
and the legacy `scoring_config`) · `proof_points` (the sender's own evidence:
`claim_text` verbatim, `client_name` NOT NULL, `occurred_on`, `source_note`,
`active` — the only source of a digit in the copy) · `mailboxes`
(the client's sending accounts, `daily_cap`, `min_spacing_seconds`,
`credential_ref` — a NAME, never a key) · `sent_messages` (one row per email
that actually left; the daily-cap denominator, and the thread key replies will
match on).
To come: `sequences`, `replies`.

## Stack
Postgres · Python · FastAPI + Jinja2 + HTMX (server-rendered; no SPA, no bundler)
· Claude via ANTHROPIC_API_KEY · Gmail API via a service account with
domain-wide delegation (`.[google]` extra, `gmail.send` scope only) · Alembic ·
pytest.

**Sending is off by default.** `EMAIL_SENDER=stub` writes the real MIME to
`sent_outbox.jsonl` and touches no network. Only `EMAIL_SENDER=google` sends,
and a `GMAIL_SA_*` key appearing in the environment must never flip it — same
rule as `DRAFT_COMPOSER`, sharper consequence.

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
   are BUILT (2026-09-21, `docs/DECISIONS.md`). Still open on this item:
   sequences/follow-ups, the unified inbox, reply classification, and
   send-window shaping. Deliverability is now our liability: warmup stays
   bought, and SPF/DKIM/DMARC per burner domain is a standing obligation.
5. Multi-tenancy, APIs/webhooks, analytics — as the business actually requires.
