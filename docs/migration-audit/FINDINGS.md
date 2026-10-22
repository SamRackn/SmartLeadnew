# Findings register

**Revision:** branch `audit/migration-readiness-2026-10-15`, base `c55de0d`, plus the uncommitted audit fixes. **Date:** 2026-10-19.

Line numbers describe the working tree on that date. "Retest" names the test that fails without the fix. Each one was run on the integrated tree; the full-suite result is in `VERIFICATION.md`.

| Term | Meaning |
|---|---|
| CONFIRMED | Reproduced by a test, or observed on the server (SO) |
| SUSPECTED | Code-inspected only |
| FIXED | Fix in the working tree, with a retest passing on the integrated tree |
| OPEN | Not fixed |
| DECISION | Waits on a business decision (`BUSINESS-PROCESS.md` §8) |
| BLOCKED | Waits on an external input |

**Owners:** web = review-app-engineer · send = pipeline-engineer · gmail and research = integrations-engineer · db = db-architect · qual = drafting-engineer · ops = VM owner (Sam).

## Summary

- **Runtime blockers:** 2, both BLOCKED on user action (VM-01 DNS/TLS, VM-02 mailboxes).
- **High:** 10 fixed in code. 2 open, both operational (VM-03 backups and escrow, VM-04 alerting).
- **No high-severity code defect is open.** Medium and low items are listed below.
- The decision remains **NO-GO** on runtime, external and business gates (`README.md`).

## 1. Security and correctness: high

| ID | Area | Finding | Evidence | Fix | Retest | Status |
|---|---|---|---|---|---|---|
| WEB-01 | Release integrity | A release job outlived its approval. Approve, release, reject, edit, approve again, and the old job sent. | CONFIRMED | Approve stamps `approved_at = now()` (DB clock). `send_lead` refuses a job created before it. Reject, edit and approve delete pending send jobs. | `test_audit_web.py::test_reject_cancels_the_pending_send_job...`; `test_recovery_send.py`; `test_qa_integration.py` stale-approval tests | FIXED |
| WEB-02 | Review | Reject turned a SENT lead into HELD | CONFIRMED | Reject on SENT refused (409) | `test_audit_web.py::test_reject_cannot_turn_a_sent_lead_into_a_held_one` | FIXED |
| WEB-07 | Tenancy and privacy | Lead detail showed person facts about other people from the shared company cache | CONFIRMED | `_fact_visible_for` filters out other people's facts | `test_audit_web.py::test_scoped_lead_detail_does_not_show_person_facts...` | FIXED |
| SND-02 / QA-03 | Company cap | The cap was not re-checked at send. Then the re-check never bound, because drafting already sets `sequence_state='active'`. After a reject, edit and re-approve cycle, two Email 1s went out under a cap of 1. | CONFIRMED (QA) | Re-check at send under `SEND_SELECT_LOCK_KEY`, counting only colleagues actually started (a sent row or an in-flight attempt) (`sequence.py` `check_company_cap(at_send=True)`) | `test_qa_integration.py::test_company_cap_holds_at_send_after_a_reject_edit_cycle`; `test_recovery_send.py` | FIXED |
| GML-1 / SND-08 | Follow-ups | A follow-up could go out after an unseen genuine reply (stale sync) | CONFIRMED | A live follow-up syncs its thread's mailbox inline before sending. A read error defers it; a stop holds it. | `test_audit_gmail.py` (GML-1 tests); `test_qa_integration.py::test_a_released_live_follow_up_reads_the_thread...` | FIXED. Residual: a reply arriving between that read and the Gmail call. |
| DB-01 | Smartlead import | COMPLETED legacy leads with 1–2 sends resumed their sequence | CONFIRMED | Only INPROGRESS, IN_PROGRESS and PAUSED continue. An unknown status is held as `held_unclear`, which blocks commit. | `test_audit_migration.py` | FIXED (synthetic data only; real exports BLOCKED) |
| DB-02a | Smartlead import | A suppression file with an `Email Address` header imported as empty, without blocking | CONFIRMED | Shared aliases. Invalid rows block commit. | `test_audit_migration.py` | FIXED |
| DB-02b | Smartlead import | An exclusion row with both email and domain blocked only the email | CONFIRMED | The domain is enforced | `test_audit_migration.py` | FIXED |
| RES-01 | Research | SSRF: the scraper followed redirects to private, loopback or metadata addresses. Responses were unbounded. | SUSPECTED → fixed | New `integrations/safe_http.py`: pinned-IP fetch, per-hop validation, byte cap. `ProviderUnsafeTarget` is never treated as `ProviderBlocked`. | `test_recovery_research.py`; `test_audit_qualify.py` SSRF test | FIXED |
| QUAL-02 | Verifier (guardrail 3) | A statistic in a first-person sentence ("We got one recent client 50+ potential clients") passed verification with no `proof_points` row | CONFIRMED (QUAL) | Digits in sender voice must trace to proof points or notes. Meeting lengths and spelled-out numbers stay exempt. (`verify.py` `scan_numbers`) | `test_audit_qualify.py::test_an_unattributed_statistic_in_sender_voice_is_held`; `test_verify.py` corpus: 0/39 false negatives, 0/35 false positives | FIXED |
| VM-03 | Backups | Backups are local only, with no key escrow | SO | Off-VM copy plus escrow of `/etc/mazz/env` | — | BLOCKED (user) |
| VM-04 | Monitoring | `ops health` said healthy with all mailboxes disconnected. No alert channel. | SO | Health fails on needs-reconnect once live; an alert channel (OD-19) | — | OPEN / DECISION |

