# Web audit recovery ledger

Recovered 2026-10-15 from `ba2814f7-e8fd-4d08-87b7-60e87c323408/subagents/agent-a396fed1d7c9bb94a.jsonl` and main-session SendMessage fix assignment. Historical findings are not current verification. Agent initial handback completed; fix wave stopped at usage limit. Last confirmed action: successful `app.py` write at `2026-10-15T22:37:16.917Z`. No post-fix test execution recorded. All present edits reached this checkout; no alternate worktree recovered for this assignment.

Recorded baseline command: `runtests.sh mazz_audit_web_test -- -q -p no:cacheprovider tests/test_web.py tests/test_workspaces.py tests/test_metrics.py tests/test_compliance_launch.py`: 153 passed, one warning, 204.51 seconds. Final initial audit command targeted `tests/test_audit_web.py`: 51 failed, 42 passed, one warning, 101.10 seconds, corroborated by scratchpad `web_run2.txt`. Earlier test-harness mistakes were corrected before this run. These results predate fix-wave edits and cannot verify them.

Severity below is the initial report's classification. CONFIRMED means historical test or exact trace, not a current passing regression. Present changes require isolated tests and independent review.

| ID | Severity | Initial evidence/finding | Recovery state / residual |
|---|---|---|---|
| WEB-01 | HIGH | Reject/edit/reapprove reuses pending release; audit worker regression failed | PARTIAL: PENDING jobs deleted; RUNNING job still unbound to draft/approval version. Remains HIGH. See critical review below. |
| WEB-02 | HIGH | Reject sent draft mutates sent history; test failed | Present SENT/receipt guards, UNVERIFIED; concurrent pre-marker rejection remains unresolved. |
| WEB-03 | MEDIUM | Suppressed/stopped lead approval; tests failed | Present READY/suppression/stopped guards, UNVERIFIED. |
| WEB-04 | LOW | Second approval overwrites actor/time; test failed | Present idempotent return, UNVERIFIED. |
| WEB-05 | LOW | Release queues unresolved fields; test failed | Present placeholder checks, UNVERIFIED; regex gap WEB-06 remains. |
| WEB-06 | MEDIUM | SUSPECTED placeholder newline/length regex bypass | Pipeline scope; not resolved. CR/LF subject guard now present but not equivalent to all placeholder checks. |
| WEB-07 | HIGH | Person fact of another prospect displayed at shared company domain; test failed | Present fact/scored filters in detail context, UNVERIFIED across all render paths. |
| WEB-08 | MEDIUM | Global removal confirmation reveals other workspace count; test failed | Present visible-count filter while applying global hold, UNVERIFIED. |
| WEB-09 | LOW | Outbox roster reveals other campaign mailboxes; test failed | Present scoped roster, UNVERIFIED; shared unpinned accounts intentionally remain visible. |
| WEB-10 | LOW | Unicode numeric scope gives 500; test failed | Present ASCII/length guard, UNVERIFIED. |
| WEB-11 | MEDIUM | Captured signed cookie survives logout; test failed | NOT FIXED; Claude deferred session epoch/schema change. Password change also does not revoke cookies. |
| WEB-12 | LOW | Cookie lifetime 14 days; test failed | Present eight-hour max age, UNVERIFIED; no server revocation. |
| WEB-13 | MEDIUM | Cross-site POST accepted with valid cookie; test failed | Present Origin/Fetch-Metadata middleware, UNVERIFIED. Full origin must include scheme/port; current helper compares netloc only when metadata absent. |
| WEB-14 | LOW | Public docs/OpenAPI; test failed | Present disabled endpoints, UNVERIFIED. |
| WEB-15 | LOW | No anti-framing headers; test failed | Present security headers, UNVERIFIED in app/proxy/browser. |
| WEB-16 | LOW | PBKDF2 count 260k; test failed | Present 600k and sync login handler, UNVERIFIED compatibility/load behavior. |
| WEB-17 | MEDIUM | Trailing-dot unsubscribe Host bypass; nine tests failed | Present canonical trailing-dot stripping, UNVERIFIED through Caddy. |
| WEB-18 | MEDIUM | Unknown unsubscribe POST returns success without suppression; test failed | NOT FIXED; historical lead explicitly deferred. Existing compliance test expects 200; resolve contract and update evidence together. |
| WEB-19 | LOW | SUSPECTED O(n) token scan CPU amplification | NOT FIXED; no runtime load proof. |
| WEB-20 | MEDIUM | javascript href rendered; test failed | NOT FIXED in templates; intake validation alone does not protect existing rows/research URLs. |
| WEB-21 | INFO | Hostile text escaped across pages; control passed; OAuth f-string self-XSS trace | Present OAuth HTML escaping, UNVERIFIED; initial ordinary-text controls passed before later edits. |
| WEB-22 | MEDIUM | Approve/send/queued/scorer labels misleading; three tests failed | NOT FIXED: templates unchanged at recovery. |
| WEB-23 | LOW | Withdraw approval only accessible through lead detail; static trace | NOT FIXED; UX enhancement deferred. Backend revocation correctness remains mandatory WEB-01. |
| WEB-24 | MEDIUM | Monthly reply rate can exceed 100%; test failed | NOT FIXED: numerator all-ever contacted vs monthly denominator. |
| WEB-25 | MEDIUM | Zero-only cost entry shows $0 per qualified; test failed | NOT FIXED. Missing billed costs must remain visibly unmeasured. |
| WEB-26 | LOW | Skipped-unconfirmed all-time under selected month; test failed | NOT FIXED; date scope or explicit all-time label needed. |
| WEB-27 | LOW | Hostile month/workspace params cause 500; four tests failed | NOT FIXED. |
| WEB-28 | MEDIUM | Eleven admin mutations and lead import lack actor audits; tests failed | NOT FIXED. Caps, campaign copy/proof-point changes particularly relevant. Login/logout also unaudited. |
| WEB-29 | LOW | Delete-person leaves address in free-text audit detail; test failed | NOT FIXED; privacy owner dependency. |
| WEB-30 | MEDIUM | Newest-500 SQL limit before attention filtering hides older replies; test failed | NOT FIXED. Filter attention criteria in SQL before limit; pagination still a separate consideration. |
| WEB-31 | LOW | SUSPECTED upload/zip/row memory exhaustion; code trace | NOT FIXED; no size/load test performed. |
| WEB-32 | LOW | Recreate mailbox resets cap accounting; static trace | NOT FIXED; original lead requested deletion audit only, which also was not reached. |
| WEB-33 | LOW | External HTMX CDN without integrity; static trace | NOT FIXED; vendoring/SRI deferred. |
| WEB-34 | INFO | Historical Render trusts all proxies; Oracle candidate trusts loopback | NOT FIXED; intended actual proxy behavior requires runtime verification. Throttle map per process/unbounded. |
| WEB-35 | INFO | No next redirect, no data exports; autoescape/cookie controls | Initial control evidence only; no full-browser or current deployment claim. |

