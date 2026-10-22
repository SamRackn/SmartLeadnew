# Sending and Gmail recovery evidence

Recovered 2026-10-15. This report records historical evidence and read-only code reconciliation, not a fresh passing test result. No provider calls, database operations, scratch scripts, or tests were executed by the recovery reviewer.

## Evidence identity and ownership

Session: `ba2814f7-e8fd-4d08-87b7-60e87c323408`, under `C:/Users/sam_r/.claude/projects/C--Users-sam-r-Claude-Cowork1-PROJECTS-smartlead/`.

| Record | Assignment and initial ownership | Outcome |
|---|---|---|
| `subagents/agent-ab81a0d7ca8004bb3.jsonl` | Sending, sequencing, queue, pacing, crash recovery and preflight. Initially only `tests/test_audit_sending.py`; later fix ownership of send/mailbox/sequence/ops and associated tests. | Initial `SubagentHandback` exists. Fix wave PARTIAL at usage exhaustion. |
| `subagents/agent-a76d6802bb5a52098.jsonl` | Gmail transport, reply sync/classification, OAuth and legacy adoption boundaries. Initially only `tests/test_audit_gmail.py`; later replies/gmail_reader/email_sender/google_oauth and associated tests. | Initial `SubagentHandback` exists. Fix wave PARTIAL at usage exhaustion. |

The scratchpad for this session contains `COMMON-BRIEF.md`, `FIX-BRIEF.md`, `CHECKPOINT.md`, `runtests.sh`, `fix_replies.py`, and `fix_other.py`. Both scratch Python files were read; their replacements appear in current source. They are one-time edit scripts with old-text assertions, not recovery commands to replay.

The original common brief prohibited application edits. Subsequent coordinator messages plus FIX-BRIEF explicitly authorized each agent's file set. Cross-agent dependencies: Gmail GML-1/8/9 to sending; GML-4/5 adoption to database/migration agent; OAuth route handling to web agent. Current source changes observed here are in the shared checkout. These two records provide no evidence of separate worktree output requiring integration.

## Tests actually recorded

Commands below abbreviate the saved runner's full scratchpad path as `runtests.sh`. That runner selected `TEST_DATABASE_URL`, using the named disposable database. Transcript command lines often pipe output through tail/grep, so shell success alone is not evidence of pytest success.

| Command | Recorded result | Meaning |
|---|---|---|
| `runtests.sh mazz_audit_send_test -- -q -p no:cacheprovider tests/test_sending.py tests/test_sequence.py tests/test_queue.py tests/test_ops.py tests/test_routing.py` | 1 failed, 153 passed, 70.32s | Historical baseline; backup round-trip test hardcodes `mazz_test` filename. |
| `runtests.sh mazz_audit_send_test -- -q -p no:cacheprovider -rfE tests/test_audit_sending.py` | 8 failed, 1 passed, 2.87s | Before fixes; failures demonstrated SND-01 through SND-07, with two SND-01 cases. AST boundary control passed. |
| `runtests.sh mazz_audit_gmail_test -- -q -p no:cacheprovider tests/test_replies.py tests/test_smartlead_cutover.py tests/test_sending.py tests/test_adapters.py` | 192 passed, 62.23s | Historical baseline. |
| `runtests.sh mazz_audit_gmail_test -- -q -p no:cacheprovider tests/test_audit_gmail.py --no-header --tb=line` | 15 failed, 4 passed, 10.62s | Before fixes. Four passing controls: MIME/threading, header refusal, interrupted sync replay, history pagination. |
| `runtests.sh mazz_audit_gmail_test -- -q -p no:cacheprovider tests/test_audit_gmail.py tests/test_replies.py tests/test_adapters.py tests/test_sending.py tests/test_smartlead_cutover.py --no-header --tb=short -rf` | 5 failed, 206 passed, 76.87s | Last completed combined Gmail run. Adoption GML-4/5 still failed, plus interested-reply lift assertion and two UTC cap expectations. |

Some intermediate invocations produced partial filtered output or were retried. They do not replace the completed results above. No completed post-fix SND run is present. The sending agent's last confirmed action was adding explicit session advisory-lock release to `ops.py`; the Gmail agent's last substantive confirmed result was the combined run above. Both end with usage-limit messages, not final fix handbacks.

All recorded runs precede possible later shared-checkout edits. Therefore no table entry below is VERIFIED COMPLETE on the recovered integrated revision.

