# Verification

**Date:** 2026-10-19. Every result below is local: Windows 11, Python 3.12.10, PostgreSQL 17.11.

**Nothing here was run on the VM.** Tests use stubs and fakes, with no network, no provider calls and no sending.

## 1. Test identity

| Item | Value |
|---|---|
| Revision | Branch `audit/migration-readiness-2026-10-15` at `c55de0d`, plus uncommitted changes (48 tracked files, +1349/−281) |
| Untracked code | `src/mazz/integrations/safe_http.py` and 8 test files (§3) |
| Fingerprint | sha256 of `git diff -- src tests migrations scripts pyproject.toml`: `8402f48dfc8f4b41…` |
| Untracked fingerprint | `src`/`tests` untracked files, concatenated in sorted order: `105647c0e9a614c1…` |
| No edits since the run | Confirmed by `find src tests -newer` against the snapshot taken at launch |
| Database | Disposable `mazz_audit_final_test`. The runner refuses any name not matching `mazz_audit_*_test`. `TEST_DATABASE_URL` is built from `.env` credentials, and the password is never printed. |
| Runner | `bash <scratchpad>/runtests.sh <db> -- <pytest args>`, where `<scratchpad>` is the 2026-10-16 session scratchpad named in `HANDOFF.md` |

## 2. Full suite on the final integrated tree

```
runtests.sh mazz_audit_final_test -- -q -p no:cacheprovider -rfE tests
1 failed, 1083 passed, 1 warning in 607.37s
FAILED tests/test_audit_web.py::test_logout_invalidates_the_session_cookie_server_side
```

- The failure is **WEB-11**, which is known and open: server-side session revocation needs a schema column.
- The warning is Starlette's httpx deprecation notice.
- Verifier corpus inside the run: 0/39 false negatives and 0/35 false positives.

| Earlier run | Result |
|---|---|
| Baseline at `c55de0d` before any fixes (2026-10-15) | 873 passed, 1 failed (a backup test hard-coded `mazz_test`) |
| Before 2026-10-19 | 1039 passed, 10 failed |
| 2026-10-19, before the `test_launch_readiness` fixture fix | 1042 passed, 9 failed |

## 3. Audit test files

All of these are new and untracked. Each assertion was written to state the correct behaviour.

| File | Author | Scope |
|---|---|---|
| `test_audit_sending.py` | sending specialist | Caps, worker lock, send-job sites (AST) |
| `test_audit_web.py` | web specialist | WEB-01..35 (92/93; WEB-11 fails) |
| `test_audit_migration.py` | db specialist | Smartlead importer and adoption |
| `test_audit_gmail.py` | gmail specialist and lead | Reply sync, DSN, OOO, GML-1 inline read |
| `test_recovery_send.py`, `test_recovery_research.py` | lead / research | Release binding, company cap in flight, SSRF |
| `test_audit_qualify.py` | qualification reviewer (independent) | 19 tests. 5 strict-xfail defects found; all 5 now pass. |
| `test_qa_integration.py` | QA reviewer (independent) | 14 cross-subsystem tests. 3 strict-xfail defects found; all 3 now pass. |

## 4. Existing tests changed, with justification

| Test | Change | Why |
|---|---|---|
| `test_launch_readiness.py` `LIVE` config | Adds `send_window=True` | SND-07: live sending now requires the window. Conftest turns it off for unit tests. |
| `test_deploy_scripts.py` | `SEND_WINDOW=true` in the live env case | Same reason |
| `test_sequence.py::test_blocked_site_falls_back...` | Records `ProviderBlocked` on the cache rows before the fallback assertions, and adds an assertion that no fallback happens without it | QUAL-03: the old premise ("no facts" = blocked) was the defect. The enrich half of the test is unchanged and drives the real blocked path. |
| `test_qa_integration.py` `_two_concurrent_releases` | The second click runs on its own thread | With a correct dedupe, the second enqueue waits for the first commit; in one thread that is a self-deadlock. Mutation check: with the lock disabled, `test_two_concurrent_release_clicks_queue_one_job` fails. |
| `test_ops.py`, `test_sending.py`, `test_replies.py`, `test_smartlead_cutover.py`, `test_compliance_launch.py` | Changed in the earlier fix wave | See `HANDOFF-RECOVERY.md` and the specialists' reports. Not re-audited line by line in this pass. |

