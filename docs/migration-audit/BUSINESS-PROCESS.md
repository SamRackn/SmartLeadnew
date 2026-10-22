# Business process: Search Site Marketing launch (Smartlead replacement)

Specification and implementation trace, 2026-10-16. Revision: branch `audit/migration-readiness-2026-10-15`, HEAD `c55de0d`, plus an uncommitted fix wave of 19 modified files that other agents are still editing. Line references are as read on 2026-10-16 and will drift as those fixes land. Evidence is code inspection unless a test is named. A named test points at coverage. It is not a current pass: the baseline was 873 passed at `c55de0d`, before the fix wave. This document is not launch approval. It supersedes the 2026-10-15 recovery draft.

Authority order: (1) dated client decisions: `LAUNCH-PLAN.md:221-264` (2026-10-06), `:655-720` (2026-10-14) and `LAUNCH-PROMPT.md:39-61` (2026-09-29); (2) `CLAUDE.md`; (3) `DECISIONS.md`, where the 2026-09-21 "remove Smartlead" and "approve enqueues send" are SUPERSEDED; (4) `CLIENT-MEETING.md` and `README.md`, which are partly obsolete; (5) code, which shows what happens, not what should happen.

Finding IDs come from `evidence/*.md` and `HANDOFF-RECOVERY.md`. New IDs from this analysis are `BP-01`..`BP-06` (§9). Open decisions are `OD-nn` (§8).

## 1. Actors

| Actor | Business role (source) | System authority (code) | Note |
|---|---|---|---|
| **Maaz Nadeem** `maaz@clientrise.io` | Operator. He runs outreach and owns the inbox, qualification and removal requests. He replies as himself, "working with Sean", and never poses as Sean. He confirms the postal address, owns Zapmail and OAuth, and runs the Smartlead pause, export and detach (`LAUNCH-PROMPT.md:43,52,55,59`; `LAUNCH-PLAN.md:224-241,662-680`). | `all_workspaces=True` (`users.py:32-35`). Operator-only: imports (`imports.py:50`), workspaces, campaigns and address confirmation (`campaigns.py:47`), mailboxes and OAuth connect (`mailboxes.py:45`), costs (`costs.py:22`), dashboard (`dashboard.py:62`), inbox LIFT (`inbox.py:175`), delete-person (`removals.py:195-197`). | He can also review, approve, release and tag in every workspace. |
| **Sean Gough** `sean@searchsitemarketing.com` | Sender of record (the "Sean Gough" mailboxes). His final copy sign-off is still open (`LAUNCH-PLAN.md:258`). He takes the qualified meetings and forwards removals to Maaz (`LAUNCH-PLAN.md:238-241,663`). | Scoped to `search-site-marketing` (`users.py:34`). Leads, review, outbox, inbox and removals are filtered by `Account.lead_filter`/`visible_lead`, and anything outside his scope returns 404 (`web/auth.py:119-140,190-201`). He can approve, release and tag, add a global email removal, and add a domain block scoped to his own workspace (`removals.py:146-158`). | The code lets Sean approve, release and tag `qualified`. The decisions say Maaz owns qualification (OD-12). |
| **Sam** | Engineering owner and `CLAUDE.md` owner. Gate 7 is "Sam says go" (`LAUNCH-PLAN.md:778,791`). | CLI and VM only (`ops`, `ingest.smartlead`, `users`). He has no app account. | CLI actions are attributed as `import:smartlead` or the OS username (§5 P4, BP-03). |
| **Worker** `python -m mazz.ops worker` | Deterministic orchestration. | Audit actor `system` (`audit.py:17`). One instance only, enforced by an advisory lock (`ops.py:687-733`). | It never approves, releases or tags. |
| **Recipient** (public) | Can opt out by link, one-click, or reply. | `/u/<token>` GET/POST (`public.py:102-133`). | The only other public routes are `/health` and the OAuth callback. |
| **External services** | Google Gmail API (send and read-only), Zapmail (mailbox tenant and Custom OAuth), Smartlead (warmup and SmartSenders, retained and paid). | `integrations/` | Smartlead must stay paused after cutover (§7). |

## 2. Confirmed rules (each checked against its source and the code)

