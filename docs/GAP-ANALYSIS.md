# Smartlead Replacement — Gap Analysis

Answering the directive's closing question up front, then showing the work.

> **Can we operate the client's outbound workflow without Smartlead, at meaningfully
> lower total cost, while maintaining reliable sending, personalization, reply
> handling, suppression and campaign management?**

**Partially — and the part you can't replace is not the part that costs money.**

You can and should own campaign management, sequencing, the unified inbox, reply
classification, suppression, research and personalization. You cannot economically
own **warmup** or **raw SMTP for cold outreach**, for structural reasons below.

More importantly: **eliminating Smartlead optimizes the wrong line item.** Its
subscription is 2–22% of total operating cost depending on scale. AI research and
personalization is 50–70%. The costs that dominate — mailboxes, domains,
verification, warmup — are paid identically whether or not we own the software.

---

## 1. What the team has already built

Phase 1 is complete and verified: 224 tests green over three clean runs, all six
compliance guardrails audited and enforced server-side, zero API spend.

Mapped against the directive's 15 required capabilities:

| # | Capability | State | Where |
|---|---|---|---|
| 1 | Import and manage leads | **Built** | `ingest/csv_import.py`, `validate.py`, `leads` |
| 9 | Suppression / unsubscribe lists | **Built** | `suppression_list`, checked pre-queue, fails closed; HMAC unsubscribe token in footer |
| 13 | Cached, reusable research | **Built** | `research_cache` keyed by company domain — proven: 2 colleagues → 1 row, 1 provider call |
| 2 | Connect/manage mailboxes | **Built** (2026-09-21) | `mailboxes` roster, `/mailboxes`, rotation by health and cap headroom, `credential_ref` names a secret it never stores |
| 10 | Performance tracking | **Built** (2026-09-21) | `/dashboard` — cost per usable lead, hold rate, cache effectiveness, queue health |
| 5 | Schedule and pace emails | *Partial* | Per-mailbox daily caps and min spacing enforced at send, with deferral that does not spend a retry. No send-window or time-of-day shaping |
| 11 | AI personalization | *Partial* | Email 1 is now a fixed template with zero AI, by design. The Claude path is retained for the reply/deep-research tier |
| 12 | Research companies/prospects | *Partial* | Live adapter built — 2 qualification fetches per domain, cached. Paid provider arm still unfunded |
| 4 | Distribute leads across campaigns/mailboxes | *Partial* | `SystemRoutingPolicy` assigns a campaign at ingest; `select_mailbox` rotates at send. No segment-level distribution rules |
| 3 | Multi-step campaigns | **Missing** | One draft per lead; no sequence model |
| 6 | Continue/stop on lead activity | **Missing** | No reply ingestion |
| 7 | Unified inbox | **Missing** | — |
| 8 | Reply classification | **Missing** | — |
| 14 | APIs / webhooks | **Missing** | — |
| 15 | Multi-client / workspaces | **Missing** | Single-user session auth; `workspaces` groups config, not access |

**5 built, 4 partial, 6 missing** as of 2026-09-21 — was 3 / 3 / 9 when this document
was written.

**The conflict this section used to describe is resolved.** It read: *"`CLAUDE.md` and
this directive are in direct conflict and one of them has to change. Nothing has been
deleted pending your call."* `CLAUDE.md` is the one that changed. It had stated the
client was NOT replacing his sending infrastructure and listed mailbox management as an
explicit non-goal; as of 2026-09-21 sending is ours, the Smartlead client is deleted
rather than flagged off, and the excluded list shrank to warmup and mailbox provisioning
alone. See `docs/DECISIONS.md`.

The cost reasoning below is preserved as written, but **the numbers predate the
cutover** and are no longer the current split — the Smartlead line is now zero, the
cold path spends no tokens at all, and owning the rail adds operational cost that has
not been measured. Re-measure before quoting a figure.