## 2. Medium

| ID | Finding | Status / note |
|---|---|---|
| QUAL-01 | "practice area" and "practice areas" counted as two distinct terms. An accounting site qualified as a law firm. | FIXED (`qualify.py`: plurals folded). Retest: `test_audit_qualify.py`. |
| QUAL-03 | The lead-record fallback fired whenever the cache held no homepage, services or contact fact, not only when blocked (guardrail 2) | FIXED: `qualify.fallback_lead` requires a recorded `ProviderBlocked`, and the verify stage goes through the same gate. Two tests were adjusted, each with a written justification: `test_sequence.py::test_blocked_site_falls_back...` now records the block, and a new assertion checks that no fallback happens without one. |
| QUAL-05 | Regression from RES-01: a successful job-board hop was fetched and then discarded (deep-research path) | FIXED (`company.py` indentation) |
| QA-02 / SND-13 | Two concurrent release clicks queued two send jobs | FIXED: `enqueue_job` takes a per-lead transaction advisory lock for `send`. Its test now really runs concurrently, and a mutation check shows it fails without the lock. Ceiling: send only. A partial unique index would cover every kind. |
| QA-01 | A release made while a stale job ran was swallowed silently | FIXED: 409 asking the operator to release again (`outbox.py`) |
| WEB-03, 08, 13, 17, 18, 20, 22, 24, 25, 28, 30 | Approve guards, removal count leak, CSRF, unsubscribe-host normalisation, unknown-token POST, `javascript:` URLs, labels, metrics, admin audit, inbox limit | FIXED (`test_audit_web.py`) |
| SND-01, 03, 04, 05, 06, 07 | Committed holds, Eastern cap day, pinning, worker lock, preflight running-sends check, live boot refuses the window turned off | FIXED. SND-03's day boundary still needs confirming (OD-02). SND-05's lock is lost if the DB drops. |
| DB-03, 04, 04b, 06, 07, 09, 10, 11, GML-4, GML-5 | Late suppression holds, one outcome per row, replay idempotent, adoption retries and step reconciliation, a count-only override, protected downgrades | FIXED (`test_audit_migration.py`, `test_audit_gmail.py`) |
| GML-2, 3, 6, 7, 8, 10 | Gone messages, fallback scan, opt-out scope, quota detection, MIME before the marker, false opt-outs | FIXED (`test_audit_gmail.py`) |
| WEB-11 / COMP-04 | Logout does not invalidate the session server-side. A stolen cookie works for up to 8 h. | FIXED 2026-10-22: `users.session_epoch` (migration d1b8f9a0c2e3) stamped at login, bumped by logout and `set-password`; `test_audit_web.py::test_logout_invalidates...` passes. |
| WEB-06 | Placeholder regex misses newlines and more than 40 characters inside braces | OPEN, SUSPECTED (qual, `draft.py:281`) |
| COMP-01 | WEB-29's audit redaction has no test, and `audit_events` is not in the delete-by-person residual check | OPEN (db) |
| BP-01 | New ZoomInfo imports cannot route to the launch campaign, which has no segment | DECISION OD-18 |
| BP-02 / GML-15 | Replies outside INBOX or outside the thread are never seen | DECISION OD-09 |
| BP-04 | Unsubscribes through Smartlead-sent links stay in Smartlead | DECISION OD-06 |
| DB-08 | Adoption queues every overdue follow-up at once | DECISION OD-03 |
| VM-05, 06, 07 | rpcbind exposed; 956 MB RAM; old backup cron | OPEN (ops; repo fix exists for VM-07) |