## Independent critical review

`queue.claim_job` marks a job RUNNING and commits before handler execution. `outbox.cancel_pending_send` deletes only PENDING jobs. `send.handle_send` calls `send_lead` with the lead alone; neither binds the release to `Job.created_at`, the approved draft, or approval generation. A claimed job can survive reject/edit/reapprove and send revised copy without a new release. A lead lock inside send does not close the period before the handler obtains it. This is a remaining HIGH approval/release defect, not a hypothetical external-provider issue.

The reject/edit routes also inspect state without acquiring the send path's lead lock. A request can read an old APPROVED row, then block on its update while send holds that row, and proceed after the marker commit. The narrow post-marker/pre-wire race must be explicitly handled as in-flight, not described as successful withdrawal.

Proposed deterministic regression: approve and release; `claim_job` and commit in an independent session without executing handler; reject, edit and approve through real routes; execute the old claimed job with stub sender; assert zero receipts/provider submissions and a fresh release required. Also exercise stale claimed job without edits and duplicate release, so a safety fix does not discard valid work.

Known limits: browser navigation/frame behavior, actual Caddy headers/Host behavior, large upload memory, closed-month untag impact, token-scale load and real Gmail one-click behavior were not exercised. Current continuation results belong in the final test evidence, not retroactively in this recovered historical ledger.