| ID | Rule | Source | Implementation | Verification / gap |
|---|---|---|---|---|
| R-01 | Launch only the Search Site Marketing workspace. ClientRise is deferred. | `LAUNCH-PLAN.md:264,664-666` | `users.py:30-31`. No ClientRise campaign is seeded. | Code-inspected. |
| R-02 | Four mailboxes: `sean.gough@` and `gough@` on getsearchsitemarketing.com and searchsitemarketinghub.com. Display name "Sean Gough", all pinned to the launch campaign. No extra mailboxes. | `LAUNCH-PLAN.md:659-660,669-670`; `LAUNCH-PROMPT.md:46` | `launch_copy.py:63-67,111-126` (seeded as `needs_reconnect`). Preflight roster check at `ops.py:348-357`. | Unit-tested (`test_launch_readiness.py:47`). BLOCKED at runtime: all four mailboxes are `needs_reconnect` on the VM (`HANDOFF.md:20`). |
| R-03 | 15 sends per mailbox per day, follow-ups included. Never raise caps for throughput. | `LAUNCH-PLAN.md:172-179,231-232,659-660`; `LAUNCH-PROMPT.md:47` | `launch_copy.py:68`; `config.py:98`. `sent_today` counts booked sends plus unbooked attempts (`mailbox.py:97-123`). Preflight refuses any cap above 15 (`ops.py:358-362`). | The cap day is the **Eastern calendar day** (`mailbox.py:76-94`), still to be confirmed (OD-02). The ORM default is still 30 (`models.py:925-927`, BP-05). Open findings: SND-03/04/05, WEB-32. |
| R-04 | At most 2 active contacts per company. | `LAUNCH-PLAN.md:231`; `LAUNCH-PROMPT.md:135` | Default 2 (`models.py:329-332`). Logic in `sequence.py:315-370`, checked at draft (`draft.py:792`) and at the Email 1 send (`send.py:356-358`). Only Email 1 is capped; a capped lead is deferred by one day. | **FAIL**, SND-02: an unbooked in-flight Email 1 was not counted (`test_recovery_send.py:84`). A fix is landing in `active_at_company` (unbooked attempts now take a slot) and is unverified. The count spans every workspace at a domain and ignores `held`/`no_angle` leads (`sequence.py:315-342`, OD-15). |
| R-05 | Send window Mon–Fri 09:00–17:00 in the prospect's timezone: from state, else location, else Eastern. A send outside it is deferred, not failed. | `LAUNCH-PLAN.md:232,297-298`; `LAUNCH-PROMPT.md:136` | `sequence.py:39-40,82-92,109-118`; `send.py:325-334`; `config.py:82`. Live boot refuses `SEND_WINDOW` off (`config.py:163-167`, SND-07 fix in flight). | Unit-tested (`test_sequence.py:253,273`). Holidays are not modelled (`sequence.py:111`, OD-04). |
| R-06 | Three steps: Email 1, FU1 at +3 days, FU2 at +4 days after FU1. Same thread ("Re: Quick Question", `In-Reply-To`/`References`, Gmail threadId) and same mailbox. | `LAUNCH-PROMPT.md:49,73-108` | Copy matches `LAUNCH-PROMPT.md:75-108` verbatim (`launch_copy.py:16-38`). Delay in `sequence.py:101-106`; threading in `send.py:148-174`; the thread's own mailbox only (`send.py:374-385`, `mailbox.py:187-195`); subject in `sequence.py:284-288`. | Unit-tested (`test_sequence.py:134`). Delays are calendar days rolled off weekends, but `CLAUDE.md` says business days (OD-01, SND-12). The due date is when the follow-up draft **enters review**, not when it sends. |
| R-07 | The cold path is a fixed template with no AI. | `LAUNCH-PLAN.md:149`; `CLAUDE.md` work order; `DECISIONS.md` 2026-09-21 | `config.py:109` (`DRAFT_COMPOSER=template`). The verify judge stays off unless `VERIFY_JUDGE` is set (`verify.py:1412-1426`, used at `:1772`). Qualification is deterministic (`test_qualify.py:79`). | Unit-tested. The VM's actual env values are unverified, and zero-AI depends on both settings (`evidence/qualification-review.md`). |
| R-08 | Vertical gate: 2 distinct law-firm terms on the prospect's own pages, disqualifiers apply. A **blocked** site may qualify only if the industry is legal AND the name is law-firm-like (cited as `lead_record`). An outage retries and never falls back. Unconfirmed means `no_angle` with `skip_reason=vertical_unconfirmed`. The generic fallback is OFF. | `CLAUDE.md` guardrail 2 (ratified 2026-10-14); `LAUNCH-PLAN.md:688` | `launch_copy.py:41-58,90`; `enrich.py:369-408`; `company.py:640-668`; `qualify.py:734-757` | Unit-tested (`test_qualify.py:68-276`, `test_sequence.py:205,452`). An "unreachable" site ends up `held`, not on the fallback (OD-08). |
| R-09 | No statistics or proof points at launch. | `LAUNCH-PLAN.md:233-234,660-662` | Preflight check at `ops.py:391-399`. A digit in the offer line is refused (`DECISIONS.md` 2026-09-21). | Unit-tested. |
| R-10 | Every email, follow-ups included, is approved by one named human. **Approval is not release.** No bulk action exists. | `LAUNCH-PROMPT.md:49,53`; `CLAUDE.md` guardrail 4 | Approve queues nothing (`leads.py:340-486`, see `:470-484`). The only send-job site is the release action (`outbox.py:257-371`, enqueue at `:367`). A follow-up writes a draft and goes back to review (`sequence.py:217-307`). | Unit-tested (`test_sending.py:716,744`; `test_web.py:441,638,708`). **FAIL**, WEB-01: a claimed RUNNING job is not bound to the approved copy, because `cancel_pending_send` deletes only PENDING jobs (`outbox.py:105-123`; `test_recovery_send.py:27`). A fix is landing: `send_lead(released_at=job.created_at)` refuses a release older than the latest approval. It is unverified, and an edit or reject with no re-approval still depends on the re-read of the `approved` status. |
| R-11 | Stop rules: a genuine or negative reply stops the sequence. OOO pauses until the business day after the return date at 09:00 local, else 7 days, at most 2 pauses. Bounce and unsubscribe suppress at once. The software never sets "interested". | `LAUNCH-PLAN.md:235-239`; `LAUNCH-PROMPT.md:50,150-151` | `replies.py:52-56,137-146,220-230,272-348` | Unit-tested (`test_replies.py:197,251,266,294`). Open: GML-1..15 (see matrix). |
| R-12 | A hard bounce suppresses and stops the sequence. A mailbox auto-pauses on any 5.7.x or above 3% hard bounces over its last 50 sends. Only a human un-pauses. | `LAUNCH-PROMPT.md:141`; `LAUNCH-PLAN.md:306-310` | `mailbox.py:255-329`; `send.py:579-583` | Unit-tested (`test_sequence.py:315`, `test_replies.py:323`). |
| R-13 | Maaz owns removal requests and Sean forwards any he gets. Suppression is immediate, and an email removal is always global. | `LAUNCH-PLAN.md:238-239`; `LAUNCH-PROMPT.md:55` | `removals.py:126-183` | Unit-tested (`test_compliance_launch.py:208,224`). WEB-08 is still open. |
| R-14 | Never contact: Smartlead unsubscribes and bounces (file A, global), Sean's clients and named companies (file C, workspace-scoped), and the five own domains. | `LAUNCH-PROMPT.md:56`; `LAUNCH-PLAN.md:242` | `launch_copy.py:71-74,99-106`; `smartlead.py:149-203` | **FAIL**: DB-02a, DB-02b. Files A and C have not been supplied. |
| R-15 | Every email footer carries the **confirmed** workspace address ("Search Site Marketing, Inc, 6 Rockford Drive, West Nyack, NY 10994"), the opt-out line and a one-click link. Every message carries `List-Unsubscribe` and `List-Unsubscribe-Post`. | `LAUNCH-PLAN.md:224-228,657-659`; `LAUNCH-PROMPT.md:110` | `draft.py:225-278`; `send.py:148-174,296-318`. Confirmation is operator-only and audited `confirm_postal_address` (`campaigns.py:365-416`). | Unit-tested (`test_compliance_launch.py:101-115`). BLOCKED: unconfirmed on the VM (`HANDOFF.md:20`). |
| R-16 | Separate logins: Maaz sees all workspaces, Sean sees Search Site Marketing only. | `LAUNCH-PLAN.md:240-241` | `users.py:28-35` | Matches. |
| R-17 | Maaz handles replies within 2 business hours, same business day at the latest. Sean takes the qualified meetings. | `LAUNCH-PROMPT.md:52,59`; `LAUNCH-PLAN.md:662-664` | An `interested` reply still unhandled after 2 business hours (Eastern, Mon–Fri 9–17) is highlighted (`inbox.py:36,60-64`; `sequence.py:121-136`). | There is **no reply composer**: replies are written by hand in Gmail (§6). |
| R-18 | Smartlead stays paid for warmup and SmartSenders, and its cost is reported. Warmup stays ON. Maaz detaches the mailboxes and pauses the campaign immediately before the switch. | `CLAUDE.md`; `LAUNCH-PLAN.md:675-677`; `LAUNCH-PROMPT.md:57` | No code. Cost category `smartlead` (`metrics.py:41`). | BLOCKED: Zapmail's written OK and whether both OAuth grants can coexist (`LAUNCH-PLAN.md:674,719-720`). |
| R-19 | Kill switch: `SEND_ENABLED` defaults to false. Live boot refuses without the https unsubscribe host, secrets and OAuth settings. Run preflight before flipping it, with no pending dry-run send jobs. | `CLAUDE.md` guardrail 7; `LAUNCH-PLAN.md:690-693` | `config.py:74-82,133-187`; `send.py:410-415`; `ops.py:321-400` | Unit-tested. SND-06 fix in flight. |
| R-20 | Primary metric = the month's recorded costs ÷ conversations a human tagged `qualified` that month. Enter costs only once the billed amounts are verified; show "—" until then. | `CLAUDE.md`; `LAUNCH-PLAN.md:259-260,432-436` | `metrics.py:1-17,103-117,137-189` | Open: WEB-24/25/26/27, OD-10, OD-11. |
| R-21 | Per-mailbox OAuth through Zapmail Custom OAuth. Web application client, In production, scopes `gmail.send` + `gmail.readonly` only. | `LAUNCH-PROMPT.md:58`; `LAUNCH-PLAN.md:671-673` | `google_oauth.py`; `oauth.py:38-85`; `mailboxes.py:355-416` | BLOCKED: no grants yet. |
| R-22 | Leads come from ZoomInfo CSV/XLSX. First name, last name and email are required. Dedupe on email. | `LAUNCH-PROMPT.md:48,134` | `validate.py`; `csv_import.py:351-439`; `models.py:511` | Unit-tested (`test_sequence.py:286`, `test_ingest.py:275-298`). |
| R-23 | Suppression is global for email and domain, subdomains included. Client blocks are scoped to the workspace. A read error rejects the lead. | `LAUNCH-PROMPT.md:129`; `CLAUDE.md` guardrail 1 | `csv_import.py:221-304,417-424,468-492` | Unit-tested (`test_ingest.py:158-220`). |