## 3. Low and info

| ID | Finding | Status |
|---|---|---|
| QUAL-04 | A site with no homepage and no person facts landed `no_angle` with no `skip_reason`, so it was missing from the unconfirmed count | FIXED (`enrich.py`) |
| WEB-04, 05, 09, 10, 12, 14, 15, 16, 26, 27, 29 | Idempotent approve, placeholder at release, roster scope, bad scope value, 8 h session, docs not public, framing, PBKDF2 iterations, metrics scope, hostile parameters, audit redaction | FIXED |
| SND-09, 10, 11 / GML-9, 11, 13 | Thread mailbox, client construction counts as not sent, resolve-send clears due date, SenderNotConfigured does not disable a mailbox, DSN parsing, token errors | FIXED |
| BP-03 | `resolve-send` was unaudited with a defaulted actor | FIXED (`--by` required, audited) |
| COMP-02 | `--by` is free text, so an unknown email gives `user_id=NULL` | DECISION: Sam runs the CLI without an app account, so requiring a `users` row would lock out the owner |
| COMP-03 | The opt-out detector lost some recall ("don't stop emailing", text after `--`). The sequence still stops; no suppression row is written. | OPEN, SUSPECTED (gmail) |
| COMP-05 | Reject and approve read the lead without `FOR UPDATE`, leaving a narrow race with a send commit | OPEN (web) |
| DB-12 | Recipient address in `mailboxes.last_error` and logs; retention of backups and dry-run files | PARTIAL (logs redacted); retention DECISION OD-14 |
| DB-13 | Legacy rows with zero or unknown sends get synthetic sent history | OPEN (db): needs an unconfirmed flag |
| DB-14 | Ambiguous or conflicting headers; invalid dates become now | PARTIAL |
| DB-15 | Email unique across workspaces; an import can move a campaign across workspaces | OPEN (db) |
| GML-12 | OOO parser accepts past dates | PARTIAL |
| GML-14 | Sync has no total time budget | OPEN |
| GML-16 | OAuth state is not bound to the operator's session | Mitigation judged adequate by the reviewer; needs owner acceptance |
| WEB-19 | O(n) HMAC per `/u/` request | OPEN |
| WEB-23 | No revoke action on the approved state | OPEN |
| WEB-31 | Import reads the whole upload before its 25 MB check | OPEN |
| WEB-32 | Recreating a mailbox resets its sent-today count | OPEN |
| WEB-33 | htmx from unpkg with no SRI | OPEN |
| WEB-34 | `--forwarded-allow-ips "*"` in `render.yaml` (historical) | INFO |
| BP-05 | ORM default `daily_cap` is 30 (the seed and form use 15) | OPEN |
| BP-06 | A follow-up on a paused mailbox fails permanently and needs re-release | OPEN |
| QA-R1 | Preflight is the only gate on send jobs left pending while sending was off | OPEN (send) |
| VM-08, 09, 10 | Patches; credential rotation; test DB on production | OPEN (user) |
| Known gap | Verification proves a claim traces to evidence, not that the evidence is true. A hidden heading on a CPA's site confirms "law firm" (`test_audit_qualify.py::test_known_gap_hidden_heading...`). | OPEN (`CLAUDE.md` known gap). Contained on the cold path: the template interpolates no research text. |

## 4. Independent review coverage

| Fixes | Author | Reviewed by |
|---|---|---|
| Send and release (WEB-01, SND-02, SND-07, GML-1) | lead | QA, compliance auditor |
| Web | review-app-engineer | QA, compliance auditor |
| Importer | db-architect | compliance auditor (QA did not cover the importer) |
| SSRF | integrations-engineer | qualification reviewer (found QUAL-05) |
| QUAL-01 to 05, QA-01 to 03 | lead (2026-10-19) | Retested only by the reviewers' own tests. **No second independent review yet.** |
