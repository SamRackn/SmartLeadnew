# MazzSmartLead

Research + personalization layer that feeds Smartlead.

This is **not** a Smartlead replacement. The client keeps Smartlead as his sending
rail and his deliverability/warmup infrastructure. This tool sits entirely
upstream of send: it decides whether there is a real reason to contact someone,
writes copy grounded only in facts it actually retrieved, proves each claim traces
to a source, and puts it in front of a human before anything moves.

See `CLAUDE.md` for the full product brief.

## Pipeline

```
ingest → enrich → score → draft → verify → human review → push to Smartlead
```

A lead arrives already complete (the client supplies name, company, site, title,
email, headcount, revenue, location, industry, LinkedIn URL, notes), so there is
no discovery step — enrichment only.

## The three behaviors that define this tool

1. **Suppression is checked before a lead enters the queue**, not at send time.
2. **No angle means no email.** If nothing scores above the threshold, the lead is
   marked `no_angle` and no draft is written. The tool never falls back to generic
   filler — that is the thing the client is trying to escape.
3. **Unverifiable claims are blocked, not flagged.** If a sentence in a draft does
   not trace to a cached fact id or the lead's notes, the draft fails verification
   and cannot be approved. Hallucinated claims about someone else's company are a
   legal risk, not a quality nit.

There is no auto-send. Every email is human-approved.

## Agent orchestration

Work on this repo is divided among six agents defined in `.claude/agents/`. The
main session orchestrates; each agent owns a file set and stays in its lane.

| Agent | Owns |
|---|---|
| `db-architect` | schema, models, migrations |
| `pipeline-engineer` | ingest, queue, enrichment, privacy |
| `drafting-engineer` | scoring, drafting, verification |
| `integrations-engineer` | Smartlead client, enrichment adapters |
| `review-app-engineer` | FastAPI + HTMX review app |
| `compliance-auditor` | read-only gate; reports, never fixes |

Commands: `/phase1` runs the build sequence, `/seed` demonstrates the pipe end to
end on fake data, `/audit` runs the compliance gate.

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

## Phase status

**Phase 1 (current)** — skeleton. Real schema, real ingest, real review UI, but
every external provider is stubbed from `tests/fixtures/` and drafting is
deterministic template copy. No API keys required to run anything. The point is to
prove the pipe before spending a credit.

**Phase 2** — live LinkedIn/company enrichment, Claude drafting behind the same
contract, real Smartlead push.

Open questions carried into Phase 2: Bright Data vs ScrapIn, and whether the
client wants one drafting queue feeding several campaigns or one campaign per
mailbox.