## 3. State vocabulary (the values in code)

| Entity | Values (source) |
|---|---|
| `Lead.status` | `researching`, `ready`, `no_angle`, `approved`, `sent`, `held` (`models.py:86-92`) |
| `Lead.sequence_state` | NULL (not started), `active`, `paused`, `stopped`, `complete` (`models.py:593-594`) |
| `Lead.skip_reason` | `vertical_unconfirmed`, `disqualified` (`qualify.py:748-750`); `suppressed`, `followup_<verdict>` (`sequence.py:247,279`); `legacy_step_unknown`, `legacy_thread_not_found`, `legacy_thread_ambiguous` (`smartlead.py:293,371`); `resolved_by_hand` (`ops.py:586`) |
| `Draft.verification_status` | `pending`, `passed`, `failed` (`models.py:95-98`). `send_attempted_at` set with no matching `sent_messages` row means "may have been sent" (`models.py:711-721`). |
| `Job.status` / `Job.kind` | `pending`, `running`, `done`, `failed` / `enrich`, `qualify`, `draft`, `verify`, `followup`, `send` (`queue.py:467-477`) |
| `Mailbox.status` | `active`, `paused`, `disabled`, `needs_reconnect` (`models.py:101-115`) |
| `Reply.classification` | `ooo`, `unsubscribe_request`, `negative`, `needs_review`. A bounce is not a reply (`models.py:1169-1171`). |
| `ReplyTag.tag` | `interested`, `qualified`, `meeting_booked`, `meeting_showed`, `client_won` (`inbox.py:33`) |

There is no distinct "rejected", "suppressed", "released" or "follow-up in review" status. `held` covers rejection, suppression, stop, failure and ambiguous send. "Released" means a pending or running `send` job exists (`outbox.py:88-102`). UI labels must not claim more than that (WEB-22: "Approve & send" and "queued to send" are misleading).

## 4. Lifecycle

### 4A. What happens at each step

| Step | Actor | Inputs and preconditions | State transition | Data written | External effect |
|---|---|---|---|---|---|
| S01 Intake | Maaz (operator only, `imports.py:50`) | CSV/XLSX up to 25 MB (`imports.py:54`) with first, last and email. Columns are aliased. | New lead → `researching` (`csv_import.py:364`). | `leads`, one `enrich` job (`csv_import.py:428`) | None |
| S02 Validation and dedup | system | Normalised email and domain (`validate.py`). `leads.email` is UNIQUE (`models.py:511`). | An existing lead keeps its fields; blank cells never erase (`csv_import.py:371-377`). An `approved` or `sent` lead, or one with a started sequence, is not re-queued (`:391-407`). Anything else, **including `held` and `no_angle`**, goes back to `researching` with a new enrich job (`:409`, OD-13). | `leads` | None |
| S03 Suppression and exclusions | system | Global rows match email, sha256, domain, subdomain or email-domain. Workspace rows apply after routing. | A match rejects the row before any lead or job is written (`csv_import.py:468-492,417-424`). | Report only | None |
| S04 Routing | system | `SystemRoutingPolicy` matches the lead's industry fields to `campaigns.segment` (`routing.py:191-270`). The Smartlead import pins the campaign instead (`smartlead.py:244`). | `campaign_id` is set once and never moved (`routing.py:323-375`). An unrouted lead keeps NULL, which later means `no_angle` (BP-01). | `leads.campaign_id` | None |
| S05 Research | worker | The domain. The cache counts as fresh for 14 days (`enrich.py:81`). | Stays `researching` and queues `qualify`. If the site yielded nothing: `no_angle` (`enrich.py:389-396`). | `research_cache`: domain-keyed facts; person facts carry `subject_email`. | Fetches `robots.txt` and up to 2 pages from the prospect's site (`company.py:676-700`). |
| S06 Qualification | worker | The campaign's vocabulary. An unrouted lead gets `DEFAULT_QUALIFICATION`, which qualifies nobody (`qualify.py:671-695`). | Qualified: a `draft` job. Otherwise `no_angle` with `skip_reason` (`qualify.py:734-757`). | `leads` | None |
| S07 Draft | worker | Lead is qualified, campaign has step 1, workspace address confirmed, company cap has room, lead never emailed. | New `Draft` (step 1, `pending`) and a `verify` job. A full company cap defers the job by one day (`sequence.py:347-370`). A lead already emailed is refused (`draft.py:650-670`). | `drafts` | None (no model call) |
| S08 Verification | worker | The latest draft, its cited facts, the campaign copy and proof points. | Passed: lead → `ready`. Failed: lead → `held` (`verify.py:1708-1727,1752-1773`). | `drafts.verification_status` and notes | None (judge off by default) |
| S09 Review, edit, reject | Maaz or Sean | The lead is visible to the account. | Edit, refused on `approved`/`sent`, re-verifies to `ready` or `held` (`leads.py:538-596`). Reject marks the draft `failed` and the lead `held` (`leads.py:489-535`). Both delete any PENDING send job. | `drafts`, `leads`, audit `edit`/`reject` | None |
| S10 Approval | Maaz or Sean | Status `ready`, latest draft `passed`, no placeholders or empty subject, sequence not stopped, not suppressed (`leads.py:362-460`). | `ready` → `approved`, sets `approved_by`/`approved_at`. A repeat click changes nothing. | `leads`, audit `approve` | None. **No job is created.** |
| S11 Release | Maaz or Sean: one message per click on /outbox | Status `approved`, draft approvable, no placeholders, sequence not stopped or paused, not suppressed, some mailbox can serve it (`outbox.py:271-363`). | One `send` job (`pending`). A stopped, suppressed or unreadable-suppression lead goes to `held`, committed. | `jobs`, audit `release_send` (with `draft_id`) | None |
| S12 Paced send | worker | Re-checks: still approved; draft unsent and passed; sequence not stopped or paused; no unresolved attempt; suppression; placeholders; footer matches the current address; ready for live send; send window; mailbox cap, spacing, pin and thread; Email 1 only once; company cap; for follow-ups, the thread's mailbox read immediately before sending (SND-08/GML-1 fix landing, `send.py:386-392,666-694`) (`send.py:199-392`). | **`SEND_ENABLED=false`**: writes `.eml` to `out/dryrun`, the job completes and the lead **stays `approved`** (`send.py:413-415,697-705`). **Live**: commits the attempt marker (`:421-426`) → Gmail → `sent_messages` row, lead `sent`, sequence `active` with a `followup` job at the due date, or `complete` (`send.py:451-499`, `sequence.py:201-214`). | Draft marker, `mailboxes.last_sent_at`, `sent_messages`, `jobs` | Gmail `messages.send` from the chosen mailbox |
| S13 Reply sync and classification | worker, every 300 s, only when `EMAIL_SENDER=google` (`ops.py:627,747-751`) | A connected `active` or `paused` mailbox, a history cursor or fallback scan, and a message on a thread we sent **from that mailbox** (`replies.py:442-458`). | `needs_review`, `negative` or `unsubscribe_request`: sequence `stopped`, and a follow-up sitting `ready`/`approved`/`researching` goes to `held` (`replies.py:240-247`). `ooo`: sequence `paused` (`:315-328`). | `replies`, suppression on unsubscribe, mailbox cursor and `last_synced_at`, audit `reply_<kind>` (`system`) | Gmail read-only |
| S14 Bounce | worker | A DSN matched by Message-ID or recipient within 45 days (`replies.py:381-418`). | Hard bounce (5.x.x except 5.2.2): suppressed, sequence `stopped`. Mailbox `paused` on 5.7.x or above 3% of the last 50 sends (`mailbox.py:289-329`). | `sent_messages.bounced_at`, `suppression_list`, `mailboxes` | None |
| S15 OOO pause and resume | worker | An OOO reply, sequence `active`, fewer than 2 earlier OOO replies. | `paused` with `next_step_due_at` = resume time, and the follow-up job is re-armed (`replies.py:315-328`). At the due time `handle_followup` sets `active` again (`sequence.py:228-234`). | `leads`, `jobs` | None |
| S16 Unsubscribe | Recipient: link, one-click or reply | A valid token (`public.py:47-59`). | Global suppression. `researching`/`ready`/`approved` → `held`; `active`/`paused` → `stopped` (`public.py:116-133`). | `suppression_list`, `leads`, audit `unsubscribe` (`system`) | None |
| S17 Follow-up: review or stop | worker, then humans | A `followup` job at its due time, lead `sent` and `active`. | If the lead is not sent and active, nothing happens. If suppressed: `stopped`/`suppressed`. A legacy lead with no cache is researched first. If it no longer qualifies: `stopped` with `followup_<verdict>`. Otherwise a new `Draft(step n)`, lead `researching` → verify → `ready`, then S09–S12 again (`sequence.py:217-307`). | `drafts`, `leads` | None |
| S18 Human qualification | Maaz (the code also lets Sean) | A visible reply, judged against the R-20 definition. | Adds or removes a `ReplyTag`; sets `handled_at`/`handled_by` (`inbox.py:114-150`). | `reply_tags`, `replies`, audit `tag`/`untag`/`reply_handled` | None |
| S19 Meeting handoff | Maaz → Sean (manual) | A qualified conversation. | Tags `meeting_booked`, `meeting_showed`, `client_won`. | `reply_tags` | Manual Gmail reply and calendar booking (§6) |
| S20 Reporting | Maaz (operator only) | Cost entries (`costs.py`) and tags. | None | `cost_entries`, audit `cost_add`/`cost_delete` | None |
| S21 Retention and deletion | Maaz (operator only, typed `DELETE`) | An email address. | Lead cascade, person facts pruned, tombstone kept (`privacy.py`, `removals.py:186-207`). | Suppression tombstone, audit `delete_person` (sha256 only) | None |