What the team built is the half Smartlead is *weakest* at — grounded research,
verified claims, compliance that fails closed. That work stands regardless of which
way this decision goes.

---

## 2. The two structural blockers

These are not engineering-effort problems. More agents will not solve them.

### Warmup is a network effect we cannot manufacture

Warmup works by sending mail between **thousands of real, opted-in mailboxes** that
reply to each other, so providers observe two-sided conversations. Our client has
five mailboxes. Five mailboxes emailing each other is not a warmup pool — it is a
pattern detectors are specifically built to catch.

Warmup must be **joined or bought**, never built. It is bundled free in Smartlead;
standalone equivalents run ~$15/mo, and there is a self-hostable Apache-2.0 option
(Warmbly) that still relies on joining an external pool.

### Cheap SMTP is not available for cold outreach

The obvious "own the sending rail" move is Amazon SES at $0.10 per 1,000 emails.
**SES prohibits cold outreach in its acceptable use policy and suspends accounts for
unsolicited sending.** That $0.10 figure — the one that makes self-hosting look
compelling — does not apply to this use case at all.

Real cold outreach runs on Google Workspace or Microsoft 365 mailboxes on burner
domains, rotated. That is what Smartlead orchestrates. **Self-building means buying
the identical mailboxes and writing the rotation ourselves.** The mailbox cost does
not move.

---

## 3. Operating cost at scale

Assumptions: 3-step sequences (3 sends per lead), ~30 sends/mailbox/day sustainable
post-warmup (~900/mo), ~40% company-domain dedup on research, drafting via Batch API,
mailbox cost $3.50 falling to $2.50 at volume, verification $0.004 falling to $0.001.

| Line item | 1,000 leads/mo | 10,000 leads/mo | 100,000 leads/mo |
|---|---|---|---|
| Mailboxes (4 / 34 / 334) | $14 | $119 | $835 |
| Domains | $2 | $12 | $110 |
| Verification | $4 | $20 | $100 |
| **AI — research** | **$52** | **$300** | **$1,500** |
| **AI — personalization** | **$35** | **$350** | **$3,000** |
| Compute + storage | $30 | $80 | $300 |
| Warmup | $15 | $50 | $200 |
| **Subtotal (identical either way)** | **$152** | **$931** | **$6,045** |
| Smartlead subscription, if kept | $39 | $174 | $379 |
| **Total with Smartlead** | **$191** | **$1,105** | **$6,424** |
| **Saving from replacing it** | **$39 (20%)** | **$174 (16%)** | **$379 (6%)** |
| **Cost per usable lead** | ~$0.19 | ~$0.11 | ~$0.064 |

Three things fall out of this table:

1. **AI is 57–70% of cost at every scale.** Research and personalization dwarf
   everything else. The highest-leverage cost work is aggressive research caching,
   model selection and batching — all of which live in code we already own.
2. **The saving from replacing Smartlead shrinks as you scale** — 20% at 1k, 6% at
   100k — because mailbox and AI costs grow while the subscription plateaus.
3. **At the client's actual current volume the saving is ~$39/month.** He runs 5
   mailboxes at ~500 emails/week, which is *half* the 1,000-lead column. Replacing
   Smartlead there trades ~$39/mo for full ownership of deliverability risk. One
   burned sending domain costs more than a year of that saving.

**Cost is not the argument for replacing Smartlead. Control and margin at scale
might be — but say so honestly rather than calling it a cost decision.**

---

## 4. Where a third party stays smarter

| Component | Verdict | Why |
|---|---|---|
| Warmup pool | **Keep buying** | Network effect; unbuildable at 5 mailboxes |
| Mailbox provisioning (Google/M365) | **Keep buying** | Identical cost either way; no margin in reselling to yourself |
| Email verification | **Keep buying** | $0.001–0.004/email, commodity, nothing to gain |
| Deliverability monitoring / blocklist watch | **Keep buying** | Specialist data feeds |
| Sending orchestration + rotation | **Build** | Just software; this is where control lives |
| Campaign/sequence engine | **Build** | Just software |
| Unified inbox + reply classification | **Build** | Just software; AI classification is cheap |
| Suppression / compliance | **Already built** | And better than Smartlead's — ours fails closed |
| Research + personalization | **Already built, extend** | The real differentiator |

