# Handoff: Smartlead migration audit (continuation)

Project `smartlead` (MazzSmartLead), `C:/Users/sam_r/Claude/Cowork1/PROJECTS/smartlead`.
Branch `audit/migration-readiness-2026-10-15` @ `c55de0d`, no upstream, no PR. Nothing committed: ~54 changed/untracked paths.

> **Update 2026-10-19 (supersedes "Not done" and "Next steps" 1-3 below).**
> - Full suite on the integrated tree: 1083 passed, 1 failed (WEB-11, schema).
> - Wave 2 is done:
>   - Compliance auditor: no guardrail weakened.
>   - QA: found QA-01..03, all now fixed.
>   - Qualification review: found QUAL-01..05, all now fixed.
> - All five docs are written and the matrix statuses are updated. Start at `docs/migration-audit/README.md`.
> - Still open: commit plus an independent review of the 2026-10-19 lead fixes, and the user-owned items in steps 4-5. Decision: NO-GO.

## Goal
Audit the platform and the Oracle VM for the Smartlead cutover, fix defects locally (never production), and finish
with a GO/NO-GO report in `docs/migration-audit/`. Brief: `docs/CLAUDE-CODE-MIGRATION-AUDIT-PROMPT.md`. Decision stays NO-GO.

## Done (uncommitted; checked as stated)
- Send/release integrity (`pipeline/send.py`, `sequence.py`, `mailbox.py`, `web/routes/leads.py`, `outbox.py`, `config.py`, `ops.py`, `integrations/email_sender.py`):
  - A release is bound to its approval: approve stamps `approved_at = now()` in the DB, and send refuses a job created before it (WEB-01).
  - Reject and edit return 409 while a send attempt is unresolved.
  - The company cap counts in-flight and ambiguous first sends (SND-02).
  - A live follow-up reads its mailbox inline before sending; this replaced the 30-minute gate (GML-1).
  - Live sending refuses `SEND_WINDOW` off (SND-07).
  - A failure building the Gmail client counts as "not sent" (SND-10).
  - `resolve-send` is audited and `--by` is required (BP-03).
  - Proving tests: `tests/test_recovery_send.py`, `test_audit_sending.py`, `test_audit_gmail.py` (GML-1 tests at the end), `test_ops.py`. The targeted runs passed.
- Agent fixes, each passing its own targeted run (summaries in `docs/migration-audit/BUSINESS-PROCESS.md` and the session notes below):
  - **Web:** WEB-02..30 minus WEB-11/19/23/31/33/34; `tests/test_audit_web.py` 92/93.
  - **Smartlead importer and adoption:** DB-01..15 and GML-4/5; `ingest/smartlead.py`, `validate.py`, two migration downgrades.
  - **SSRF:** new `integrations/safe_http.py`, wired into `company.py`; `httpx` moved to base deps in `pyproject.toml`.
- `scripts/oracle_setup.sh`: the backup cron now uses the verified `ops backup` and sets `BACKUP_DIR`. Repo only; the VM is untouched.
- Docs written: `docs/migration-audit/BUSINESS-PROCESS.md` (open decisions OD-01..20, findings BP-01..06) and `REQUIREMENTS-MATRIX.md`. Statuses predate the latest fixes.
- Full suite on the integrated tree, before the last test edits: 10 failed, 1039 passed.
  - 1 is WEB-11, a known schema gap.
  - 1 is fixed since: `test_deploy_scripts` with `SEND_WINDOW=true`.
  - 8 are still open: `test_launch_readiness.py` live-config fixtures inherit `SEND_WINDOW=false` from conftest.

## Not done
- `test_launch_readiness.py`: add `SEND_WINDOW=true` to its live-sending env (see `tests/test_deploy_scripts.py`), then rerun the full suite.
- Wave 2 died at the spend limit with no results: independent QA (`tests/test_qa_integration.py`), the read-only compliance-auditor, and the qualification review (`tests/test_audit_qualify.py`). Each needs relaunching.
- Not written: `README.md`, `FINDINGS.md`, `VERIFICATION.md`, `VM-READINESS.md`, `CUTOVER-RUNBOOK.md` in `docs/migration-audit/`.
- Open, not fixed:
  - WEB-11: session revocation needs a `users.session_epoch` column.
  - WEB-33: htmx SRI.
  - SND-13 / DB-15: duplicate-job race, needs a partial unique index.
  - DB-13: unconfirmed-send flag.
  - BP-01: new imports can't route to the launch campaign, which has no segment.
  - WEB-01 residual: re-releasing while a stale job is RUNNING dedupes onto the doomed job.

## Next steps
1. Fix the `test_launch_readiness.py` fixture, then rerun everything:
   `bash <scratchpad>/runtests.sh mazz_audit_full_test -- -q -p no:cacheprovider -rfE tests`. The runner is copied in the session scratchpad below; DB names must match `mazz_audit_*_test`.
2. Relaunch wave 2 (QA, compliance-auditor, qualification review) with `BRIEF.md` and `FIXES-FOR-REVIEW.md` from the scratchpad. Reviewers must not review fixes they authored.
3. Write the five remaining docs. Update the matrix statuses from the verified results.
4. **needs user:**
   - Decide OD-01 (delay semantics), OD-02 (Eastern cap day), OD-03 (overdue pacing), OD-05 (4→3 step mapping) and OD-18 (routing new batches). See `BUSINESS-PROCESS.md`.
5. **needs user:**
   - Point DNS for app./outreach.getsearchsitemarketing.com at the VM.
   - Provide Smartlead exports A/B/C and the campaign settings.
   - Get Zapmail's written OK.
   - Connect the 4 mailboxes.
   - Have Maaz confirm the postal address.
   - Set up off-VM backups plus key escrow for `/etc/mazz/env`.
   - Decide what to do with the `tests/` folder and the `mazz_test` DB on the VM.
   - Rotate the SSH passphrase and the Oracle password.
   - Give go-live approval.

## Pointers
- Session scratchpad: `C:/Users/sam_r/AppData/Local/Temp/claude/C--Users-sam-r-Claude-Cowork1-PROJECTS-smartlead/180d7b71-de50-4140-b8f6-32ff1178ff48/scratchpad/`.
  - `BRIEF.md`: agent rules and instruction conflicts.
  - `FIXES-FOR-REVIEW.md`: every fix, its author and its residuals.
  - `web_report.txt`: WEB-01..35.
  - `runtests.sh`, plus `r_*.log` test logs.
- Recovery evidence: `docs/migration-audit/HANDOFF-RECOVERY.md` and `docs/migration-audit/evidence/*.md`. The VM facts are there too: DNS points elsewhere, there's no TLS, backups are local only, and `ops health` falsely reports healthy.

## Limits
- The VM is production and read-only: no restarts, setup script, migrations, tests or sending there.
- Tests run only on `mazz_audit_*_test` DBs, with stubs and no network.
- Commit only when asked.

## Do not redo
- VM code identity is settled (CRLF-normalised hashes).
- Never guess Smartlead headers.
- The approval binding uses DB `now()` on both sides on purpose: Python and DB clocks differ.
- Test sessions use `expire_on_commit=False`, so set relationships, not raw FK ids, in fixtures.
- Don't embed heredocs that contain `EOF` inside bash heredocs. Write a script file instead.
