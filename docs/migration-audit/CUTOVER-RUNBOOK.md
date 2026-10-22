# Cutover runbook: Smartlead to the owned platform

**Status: NOT EXECUTABLE YET.** Every gate in §1 must be green first. The decision is NO-GO (`README.md`).

This runbook adapts `docs/LAUNCH-PLAN.md` §cutover for the Oracle VM; the Render steps there are historical. Commands were checked against the `--help` of the current tree on 2026-10-19. On the VM, run every app command as:

```
sudo -u mazz sh -c 'set -a; . /etc/mazz/env; set +a; cd /opt/mazz && .venv/bin/python -m <module> <args>'
```

`$M` below abbreviates that wrapper. Change the env with `sudoedit /etc/mazz/env`, then `sudo systemctl restart mazz-web mazz-worker`.

## 1. Preconditions (all required)

| # | Gate | Evidence | Owner |
|---|---|---|---|
| P1 | Audit fixes committed, reviewed and deployed as one revision. The VM's code hashes match that commit. | `VERIFICATION.md`, plus a hash manifest from the VM | Sam |
| P2 | DNS for `app.` and `outreach.` points at the VM, and Caddy serves valid TLS (VM-01) | `VM-READINESS.md` §6 | DNS owner / Maaz |
| P3 | Off-VM backup and escrow of `/etc/mazz/env` done. One restore of that copy tested. | Restore log | Sam |
| P4 | Alert channel for `ops health` (VM-04, OD-19) | Test alert received | Sam |
| P5 | Business decisions OD-01, 02, 03, 05, 06, 07, 17 and 18 recorded (`BUSINESS-PROCESS.md` §8) | Signed-off answers | Maaz/Sean/Sam |
| P6 | Zapmail's written OK for Custom OAuth | Email on file | Maaz |
| P7 | Maaz confirmed the postal address on `/workspaces/<id>/edit` | `ops preflight` gate clears | Maaz |

**Stop if** any precondition lacks evidence. Do not substitute "probably fine".

## 2. Before cutover day (sending stays off: `SEND_ENABLED=false`, `EMAIL_SENDER=stub`)

