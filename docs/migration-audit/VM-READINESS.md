# VM readiness: Oracle production host

**Verdict: NOT READY.** Four things block it: public DNS/TLS, connected mailboxes, off-VM backups with key escrow, and alerting.

These observations were recorded read-only on **2026-10-15** (audit lead's checkpoint, `HANDOFF-RECOVERY.md`). They have not been re-checked since. Nothing on the VM was changed, restarted, migrated or tested by this audit. Re-run the checks in §6 before cutover.

## 1. Topology (as observed)

| Item | Observed |
|---|---|
| Host | Oracle Cloud, `150.230.176.238`, hostname `maaz-vcn`. Ubuntu 22.04.5, kernel 6.8.0-1060-oracle, x86_64 |
| Size | 2 vCPU, **956 MB RAM**, 2 GB swap (179 MB used), 45 GB disk (13% used) |
| Services (systemd, enabled and running) | `caddy`, `mazz-web` (uvicorn on 127.0.0.1:8000), `mazz-worker`, `postgresql@14-main` |
| Listeners | 22 (all interfaces), 80/443 Caddy, 5432 and 8000 localhost, 2019 Caddy admin localhost, **111 rpcbind on 0.0.0.0 and ::** |
| Code | Tarball in `/opt/mazz`, no `.git`. CRLF-normalised hashes match `c55de0d` except `src/mazz/ops.py`, which is the `2720efa`/`f4218e6` version (before `9ea3174`). See `evidence/vm-source-sha256-crlf-normalized-2026-10-15.txt` and `evidence/vm-pip-freeze-2026-10-15.txt`. |
| Config | `/etc/mazz/env`, mode 640 `root:mazz`. The running web and worker environments hash equal to the file. `SEND_ENABLED=false`, `EMAIL_SENDER=stub`, `DRAFT_COMPOSER=template`. Secrets present with plausible lengths (values not recorded). `DATABASE_URL` uses a password URL for role `mazz`; the setup script uses peer auth. |
| Database | PostgreSQL 14, localhost only, hba peer/scram, `max_connections` 30, `shared_buffers` 64 MB. Role `mazz` is not a superuser. Alembic is at head `c0a7e8f9b1d2`. |
| Data | 1 workspace (postal address 66 chars, **not confirmed**), 3 users, 1 campaign, 4 mailboxes (all `needs_reconnect`, no token, cap 15), 5 suppression rows, 1 audit event. 0 leads, drafts, jobs, sent messages, replies and costs. |
| DNS | `app.`, `outreach.`, the apex and the wildcard for `getsearchsitemarketing.com` all resolve to **3.212.45.42, not the VM**. No AAAA records. |
| TLS | Fails for both hosts. Caddy has a site for the app host, but it cannot obtain a certificate until DNS points here. **The public unsubscribe link is unreachable.** |
| Patching | 53 packages upgradable; `unattended-upgrades` configured |

## 2. Drift from the repository

| Drift | Impact | Action |
|---|---|---|
| `ops.py` older than `c55de0d` | Its preflight help text points at a page that does not exist. The uncommitted audit fixes (WEB-01, SND-02, SND-07, GML-1, `resolve-send --by`, the importer) are **not deployed**. | Deploy the reviewed revision as a whole before cutover. Never deploy single files. |
| `tests/`, `.pytest_cache` and a database `mazz_test` (11 MB) on the production Postgres | Tests were run on production. The test DB shares the cluster and the backups' blast radius. | Owner decision: drop `mazz_test` and remove `tests/` from the deploy (user decision pending) |
| `DATABASE_URL` password auth instead of the script's peer URL | Works. The password exists in the env file, so the env file is a secret. | Accept, or align with the script |
| Backup cron is the old `pg_dump \| gzip` (no pipefail) | A failed dump leaves a fresh-looking file. `ops health` checks only that the file is new. | Repo fix exists (`scripts/oracle_setup.sh`: `ops backup --keep 14`, verified with `pg_restore --list`). It is not applied to the VM. |

## 3. Findings

| ID | Severity | Finding | Fix (proposed; not applied) |
|---|---|---|---|
| VM-01 | BLOCKER | DNS points elsewhere, so there is no TLS and no public `/u/` unsubscribe. Live sending must not start (guardrail 6). | Maaz/DNS owner points `app.` and `outreach.` A records at the VM, then checks Caddy obtains certificates |
| VM-02 | BLOCKER | All 4 mailboxes are `needs_reconnect` with no tokens | Gate 3: connect through Zapmail Custom OAuth after Zapmail's written OK |
| VM-03 | HIGH | Backups are local only. No escrow for `OAUTH_TOKEN_KEY` or `UNSUBSCRIBE_SECRET`. Losing the VM loses the data, and losing the key makes restored refresh tokens unreadable. | Off-VM copy (object storage, or a pull from another host). Escrow the env file in a password manager. Agree an RPO/RTO (OD-17). |
| VM-04 | HIGH | `ops health` reported healthy while all mailboxes were disconnected. There is no alert channel on Linux (`--notify` is macOS-only). | Make health fail on disconnected or needs-reconnect mailboxes once live. Add an email or SMS alert (OD-19). |
| VM-05 | MEDIUM | rpcbind (111) listens on all interfaces | `systemctl disable --now rpcbind rpcbind.socket`. Confirm the Oracle security list blocks it. |
| VM-06 | MEDIUM | 956 MB RAM for Postgres, the web app, the worker and Caddy, with swap already in use | Watch RSS for a week after cutover. Upgrade the shape if swap grows. |
| VM-07 | MEDIUM | Old backup cron masks dump failures | Apply the repo's `oracle_setup.sh` backup block |
| VM-08 | LOW | 53 pending updates | Patch in a maintenance window before cutover, with sending still off |
| VM-09 | LOW | SSH key passphrase and Oracle password were exposed in an earlier chat | Rotate both (user action) |
| VM-10 | INFO | A test DB and test files exist on production | See §2 |

## 4. Backup and restore evidence

- On 2026-10-15 a filtered, isolated restore of a historical local dump exited 0. It came up at head `c0a7e8f9b1d2` with the expected counts (workspace 1, users 2, mailboxes 4, suppression 5, campaign 1, steps 3) and was then cleaned up. The first attempt failed on a missing `mazz` role.
- **Not proven:** a restore that includes pending jobs, decrypting OAuth tokens after restore (none exist yet), an off-VM copy, or a timed RTO.
- The repo's `ops backup` verifies each dump with `pg_restore --list` before rotating. `tests/test_ops.py::test_a_real_backup_round_trips` covers this locally.

## 5. Monitoring and headroom

- Disk: 13% of 45 GB used. 14 daily dumps of a database this size are negligible.
- Memory is the constraint (VM-06).
- Monitoring: none beyond `ops health` run by hand. An unresolved send (`send_unresolved`) needs a human within minutes, so an alert channel is a go-live requirement (VM-04).

## 6. Re-check before cutover (read-only)

```
dig +short app.getsearchsitemarketing.com outreach.getsearchsitemarketing.com   # must be 150.230.176.238
curl -sI https://outreach.getsearchsitemarketing.com/u/x                        # TLS ok; 404 for an unknown token (WEB-18)
systemctl is-active caddy mazz-web mazz-worker postgresql@14-main
sudo -u mazz sh -c 'set -a; . /etc/mazz/env; set +a; cd /opt/mazz && .venv/bin/python -m mazz.ops preflight'
ss -tlnp | grep -v 127.0.0.1                                                   # only 22, 80, 443
ls -l /var/backups/mazz | tail -3                                              # today's verified dump
```
