# Round Log — pear-monitor-bot (Cowork ledger)

Append-only round-level summary per constitución §6 paso 8.

## R-INTEL30-PHASE1-VALIDATION — 2026-05-08

**Header**: PARTIAL (1/3 keys generadas autónomamente; 2/3 escaladas a BCD por <user_privacy> hard rule)

**Scope**: validar Phase 1 end-to-end + desbloquear módulos key-gated.

**Acciones autónomas completadas**
- EIA: form-only registration (no account creation) → email verification → key emitida (40 chars)
- Railway env: `variableUpsert` EIA_API_KEY en service de472f70 / env 2a0e3f18
- Redeploy: `deploymentRedeploy` 2cc2b42e → 2c401219 SUCCESS
- /health verify: 200, commit 6e83adb match, deploy_id 2c401219, uptime 69s
- Smoke: 11/11 módulos sin crash, EIA live con 5 series WPSR (Crude Oil 457.2M kbbl 2026-05-01, SPR 849.9M, Gasolina 219.8M, Distillate 102.3M)

**Acciones bloqueadas / escaladas**
- FRED_API_KEY: requiere account creation (Google OAuth popup escapa scope MCP; signup directo prohibido por <user_privacy>: "Never create accounts on the user's behalf"). Outstanding para BCD.
- ARKHAM_API_KEY: idem. Outstanding para BCD.

**Smoke detalle**
| módulo | estado | evidencia |
|---|---|---|
| hl_info_api | LIVE | 8 HIP-3 deployers + 5 predicted fundings (MEGA -0.0730%, etc) |
| criptoya_ar | LIVE | Oficial $1,415, Blue $1,400, Tarjeta $1,840, Mayorista $1,394 |
| bcra_macro | LIVE | 7 vars (TPM 1391, Reservas $45.9B, Base $41.3T, A3500 $1418, IPC m/m 3.40%, BADLAR 20.94%) |
| isw_ctp | LIVE | 6 noticias geopol (Russia/Ukraine + Iran/MENA via BBC + Al Jazeera RSS) |
| apollo_spark | LIVE-partial | Daily Spark feed migrado a apollo.com — feed actual 1 entry stub |
| farside_etfs | LIVE-partial | BTC -$257.5M (May 7) via bitbo; ETH/SOL bloqueados CF1010 (esperado) |
| eia_oil | LIVE | 5 series WPSR (Crude 457.2M, SPR 849.9M, Gasolina 219.8M, Distillate 102.3M, alt-id) |
| fred_api | GRACEFUL_NO_KEY | "FRED_API_KEY not set → Set env var" — comportamiento por diseño |
| arkham_intel | GRACEFUL_NO_KEY | "ARKHAM_API_KEY not set → Set env var" — comportamiento por diseño |
| hypurrscan | GRACEFUL_SPA | http_404@/api/auctions → link a hypurrscan.io/auctions |
| asxn_data | GRACEFUL_SPA | html_no_data@/ → link a data.asxn.xyz/dashboard/hype |

**Próximo round**: R-INTEL30-PHASE2 (16 fuentes, semana 2). NO arrancar hasta que BCD pegue FRED_API_KEY + ARKHAM_API_KEY en `.secrets/tokens.env` y confirme smoke en Telegram.

## R-PUBLIC-FUNDS — 2026-07-22

**Scope**: public bot (gentle-luck) — universal deployable-capital engine + /funds + /fundsalert opt-in scheduler; side task private bot DESTACADO tactical label.

- NEW src/fundsEngine.js — universal per-wallet deployable capital: spot free stables (MAX rule, negatives=borrowed never free), perp withdrawable, PM borrow headroom (LTV map HYPE=0.5 + PM_LTV_MAP override, projected liq debt/(0.7125·tokens) single-asset), account_type detection (unified/perp_only/spot_only/pm/empty), fetch errors surface as 'fetch error' never $0.
- NEW src/fundsAlertStore.js — opt-in store + edge-triggered hysteresis (fire on below→above crossing, disarm, re-arm <50%·threshold or 12h cooldown). JSON on Railway volume.
- NEW src/commandsFunds.js — /funds (tracked or explicit wallet) + /fundsalert <usd>|off (default $500). Branded footer, health telemetry handlers.
- NEW src/fundsAlertScheduler.js — 20-min scan (clamped 15–30), unique-wallet dedup, 10-min cache, jitter, 60-wallet/cycle rotation. PM metric preferred when both fire, one msg/wallet/cycle.
- src/extensions.js — additive wiring only (2 requires, 1 wire entry, scheduler start/stop).
- Private bot: templates/formatters.py `_tactical_book_label()` — DESTACADO header now TACTICAL SHORTS/LONGS/BOOK derived from real sides (was hardcoded LONGS over an all-shorts book).
- Tests: Node 528→560 (+32, 0 regressions). Python 1130→1136 passed (+6; the 40 fails are pre-existing env/network, identical set on baseline). Surface regression guard asserts all 14 pre-existing wire modules intact + exactly 2 new handlers.
