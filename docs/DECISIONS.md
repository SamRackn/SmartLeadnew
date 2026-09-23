# Decision record

Decisions that are not derivable from the code, with the evidence that drove
them. Newest first.

---

## 2026-09-21 — Send from the client's own Google Workspace mailboxes; remove Smartlead

**Decision:** the client sends his own email. `smartlead.py` and its tests are
deleted, not kept behind a flag, and approved mail goes out of Google Workspace
mailboxes he owns, over the Gmail API.

**Status:** decided by Sam, who also chose Google Workspace over M365 and chose
removal over keeping a fallback. This closes W5 in `docs/PHASE2.md` (which
assumed a live Smartlead push) and starts work-order item 4.

### What changed, concretely

| | Before | After |
|---|---|---|
| Approve button | "Approve & push to Smartlead" | "Approve & send" |
| What approve does | POST to Smartlead's campaign API | enqueues one `send` job |
| What sends | Smartlead | `pipeline.send` + `integrations.email_sender` |
| Rotation / caps / pacing | Smartlead's | `pipeline.mailbox`, per mailbox row |
| Lead status on click | `sent` | `approved`, then `sent` on a real receipt |

### Why not keep Smartlead behind a flag

A fallback is only worth its seam if someone would actually use it. Keeping the
client meant keeping its config, its stub, its outbox, and the test that asserts
it has no send verb — all describing a rail we had stopped using. The cost of
being wrong is one `git revert`; the cost of being right is a second sending
path that nobody exercises and that drifts out of correctness silently.

### What this does and does not buy

The Smartlead subscription was 6–20% of cost per usable lead depending on
volume (`docs/GAP-ANALYSIS.md`), so this is not the cost lever — AI at 57–70%
still is, and the work order still leads with it. What it buys is control:
rotation, pacing, suppression re-checks at the moment of sending, and the
reply/thread ids a unified inbox will need. It also buys a liability: **we now
own deliverability.** Warmup is still bought, never built (five mailboxes
cannot warm each other — that is the pattern spam detectors exist to catch),
and SPF/DKIM/DMARC per burner domain are now the client's standing obligation.

### The guardrail that replaced the old one

`test_smartlead.py` asserted the client had no send/launch/trigger verb
anywhere on it. That guarantee is gone by construction, so `test_sending.py`
carries its replacements: live sending is switched on by `EMAIL_SENDER`, never
by a credential appearing in the environment; suppression is re-checked at send
time, not only at ingest; and the pipeline chain stops at `verify`, so only a
human pressing approve can ever create a `send` job.

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

---

## 2026-09-21 — Research answers a qualification question, not an event question

**Decision.** Replace event-based signal research on the cold path with
qualification, and render email 1 from a fixed template with no model call.

### What forced it

A real email out of the client's account, to a real lead on his current list:

> **Subject:** Quick Question
>
> Hey Louis,
>
> Quick question — is DAME Legal currently looking to bring in more clients?
>
> We help law firms generate qualified client opportunities through search
> marketing. One recent client saw 50+ potential clients in a single month.
>
> Worth sending over a little more info?

Three variable slots. Two are on the lead row already (first name, company
name). The third is one noun — "law firms" — which is a *category*, not an
event. The only number is about the SENDER's client, not the recipient's
business.

Everything the research stack retrieved — job postings, changelogs, press,
funding, recency decay, role-fit vocabulary — appears nowhere in that message.

### Two consequences, and the second was worse

1. **Cost.** Six to eight fetches per domain, then Opus 5 with a ~1,200-token
   grounding prompt, to produce copy whose entire personalised surface is one
   noun. AI was ~70% of cost per usable lead.

2. **Coverage collapse.** The leads on the client's actual list are
   nine-person law firms: `damelegal.com`, `jasongoldmanlaw.com`,
   `rileycate.com`. They have no careers page, no changelog, no press feed. So
   nothing cleared the threshold, guardrail 2 fired, and the pipeline declined
   to write to **almost every lead in the business**. The "40% standalone
   signal coverage" figure is SaaS-shaped; against this list it is near zero.

   A control that refuses to write to the entire addressable market is not a
   strict control, it is a broken one.

### What research asks now

Not *"what happened at this company recently?"* but *"does the premise of this
email hold?"* — is this the vertical the campaign sells to (from the
prospect's own homepage / services / contact pages), do they take on new
clients, and is there any reason not to write. Two secondary fetches per
domain instead of six, cached by domain as before, and **zero AI on the cold
path**: the campaign supplies the vocabulary and `pipeline.qualify` matches it
deterministically. The model budget is now available for leads that reply,
where deep research is a completely different economic proposition.

### Guardrail 2, restated rather than relaxed

"Weak signals mean no email" survives with the gate moved to where the copy's
risk actually is. For this copy the failure is not a missing news item — it is
telling a dental practice we help law firms.

