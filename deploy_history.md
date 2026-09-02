# Deploy History — pear-monitor-bot (amusing-acceptance)

Append-only log per Cowork constitución §6 paso 8.

## 2026-09-01 — R-BOT-DEFINITIVE (ronda final de endurecimiento)

- **base commit**: `97d42ef` · **head**: ver commit final de la ronda
- **service**: pear-monitor-bot (amusing-acceptance) / branch `master`
- **deploy**: automatico por push a master (no hubo token de Railway en esta
  sesion, asi que el deploy_id NO se pudo capturar ni verificar desde aca).
- **suite**: 1300 -> 1402 passed, verde desde los dos cwd y con orden aleatorio
- **comandos**: 95 -> 97 (`/diagnostico`, `/trackrecord`)
- **fases**: 0 (autosuficiencia) · 1 (barrido de degradacion silenciosa) ·
  2 (/health como fuente unica + selftest) · 3 (invariantes + recompute
  independiente + PPC) · 4 (feeds) · 5 (backup verificable) · 6 (/trackrecord)
- **hallazgo principal**: el backup nocturno no estaba viejo, no tenia tablas.
  `tar.add()` sobre el .db crudo con una conexion viva abierta empaqueta un
  sqlite sin el WAL integrado. Demostrado: 1000 filas, la copia cruda levanta
  "no such table". Venia informando exito todas las noches.
- **variables que deben vivir en el secrets store de Railway** (nombres, nunca
  valores): `GITHUB_TOKEN`, `GITHUB_REPO` (autoactualizacion, Fase 0.3);
  `FRED_API_KEY`, `ARKHAM_API_KEY` (pendientes de alta por BCD).
- **variables nuevas, todas con default seguro**: `AUTODIAG_ENABLED`,
  `AUTODIAG_HORAS` (6), `AUTODIAG_COOLDOWN_H` (24), `VOLUME_ALERT_PCT`.
- **pendiente de verificacion en vivo**: `/health` con el commit nuevo y la
  salida real de `/diagnostico` en produccion.

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

## 2026-08-26 — R-LEDGER-FIX (D1 funding cero / D2 wallet faltante / D3 ROE inflado)

- **base commit**: `2d527ce` (R-TRADE-LEDGER: first-run cursor seed + section render cap)
- **service**: pear-monitor-bot (amusing-acceptance) / branch `master`
- **archivos**: `modules/trade_ledger.py`, `bot.py`, `.env.example`, `requirements.txt`, `tests/test_trade_ledger.py`
- **suite**: 1291 passed (`python3 -m pytest tests/ --asyncio-mode=auto`)
- **env vars nuevas (opcionales, todas con default seguro)**:
  - `LEDGER_ASSUMED_LEVERAGE=3` (antes 5 hardcodeado → ROE inflado ~67%)
  - `LEDGER_PAGE_PAUSE_SEC=1.1` (pacing anti-429, HL cobra weight 20 / 1200 por min)
  - `LEDGER_PAGE_MAX_ATTEMPTS=3`
  - `LEDGER_WALLETS=` (CSV extra; la wallet de RETO ya entra por FUND_WALLET_2)
- **migracion automatica al boot**: `ledger_positions.margin_open` (ALTER TABLE),
  tabla `ledger_sync_health`, y recompute one-shot de todas las filas via
  `semantics_version=2` (re-precia el ROE historico a 3x sin intervencion).
- **verificacion en vivo (sync real contra HL, DB limpia)**: ciclo 2026-08-13
  con 15 patas por wallet, funding real, alerts=0, health ok en ambas.
  - core 0xc7ae: gross +1,417.14 / fees -62.75 / funding -9.32 / NET **+1,345.07**
  - reto 0x171b: gross +67.93 / fees -6.26 / funding -2.03 / NET **+59.64**
  - COMBINADO 30 patas: NET **+1,404.71**
