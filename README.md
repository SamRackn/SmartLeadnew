# MazzSmartLead

Outbound sales software the client owns and operates, replacing his dependence on
Smartlead. Smartlead is a **reference for required business capabilities, not a
thing to clone feature-for-feature**.

The system decides whether there is a real reason to contact someone, writes copy
grounded only in facts it actually retrieved, proves every claim traces to a
source, and puts it in front of a human before anything moves.

See `CLAUDE.md` for the governing brief, `docs/DECISIONS.md` for decisions and the
evidence behind them.

## Primary constraint: cost per usable lead

Cost is the stated primary objective — not feature parity, not eliminating
subscriptions for their own sake. Measured (see `docs/GAP-ANALYSIS.md`), AI
research and personalization is **57–70% of total cost at every scale**, while the
Smartlead subscription is 20% falling to 6%. That sets the work order: AI cost
engineering leads, and "keep paying the third party" is a valid answer.

Cost per usable lead today: ~$0.19 at 1k/mo, ~$0.11 at 10k, ~$0.064 at 100k.
Those remain projections until real leads run through the drafting path.

## Pipeline

```
ingest → enrich → score → draft → verify → human review → send
```

A lead arrives already complete — name, company, site, title, email, headcount,
revenue, location, industry, LinkedIn URL, notes — so there is no discovery step,
enrichment only.

## The guardrails

Compliance controls. They fail closed and are not softened for throughput.

1. **Suppression is checked before a lead enters the queue** — email and domain,
   subdomains included. A read error rejects the lead rather than admitting it.
2. **Weak signals mean no email.** Funding-only leads land `no_angle` with no draft
   written. No fallback to generic filler — that is the exact failure the client is
   paying to escape.
3. **Unverifiable claims are blocked, not flagged.** A claim that does not trace to
   a cited fact id or the lead's notes fails verification, holds the lead, and is
   refused server-side at the approve route.
4. **No auto-send.** Every email is human-approved individually.
5. **Delete-by-person is complete** — leads, drafts, jobs, and person-scoped facts
   pruned from the shared company cache.
6. **Footer carries a physical mailing address and one-click unsubscribe**, added
   outside the composer so no model can omit it.

Known gap: verification proves a claim traces to retrieved evidence, not that the
evidence is *true*. Research provenance needs a check at cache-ingest.

## What we deliberately do not build

**Warmup** (a network effect needing thousands of opted-in mailboxes; the client
has five), **cheap SMTP for cold outreach** (it does not exist — SES prohibits it
in its AUP), **mailbox provisioning**, **email verification**, and
**deliverability feeds**. All commodity or structurally unbuildable. We build the
orchestration on top.

## Agent orchestration

Work is divided among six agents in `.claude/agents/`. The main session
orchestrates; each owns a file set and stays in its lane.

| Agent | Owns |
|---|---|
| `db-architect` | schema, models, migrations |
| `pipeline-engineer` | ingest, queue, enrichment, privacy |
| `drafting-engineer` | scoring, drafting, verification |
| `integrations-engineer` | research adapters, provenance, Smartlead client |
| `review-app-engineer` | FastAPI + HTMX review app |
| `compliance-auditor` | read-only gate; reports, never fixes |

Commands: `/phase1` runs the build sequence, `/seed` demonstrates the pipe end to
end, `/audit` runs the compliance gate.

## Local setup

Postgres 17 via Homebrew, Python 3.12+:

```bash
brew services start postgresql@17
createdb mazz_dev
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # fill SESSION_SECRET, REVIEW_PASSWORD_HASH, SENDER_POSTAL_ADDRESS
.venv/bin/alembic upgrade head
```

`psql` is keg-only; it lives at `/usr/local/opt/postgresql@17/bin/psql`.

Run the app: `.venv/bin/uvicorn mazz.web.app:app --reload`
Run the tests: `.venv/bin/pytest`

A plain `pytest` run cannot reach a paid API or bill the client's account:
`tests/conftest.py` has a session-wide autouse fixture pinning every provider to
stub and blanking credentials.

## Spending money

Live adapters and the real Claude composer exist behind settings, not behind key
presence. `DRAFT_COMPOSER=template` is the default and stays that way until the
verifier is precise enough to run against paraphrase — a key in the environment is
not consent to spend.

## Current state

**Built:** lead import/management · suppression & unsubscribe · domain-keyed
research caching · own company research engine (40% standalone signal coverage) ·
Batch API + prompt caching on the drafting path.

**Partial:** scheduling/pacing (queue primitive only) · AI personalization
(`compose_claude` built, not switched on) · company research (live adapter built).

**Missing:** mailbox management · multi-step campaigns · lead distribution ·
activity-based stop/continue · unified inbox · reply classification · performance
tracking · APIs/webhooks · multi-client workspaces.

## Work order

1. **AI cost engineering** — largest line item, mostly already-owned code.
2. **Verifier precision + research provenance** — gates every AI-written email.
3. **Own research engine over paid providers** where the free baseline suffices.
4. **Sending platform** — on a slower clock; it is a 6–20% cost line.
5. Multi-tenancy, APIs/webhooks, analytics — as the business requires.