### 4B. Failure, retry, attribution, operator action, acceptance

| Step | Failure behaviour | Retry | Audit attribution | Operator action | Existing acceptance test |
|---|---|---|---|---|---|
| S01 | A bad row is rejected with its reason. A bad file shows a page error (400/413). | Re-uploading is idempotent. | **None**: imports are unaudited (WEB-28, `test_audit_web.py:721`). | Read the per-row report, fix, re-upload. | `test_ingest.py:147,338,364`; `test_excel_import.py`; `test_sequence.py:286` |
| S02 | — | — | none | Decide OD-13. | `test_ingest.py:275,289,298`; `test_routing.py:574` |
| S03 | An unreadable list rejects the row as `suppression_unavailable` (fails closed). | Re-upload | none | — | `test_ingest.py:158-220`; `test_compliance_launch.py:267`; `test_audit_web.py:520` |
| S04 | A routing error leaves the lead unrouted and never raises (`routing.py:353-367`). | — | Log only | Set the campaign `segment`, or use a pinned import (BP-01, OD-18). | `test_routing.py:295,494,605` |
| S05 | Blocked (robots, 401, 403): recorded, and qualify uses its fallback. Network error or 5xx: raises. | 5 attempts at 1, 2, 4 and 8 min, then the lead is `held` (`queue.py:86-106,289-299`). | None. The error lands in `jobs.last_error` and `research_cache.last_error`. | Review held leads. | `test_live_company.py`; `test_recovery_research.py` (SSRF fix in flight) |
| S06 | — | — | none | Check `/review?status=no_angle`. | `test_qualify.py:68-143`; `test_sequence.py:205,452` |
| S07 | A missing or unconfirmed address raises `MissingSenderAddress`; the job fails and the lead is `held`. | 5 attempts | none | Confirm the address, then re-draft. | `test_sequence.py:182`; `test_compliance_launch.py:101-115` |
| S08 | A judge that cannot answer (if enabled) holds the draft. | 5 attempts | none | Read `verification_notes`, then edit or reject. | `test_verify.py:230-270` |
| S09 | 409 when editing an `approved` or `sent` draft. 400 for a CR/LF in the subject. | — | `edit`, `reject` (user) | Edit or reject. | `test_web.py:664,681`; `test_audit_web.py:259,314,338` |
| S10 | Refusals come back as a panel with 409, or 503 when suppression is unreadable. | A human tries again. | `approve` (user) plus `leads.approved_by`/`approved_at` | — | `test_web.py:412,441,496`; `test_audit_web.py:270,281,301,374` |
| S11 | 409 or 503. A hold is committed. | Human | `release_send` (user) | — | `test_web.py:480,542,556,571,593,638`; `test_audit_web.py:350,362`; `test_recovery_send.py:27` (WEB-01) |
| S12 | Lead not approved or copy refused: permanent failure. Suppressed, stopped, footer, placeholder or unthreaded: `held`. Window, cap, spacing, OOO pause, company cap, or an unreadable thread mailbox before a follow-up: deferred, using no attempt. A provider error that proves nothing left: marker cleared, then retried. **Ambiguous outcome: `held` with "may have been sent".** 401: mailbox `disabled`. `invalid_grant`: `needs_reconnect`. 5.7.x: `paused`. A follow-up whose thread mailbox is not active fails permanently, and the lead stays `approved` (`send.py:384-385`; `queue.py:328-329`). | Queue backoff only when nothing was sent. **Never retried when ambiguous.** | A live send writes **no `audit_events` row**: `sent_messages` is the record. A dry run audits `dry_run_send` (`system`). | Resolve ambiguous sends (§5 P4). Re-release after a mailbox reconnect. | `test_sending.py:527-1010`; `test_compliance_launch.py:288`; `test_audit_sending.py`; `test_recovery_send.py:84,111` |
| S13 | A provider error leaves the cursor where it was and retries next cycle. A revoked grant or wrong account sets `needs_reconnect` (`replies.py:546-592`). | Every 300 s | `reply_<kind>` (`system`) | Work `/inbox`; confirm unsubscribe flags. | `test_replies.py:155-459`; `test_audit_gmail.py:182,208,240,253,267` |
| S14 | A soft or unreadable DSN is only logged. | — | `hard_bounce` (`system`, no address) | Review the mailbox, then un-pause it on `/mailboxes` (unaudited, WEB-28). | `test_replies.py:323,343`; `test_sequence.py:315`; `test_audit_gmail.py:305` |
| S15 | A third OOO is recorded with no pause. A return date more than 180 days out stops the sequence as `needs_review`. | — | `reply_ooo` (`system`) | — | `test_replies.py:266,294,309`; `test_audit_gmail.py:318` |
| S16 | An unknown token shows "link no longer active" with **HTTP 200**, so a one-click client reads it as success (WEB-18). | — | `unsubscribe` (`system`) | Confirm reply unsubscribes in `/inbox`; operator can LIFT a false positive (§5 P5). | `test_compliance_launch.py:131,157,163`; `test_audit_web.py:477,492,511`; `test_launch_readiness.py:61` |
| S17 | Suppression unreadable: raises and retries, then the job fails and the lead stays `sent` with no follow-up (accepted, `LAUNCH-PLAN.md:716-718`). | 5 attempts | Log only | Watch `jobs_failed` in `ops health`. | `test_sequence.py:134,435`; `test_smartlead_cutover.py:243` |
| S18 | — | — | `tag`, `untag`, `reply_handled` (user) | Tag within 2 business hours. | `test_replies.py:644,676,697`; `test_metrics.py:58` |
| S19 | **Not implemented in software.** | — | Tags only | §6 | None. Needs business acceptance from Maaz and Sean. |
| S20 | No costs entered shows "—". A zero-only entry shows **$0** (WEB-25). | — | `cost_add`, `cost_delete` | Enter verified billed costs each month. | `test_metrics.py:58-182`; `test_audit_web.py:607-676` |
| S21 | A residual reference raises and rolls the deletion back. | — | `delete_person` (sha256) | — | `test_privacy.py:59-278`; `test_compliance_launch.py:249`; `test_audit_web.py:734` (WEB-29) |

