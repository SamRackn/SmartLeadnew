# MazzSmartLead

Outbound sales software the client owns and operates, replacing his dependence on
Smartlead. Smartlead is a **reference for required business capabilities, not a
thing to clone feature-for-feature**.

The system decides whether there is a real reason to contact someone, writes copy
grounded only in facts it actually retrieved, proves every claim traces to a
source, puts it in front of a human before anything moves, and then sends it
from the client's own Google Workspace mailboxes — rotated, capped and paced.
Smartlead is no longer in the path (2026-09-21, `docs/DECISIONS.md`).

See `CLAUDE.md` for the governing brief, `docs/DECISIONS.md` for decisions and the
evidence behind them.

## Primary constraint: cost per usable lead

Cost is the stated primary objective — not feature parity, not eliminating
subscriptions for their own sake. Measured (see `docs/GAP-ANALYSIS.md`), AI
research and personalization is **57–70% of total cost at every scale**. The
Smartlead subscription was 20% falling to 6% and is now zero — removing it made
AI a larger share of what remains, not a smaller problem. That sets the work
order: AI cost engineering leads, and "keep paying the third party" is still a
valid answer for anything else.

Cost per usable lead today: ~$0.19 at 1k/mo, ~$0.11 at 10k, ~$0.064 at 100k.
Those remain projections until real leads run through the drafting path.

## Pipeline

```
ingest → enrich → qualify → draft → verify → human review → send
```

A lead arrives already complete — name, company, site, title, email, headcount,
revenue, location, industry, LinkedIn URL, notes — so there is no discovery step,
enrichment only.

## The guardrails

Compliance controls. They fail closed and are not softened for throughput.

1. **Suppression is checked before a lead enters the queue** — email and domain,
   subdomains included. A read error rejects the lead rather than admitting it.
2. **An unconfirmed premise means no email.** The copy calls the recipient
   something ("We help *law firms* …"), and that word has to be earned from the
   prospect's own homepage, services or contact page. A lead whose vertical
   `pipeline.qualify` cannot confirm lands `no_angle` with no draft written, as does
   one its own site disqualifies. There is no fallback to generic filler and no
   writing the sentence with the noun left vague — that is the exact failure the
   client is paying to escape. `DEFAULT_QUALIFICATION` ships with an EMPTY vertical
   vocabulary, so an unconfigured campaign writes to nobody rather than to everybody.
   One documented exception, opt-in per campaign and off by default:
   `campaigns.generic_vertical_fallback` sends "We help *businesses like yours* …"
   when no vertical is set. The phrase asserts nothing about the recipient, so it
   waives the vertical gate and nothing else — intake evidence and disqualifiers
   still apply.
3. **Unverifiable claims are blocked, not flagged.** A claim that does not trace to
   a cited fact id, a `proof_points` row, or the lead's notes fails verification,
   holds the lead, and is refused server-side at the approve route. Numbers have
   exactly one door into the copy: a `proof_points` row, which requires a named
   client. A digit in `campaigns.offer_line` is refused.
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
| `integrations-engineer` | research adapters, provenance, the email sender |
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
cp .env.example .env          # fill SESSION_SECRET
.venv/bin/alembic upgrade head
.venv/bin/python -m mazz.users seed-launch          # Maaz + Sean, no passwords
.venv/bin/python -m mazz.users set-password <email>  # each person sets their own
```

`psql` is keg-only; it lives at `/usr/local/opt/postgresql@17/bin/psql`.

Add the client's mailboxes at `/mailboxes` once the app is up. Sending stays
stubbed (`EMAIL_SENDER=stub`, the default) until you install the Google extra
and switch it on deliberately:

```bash
.venv/bin/pip install -e ".[google]"   # google-api-python-client, google-auth
# then, only when you mean it:
EMAIL_SENDER=google GMAIL_SA_DEFAULT=/path/to/service-account.json
```

The service account needs domain-wide delegation for `gmail.send` on each
burner domain's Workspace. `mailboxes.credential_ref` names which key to use;
no key is ever stored in the database.

Run the app: `.venv/bin/uvicorn mazz.web.app:app --reload`
Run the worker: `.venv/bin/python -m mazz.ops worker` — without it, Send in the
outbox and a browser import only queue work; nothing runs it.
Run the tests: `.venv/bin/pytest`

A plain `pytest` run cannot reach a paid API or bill the client's account:
`tests/conftest.py` has a session-wide autouse fixture pinning every provider to
stub and blanking credentials. It also cannot touch your data: the suite
TRUNCATEs every table, so it runs in `mazz_test` (created and migrated
automatically; override with `TEST_DATABASE_URL`) and refuses any database
whose name does not end in `_test`.

## Operations

```bash
.venv/bin/python -m mazz.ops worker            # drain the queue; reclaims crashed jobs each minute
.venv/bin/python -m mazz.ops health --notify   # exit 1 + macOS notification if anything needs a human
.venv/bin/python -m mazz.ops backup            # pg_dump, verified with pg_restore --list, keeps 14
.venv/bin/python -m mazz.ops resolve-send LEAD_ID --sent | --not-sent
```

Hosted: `render.yaml` (Render blueprint) runs the web app and ONE worker
against managed Postgres; see `docs/LAUNCH-PLAN.md` "WP8".

`health` reports, most urgent first: a send that may have gone out with no
record, a mailbox the system disabled, a queue no worker is draining, jobs
stuck `running`, jobs that gave up in the last 24h, a verification hold rate
over 50% (7 days, at least 5 drafts), and a missing or >36h-old backup.

**Sends go out at most once.** The send step commits a marker on the draft
before handing it to Gmail. If anything fails after that and the outcome is
not a clear refusal (401/403, 429, other 4xx), the lead is held and `health`
says which mailbox's Sent folder to check. `resolve-send` records the answer;
`--not-sent` puts the lead back in the outbox and never queues a send itself.

Run ONE worker. Mailbox selection takes no row lock, so two workers sending
at once could pick the same mailbox and break its spacing.

Backups default to `~/Library/Application Support/MazzSmartLead/backups`
(`BACKUP_DIR` overrides) — outside the repository, but on the same disk. Copy
them somewhere else before calling this backed up.

## Spending money

Live adapters and the real Claude composer exist behind settings, not behind key
presence. `DRAFT_COMPOSER=template` is the default and stays that way until the
verifier is precise enough to run against paraphrase — a key in the environment is
not consent to spend.

## Current state

**Built:** lead import/management · suppression & unsubscribe · domain-keyed
research caching · own company research engine (qualification pages: homepage,
services, contact — 2 fetches/domain, no AI) · mailbox management (roster, daily
caps, spacing, rotation, health) · owned sending (Gmail API, one human-approved
message at a time) · performance tracking (`/dashboard` reports cost per usable
lead, hold rate, cache effectiveness and queue health).

**Partial:** scheduling/pacing (per-mailbox caps and spacing enforced at send; no
send-window or time-of-day shaping) · company research (live adapter built; paid
provider arm under evaluation) · email 1 (fixed template, zero AI; a per-campaign
body template is the next step).

**Missing:** multi-step campaigns · lead distribution · activity-based
stop/continue · unified inbox · reply classification · APIs/webhooks ·
multi-client workspaces.

## Work order

1. **AI cost engineering** — largest line item, mostly already-owned code.
2. **Verifier precision + research provenance** — gates every AI-written email.
3. **Own research engine over paid providers** where the free baseline suffices.
4. **Sending platform** — on a slower clock; it is a 6–20% cost line.
5. Multi-tenancy, APIs/webhooks, analytics — as the business requires.