---

## 5. W3 — replacing the paid LinkedIn dependency

This one is a clear **yes**, and it is the best cost work available.

The directive's priority order — company websites, permitted public sources,
authorized APIs, licensed data, existing client data — points away from LinkedIn
scraping, which is simultaneously the most expensive, the most legally exposed, and
the most ToS-fragile input we have.

The adapter interface already isolates it: `fetch_profile(url, *, subject_email=…)`
leaks no vendor shape, so this is a swap, not a rewrite.

**Revised spike — three-way, not two.** Per your instruction to spike before
committing, but widened per the directive:

| Arm | Needs | Measures |
|---|---|---|
| Own research engine | nothing — buildable now | signal coverage from company sites, careers pages, changelogs, press |
| Bright Data | vendor account | coverage delta over own engine, true cost per *successful* record |
| ScrapIn | vendor account | same |

Run the own-engine arm **first**, because it sets the baseline the paid arms have to
beat, and because it needs no vendor account. If own-engine coverage produces enough
signal to clear the scoring threshold on most leads, the paid providers may be
unnecessary — which removes a recurring cost *and* the ToS exposure.

The one thing every arm must preserve: a provider failure raises rather than
returning `[]`. An empty result legitimately means `no_angle`, so a swallowed error
would quietly tell the client there was no reason to reach out.

No CAPTCHA, anti-bot, or access-control circumvention in any arm, per the directive
and per our own compliance posture.

---

## 6. Campaign routing — your answer, implemented

You specified: **one queue, with the system determining campaign, segment and
mailbox per prospect.** The directive separately says not to hard-code that choice.

Both hold if routing is a **policy object**, not a topology:

```
lead → RoutingPolicy.route(lead) → (campaign_id, segment, mailbox_id)
```

Ship `SystemRoutingPolicy` as the default — it reads segment from lead attributes,
picks the campaign, and selects a mailbox by health, daily-cap headroom and
rotation. A `ManualCampaignPolicy` covering one-campaign-per-mailbox stays possible
without a migration. The model change is small: a nullable campaign reference on the
lead plus a `mailboxes` table.

---

## 7. Recommended work — only what is necessary

**Now, and already approved:**

- **W3-own** → `integrations-engineer` — build the own-research-engine arm and
  measure signal coverage on real domains. Needs no vendor account, directly answers
  the directive's W3 question, and is the highest-leverage cost work available.
- **W1 verifier precision** → `drafting-engineer` — unchanged and still first. It
  gates every AI-written email regardless of who sends it.
- **W2 Claude drafting** → `drafting-engineer` — unchanged.

**Hold pending your decision on this analysis:**

- Mailbox management, sequence engine, sending scheduler, unified inbox, reply
  classification, analytics, webhooks, multi-tenancy.

That hold is deliberate. Dispatching six agents to build a sending platform before
you have seen that it saves $39/month at current volume would be exactly the
duplicated, unnecessary work the directive tells me to avoid. The moment you say the
goal is control or scale margin rather than cost, the hold lifts and the sequencing
is ready.

---

## 8. What I need from you

1. **`CLAUDE.md` contradicts this directive.** Confirm the directive governs and I
   will rewrite it. Nothing deleted until you say so.
2. **Is the real objective cost, or control/margin at scale?** The honest cost case
   for replacing Smartlead is weak at current volume and modest at 100k. The control
   case may be strong — but it should be argued on its own terms.
3. **Vendor accounts for Bright Data / ScrapIn**, if you want those spike arms run.
   The own-engine arm proceeds without them.