## Sending findings and current reconciliation

Original severity labels are retained; they are historical judgments, not acceptance of release risk.

| ID | Severity | Current status | Evidence and remaining work |
|---|---|---|---|
| SND-01 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | `send.py` now uses committing `_hold` for stopped/suppressed/unreadable-suppression refusals. Original two tests exercised actual worker rollback. Re-run integrated worker tests. |
| SND-02 | HIGH | PARTIAL | `send_lead` now calls `check_company_cap` before Email 1. Sequential regression unverified; in-flight starts are not reserved, see independent review below. |
| SND-03 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | `mailbox.day_window` now uses Eastern midnight, avoiding UTC reset during Western US business hours. Two existing sending tests still expect UTC reset and failed in last Gmail run. Document business-day definition and verify DST. |
| SND-04 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Campaign pin presence computed across all mailbox statuses; paused pins no longer spill to unrelated unpinned mailboxes. |
| SND-05 | MEDIUM | PARTIAL | Transaction advisory selection lock, unresolved attempt accounting and attempt-time spacing marker present; CLI worker session lock added and explicitly released. Company reservation gap remains; worker-lock failure/reacquisition behavior needs tests. |
| SND-06 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Preflight checks PENDING and RUNNING sends plus unresolved attempt markers. |
| SND-07 | LOW–MED | NOT STARTED | `config.py` unchanged; disabling send window still lacks live boot/preflight refusal. Existing audit regression remains. |
| SND-08 | MEDIUM | PARTIAL | Stale reply gate added at 30 minutes. It does not sync immediately before follow-up, so periodic-sync lag still permits a follow-up after an unseen genuine reply. |
| SND-09 | LOW | REPORTED COMPLETE—UNVERIFIED | Outbox `mailbox_for` selects thread parent's mailbox and is called for display/release. Re-check failed/paused mailbox integration. |
| SND-10 | LOW | PARTIAL | MIME validates before marker. Gmail service construction still occurs outside translated exception block, so pre-wire construction failure can retain an ambiguous marker. |
| SND-11 | LOW | REPORTED COMPLETE—UNVERIFIED | `resolve_send --sent` explicitly sets stopped/resolved-by-hand and clears due date. No Gmail thread is recovered by that operation. |
| SND-12 | INFO | BLOCKED | Delay implementation uses calendar days rolled off weekends; CLAUDE.md says business days, LAUNCH-PROMPT says roll to next business day. Business decision required. |
| SND-13 | LOW | NOT STARTED | Concurrent release can create duplicate pending/running jobs; no partial unique index. Send gates mitigate duplicate delivery but not failed jobs/operator noise. |
| L1/L2 | LOW | PARTIAL | New AST test broadens send-site search. Rehearsal still directly sends seeds without normal suppression/caps/booking. Review separately before any authorized live rehearsal. |

The initial sending audit read core send/queue/sequence/mailbox/ops/config files, relevant route/provider boundaries and rehearsal. It explicitly did not read `routing.py` in depth; routing baseline tests alone do not close that requirement.

## Gmail findings and current reconciliation

