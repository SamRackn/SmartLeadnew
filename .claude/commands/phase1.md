---
description: Run the MazzSmartLead Phase 1 build sequence, dispatching each step to its owning agent.
---

Build the Phase 1 skeleton described in `CLAUDE.md`. Prove the pipe end to end on
fake data before any real enrichment or LLM logic exists.

Dispatch each step to its owning agent via the Agent tool, in order. Steps 1 and 4
are independent and may run in parallel; everything else depends on the schema.

1. **Schema** → `db-architect`: five tables, Alembic migration, applies clean to an
   empty database.
2. **Ingest** → `pipeline-engineer`: CSV + manual entry, validation, domain
   derivation, suppression check before enqueue.
3. **Stages** → `pipeline-engineer` (queue, enrich) and `drafting-engineer`
   (score, draft, verify), all fixture-backed.
4. **Smartlead stub** → `integrations-engineer`: real method shapes, payloads
   recorded to `smartlead_outbox.jsonl`.
5. **Review app** → `review-app-engineer`: login, lead table, draft beside its
   source snippets, approve/edit/reject.
6. **Privacy** → `pipeline-engineer`: `delete_person(email)`.

Then run `/audit`, fix what it finds, and run `/seed` to demonstrate the full path.

Do not start Phase 2 work — no live scraping, no Claude API calls, no real
Smartlead credentials. If a step is blocked, finish every other step and say
plainly which one you left and why.