So a lead whose vertical is not confirmed **from its own pages** is `no_angle`
and no draft is written. No generic fallback, no "businesses like yours", no
writing the email with the noun left vague. `DEFAULT_QUALIFICATION` ships with
an EMPTY vertical vocabulary, so a campaign nobody has configured writes to
nobody rather than to everybody.

### Guardrail 3 got stricter, not looser

Email 1 makes exactly two assertions about the recipient — the company name
(the lead row) and the vertical (cited qualification facts) — and one about
the sender, the proof-point statistic. `verify` re-derives all of it from the
database at verification time, so a research row that changed or a claim the
client retired stops verifying a draft already waiting for review.

Two holes were found and closed while building this, both of which had existed
in the old shape too:

* **The vertical noun was passing verification by accident.** The sentence
  containing it reads as sender voice ("We help …"), so the verifier skipped
  the whole span — meaning the one claim in the email about the recipient's
  business was the one claim nothing checked. It also failed the other way: a
  vertical noun overlapping the prospect's own name ("Northwind Logistics" /
  "logistics companies") stopped looking like sender voice and held the draft
  over ordinary words. `verify.CampaignCopy` replaces the guess with the
  structural fact.

* **A statistic typed into the offer line sailed through**, for the same
  sender-voice reason — which would have made `proof_points` optional in
  practice. A digit in `offer_line` is now refused at the campaign form and in
  `outreach.offer_for_campaign` both.

### New data

`campaigns.vertical`, `campaigns.offer_line`, `campaigns.qualification_config`
(partial JSONB override, same contract as `scoring_config`), and a
`proof_points` table: the claim verbatim, the client it belongs to (NOT NULL —
an unattributed statistic is the shape of claim the table exists to prevent),
when, where the evidence lives, and an `active` flag. Retired, never deleted:
a draft sent last month may still cite one.

### What is NOT done

The event stack — `pipeline/score.py`, `pipeline/compose_claude.py`,
`pipeline/draft_batch.py`, the event half of `pipeline/draft.py`, and the
`ai`-extra eval scripts — is out of the pipeline chain but still on disk and
still imports. `LiveCompanyAdapter` still knows how to fetch and parse event
pages, behind `secondary_page_kinds=EVENT_PAGE_KINDS`, and that machinery is
deliberately kept: researching a lead that REPLIED is worth Opus and a crawl.
Deleting the rest is a mechanical follow-up, not a design question.

---

## 2026-09-21 — "Businesses like yours", opt-in only

**Decision.** A campaign may send a generic vertical instead of refusing to
draft, behind an explicit per-campaign flag
(`campaigns.generic_vertical_fallback`, default FALSE).

### Why it is allowed at all

The phrase is not a claim. "We help businesses like yours" asserts nothing
about the recipient that could be false, so it carries none of the exposure a
wrong vertical would — guardrail 3 has nothing to bite on. The objection to it
is quality and deliverability, not legality: "We help law firms" is specific
enough that a partner at a law firm reads the next sentence, and "businesses
like yours" is what every competitor's template already says. Since we now own
the sending rail, generic filler at volume from burner domains is our
deliverability problem, not a vendor's.

That is a trade an operator is entitled to make for a genuinely
vertical-agnostic offer. It is not a trade a blank form field should make for
them.

### Why opt-in rather than the behaviour of a blank field

A silent fallback fails in the worst available way: someone creates a
campaign, forgets the vertical, and it starts sending plausible copy that
nobody investigates because it looks like it is working. The loud refusal is
what gets the field filled in. With the flag, the generic phrase is something
a person chose, and the choice is visible on the campaign.

### What it waives, and what it does not

Only the vertical gate — because only the vertical gate was checking a claim
that is no longer being made. Still enforced:

* **intake evidence.** The opening question ("are you currently looking to
  bring in more clients?") presupposes they take client intake regardless of
  what we call them.
* **disqualifiers.** A parked domain or a "not accepting new clients" notice
  is a reason not to write whoever they are.

A real vertical always wins over the flag: it is a fallback, not a mode.

### Where it is enforced

`QualificationConfig.vertical_required` is derived from the CAMPAIGN row, never
from stored JSON — `qualification_config_from_json` refuses the key. A campaign
whose copy names a vertical while its config says none is required would drift
silently, in exactly the place that matters most. Drafting and verification
both derive it through `qualify.config_for_campaign` and
`outreach.effective_vertical`, so the two cannot disagree about what the email
claimed: a draft written believing nothing was claimed is never checked
believing something was.

Verification notes say `generic vertical (claims nothing about the recipient)`
rather than reporting a confirmation that never happened, and a generic
campaign that stops qualifying fails with "this lead no longer qualifies"
rather than "the draft calls this company 'businesses like yours'" — which
would send a reviewer hunting for a claim nobody made.