1. **Users.** `$M mazz.users list`. Set passwords with `$M mazz.users set-password ...`. Expect Maaz and Sean listed.
2. **OAuth settings.** `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `OAUTH_TOKEN_KEY` and `UNSUBSCRIBE_SECRET` are already present on the VM. Never rotate the last two after drafting: old tokens and links stop working.
3. **Mailboxes** (gate 3). Maaz connects each of the 4 at `/mailboxes/<id>/connect`. Expect every mailbox `active` and one read each working. This is safe while Smartlead still sends: nothing sends while `SEND_ENABLED=false`.
4. **DNS auth.** Run `python scripts/verify_dns.py <domain>...` for both sending domains. Expect SPF, DKIM and DMARC to pass.
5. **Seed rehearsal** (gate 5). Run `scripts/rehearsal.py --seeds <allowlisted seeds>` in a one-off shell with `SEND_ENABLED=true EMAIL_SENDER=google` set on **that command only**. Expect one message per mailbox, with headers showing a DKIM pass and the footer plus `List-Unsubscribe`. Click the unsubscribe link once and confirm the seed lands in suppression. Seeds need OD-20 resolved.
   - **Stop if** any header fails, or the link is not https on `outreach.`.
6. **Preflight.** `$M mazz.ops preflight`. Expect only the gates that depend on cutover to remain.
7. **Backup.** `$M mazz.ops backup`. Expect a verified dump in `/var/backups/mazz`. Copy it off the VM.

## 3. Cutover day

| Step | Action | Expected result | Stop condition | Owner |
|---|---|---|---|---|
| C1 | Maaz **pauses** the Smartlead campaign. Writers stay idle from here on. | Campaign shows paused | It is still sending | Maaz |
| C2 | Export **A** (block list/unsubscribes), **B** (campaign leads with status and sent counts) and **C** (exclusions) from the paused campaign. Read the campaign's counts off the screen. | 3 CSVs and the on-screen totals | Missing an export; headers differ from what `COLUMNS` expects. **Never guess headers.** | Maaz → Sam |
| C3 | Update `smartlead.EXPECTED` in the repo to the paused totals. Review, commit and deploy (P1 applies). | VM hash matches the commit | — | Sam |
| C4 | `$M mazz.ingest.smartlead suppression A.csv` (dry run) | Row counts equal A. Zero invalid rows. | Any invalid or unknown row. Do not reach for `--accept-mismatch` to waive row problems (it cannot). | Sam |
| C5 | `... exclusions C.csv --workspace <slug>` (dry run) | Domains and emails counted. Zero invalid. | Same as C4 | Sam |
| C6 | `... leads B.csv --campaign <id>` (dry run) | Totals equal `EXPECTED`. Zero `held_unclear` rows. Contacted rows become `legacy` sent rows; not-started rows queue enrich. | Any count mismatch you cannot explain. Any unknown status. | Sam |
| C7 | Backup (`$M mazz.ops backup`), then rerun C4, C5 and C6 with `--commit`, in that order | Same numbers as the dry runs. Replays are idempotent. | Numbers differ from the dry run | Sam |
| C8 | Maaz **detaches the 4 mailboxes** from the Smartlead campaign. **Warmup stays on.** | Mailboxes free in Smartlead | — | Maaz |
| C9 | Set `EMAIL_SENDER=google` (with `SEND_ENABLED` still false). Restart. Run `$M mazz.ingest.smartlead adopt` dry, then `--commit`. | Each contacted lead has a Gmail thread, and pre-cutover replies are recorded. Leads whose thread can't be found are **held**. | Many unfound threads; adoption errors | Sam |
| C10 | Re-pace overdue follow-ups per OD-03 (no code exists for this; it is a release discipline in `/outbox`) | — | — | Maaz |
| C11 | `$M mazz.ops preflight` exits 0. **Sam says go.** Set `SEND_ENABLED=true` (keeping `SEND_WINDOW=true`, which boot now enforces) and restart. | Web and worker boot. `ops health` is clean. | Boot refuses (`deployment_problems`) | Sam |
| C12 | First release: Maaz releases **one** approved Email 1 in `/outbox` | One `sent_messages` row, the message in that mailbox's Sent folder, threaded correctly | Anything else: set `SEND_ENABLED=false` | Maaz |

## 4. Observation (first 5 business days)

- Run `ops health` at least twice a day until alerting exists. Act on `send_unresolved` within minutes:
  - Check the named mailbox's Sent folder.
  - Then run `$M mazz.ops resolve-send <lead_id> --sent|--not-sent --by <login email>`.
  - Never `--not-sent` without looking.
- Cap: 15 per mailbox per day, follow-ups included. **Never raise it.**
- Bounce auto-pause, the reply inbox, and the human qualified tag drive the metric. Maaz scans the four mailboxes directly as well (BP-02: replies outside the thread or outside INBOX are not seen).
- Weekly: a delta export of Smartlead's block list, imported with `suppression` (BP-04, OD-06).
- Watch memory and swap (VM-06).

## 5. Rollback

There is only one sender at a time. **Never resume the Smartlead campaign** after C8: it would duplicate every follow-up we sent.

1. Set `SEND_ENABLED=false` and restart. Nothing leaves; queued sends render dry-run MIME.
2. Resolve any `send_unresolved` by hand (§4).
3. Continue conversations manually from Gmail. Suppression stays in our DB. Keep importing Smartlead deltas.
4. If the platform itself is lost, restore the latest off-VM dump plus the escrowed env to a fresh VM (`VM-READINESS.md` §4). Without `OAUTH_TOKEN_KEY` the mailboxes must be reconnected.
5. Resuming in Smartlead means a **new** campaign built from our exported state. This is not designed (OD-07) and needs a decision before C1.