## 5. Operating procedures

| ID | Procedure | Steps and code | Gaps |
|---|---|---|---|
| P1 | **Removal request** | Maaz (or Sean) adds the email or domain on `/removals`. It takes effect at once (`removals.py:126-183`). Global entries hold every lead not yet sent and stop live sequences (`:83-123`). Audited as `suppress` and `hold_suppressed`. Sean forwards removals he receives to maaz@clientrise.io by hand. | A workspace-scoped domain block only bites at the next check (accepted, `LAUNCH-PLAN.md:714-715`). The person's record in Smartlead must be removed there by hand. |
| P2 | **Delete person** | Operator only, typed `DELETE` (`removals.py:186-207`). Runs `privacy.delete_person`, which keeps an email + sha256 tombstone. The audit keeps only the sha256. | Residue remains: free text in `audit_events` (WEB-29); the address in `mailboxes.last_error` on auto-pause (`mailbox.py:324,328`, DB-12); logs; the 14 local backup dumps (`ops.py:461-520`); `sent_outbox.jsonl` (stub only). Retention policy is undefined (OD-14). |
| P3 | **Mailbox reconnection** | Trigger: `needs_reconnect` (from `invalid_grant` or the seeded state), raised as CRITICAL in `ops health` (`ops.py:172-184`). Maaz opens `/mailboxes/<id>/connect`. The link carries a single-use state with a 7-day TTL (`google_oauth.py:42`, `mailboxes.py:355-396`). He submits it through Zapmail Custom OAuth, which allows 3 per mailbox per 7 days (`LAUNCH-PLAN.md:348`). The callback checks the account identity, stores the encrypted token and sets the mailbox `active` (`oauth.py:38-85`). A manual resume of `needs_reconnect` is refused (`mailboxes.py:451-456`). A `disabled` mailbox (401) can be resumed by hand. | Follow-ups pinned to that mailbox's threads fail permanently while it is down and must be **re-released** afterwards. Losing `OAUTH_TOKEN_KEY` forces every mailbox to reconnect (GML-9). Status changes are unaudited (WEB-28). The callback is attributed to `system`, not a person. |
| P4 | **Held and ambiguous sends** | `ops health` raises CRITICAL `send_unresolved` 5 minutes after an attempt (`ops.py:137-155`), and preflight also lists it (`:379-389`). The operator checks the Sent folder of the named mailbox, then runs `python -m mazz.ops resolve-send LEAD --sent\|--not-sent --by <name>`. `--sent` books a `sent_messages` row with no Gmail ids, sets the lead `sent` and the sequence `stopped`/`resolved_by_hand`, so no follow-up can thread from the system (`ops.py:569-591`). `--not-sent` clears the marker; if the attempted draft is still current and approved, the lead returns to `approved` and needs a new release click (`:592-610`). | **BP-03**: no `audit_events` row is written, and `--by` defaults to the OS username (`ops.py:846`). Never mark `--not-sent` just because the local receipt is missing. Other hold reasons: suppressed or stopped since approval (correct, no action); footer address changed (re-draft); unthreaded legacy lead (adopt, or continue by hand); Email 1 already sent (check Smartlead history). |
| P5 | **False-positive auto-unsubscribe** | Operator types LIFT in `/inbox`. It removes only the global row reply sync created (`system:reply-sync`), and refuses if the person also used the link. The sequence stays stopped (`inbox.py:169-220`). | — |
| P6 | **Bounce auto-pause** | The mailbox is `paused` with a reason in `last_error`. The operator reviews it and sets it `active` on `/mailboxes`. | Unaudited (WEB-28). Follow-ups on that mailbox's threads wait while it is paused. |
| P7 | **Downtime recovery** | Before restarting, run `ops health` and look for `send_unresolved`, `jobs_failed`, `queue_stalled` and `reply_sync_stale`. The worker refuses to start if another holds the lock (`ops.py:719-733`). RUNNING jobs older than 15 minutes are reclaimed (`queue.py:361-401`), and a reclaimed send with a marker is held, not resent (`send.py:257-268`). Jobs that fell due during the outage run in `run_after` order: follow-up drafts all land in review together, while sends still need a release and are paced by caps, spacing and the window. Reply sync resumes from the history cursor. If Gmail expired the cursor (404), the fallback scan covers live sequences of any age and finished ones from the last 45 days (`replies.py:463-504`). In live mode, a follow-up first reads its thread's mailbox and is deferred if that read fails (`send.py:386-392,666-694`, fix landing). | **No alert channel on the VM**: `--notify` is macOS-only (`ops.py:411-429`, OD-19). Restoring an older database loses `sent_messages` rows for mail that already left, which means duplicate risk: reconcile against the Sent folders before releasing anything. |

## 6. Maaz → Sean handoff (software implements only the inbox and tags)

