# Migration recovery evidence

Recovered 2026-10-15 from Claude session `ba2814f7-e8fd-4d08-87b7-60e87c323408`, subagent `agent-a7fa578b853ad2ac4.jsonl`; `.meta.json` identifies `db-architect`. This is historical local evidence, not production or current-suite certification.

## Assignment and confirmed actions

Scope: models, DB connection, all migrations, ingest (especially Smartlead), privacy, source-to-target mapping, reconciliation and adoption. Existing files were read-only for this assignment; the agent created `tests/test_audit_migration.py`, which is present as an untracked working-tree file. No isolated implementation changes were found for this assignment. Status: **REPORTED COMPLETE—UNVERIFIED** for its investigation; **PARTIAL** for remediation.

| Evidence | Recorded result |
|---|---|
| `toolu_01QvBWE9LuBJUfej9z9GXDdk` | Existing cutover/ingest/Excel/privacy suites: 94 passed, 1 warning, 31.82s; runner named `mazz_audit_db_test` through `TEST_DATABASE_URL` |
| `toolu_018LMpqBqaquMEwCxgKQhFzt` | Empty-schema upgrade, metadata comparison, full downgrade, reupgrade, comparison: 15 revisions; one head `c0a7e8f9b1d2`; zero differences before/after; downgrade left only alembic_version and no enums |
| `toolu_014onTsmqjgCo6p11YpbZCw5` | New audit test file written |
| `toolu_018KC7mt5cwx4TPxepvx6j2p` | New migration audit: 14 failed, 3 passed |
| `toolu_01FNkowQLveuhWe2iEhBmN4d` | Detailed SubagentHandback with DB-01 through DB-16 |
| `toolu_01KrvuHvFsMQL1rw7csN8u89`, 22:37:30.612Z | Last confirmed tool result: read reply stop behavior; session ultimately ended with spend-limit message |

The scratch script `scratchpad/db/mig.py` was recovered and inspected, never run during Codex recovery. It substitutes `mazz_audit_db_test` into the configured URL, asserts only the suffix, and its `up` operation drops/recreates public schema. Historical successful empty-schema downgrade does not validate rollback with customer data.

## Findings reconciled against recovery-time files

Locations below describe recovery-time code; subsequent remediation may move lines. Test failures are historical until rerun by the integration owner. Current `smartlead.py`, `csv_import.py`, models and migrations matched the inherited implementation. `validate.py` already contains a later header-precedence correction, so DB-05 needs fresh verification.

