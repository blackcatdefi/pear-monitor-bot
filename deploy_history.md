# Deploy History — pear-monitor-bot (amusing-acceptance)

Append-only log per Cowork constitución §6 paso 8.

## 2026-05-08T16:45:21Z — R-INTEL30-PHASE1-VALIDATION (redeploy, env var update)

- **commit**: `6e83adb` (R-INTEL30-PHASE1 hotfix — fix 5 broken endpoints)
- **deployment_id**: `2c401219-ddbb-4276-8289-f0890dbeb32e`
- **status**: SUCCESS
- **service**: pear-monitor-bot (de472f70)
- **project**: amusing-acceptance (be38a440)
- **env**: production (2a0e3f18)
- **branch**: master
- **public domain**: pear-monitor-bot-production.up.railway.app
- **action**: variableUpsert(EIA_API_KEY) → deploymentRedeploy(2cc2b42e → 2c401219)
- **/health match**: ✅ commit=6e83adb, status=ok, uptime 69s post-restart
- **env vars set this deploy**: `EIA_API_KEY` (40-char, Fw1t…XH)
- **outstanding env vars (BCD signup)**: `FRED_API_KEY`, `ARKHAM_API_KEY`
- **smoke result**: 11/11 modules healthy
  - LIVE: hl_info_api, criptoya_ar, bcra_macro, isw_ctp, apollo_spark, farside_etfs (BTC), eia_oil
  - GRACEFUL (no key): fred_api, arkham_intel
  - GRACEFUL (SPA migration): hypurrscan, asxn_data