| # | Step | Who | In software? |
|---|---|---|---|
| 1 | A reply arrives, the sequence stops, and the reply shows in `/inbox`. Sean sees his own workspace's replies. | system | Yes |
| 2 | Maaz reads it and tags `interested` if it is. | Maaz | Yes |
| 3 | Maaz answers from the Sean Gough mailbox in Gmail, introducing himself as working with Sean. `CLIENT-REPLY-TEMPLATES.md` is reference only. | Maaz | **No.** There is no reply composer, and the reply is not recorded: our own mailbox's messages are ignored (`replies.py:436-438`). Manual replies do not count toward the 15/day cap (OD-16) and carry no system footer. |
| 4 | Maaz presses Handled, which clears the 2-hour timer. | Maaz | Yes |
| 5 | Maaz tags `qualified` once the definition is met: a decision-maker in the vertical, with interest in discussing the services. The tag's date sets the metric month. | Maaz | Yes |
| 6 | Maaz offers times from Sean's availability, books a Google Meet on Sean's calendar, and briefs Sean on the thread. | Maaz | **No.** There is no calendar, CRM or notification, so Sean is told outside the system. |
| 7 | Maaz tags `meeting_booked`; after the meeting `meeting_showed`; after signing `client_won`. | Maaz, with Sean reporting the outcome | Tags only. The outcome is reported by hand. |
| 8 | The prospect's later replies on the thread sync as `needs_review`. | system | Yes |

Gaps the operator must cover by hand:
- **Missed replies.** A reply on a new thread or from another address (`replies.py:442-458`), or one filtered to spam or archived (`gmail_reader.py:150-151`), is never seen (BP-02, GML-15, OD-09). Maaz should also scan the four mailboxes directly.
- **Late legacy replies.** Replies to `complete` or `stopped` legacy threads are not adopted (DB-11).

## 7. Smartlead migration: source-to-target mapping (prompt §7)

**Source evidence:** no real exports have been seen. The header aliases are guesses (`smartlead.py:69-78`), the status values are assumed (`:80-89`), and `EXPECTED` comes from the Smartlead screen on 2026-10-06 (`:95-99`). The older counts in `LAUNCH-PROMPT.md:162` are superseded. Everything below is **BLOCKED** until fresh exports A, B and C and the campaign settings (four-step copy and waits, sender assignment) are captured, with export time and checksums kept in restricted storage.

### 7.1 State mapping

| Smartlead source | Current code outcome | Required | Status |
|---|---|---|---|
| Not started, sent blank or 0 | Normal gate, pinned to the campaign, enrich (`smartlead.py:234-236,244`). | Same, but only from a file exported **after** the pause. | Unit-tested (`test_smartlead_cutover.py:82`); real file BLOCKED |
| Not started with sent > 0 | Treated as legacy (`:231`). | Legacy, never Email 1. | OK |
| In progress or Paused, sent 1 / 2 | `sent`, `active`, step = sent; due = last send + wait; awaits adoption (`:298-302`). | Continue with our FU1 / FU2 **only if the content mapping is approved**. | OD-05 |
| In progress, sent ≥ 3 | `complete`. Smartlead's step 4 is never sent (`:295-297`). | Business decision. | OD-05 |
| Completed, sent ≥ 3 | `complete` | `complete` | OK |
| **Completed, sent 1–2** | **`active`: the sequence resumes.** | Stays `complete` or `stopped`. | **FAIL** DB-01 |
| Blocked / Stopped | `stopped` (`:289-291`) | `stopped` | OK |
| Bounced or unsubscribed (as a status or a flag column) | An unknown status with a sent count becomes legacy `active`; flag columns are ignored. | `stopped` and suppressed | **FAIL** DB-01b |
| Replied flag | `stopped` (`:287-291`) | `stopped` | OK if the column exists; it is optional (L9) |
| Replied after export, before adoption | Adoption reads the thread and stops the lead (`:391-397`). | Stop | Unit-tested (`test_smartlead_cutover.py:177`) |
| OOO | No export field. Adoption reads the thread and pauses. | Pause | PARTIAL |
| Unknown status, no sent count | Refused, and blocks the commit (`:237-241,426`). | Hold for reconciliation | OK |
| Unknown status with a sent count | Legacy `active` | Hold for reconciliation | **FAIL** DB-01b |
| Contacted, invalid row / no sent count | `MUST_FIX` (blocks commit) / `stopped` + `legacy_step_unknown` (`:257,292-294`) | Fix or hold | OK |
| File C exclusions | Workspace-scoped rows. When a row has both email and domain, only the email is blocked (`:178-183`). Company-name-only rows are listed, not enforced. | Block the whole company domain. | **FAIL** DB-02b |
| File A suppression | Global. Only the `email`/`Email` headers are read (`:157`). | Accept every alias; an invalid row must block the commit. | **FAIL** DB-02a |
| `--accept-mismatch` | Waives the count check **and** the row blockers (`:485-489`). | Count-only override | **FAIL** DB-09 |

### 7.2 Field mapping

| Field | Mapping and gap |
|---|---|
| Identity: name, email, company, website | Existing alias table in `validate.py`. DB-05 header-precedence fix in flight. |
| `emails_sent` | Drives the next step via `NEXT_STEP_AFTER {1:2, 2:3}` (`smartlead.py:91-93`). Whether the count means "last step sent" or "next step" is unverified. |
| `last_sent_at` | Provisional legacy `sent_at`, replaced by Gmail's date at adoption (`:285,388`). An invalid date silently becomes "now" (DB-14). |
| Sending mailbox | Not in the export. Found by a Gmail search of each connected mailbox (`:361-373`). |
| Thread identity | Gmail thread id and Message-ID of the newest chain (`:375-388`). The step is not reconciled to the chain length (GML-4, DB-07). |
| Step copy | Not imported; replaced by our three steps (OD-05). |
| Smartlead reply categories ("positive": 0) | Not imported. Legacy sends and replies are left out of `sent` and `reply rate` (`metrics.py:151-172`), but tags on adopted threads **do** count as qualified (`:174-182`, OD-11). |

### 7.3 Cutover boundary and ownership

This follows `LAUNCH-PLAN.md:751-779`; its Render references must change to the Oracle VM.

| # | Step | Owner |
|---|---|---|
| 1 | Deploy the reviewed release with `SEND_ENABLED=false` and `EMAIL_SENDER=stub`. | Sam |
| 2 | Run `seed-launch`, set passwords; Maaz confirms the address. | Sam, Maaz |
| 3 | Create the OAuth client; set `OAUTH_TOKEN_KEY` and `UNSUBSCRIBE_SECRET`, escrowed and never rotated. | Maaz, Sam |
| 4 | Get Zapmail's written OK; connect the four mailboxes. | Maaz |
| 5 | Run the authorised seed rehearsal with an allowlist and a fixed count. | Sam, with authorisation |
| 6 | Run `ops preflight`. | Sam |
| 7 | Pause the Smartlead campaign. Export A, B and C from the paused campaign, record export time and checksums, update `EXPECTED`. | Maaz → Sam |
| 8 | Import `suppression` → `exclusions` → `leads`: dry run, reconcile every row, then commit. | Sam |
| 9 | Detach the four mailboxes from the campaign; warmup stays ON. | Maaz |
| 10 | Set `EMAIL_SENDER=google`, run `adopt --commit`, let reply sync catch up. | Sam |
| 11 | Preflight exits 0 and Sam says go; set `SEND_ENABLED=true` at 15/mailbox/day. | Sam |