| ID | Severity | Evidence / behavior | Recovery status and remaining action |
|---|---|---|---|
| DB-01 | HIGH | `_legacy`: COMPLETED with one send becomes active and schedules FU1 after adoption; terminal-status audit test failed | PARTIAL: allow continuation only for explicitly eligible source states; preserve terminal state |
| DB-01b | MEDIUM | UNSUBSCRIBED/BOUNCED/REPLIED with sent count become active; flag columns ignored; six parameterized historical failures cover terminal status/flags | PARTIAL: fail closed; real export flags remain unconfirmed |
| DB-02a | HIGH | Suppression accepts only email/Email; Email Address produces invalid count without blocking commit | PARTIAL: share exact lead aliases and block invalid source rows |
| DB-02b | HIGH, source-shape dependent | Exclusions chooses email instead of domain when both supplied, leaving colleagues eligible | PARTIAL: enforce company domain exclusions with explicit semantics; real File C still needed |
| DB-03 | MEDIUM | Suppression imported after fresh leads leaves pending enrich jobs; audit test failed | PARTIAL: cancel pending work/hold matching leads, preserving send-time rechecks |
| DB-04 | MEDIUM | Four source rows yield inconsistent terminal outcomes; duplicate fresh row counted twice despite one job | PARTIAL: one terminal outcome per source row; reconcile totals |
| DB-04b | MEDIUM | Replaying fresh source row resets a HELD lead to researching and queues enrichment | PARTIAL: Smartlead replay preserves existing pipeline state |
| DB-05 | MEDIUM | Unknown Email Account prefix could override exact Email; historical test failed | REPORTED COMPLETE—UNVERIFIED: correction present in validate.normalize_row; retest all importers |
| DB-06 | MEDIUM | Adoption only selects SENT; a HELD not-found/ambiguous lead cannot be retried after mailbox repair | PARTIAL: explicit repairable hold selection; do not resume arbitrary holds |
| DB-07 | MEDIUM | Adoption keeps stale export step despite Gmail chain showing two sends | PARTIAL: reconcile source step with confirmed thread history without blindly counting unrelated messages |
| DB-08 | LOW–MEDIUM | Overdue adopted followups all queue immediately | VERIFIED historical observation only: passing burst test demonstrates backlog; human release/caps remain. Business pacing plan needed; do not raise caps |
| DB-09 | MEDIUM | --accept-mismatch waives row-level refused/MUST_FIX/unknown-status blockers as well as stale expected totals | PARTIAL: count-only override; row blockers remain mandatory |
| DB-10 | MEDIUM | Legacy downgrade deletes draft_id=NULL sends; compliance downgrade deletes SHA256-only suppression tombstones | PARTIAL: fail downgrade when protected data exists; use application rollback instead of destructive schema rollback |
| DB-11 | MEDIUM | Complete/stopped legacy leads are excluded from adoption and therefore late replies cannot match | PARTIAL: adopt history for all applicable legacy states; enqueue only eligible active sequences |
| DB-12 | LOW | privacy logs erased email; bounce/mailbox reasons retain recipient PII; local dryrun/stub artifacts/backups require separate retention review | PARTIAL: redact durable logs; define and inspect complete retention surface |
| DB-13 | LOW | Zero/unknown-send stopped rows receive invented legacy sent history | PARTIAL: retain no-repeat safety without presenting synthetic history as confirmed send |
| DB-14 | LOW | Alias conflicts first-win; duplicate CSV headers last-win; invalid timestamps silently become now | PARTIAL: reject ambiguous/conflicting input; report invalid dates explicitly |
| DB-15 | LOW/INFO as reported | Global lead-email uniqueness; import can move campaign across workspace; queue dedupe check/insert race; normalization/index limits | PARTIAL: prioritize cross-workspace refusal and queue concurrency; schema/business decisions require integrated review |
| DB-16 | INFO | Linear migration chain, no metadata drift, FKs/cascades inspected | REPORTED COMPLETE—UNVERIFIED against current environment; historical empty-data cycle successful |

## Source mapping and blocked external gate

Historically measured: unambiguous NOT_STARTED variants with blank/zero sends queue enrich; any positive sent count prevents fresh Email 1. INPROGRESS/IN_PROGRESS and PAUSED with 1/2 sends become active. COMPLETED with 3+ sends becomes complete, but with 1/2 resumes. BLOCKED/STOPPED stops. Unknown status without send count is refused rather than fresh; unknown status with send count becomes active. Replied flag stops. Missing required status/count headers aborts import.

No real Smartlead exports were found by the prior assignment: fixtures are generic/synthetic. **BLOCKED**: actual headers, distinct status/category/flag values, count meaning (last sent versus next step), timezone/date format, suppression and exclusion shapes, exact sequence variants and sender aliases, and pause-time counts must be obtained and reconciled. Code hardening and synthetic tests cannot certify real-account reconciliation. Do not pause the production campaign or import against production without specific authorization.

Ordered continuation: root verifies inherited tests in a fresh isolated disposable database; fixes terminal mapping, suppression/exclusions and nonwaivable blockers; fixes replay/reconciliation/adoption; adds regression for protected-data downgrades; reruns integrated Gmail/migration/ingest/privacy suites; performs real-export dry run only when source exports are supplied. Root owns application edits; migration recovery specialist owns this evidence note.
