# Smartlead cutover audit: decision

## Decision: **NO-GO**

The code is close to ready. Every high-severity code defect found is now fixed and retested. Launch is still blocked by four things outside the code:

- the hosting runtime (DNS/TLS),
- the connected mailboxes,
- disaster recovery and alerting,
- real Smartlead exports, plus business decisions on how legacy state maps across.

None of these can be closed from the repository.

| | |
|---|---|
| Assessed | 2026-10-19 (VM observed read-only 2026-10-15) |
| Revision | Branch `audit/migration-readiness-2026-10-15` at `c55de0d` plus **uncommitted** fixes. Fingerprint in `VERIFICATION.md` §1. Not committed, not deployed. |
| Environment | Local disposable PostgreSQL 17 test DBs. Production VM untouched. |
| Full suite | **1083 passed, 1 failed** (WEB-11, open) |
| Independent reviews | Compliance auditor: no guardrail weakened. QA: 3 defects. Qualification: 5 defects. All 8 fixed since, by the lead; not yet re-reviewed. |

## Priority actions (in order)

1. **Commit and review this tree.** About 55 paths are uncommitted. Then have someone other than the lead review the 2026-10-19 fixes (QUAL-01..05, QA-01..03).
   - _Owner:_ Sam.
2. **DNS:** point `app.` and `outreach.getsearchsitemarketing.com` at `150.230.176.238`, then confirm TLS (VM-01).
   - _Owner:_ Maaz / the DNS owner.
3. **Recovery:** set up an off-VM backup and escrow `/etc/mazz/env`, with an RPO/RTO agreed (VM-03, OD-17).
   - Add a Linux alert channel for `ops health` (VM-04, OD-19).
   - _Owner:_ Sam.
4. **Mailboxes:** get Zapmail's written OK, then connect the 4 mailboxes. Maaz confirms the postal address.
   - _Owner:_ Maaz.
5. **Decisions:** answer OD-01, 02, 03, 05, 06, 07 and 18 (`BUSINESS-PROCESS.md` §8).
   - _Owners:_ Sean, Maaz and Sam.
6. **Exports:** provide Smartlead exports A, B and C from the paused campaign. Dry-run the import against them.
   - _Owners:_ Maaz → Sam.
7. **Deploy:** deploy the reviewed revision as a whole. Drop `mazz_test` and `tests/` from production. Disable rpcbind and patch.
   - Then follow `CUTOVER-RUNBOOK.md`.
   - _Owner:_ Sam.

## Coverage ledger

| Area | Covered | Not covered |
|---|---|---|
| Send, release and caps | Code, unit, integration, concurrency, compliance review | Live Gmail |
| Qualification, research, verification | Code, unit, adversarial tests, SSRF | Provenance at cache ingest (known gap) |
| Web, tenancy, auth | Code, route tests | Browser walkthroughs; WEB-11 |
| Replies, OOO, bounces | Unit with fakes | Real DSN and reply shapes |
| Smartlead importer and adoption | Synthetic exports | **Real exports** |
| VM | Read-only observation 2026-10-15 | Re-check, deployment, restore with tokens |
| Business process | `BUSINESS-PROCESS.md`, `REQUIREMENTS-MATRIX.md` | Client sign-off |

## Remaining questions (need the user)

- **Open decisions:** OD-01 (follow-up delay meaning), OD-02 (cap day), OD-03 (overdue pacing), OD-05 (4→3 step mapping), OD-18 (routing new batches).
- **COMP-02:** should `resolve-send --by` have to match an app login? Sam has no app account today.
- **Production test leftovers:** what happens to `tests/` and `mazz_test` on the VM?
- **Credentials:** rotate the SSH key passphrase and the Oracle password.

## Documents

- [FINDINGS.md](FINDINGS.md): every finding, with severity, status, retest and owner
- [VERIFICATION.md](VERIFICATION.md): commands, results, test identity, scenario coverage, unavailable checks
- [VM-READINESS.md](VM-READINESS.md): production host topology, drift and findings
- [CUTOVER-RUNBOOK.md](CUTOVER-RUNBOOK.md): ordered preflight, cutover, observation and rollback
- [BUSINESS-PROCESS.md](BUSINESS-PROCESS.md): actors, lifecycle, migration mapping, open decisions
- [REQUIREMENTS-MATRIX.md](REQUIREMENTS-MATRIX.md): 51 requirements with current status
- [HANDOFF-RECOVERY.md](HANDOFF-RECOVERY.md) and [evidence/](evidence/): recovered history of the interrupted 2026-10-15 session
