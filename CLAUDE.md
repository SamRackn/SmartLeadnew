# Project: AI Outreach Research & Drafting Agent

## What this is
A tool that replaces the "personalized mass email" feature of an incumbent
SaaS (originally investigated as smartleague.ai — turned out to most closely
match Smartlead.ai's feature set) for a client who runs cold outreach for his
business. He is NOT replacing his sending infrastructure — he keeps Smartlead
as the sending rail. We are building the research + personalization layer
that feeds it.

## The client's current setup
- Sends via **Smartlead** (has API access)
- Runs **5 mailboxes**, ~100 emails/week each (~500/week total, ~25/day)
- Sells **B2B SaaS / tech** to other companies
- For each lead he already has: full name, company name, website, job title,
  email address, employee count, revenue range, exact location, primary +
  specific industry, LinkedIn page URL, and free-text notes.
  → No "identity resolution" step needed. Enrichment only, not discovery.

## What the agent does (pipeline)
1. **Ingest** — lead record comes in (CSV upload or manual entry) with the
   fields above already populated.
2. **Enrich**
   - LinkedIn: direct profile fetch by URL (NOT search/discovery) via a paid
     scraper API (Bright Data or ScrapIn). Single-URL lookups only — cheaper
     and lower legal/ToS risk than bulk/crawl scraping.
   - Company: scrape homepage, careers/jobs page, blog/changelog/press for
     anything recently dated.
   - Cache all research by **company domain** (not by person) in
     `research_cache` — multiple leads often share an employer.
3. **Signal scoring** — rank retrieved facts by "reason to reach out right
   now" for a B2B SaaS pitch. A job posting for a role the client's product
   replaces/supports > old blog post > generic funding news (funding news is
   weak — every competitor's tool already uses it).
   - If nothing scores above threshold: mark lead `no_angle`, do NOT force a
     generic draft. Surface this to the client plainly.
4. **Draft** — LLM call, structured output, grounded ONLY in retrieved
   research + the lead's `notes` field. No inventing facts.
5. **Verify** — every factual claim in the draft must trace back to a
   specific cached snippet or the notes field. Fails verification → held,
   never auto-approved.
6. **Review** — simple web app (login required). Table of leads with status
   (researching / ready / no_angle / approved / sent). Click a row to see
   the draft plus the exact signals/snippets it was built from. Approve /
   edit / reject actions.
7. **Send** — on approve, push to Smartlead via its API into the correct
   campaign. We do not send email directly ourselves.

## Explicit non-goals for v1
- No auto-send. Every email is human-approved before it goes out.
- No LinkedIn search/discovery — URL-based lookup only, since we're always
  given the profile link already.
- No warmup infrastructure, no multi-tenant SaaS, no mailbox management —
  Smartlead already owns sending/deliverability.
- No building a second sender. This tool's entire job is upstream of send.

## Data model (target)
- `leads` — the incoming record (all fields listed above) + pipeline status
- `research_cache` — keyed by company domain; raw scraped/enriched facts
  with source + timestamp
- `drafts` — generated email text, linked to the signals/snippets used,
  verification status
- `suppression_list` — anyone who must never be contacted
- `jobs` — simple job/queue table for async enrichment steps (no Redis/
  Celery needed at this volume — ~25 leads/day)

## Compliance guardrails (build these in from day one, not later)
- Check `suppression_list` before a lead ever enters the queue.
- Footer must include a physical mailing address and one-click unsubscribe.
- Since we store scraped personal data, need a delete-by-person function
  early (GDPR/CCPA relevance if any lead is EU/CA-based).
- Verification step (see pipeline #5) is a compliance control, not just a
  quality one — hallucinated claims in outbound B2B email are a real risk.

## Stack decisions
- Postgres for all state (leads, cache, drafts, suppression, jobs)
- Python for backend (best library support for scraping + LLM orchestration)
- Web framework for the review app: TBD in-session (FastAPI+frontend or
  Next.js full-stack are both fine — no strong constraint from the client)
- LLM for research synthesis / drafting: Claude, via ANTHROPIC_API_KEY
- LinkedIn enrichment: Bright Data or ScrapIn (paid, per-record, single-URL
  lookup mode)
- Smartlead: push via their public API on approval

## Current build phase
Phase 1: skeleton. Get Postgres schema in place (`leads`, `research_cache`,
`drafts`, `suppression_list`, `jobs`), a basic ingest path, and a stubbed
Smartlead API connection working end-to-end before adding real enrichment
or LLM logic. Prove the pipe with fake/sample data first.

## Open questions to resolve during build
- Which web framework for the review app (no client preference given)
- Exact LinkedIn provider: Bright Data vs ScrapIn (pricing/DX comparison
  still needed)
- Smartlead campaign structure — does the client want one drafting queue
  feeding multiple campaigns, or one campaign per mailbox/segment?
