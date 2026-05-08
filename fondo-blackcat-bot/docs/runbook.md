# R-PERFECT Runbook — pear-monitor-bot (amusing-acceptance / private)

Operational playbook for the BCD private-fund bot in production. Stays in
GitHub so any future Cowork session has the same playbook on hand.

> Public bot (gentle-luck / pear-monitor-bot-production-be98) is OUT OF SCOPE
> here — see `docs/PUBLIC_BOT_RULES.md` for that one. Never touch its env or
> code from R-PERFECT operations.

## 1. Service identity

| Field | Value |
|---|---|
| Project | `BlackCatDeFi PA FUND` |
| GitHub | `blackcatdefi/pear-monitor-bot` (subdir `fondo-blackcat-bot/`) |
| Railway service | `amusing-acceptance` |
| Health URL | `https://<railway-domain>/health` |
| Volume mount | `/app/data` (5 GiB) |

Canonical Railway IDs are stored in `auto/RAILWAY_TOKEN_ROTATION.md`. If they
go stale, refresh via the GraphQL `project(id)` query — never hard-code from
memory.

## 2. Restart procedures

### 2.1 Soft redeploy (no code change)
```graphql
mutation { serviceInstanceDeployV2(serviceId: "<svc>", environmentId: "<env>") { id } }
```
Triggers a fresh container with the same image. Use after env-var changes.

### 2.2 Code redeploy
1. `git push origin master` — Railway watches `master` (but
   `ignoreWatchPatterns=true` may suppress auto-deploy; if so, use 2.1).
2. Wait for Railway deployment to reach `SUCCESS`.
3. Verify `/health` reports the new commit hash.

### 2.3 Cold restart (last resort)
If Telethon session corruption or asyncio loop deadlock blocks polling:
1. Clear `TELETHON_SESSION` env var (bot will regenerate on next restart).
2. Trigger 2.1 redeploy.

## 3. Key rotation

1. Generate new key in the upstream provider (FRED, Etherscan, Dune, etc.).
2. Upsert via Railway GraphQL `variableUpsert`.
3. Trigger 2.1 redeploy.
4. Verify with `/selftest` — the source moves from `GRACEFUL_NO_KEY` to
   `LIVE`.
5. Revoke the old key in the upstream provider's UI.

PAT rotations follow `auto/RAILWAY_TOKEN_ROTATION.md`. Never commit any
key to git; always use Railway env vars.

## 4. Failure modes by source

### 4.1 LIVE → GRACEFUL_NO_KEY
The API now requires a key (e.g., L2Beat moved to paid tier). Either acquire
a free-tier key and rotate per §3, or accept the GRACEFUL state — it
returns `[]` and `format_for_telegram` shows the signup URL.

### 4.2 LIVE → DEGRADED
Source moved to a SPA with no JSON endpoint (e.g., HypeTrad, Visa
Onchain). The fallback parser hits a probe URL; if the probe fails, the
module returns `_global_error="spa_only_no_data"`. Action: file a code-fix
ticket only when BCD requests it; do NOT auto-attempt browser scraping
since gentle-luck-style headless playwright bloats the container.

### 4.3 LIVE → UNAVAILABLE
Network or upstream outage. Flap-alert fires after 6h sustained DOWN
(`SOURCE_FLAP_THRESHOLD_HRS`). On recovery, single LIVE alert deduped 24h.

### 4.4 LIVE → TIMEOUT (>10s)
`PER_MODULE_TIMEOUT=10.0` in `intel_selftest`. If a single source is too
slow, classify TIMEOUT and continue. Persistent TIMEOUTs over 6h will
flap-alert. Action: add stricter `params` filter or pagination.

### 4.5 LIVE → IMPORT_FAIL
Module import raised. Rare but indicates a broken commit. Action: roll
back via `git revert` + 2.2.

## 5. Cost tracking

| Threshold | Default | Env var |
|---|---|---|
| Daily LLM USD alert | 3.00 | `COST_DAILY_ALERT_USD` |
| Monthly LLM USD alert | 50.00 | `COST_MONTHLY_ALERT_USD` |

`/cost` shows last-7d breakdown. Hourly cron `_cost_alert_job` fires the
threshold check. If both thresholds trip simultaneously, message lists
both lines.

## 6. Backups

Daily at `BACKUP_HOUR_UTC` (default 04:00 UTC). Output:
`/app/data/backup/backup-YYYYMMDD-HHMMSS.tar.gz`. Retention 30d (env
`BACKUP_RETENTION_DAYS`). Optional GitHub push: set `GITHUB_BACKUP_REPO`
and `GITHUB_TOKEN`; commit goes to branch `backup`.

### 6.1 Restore
1. Download desired tarball from `/app/data/backup/` (use Railway shell)
   or from the `backup` branch on GitHub.
2. SCP/extract to `/app/data/`.
3. 2.1 redeploy. SQLite DBs reload on first DB call.

## 7. Scheduler jobs (Fase 4)

| Job | Frequency | Disable env |
|---|---|---|
| `selftest_cron` | 00/06/12/18 UTC | `SELFTEST_CRON_ENABLED=false` |
| `backup_volume` | daily 04:00 UTC | `BACKUP_VOLUME_ENABLED=false` |
| `cost_alert` | hourly | `COST_ALERTS_ENABLED=false` |

All three default to ENABLED. Disable individually via env if needed.

## 8. Anti-re-ship matrix examples

Per Cowork constitution §8: never re-ship a feature that's already shipped
without proof of regression.

| Symptom | Likely already-shipped | Verify before re-implementing |
|---|---|---|
| "30 sources missing" | R-INTEL30 (Phase 1+2+3) | `git log --oneline | grep INTEL30` |
| "no /selftest" | Fase 3 #9 | `grep cmd_selftest bot.py` |
| "no cost tracking" | Fase 3 #3 | `grep -l format_cost_report modules/` |
| "no daily backup" | Fase 3 #5 | `grep run_backup modules/backup_volume.py` |
| "no flap alerts" | Fase 3 #4 | `grep -l evaluate_matrix modules/` |

## 9. Failure escalation tree (constitution §9)

If blocked >2h on a sub-problem:
1. Document the blocker in `deploy_history.md` with the exact error.
2. Mark the affected source/feature as **Outstanding** in the round log.
3. Continue with the next phase — never stall the whole round.
4. At the end, surface Outstanding items in the §13 final message.

## 10. /selftest matrix interpretation

| Status | Meaning | Action |
|---|---|---|
| `LIVE` | OK, fresh data | none |
| `GRACEFUL_NO_KEY` | env var missing, source skipped | rotate key per §3 |
| `DEGRADED` | SPA / partial response | file code-fix only on request |
| `UNAVAILABLE` | upstream error | wait for §4.3 flap alert |
| `TIMEOUT` | >10 s | tune params |
| `EMPTY` | response had no parseable rows | inspect schema drift |
| `IMPORT_FAIL` | broken module | roll back the offending commit |
| `EXCEPTION` | uncaught error | grep `intel.log` for traceback |

## 11. Health endpoint contract

`/health` returns JSON with the following R-PERFECT-required fields:
`commit`, `commands_registered`, `intel_24h_calls.total`,
`cost_24h_usd`, `backup_last_run.iso`, `selftest_last.counts`. Add new
fields here when expanding observability — keep the contract additive.
