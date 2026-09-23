# Phase 2 — going live

Phase 1 proved the pipe on fixtures: ingest → enrich → score → draft → verify →
review → push, 224 tests, all six compliance guardrails enforced structurally.
Nothing in it has touched the open internet or spent a cent.

Phase 2 replaces four stubs with real systems. The order below is deliberate and
should not be reshuffled: **the verifier has to be fixed before a real LLM writes
anything**, for reasons in W1.

---

## W1 — Verifier precision (do this first)

**The problem.** The current verifier is token-level. It checks that every
significant word and number in the copy appears somewhere in the cited evidence.
That was adequate in Phase 1 because the template composer only ever quotes
retrieved facts verbatim, so nothing could fail.

A real Claude call paraphrases. Running the seed corpus through a hand-written
"LLM-style" draft, the verifier correctly caught the fabricated `40`, but it also
flagged `hiring`, `scaled`, `reps`, `since`, and `Series`. Ordinary English that
merely restates a cited fact in different words gets held.

**Why this is the first task and not a polish item.** If Phase 2 ships with a
verifier this strict, nearly every real draft lands in `held`. The client then
faces a review queue that is almost entirely false positives, and the rational
response is to stop reading the warnings and approve on sight. That converts the
strongest compliance control in the system into a rubber stamp — a worse outcome
than not having it, because it looks like it is working. Precision is a safety
property here, not UX.

**Approach.** Move from token containment to claim-level entailment:

1. Extract discrete claims from the draft (numbers, named entities, dated events,
   role/title assertions, quoted text) rather than scanning every word.
2. Check each claim against the cited facts. Numbers, names and dates stay exact-match
   — those are what actually create legal exposure. Paraphrase of the surrounding
   prose does not need to be word-for-word.
3. Keep the existing structural failures unchanged and unweakened: dangling
   citation, `source_url=None`, no citations, missing footer. Those are cheap,
   unambiguous, and already correct.
4. Consider a second Claude call as an entailment judge for the residual grey area,
   with the deterministic checks above still binding. A judge is an addition to the
   hard checks, never a replacement — it can be wrong, and the exact-match rules
   cannot.

**Done when:** on a 50-draft labelled set, no fabricated claim passes (false-negative
rate zero is the non-negotiable side) and the false-positive rate is low enough that
`held` means something. Build that labelled set as part of this workstream — it is
also the regression suite for every prompt change afterwards.

Owner: `drafting-engineer`.

---

## W2 — Claude drafting

The seam already exists. `draft.py` defines a `Composer` and
`compose_template` is one implementation; `default_composer` selects it. Phase 2
adds `compose_claude` behind the same interface. Nothing else in the pipeline
changes, and the footer is appended by `render_body` *outside* the composer, so a
model can never omit it.

**Current API specifics** (verified against the bundled Claude API reference, not
from memory — several of these changed in 2025-26):

- **Model: `claude-opus-5`.** $5/M in, $25/M out. Do not downgrade to Sonnet for
  cost without measuring quality first — see the cost note below, where the whole
  line item is rounding error.
- **Structured output:** `output_config={"format": {...}}` on `messages.create()`,
  or `client.messages.parse()` to validate against a schema automatically. The
  `output_format` parameter is deprecated — do not use it.
- **Thinking:** `thinking={"type": "adaptive"}`. `budget_tokens` is *removed* on
  Opus 5 and returns a 400. Depth is controlled by `output_config={"effort": ...}`.
- **No assistant prefill** — returns 400 on Opus 5. Shape the output with the
  structured-output schema or the system prompt.
- **Handle `stop_reason == "refusal"`** before reading `content`, and enable
  server-side fallbacks.

**Prompt caching.** Render order is `tools` → `system` → `messages`, and caching is a
prefix match, so put the frozen system prompt and the drafting instructions first and
the per-lead research last. This pays off most for colleagues at a shared employer,
where the company research corpus is identical. Verify it is actually working by
asserting `usage.cache_read_input_tokens > 0` on the second lead at a known shared
domain — a `datetime.now()` or an unsorted `json.dumps()` anywhere in the prefix
silently disables caching with no error.

**Use the Batch API.** Drafting has no latency requirement — a human reviews the
output hours later — and batch is 50% cheaper. This is the single biggest cost lever
and it costs nothing in quality.

**Cost, so nobody over-engineers it.** At ~25 leads/day with ~8K tokens of research
context and ~1K output, drafting runs roughly $0.07/lead, about $50/month at list
price. Batch halves it; caching cuts it further on shared domains. Cost is not a
design constraint at this volume. Do not trade draft quality for it.

Owner: `drafting-engineer`.

---

## W3 — LinkedIn provider: Bright Data vs ScrapIn

Still open from the original brief, and I am not going to resolve it from memory —
scraper pricing and per-endpoint reliability move faster than any training data, and
picking wrong here means re-integrating later.

The adapter interface was built for exactly this. `linkedin.py` exposes
`fetch_profile(url, *, subject_email=...)` and deliberately leaks no vendor shape, so
both can be implemented and swapped by config.

**Run a spike, not a spreadsheet.** Implement both against the existing interface,
run the same 20 real profile URLs through each, and compare on:

- field coverage on profiles that actually matter (current role, tenure, recent
  activity) — not on their marketing examples
- failure behavior: does a private or deleted profile return an error, or an empty
  success? Our pipeline distinguishes those, and a provider that conflates them will
  silently manufacture `no_angle` leads
- true cost per successful record, counting failures and retries
- contractual posture on ToS and personal data

Both must keep raising on failure rather than returning `[]`. That distinction is
load-bearing: an empty result legitimately means `no_angle`, so a swallowed error
would quietly tell the client there was no reason to reach out.

Owner: `integrations-engineer`. Decision needed from Sam before W5 ships.

---

## W4 — Live company research

Replace the fixture-backed company adapter with real fetching: homepage, careers/jobs,
blog/changelog/press. Job postings remain the highest-value signal downstream, so the
careers page is the one to get right.

- Respect `robots.txt` and rate-limit per domain. We are a small identifiable client;
  getting blocked is a real operational risk, not a theoretical one.
- Cache freshness already keys on `research_cache.fetched_at`, not row existence, so
  a failed fetch cannot masquerade as warm cache. Keep it that way.
- Set a re-scrape TTL. A job posting three months stale is a worse opener than none —
  it signals you are working from old data.
- Facts must keep stable ids (`make_fact_id` hashes a natural key, not content, so a
  copy-edit to a source page does not orphan citations in already-approved drafts).

Owner: `integrations-engineer`.

---

## W5 — Owned sending ✅ DONE (2026-09-21)

**Superseded before it was built.** This workstream assumed a live Smartlead
push. Sam decided instead that the client sends his own email, and that the
Smartlead client goes away rather than staying behind a flag —
`docs/DECISIONS.md` has the reasoning and the before/after.

What shipped in its place:

- `mailboxes` and `sent_messages` (migration `9377c345f195`). No credential is
  stored: `credential_ref` NAMES one, resolved from the environment at send.
- `integrations/email_sender.py` — `EmailSender` seam, `StubEmailSender`
  (writes real MIME to `sent_outbox.jsonl`, no network) and `GmailSender`
  (service account + domain-wide delegation, `gmail.send` only).
- `pipeline/mailbox.py` — selection at SEND time, not ingest: pinned campaign
  mailboxes first, then daily-cap headroom, spacing, health and rotation.
- `pipeline/send.py` — re-checks the approval, the verification verdict and
  suppression at the moment of sending; records a `sent_messages` row against a
  real receipt; disables a mailbox whose credential is dead.
- `queue.JobDeferred` — a third job outcome. A send waiting on a cap is not a
  failure and must not spend one of its five attempts.
- `/mailboxes` — roster screen: add, edit, pause, resume, remove, and today's
  usage against each cap.

**The campaign-structure question is answered by construction.** The pin lives
on `mailboxes.campaign_id`: NULL means the general rotation, set means one
campaign per mailbox. Both topologies work with no migration, so the client can
answer late or change his mind.

**Still open on this workstream:** sequences and follow-ups, the unified inbox,
reply classification (the `provider_thread_id` hook is recorded and unused), and
send-window / time-of-day shaping. Per-mailbox caps and spacing are enforced;
"only send 9am–5pm in the prospect's timezone" is not.

Owner: `integrations-engineer` (adapter), `pipeline-engineer` (send step).

---

## W6 — Operations

Everything above assumes someone notices when it breaks.

- Run the worker as a real service (launchd or systemd) rather than a manual
  invocation, with `reclaim_stale_jobs()` on a timer — `locked_at` exists as the
  crashed-worker hook but nothing reclaims automatically yet.
- Alert on: jobs exhausting `max_attempts`, provider error rate, and the `held`
  rate. A rising `held` rate is the early warning that W1's precision has regressed
  or a prompt change went bad.
- Log per-draft token usage and cost.
- Back up Postgres. The research cache is the expensive artifact — it represents real
  money already spent with the scraping provider.
- Secrets out of `.env` and into something managed once real keys exist.

Owner: `pipeline-engineer`.

---

## Sequencing

```
W1 verifier precision  ─────────►  W2 Claude drafting  ──┐
W3 LinkedIn spike ──► decision ──► W4 live research  ────┼──►  W5 owned sending ✅
                                                          │
                                   W6 operations ─────────┘
```

W1 gates W2. W3's decision gates W4. W5 shipped ahead of that sequence because
the client's sending rail changed. W6 runs alongside and **must land before the
first real send** — that gate now matters more, not less: with Smartlead gone,
nothing outside this system is watching the sending.

## Decisions

1. ~~**Bright Data vs ScrapIn**~~ — settled 2026-09-15: neither.
2. ~~**Smartlead campaign structure**~~ — moot. The pin lives on
   `mailboxes.campaign_id`, so both topologies work without a migration.

## Explicitly still out of scope

Auto-send (the brief forbids it and nothing here changes that — the pipeline
chain stops at `verify` and only a human click creates a `send` job),
multi-tenant, mailbox PROVISIONING, warmup.

**Deliverability is no longer out of scope — it is now our liability.** We do
not build warmup (five mailboxes cannot warm each other) and we do not
provision domains, but SPF/DKIM/DMARC per burner domain, complaint-rate
monitoring and sensible caps are now the client's standing obligation and
partly ours to surface.
