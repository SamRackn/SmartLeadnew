# Decision record

Decisions that are not derivable from the code, with the evidence that drove
them. Newest first.

---

## 2026-09-15 — Do not buy Bright Data for LinkedIn/company research

**Decision:** no Bright Data subscription. Invest the equivalent effort in the
own research engine's careers-page extraction instead.

**Status:** decided by Sam. Closes the W3 open question in `docs/PHASE2.md`
and item 3 of the `CLAUDE.md` work order. ScrapIn was not spiked and is not
being spiked — the reasoning below is about signal tier, not vendor quality,
and applies to any LinkedIn-derived dataset.

### The measurement

Two spike arms, same 10 real B2B SaaS domains (commits `7d82896`, `6131d19`):

| | Own engine | + Bright Data |
|---|---|---|
| Domains clearing the scoring threshold | 4 / 10 (40%) | 9 / 10 (90%) |
| `JOB_POSTING` facts added | — | **0 / 10** |
| Person-data contribution to any outcome | — | none (scored WEAK every time) |
| Cost per successful record | n/a | **unmeasured** |

### Why 90% is not the number it looks like

**Every rescue landed on the wrong tier.** All five domains Bright Data
recovered came from LinkedIn company *updates* scoring as `BLOG_POST` on
freshness — LinkedIn acting as a substitute company blog. That is the MEDIUM
tier. The gap we were paying to close is missing STRONG job-req signal, and
the LinkedIn company dataset has no jobs field at all, verified on every
record pulled. It structurally cannot close that gap.

This matters because of what the tiers mean in `score.py`. A job req is the
strongest signal in the system (75.0 against a 40.0 threshold) because it
means budget was approved and the pain is present-tense with a named owner. A
company update is a reason to reach out while it is fresh. Buying more of the
second does not supply the first.

**Person data contributed nothing.** It scored WEAK on every record and
changed no outcome.

**Cost could not be measured.** `/customer/balance` returns 403 on the spike
key — no billing-read scope — so there is no $/record figure and no
1k/10k/100k extrapolation. Rather than fabricate one, the spike reported the
absence. With cost as the stated primary objective, a subscription whose price
per successful record is unknown is not something to commit to.

21 records were billed against a ~20 budget; two were wasted on a wrong
LinkedIn slug guess and a wrong-identity person record.

### What we do instead

Harden careers-page extraction in the own engine. The same spike found the
own engine is clean on standard ATS pages and produces false positives on
homegrown careers pages, because a job req is identified by **URL shape**.
Trello's careers link routes to a JS-rendered hub where every "job" is a
filter control; Zapier mixes real reqs with policy pages under one path
pattern.

That is the same defect as the research-provenance guardrail gap in
`CLAUDE.md`, approached from the other side: precision and coverage on the
careers page are one piece of work, and it moves the STRONG tier, which is the
tier that decides whether a lead is worth an email.

### What would reopen this

A provider that returns actual job requisitions — an ATS aggregator rather
than a LinkedIn mirror — priced per successful record. The adapter interface
(`fetch_company(domain)`) leaks no vendor shape, so that stays a swap, not a
rewrite.

---

## 2026-09-15 — Scoring configuration is per-campaign, not global

**Decision:** `ScoringConfig` is loaded per campaign. `workspaces` and
`campaigns` enter the data model now; `mailboxes`, `sequences` and `replies`
stay out until work-order item 4.

**Status:** decided by Sam.

### Why

ClientRise (clientrise.io) sells outbound lead-gen to marketing agencies and
runs campaigns **on behalf of each agency-client**, at that client's own
target industry. The evidence is the client's own material, not inference:

- `docs/CLIENT-REPLY-TEMPLATES.md`, transcribed from the templates he used in
  Smartlead, is headed *"Change the highlighted segments for clients target
  industry"*, and the booking email books *"our Founder, CLIENT NAME"*.
- The site's FAQ: *"Qualification criteria are agreed with each client before
  campaigns launch, so expectations are clear on both sides."*

So the prospect ICP varies per engagement. `score.py` currently hardcodes one
`SUPPORTED_ROLE_TERMS` vocabulary — "revops", "sdr", "sales engineer", "crm" —
described in the source as *"a role the client's product replaces or
supports"*. That is written for a B2B SaaS seller. ClientRise is not one. It
sells pipeline to agencies, and the signals that mean "this agency needs
clients right now" are not the signals that mean "this company needs revenue
tooling".

That vocabulary decides which leads clear the threshold and which are settled
as `no_angle`. It is therefore the largest single lever on both cost per
usable lead and on conversations missed — and one global constant cannot serve
two engagements.

### Shape

`ScoringConfig` is already a frozen dataclass with every knob in one
inspectable place, already overridable, and already refuses a config where
funding alone could clear the threshold. The change is *where it is loaded
from*, not its shape.

`research_cache` stays keyed by company domain and is **not** scoped to a
workspace. Two agency-clients may target the same company, and the dedup that
makes 50 prospects at one employer cost one research operation has to hold
across tenants. That is the largest cost lever in the system.

### Note on the commercial model

The site describes a *"performance-based partnership"* — *"ZERO RISK. ALL
REWARD"* — and disclaims outcome guarantees: *"ClientRise focuses on creating
qualified sales conversations and a stronger outbound process."* If ClientRise
is paid per qualified conversation rather than per email, then the metric that
governs is **cost per qualified conversation**, not cost per lead. That makes
`no_angle` cheap and correct, and makes a missed real angle expensive — which
sharpens, rather than softens, the case for getting the scoring vocabulary
right. Worth confirming with the client before analytics (work-order item 5)
is designed around the wrong denominator.