| ID | Severity | Current status | Evidence and remaining work |
|---|---|---|---|
| GML-1 | HIGH | PARTIAL | Non-stub follow-up freshness gate and ops stale-sync alert present. Covers dead sync, not fresh-but-unread incoming replies; see SND-08. |
| GML-2 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | `MessageGone` translates message/thread 404 and sync skips only those, retaining errors. Other exceptions re-raise before cursor advancement. |
| GML-3 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Fallback scans active/paused sequence threads regardless of age and recent sends for 45 days; DSN listing follows pagination. Cursor continuity and outage integration require rerun. |
| GML-4 | MEDIUM | NOT STARTED | Adoption still uses exported send count rather than reconciling Gmail thread history; last historical regression failed. Migration owner dependency. |
| GML-5 | MEDIUM | NOT STARTED | Skipped disconnected mailbox can turn absence-of-search into permanent legacy-thread-not-found hold; last historical regression failed. Migration owner dependency. |
| GML-6 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Reply opt-out suppresses original lead and other asking address, excluding own mailboxes. |
| GML-7 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Quota detection uses parsed reason plus case-insensitive detail. Review malformed errors and auth/rate distinction. |
| GML-8 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | MIME pre-render before attempt marker. Header injection refusal control existed already. |
| GML-9 | MEDIUM | PARTIAL | `SenderNotConfigured` no longer disables individual mailbox. Preflight decryption of every stored token remains absent; historical decrypt message misleadingly recommends reconnect. |
| GML-10 | MEDIUM | REPORTED COMPLETE—UNVERIFIED | Negated stop requests and `--` signature newsletter text no longer cause false opt-out. Existing manual-lift test still uses formerly misclassified text and failed. Preserve genuine opt-out negative cases when updating fixture. |
| GML-11 | LOW | REPORTED COMPLETE—UNVERIFIED | DSN Status/SMTP status preferred; dotted-quad IP prefix rejected. |
| GML-12 | LOW | PARTIAL | Explicit old year refused and >180-day OOO stops for human review. Parser still accepts yesterday, despite docstring saying no past dates; ambiguous numeric dates/default policy remain limitations. |
| GML-13 | LOW | REPORTED COMPLETE—UNVERIFIED | Token endpoint URLError/OSError/JSON failure translated to OAuthExchangeError. Callback/provider-state tests require integrated rerun. |
| GML-14 | LOW | NOT STARTED | Sync runs inline with per-request timeout but no total time budget; large mailbox can starve worker. |
| GML-15 | LOW | BLOCKED | History remains INBOX-filtered; archived/spam reply policy requires business decision and corresponding tests. |
| GML-16 | INFO | REPORTED COMPLETE—UNVERIFIED | Historical review: random mailbox-bound OAuth state, public callback, not operator-session-bound. Agent judged mitigation adequate; that judgment is not owner risk acceptance. |

## Independent integrated review

1. **WEB-01 remains incomplete for RUNNING jobs (HIGH, code-inspected).** `cancel_pending_send` deletes only PENDING jobs. A worker can claim a job, then pause before send. Reject/edit/approve leaves the old RUNNING release intact; handler subsequently reads current approved state and latest draft. A release is not tied to its copy. This is earlier than external-send TOCTOU and requires a regression, not risk acceptance. Schema-free approach: consistent lead-row lock in mutation/release routes; invalidate PENDING and RUNNING release rows on approval/copy changes; re-query job existence/status after obtaining the same lead lock in sender before selecting copy. Avoid cached ORM reads. Existing attempt markers must prevent post-marker historical-copy mutation.
2. **SND-02/05 company capacity reservation gap (MEDIUM under unsupported concurrent callers; code-inspected).** `SEND_SELECT_LOCK_KEY` ends at marker commit, while `active_at_company` counts only active/paused sequences established after receipt. A second send can pass company cap while first provider call is outstanding. CLI singleton helps, but new claim that send-time lock alone makes company caps exact is false. Count unresolved first-step attempts as occupied company slots, or retain a coherent reservation until booking. Test with independent sessions and two mailbox slots.
3. **SND-08/GML-1 residual is material.** Freshness timestamp only proves earlier sync succeeded; it does not establish that a reply arriving afterward has been read. Define immediate pre-follow-up sync and fail-closed error semantics, then re-check stopped/paused/suppressed state before marker. A final provider-call TOCTOU window still cannot be eliminated; document precisely.
4. **GML-9 remains operationally partial.** All mailboxes can remain apparently active while encryption key is wrong; health stale-sync eventually alerts but preflight does not prove token recovery. Key restoration and authorized provider verification are separate gates.

## Ordered continuation and evidence expectations

1. Root owns application changes. Correct stale RUNNING release invalidation with lead-lock ordering and direct-request/claimed-worker regression; independent reviewer checks both cancellation-first and sender-first orderings.
2. Root/migration owner reconcile GML-4/5 and actual export history; do not infer safe sequence continuation from a send-count maximum alone when copy/step mappings are unapproved.
3. Root finish send-window refusal, company reservations, and pre-wire factory classification. Add focused negative tests without relaxing caps or suppression.
4. Update only tests asserting intentionally superseded behavior, documenting why; use an actually auto-suppressed phrase for manual-lift coverage.
5. Run sending/Gmail and affected integration suites in distinct `_test` databases via the fixture's actual `TEST_DATABASE_URL`, followed by final integrated suite. Record exact revision/diff identity, environment, command, exit status and counts.
6. Obtain missing business decisions and provider/runtime evidence. No normal test may send real prospect mail or spend provider credits. This evidence report cannot close VM authorization, Gmail round-trip/threading, real export reconciliation, backup/key restoration or release approval gates.