Exclusivity and rollback rules:
- The old Smartlead campaign stays paused **permanently**. Resuming it would duplicate follow-ups, because Smartlead has no record of our sends (OD-07).
- Warmup keeps sending from the same mailboxes by design.
- Between steps 7 and 11 nothing sends from our side.
- **Legacy unsubscribe links** in mail Smartlead already sent still resolve in Smartlead. Unsubscribes recorded there after file A was exported never reach us, so Smartlead's block list needs a recurring delta export (BP-04, OD-06).

### 7.4 Migration acceptance (with real exports, in an isolated `_test` database)

| Criterion | Finding / test |
|---|---|
| Every source row gets exactly one outcome (imported, duplicate, excluded, held or rejected), and the totals match the paused campaign's counts. | DB-04 |
| A replay produces identical records, jobs and suppression. | DB-04b |
| No contacted lead can reach Email 1. | `test_smartlead_cutover.py:82,301` |
| No ended, replied, unsubscribed or bounced lead is `active`. | DB-01, DB-01b |
| Leads not found or ambiguous at adoption are held, and can be retried once the mailbox is repaired. | DB-06, GML-5 |
| Overdue follow-ups are paced under the agreed policy. | DB-08, OD-03 |

## 8. Open business decisions

| ID | Decision | Conflict / evidence | Options | Decides | Blocks launch? |
|---|---|---|---|---|---|
| OD-01 | Meaning of follow-up delays | `CLAUDE.md:76` says "business days". `LAUNCH-PROMPT.md:49,136` says "+3 days… due dates roll to the next business day". Code adds calendar days and rolls off weekends (`sequence.py:101-106`, SND-12). Example: Email 1 on Thursday → FU1 due Monday in code, Tuesday under business-day counting. | (a) Calendar days + roll, matching the client wording. (b) Business-day increments. Then align `CLAUDE.md` or the code. | Sean/Maaz; Sam aligns | Yes (must be explicit) |
| OD-02 | Day boundary for the daily cap | Code uses Eastern midnight (`mailbox.py:76-94`). `models.py:922` and `test_sending.py:448,458` still assume UTC (SND-03). `LAUNCH-PLAN.md:172` says "per business day". | Eastern day (code), UTC, or mailbox-local day | Maaz | Yes (confirm) |
| OD-03 | Overdue adopted follow-ups | Adoption queues follow-ups whose due date has already passed, so all of them land in review at once (`smartlead.py:404-411`, DB-08). Follow-ups sort ahead of new leads (`outbox.py:222-226`), and FU1 ("Just following up on my last email") may go out weeks after Email 1. | Re-pace over N days; treat anything stale beyond X days as `complete`; or send as-is | Maaz/Sean | Yes |
| OD-04 | Holiday calendar | Holidays are not modelled (`sequence.py:111`; `CLAUDE.md` Missing). The launch window includes Thanksgiving (2026-11-26) and Christmas. | Operator withholds releases on holidays, or add a US holiday list to the send window | Maaz | Before the launch date |
| OD-05 | Mapping the four-step Smartlead sequence to our three steps | Smartlead runs 4 steps (waits 2, 2, 3) with different bodies (`LAUNCH-PROMPT.md:112`). The code maps `{1:2, 2:3}` and treats 3+ sends as complete (`smartlead.py:91-93`). A Smartlead "paused" lead is treated as in progress (`:86`). Completed leads with 1–2 sends resume (DB-01). | Approve which of our bodies follows each sent count, or stop every legacy sequence at cutover | Sean (copy), Maaz (state) | Yes |
| OD-06 | Smartlead unsubscribe links after cutover | Those links resolve in Smartlead, not at `/u/` (BP-04). | Cadence and duration of the delta export, and its owner | Maaz | Yes |
| OD-07 | Rollback with one active sender | No rollback is documented. Resuming the old campaign would cause duplicates. | Never resume the old campaign; rollback means `SEND_ENABLED=false` plus manual handling; or re-import our state into a new Smartlead campaign | Sam + Maaz | Yes |
| OD-08 | What counts as an "unreachable" site | `CLAUDE.md:124-128` says "blocked or unreachable… An outage retries; it never falls back". `LAUNCH-PROMPT.md:133` says "unreachable or blocked". Code falls back only on robots, 401 or 403; a network error or 5xx is `held` after 5 attempts (`enrich.py:400-408`; `company.py:640-668`). | Keep it (fails closed), or treat N days of failure as unreachable and allow the fallback | Sam (guardrail owner), Sean | No (fails closed) |
| OD-09 | Archived, spam and out-of-thread replies | Sync reads INBOX only (`gmail_reader.py:150-151`, GML-15) and stores only replies on our own threads (`replies.py:442-458`). | Read every label including spam, or require a manual daily mailbox check | Maaz | Yes. A missed reply means a follow-up to someone who answered. |
| OD-10 | Metric month and closing | Months are UTC (`metrics.py:15,80-83`) but the business runs on Eastern time. Untagging deletes the row and changes past months (`inbox.py:132-134`), and delete-person removes tags too. | Eastern months; freeze closed months; take snapshots | Maaz | No |
| OD-11 | Metric attribution edge cases | A lead with replies tagged in two months counts in both (`metrics.py:174-182`). Tags on legacy threads count even though legacy sends are excluded (`:151-172`). "All workspaces" includes unrouted leads. A zero-only entry yields $0 (WEB-25). There is no completeness check: any single entry counts as "has costs" (`:103-105`). Annual bills have no allocation rule. | Define each case. | Maaz (business acceptance) | Before quoting the number |
| OD-12 | Who may approve, release and tag | Maaz owns qualification (`LAUNCH-PROMPT.md:52`). Sean signs off the copy (`LAUNCH-PLAN.md:258`). The code lets Sean approve, release and tag (`leads.py`, `outbox.py`, `inbox.py:114-135`). | Restrict, or accept | Maaz + Sean | No (attributable either way) |
| OD-13 | Re-importing held, no_angle or rejected leads | A lead with no sequence is researched and drafted again on re-upload (`csv_import.py:391-409`), including one a reviewer rejected. | Re-evaluate, or preserve held and rejected leads | Maaz | No |
| OD-14 | Retention | No retention periods for replies, sent bodies, research cache, audit events, dry-run files or the 14 local backups. | Set periods and the legal basis | Maaz/client (legal), Sam | Yes, for privacy sign-off |
| OD-15 | What the company cap counts | It counts leads at a domain across every campaign and workspace; `held`/`no_angle` leads with a live sequence are not counted (`sequence.py:315-342`). | Per workspace, or global | Maaz | No (one workspace at launch) |
| OD-16 | Manual replies and the cap | Manual Gmail replies are not counted in the 15/day. | Accept, or count them | Maaz | No |
| OD-17 | RTO/RPO, backups and key escrow | No targets exist. Backups are local only and the cron masks a `pg_dump` failure (`HANDOFF.md:22`). | e.g. RPO 24 h, RTO 4 h; an off-VM copy; escrow for `OAUTH_TOKEN_KEY` and `UNSUBSCRIBE_SECRET` | Sam + client | Yes |
| OD-18 | How new ZoomInfo batches reach the campaign (BP-01) | The launch campaign is seeded with no `segment` (`launch_copy.py:77-91`). The browser import has no campaign picker (`imports.py:185-189`). A campaign with no segment never matches (`test_routing.py:295`). Leads therefore end up unrouted and `no_angle`/`vertical_unconfirmed` after the research fetches. | Set the campaign `segment` to the exact ZoomInfo industry value (Maaz supplies it), or add a pinned-campaign import (code) | Maaz (values), Sam | Yes, for new batches |
| OD-19 | Alert channel and recipient | `--notify` is macOS-only (`ops.py:411-429`); the VM has no channel. | Email or SMS, and to whom | Sam/Maaz | Yes |
| OD-20 | Seed rehearsal allowlist and count; Sean's seed inbox | `LAUNCH-PLAN.md:258,797-798`: Sean's seed inbox has not been given. | — | Maaz/Sean; Sam authorises | Yes |