## 5. Critical scenarios (audit brief §8)

| # | Scenario | Evidence | Result |
|---|---|---|---|
| 1 | Lead → approve → separate release → send → follow-up → reply → human qualified tag | `test_qa_integration.py::test_eligible_lead_end_to_end...` | PASS (stub sender) |
| 2 | Suppression after release, during send, store failure | QA §3; `test_audit_sending.py`; compliance review | PASS |
| 3 | Colleagues, cache reuse, company cap, workspace isolation | `test_audit_qualify.py` cache test; QA-03 fix; `test_audit_web.py` tenancy | PASS |
| 4 | Unconfirmed vertical, blocked site, outage, false evidence, unsupported claim, edit after approval | `test_audit_qualify.py` | PASS. False source evidence passes verification (known gap). |
| 5 | Double click, competing workers, ambiguous outcome, reconciliation | `test_qa_integration.py` §5; `test_sending.py`; `test_ops.py` resolve-send | PASS. A real Gmail ambiguity is not exercised. |
| 6 | Quota, revocation, pause, bounce, day rollover, spacing, DST/weekend | `test_sending.py`, `test_audit_sending.py`, `test_audit_gmail.py` | PARTIAL: no explicit DST-boundary test; holidays not modelled (OD-04) |
| 7 | Reply or unsubscribe before a queued follow-up; OOO; sync replay | QA §7; `test_replies.py`; `test_audit_gmail.py` | PASS |
| 8 | Work created while sending is off, then switched on | QA §6 | PASS. Residual: preflight is the only gate on a still-pending job (QA-R1). |
| 9 | Realistic Smartlead export replay | `test_audit_migration.py` | **BLOCKED**: synthetic fixtures only. No real exports. |
| 10 | Backup restore and restart in isolation | `test_ops.py::test_a_real_backup_round_trips`; historical VM restore (`VM-READINESS.md` §4) | PARTIAL: no restore with pending work or token decryption |
| 11 | Maaz and Sean's browser workflows | Route-level tests only (`test_web.py`, `test_audit_web.py`) | **NOT DONE**: no browser walkthrough |

## 6. Independent reviews (2026-10-19)

None of the three reviewers authored the fixes they reviewed.

| Reviewer | Scope | Result |
|---|---|---|
| Compliance auditor (read-only) | Guardrails 1–7, tenancy, attribution | No guardrail is weakened by the diff. No high or critical findings. COMP-01..05 are low/medium (`FINDINGS.md`). |
| QA (general-purpose) | Cross-subsystem behaviour | 11 passed, 3 defects (QA-01..03), now fixed by the lead |
| Qualification reviewer (drafting-engineer) | Qualify, research, verify, SSRF | 14 passed, 5 defects (QUAL-01..05), now fixed by the lead |

**Gap:** the lead's 2026-10-19 fixes (QUAL-01..05, QA-01..03) are proven only by the reviewers' own tests. Nobody other than the lead has reviewed them.

## 7. Unavailable checks and why each matters

| Check | Why it was not run | What it leaves open |
|---|---|---|
| Real Smartlead exports A/B/C | Not supplied | Header shapes, status values and count meaning are unconfirmed. The importer is proven on synthetic data only. |
| Live Gmail send, read and threading | No connected mailboxes; no authorisation to send | OAuth grant, quotas, threading and real DSN shapes |
| VM re-inspection | VM is production and read-only; nothing changed since 2026-10-15 | Drift since then; deployment of this revision |
| Seed rehearsal | Needs seeds (OD-20) and authorisation | Deliverability headers, the real unsubscribe round trip |
| Browser workflows | Not run | Operator usability and error messages |
| Restore with pending jobs and tokens | No off-VM backup; no tokens yet | Disaster recovery (RTO/RPO) |
| Dependency advisories | Not run in this pass | Known-vulnerable dependencies |