## 9. Source discrepancies and new findings

| ID | Where | States | Actual | Action |
|---|---|---|---|---|
| D-01 | `CLAUDE.md:76` | Sequences use business days | Calendar days rolled off weekends (`sequence.py:101-106`) | OD-01 |
| D-02 | `models.py:922`; `test_sending.py:448,458` | Cap per UTC day | Eastern day (`mailbox.py:76-94`) | Fix the comment and tests after OD-02 (SND-03) |
| D-03 | `models.py:925-927` | `daily_cap` default 30 | The launch value and `config.py:98` are 15 | BP-05 (LOW) |
| D-04 | `CLAUDE.md:124-128` | Fallback when "blocked or unreachable" | Only robots, 401 or 403 (`enrich.py:400-408`) | OD-08 |
| D-05 | `queue.py:453-456,472` | A send job comes from pressing approve | It comes from release (`outbox.py:367`) | Stale docstring (`DECISIONS.md` 2026-09-21 superseded) |
| D-06 | `ops.py:636-637` | "Mailbox selection takes no row lock" | An advisory lock is taken (`send.py:340`) | Stale docstring |
| D-07 | `models.py:872-877`; `mailboxes.py:429-432` | Service account and domain-wide delegation | Per-mailbox OAuth | Stale docstring |
| D-08 | `routing.py:42-47` | "the mailboxes table… does not exist" | It exists | Stale docstring |
| D-09 | `README.md:180-192` | Multi-step campaigns, inbox, classification and multi-client listed as Missing | All are built | Obsolete |
| D-10 | `LAUNCH-PLAN.md:364-367` | Fallback scan covers 14 days | Live sequences of any age plus 45 days (`replies.py:463-487`) | Doc stale |
| D-11 | `LAUNCH-PLAN.md:372-374` | OOO with no date resumes at 09:00 + 7 days | Same local time, 7 calendar days later, rolled off weekends (`replies.py:225-226`) | Minor |
| D-12 | `LAUNCH-PLAN.md:439` | Baseline 133 contacted, 6 replies | 223 / 11 as of 2026-10-06 (`metrics.py:53-63`) | Doc stale |
| D-13 | `LAUNCH-PROMPT.md:162` | 864 / 133 / 731 / 124 / 7 / 2 | `smartlead.py:98-99` (2026-10-06 figures) | Superseded; refresh at the pause |
| D-14 | `LAUNCH-PLAN.md:566-588,724-779` | Render hosting, CNAME to onrender | Oracle VM (`scripts/oracle_setup.sh`) | Runbook must use the VM |
| D-15 | `LAUNCH-PROMPT.md:52` | Maaz owns qualification | Any scoped user can tag (`inbox.py:114-135`) | OD-12 |
| D-16 | UI labels | "Approve & send", "queued to send" | Approve queues nothing | WEB-22 |
| D-17 | `send.py:120-130` | Token keyed on `SESSION_SECRET` | `UNSUBSCRIBE_SECRET`, falling back to `SESSION_SECRET` (`draft.py:206`) | Stale docstring |

| New ID | Severity | Status | Finding |
|---|---|---|---|
| BP-01 | MEDIUM | SUSPECTED (code + `test_routing.py:295` premise; no end-to-end test) | Browser and Excel imports route with `SystemRoutingPolicy`, and the seeded launch campaign has no `segment`. New ZoomInfo batches therefore stay unrouted. They spend research fetches, land `no_angle`, and are counted as `vertical_unconfirmed` on the dashboard. It fails closed, so no wrong email is sent, but the client's "one queue" workflow is blocked. |
| BP-02 | MEDIUM | Code-inspected | Replies on a new thread or from another address, and replies filtered out of INBOX, are never stored. The sequence is not stopped (`replies.py:442-458`; GML-15). |
| BP-03 | LOW | Code-inspected | `ops resolve-send` writes no `audit_events` row, and `--by` defaults to the OS user (`ops.py:532-614,846`). |
| BP-04 | MEDIUM (process) | Confirmed by design | After cutover, unsubscribes through links in Smartlead-sent mail are recorded only in Smartlead. |
| BP-05 | LOW | Code-inspected | `Mailbox.daily_cap` ORM and server default is 30 (`models.py:925-927`). Seeding and the form use 15. |
| BP-06 | LOW | Code-inspected | A follow-up whose thread mailbox is paused, disabled or `needs_reconnect` fails its send job permanently. The lead sits `approved` and must be re-released after the fix (`send.py:384-385`; `queue.py:328-329`). |

## 10. Launch acceptance (measurable) and who accepts

| # | Criterion | Evidence | Accepts |
|---|---|---|---|
| A1 | Every FAIL in `REQUIREMENTS-MATRIX.md` is fixed and re-tested, or explicitly accepted by the accountable person. | Targeted and full suite on disposable `_test` databases, stubs only | Sam |
| A2 | Every email, follow-ups included, needs two separate human actions, and nothing else creates a send job. | `test_sending.py:716`, `test_recovery_send.py:27` pass on the final tree; compliance-auditor challenge | Sam |
| A3 | Copy is exact and carries no statistics. | Sean's written sign-off (`LAUNCH-PLAN.md:258`); preflight `proof_points` clean | Sean |
| A4 | Postal address confirmed by Maaz on the production workspace. | Preflight `postal_address` clean | Maaz |
| A5 | All four mailboxes connected, with one send and one read each in the authorised seed rehearsal. Headers show SPF/DKIM/DMARC pass and `List-Unsubscribe`. | Rehearsal output | Maaz + Sam |
| A6 | The unsubscribe host serves https from the VM and only `/u/*` and `/health`. | External check | Sam |
| A7 | Real exports reconciled: one outcome per row, totals equal the paused-campaign counts, replay is idempotent. | Reconciliation report | Sam (run), Maaz (counts) |
| A8 | OD-01..07, 09, 14, 17, 18, 19 and 20 decided and dated in this file. | This file | Owners in §8 |
| A9 | Off-VM backup, key escrow, and an isolated restore that includes token decryption. | Restore log | Sam |
| A10 | Production `ops preflight` exits 0, with zero unresolved attempts and zero pending dry-run send jobs. | Preflight output | Sam |
| A11 | Operator walkthrough: Maaz runs import → review → approve → release → inbox tag → removal; Sean sees Search Site Marketing only. | Browser session notes | Maaz, Sean |
| A12 | Go decision (gate 7). | Recorded decision | Sam |

The current result is **NO-GO**: open HIGH findings (WEB-01, SND-02, WEB-07, DB-01, DB-02a/b, GML-1) and BLOCKED runtime gates (DNS/TLS, OAuth, address, exports, backups) remain.
