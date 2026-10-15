# Pear Monitor Bot - Project Memory

## What This Bot Does
Telegram bot that monitors Hyperliquid positions for multiple wallets and sends alerts for:
- New positions opened
- Take Profit (TP) hit
- Stop Loss (SL) triggered
- Position manually closed
- Funds available (balance >= $10)
- **HyperLend Borrow Available** (borrow power >= $50 on HyperEVM)

## Architecture
- `index.js` — Entry point, wires bot + monitor + HyperLend API
- `src/bot.js` — Operator Telegram UI (HyperLend Borrow menu, /menu, personal wallets)
- `src/extensions.js` — Bootstrap hub: wires all R(v2)+R-PUBLIC+R-AUTOCOPY modules
- `src/commandsStart.js` — Public /start handler (first-time onboarding + recurring dashboard)
- `src/commandsTrack.js` — /track: external wallet add/remove/list for any public user
- `src/commandsCopyTrading.js` — /copy_trading: unified menu (BCD wallet / BCD signals / custom)
- `src/i18n/en.js` — Single source of truth for ALL English user-facing strings
- `src/i18n/index.js` — `t(key, vars)` helper; returns `[MISSING_STRING: key]` on miss
- `src/monitor.js` — Core polling loop, TP/SL/position change detection, HyperLend borrow check
- `src/hyperliquidApi.js` — Hyperliquid API client (positions, fills, trigger orders)
- `src/hyperLendApi.js` — HyperLend API client via HyperEVM RPC (Aave v3 fork, `getUserAccountData`)
- `src/pearApi.js` — Pear Protocol API (not actively used in monitor)
- `src/store.js` — JSON file persistence in `data/` directory (wallets + borrowWallets)

## Language
- **Bot is English-only.** All user-facing strings live in `src/i18n/en.js` (nested keys).
- Call via `const { t } = require('./i18n/index'); t('start.recurring_welcome')`.
- Spanish-leak detector: `spanishLeakRegex()` / `strictSpanishRegex()` in `src/i18n/index.js`.
- Tests: `tests/i18n_en_only.test.js` + `tests/i18n_consistency.test.js` — 438 pass.

## Key Configuration (src/monitor.js)
- `this.minAvailableBalance = 50` — Minimum $50 balance to trigger "funds available" alert
- `this.minBorrowAvailable = 50` — Minimum $50 borrowable on HyperLend to trigger alert
- `POLL_INTERVAL` env var — Seconds between polling cycles (default 30s)
- `HYPEREVM_RPC_URL` env var — HyperEVM RPC (default `https://rpc.hyperliquid.xyz/evm`, chainId 999)
- `HYPERLEND_POOL_ADDRESS` env var — HyperLend Pool (default `0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b`)

## Alert Logic (src/monitor.js:83-131)
TP/SL alerts fire when a trigger order disappears from `getAllTriggerOrders()`.
PnL comes from `getUserFills()` → `recentFill.closedPnl`.

## Rules & Decisions Made
### Minimum PnL threshold for TP/SL alerts = $50
- **Date:** 2026-04-14
- **Reason:** Bot enviaba falsas alertas con PnL: $0.00 o muy pequeño (partial closes en posiciones chicas)
- **Fix:** `src/monitor.js` line ~105: `if (Math.abs(closedPnl) < 50) continue;`
- Alerts only fire if |closedPnl| >= $50 (gain or loss)

### HyperLend Borrow Available feature added
- **Date:** 2026-04-13
- **What:** Independent wallet list (`borrowWallets` in `user_<chatId>.json`) that monitors
  HyperLend borrow power on HyperEVM. Fires alert when `availableBorrowsUsd` crosses from
  `< threshold` to `>= threshold` (edge-triggered, same pattern as `hadFunds`).
- **How:** `src/hyperLendApi.js` calls the Pool contract's `getUserAccountData(address)` via
  ethers + HyperEVM RPC. Aave v3 base-currency is USD with 8 decimals.
- **UI:** New `/borrow` command and "🏦 HyperLend Borrow Available" main-menu button with
  add/remove/list/check-status sub-menu.

### HyperLend Borrow alert threshold raised to $50
- **Date:** 2026-04-13
- **Reason:** User requested alerts only when borrow power >= $50 (was $10).
- **Fix:** `src/monitor.js` → `this.minBorrowAvailable = 50`.

## How Claude Should Work on This Project
- **Siempre completar el ciclo entero:** fix → commit → push → crear PR → mergear a master. Sin esperar confirmación del usuario.
- Railway redeploya automaticamente al detectar cambios en master.
- No dejar pasos pendientes para el usuario. Si algo se puede hacer, hacerlo.
- **Actualizar esta memoria (CLAUDE.md) automáticamente en cada mensaje del usuario**, sin que él lo pida. Anotar: cambios de configuración, umbrales, decisiones, features nuevas, bugs resueltos, y cualquier preferencia de estilo o flujo que el usuario exprese. Si el mensaje no genera cambios en el código/config (ej. saludo), igual registrar en la sección "Historial de interacciones" el tipo de pedido y la decisión tomada.
- **Omitir TODOS los permisos por default** — el usuario autorizó explícitamente "hace todo sin pedirme ningun permiso, omiti todos". Nunca pedir confirmación para acciones reversibles (edits, commits, pushes, PR creation/merge).
- **Reanudar automáticamente cuando se resetee el rate limit** — si la sesión se corta por límite de uso, retomar exactamente donde quedó al resetearse, sin esperar nuevo mensaje del usuario.
- **Revisar CLAUDE.md al inicio de cada turno** y actualizarlo al final si hubo cambios/decisiones/preferencias nuevas. Esto aplica universalmente en Claude Code, no solo a este proyecto.

## Manual de Procedimientos (operativa estándar)
Seguir este ciclo SIEMPRE al recibir un pedido del usuario, sin pedir permiso:

1. **Entender el pedido** y si aplica, buscar en internet (HyperLend, Hyperliquid, Aave, etc.) antes de codear.
2. **Leer el código relevante** antes de modificar (Read/Grep/Glob — nunca editar a ciegas).
3. **Crear branch nueva** desde `master` actualizado: `git checkout master && git pull && git checkout -b claude/<descripcion-corta>`.
4. **Implementar cambios** con Edit/Write. Si hay nueva dependencia, `npm install`.
5. **Verificar**: `node -c` sobre todos los archivos tocados y, si aplica, cargar los módulos con `node -e` para detectar import errors. No ejecutar llamadas de red en el sandbox (no tiene internet).
6. **Actualizar `CLAUDE.md`** con lo que se hizo (feature, umbral, decisión, fecha).
7. **Commit** con mensaje descriptivo (estilo `feat:` / `fix:` / `docs:` / `chore:`) usando HEREDOC.
8. **Push** con `git push -u origin <branch>`.
9. **Crear PR** contra `master` usando el MCP de GitHub (`mcp__github__create_pull_request`). Título corto, body con Summary + Test plan.
10. **Mergear** con `mcp__github__merge_pull_request` (squash). Railway redeploya solo.
11. **Resumir al usuario** qué cambió y el SHA/URL del PR.

Reglas:
- Nunca pedir permisos — "omití todos los permisos" es default.
- Nunca pushear a `master` directo; siempre PR + merge.
- Si el sandbox no tiene red, confiar en los syntax checks + ABI/selector validations y dejar el smoke test para Railway.
- Branch naming: `claude/<kebab-case-descripcion>`.
- Mantener este manual actualizado cuando el usuario agregue nuevas preferencias de flujo.

## Historial de interacciones
- **2026-10-15** — **R-RONDA-OCT** — cinco items verificados en produccion + rotacion del PAT.
  - **1. Deuda PM: por que subio 92.9K → 94.2K en 22 dias.** Dos causas que ahora se distinguen. **(a) Interes** — `modules/debt_carry.py`: lee el APR VIVO del reserve USDC de HL (`borrowLendReserveState` + `token: 0` → `borrowYearlyRate "0.05"`, el 5% que BCD ve en Earn), con procedencia (`HL live` / `HL cache Nmin` / `CONFIG — HL no leido (<error>)`) y subsistema `debt_carry` (money) que se degrada si HL no responde; acumula `deuda × APR × Δt/365` en `intel_memory.db` (`debt_carry_accrual`) en cada lectura del PM (job hourly `pm_alert` + /reporte + /hf + /pm + /posiciones) y guarda marcas (`debt_carry_marks`: `last_report`, `cursor`) para imprimir "desde ult. reporte" y "desde cursor"; `/carrycursor` reinicia el cursor. **(b) Retiros personales** — `modules/retiros_personales.py`: detecta el patron compra spot de HYPE → `send` de HYPE a `0x2222…2222` (bridge HyperEVM) dentro de 24h en 0xc7ae, lo guarda UNA vez por hash del send (`retiros_personales`: fecha, USDC prestado = notional de las compras emparejadas + fee, HYPE comprado, HYPE retirado, px, fill ids) y lo muestra en /reporte (seccion `🏧 RETIROS PERSONALES`, cursor propio), en `/retiros` (ledger completo) y en el delta como `deuda: +X por retiro, +Y por interes, ±Z sin atribuir` (el residuo se imprime, nunca se esconde). Scan en `/reporte` y en el job `ledger_sync` (boot +2min, cada 6h). **No tocan PPC/track record/tesis**: `hype_acquisition` excluye los fills de compra emparejados (`retiros_fills_excluidos`), y la linea de colateral imprime `retiros personales acumulados −N HYPE` al lado del stack vivo. **Limite honesto:** HL no expone el borrow como evento del ledger (es un saldo spot negativo), asi que "USDC prestado" es el notional de la compra, no una lectura del prestamo.
  - **Caso prueba (on-chain, 0xc7ae):** 2026-10-14 14:15 UTC compra 12.53 HYPE @ 79.767 ($999.5, par @107, fee 0.0084 HYPE) → 14:16 send 12.53 HYPE al bridge. Es el "retiro de 1,000 USDC de principios de octubre" de BCD: la fecha real es el **14 de octubre**. El scan historico (120 dias) ademas encontro 2026-08-31 ($1,000.25, 12.21 HYPE @ 81.87) y 2026-09-21 ($1,099.95, 11.64 HYPE @ 94.43), mas 4 sends de HYPE al bridge SIN compra previa (3+3+3 HYPE el 17-jun, 20 HYPE el 21-ago) que se registran como `withdraw_only` (salen del stack igual). Cuenta: 92,900 × 5% × 23/365 ≈ $293 de interes + $1,000 del retiro del 14-oct = $1,293 ≈ el +$1,292 observado.
  - **2. Tormenta candleSnapshot 429/500 (unlock monitor).** Root cause en tres partes (logs 13:50 UTC): (a) HL mide `/info` por **peso** (1200/min por IP) y `candleSnapshot` pesa 20 → techo ~60 velas/min; el screener universal barria ~180 nombres a 5 req/s → 429 por aritmetica, y despues HL devolvia 500 para ese tipo; (b) cada payload llevaba `endTime=now_ms`, asi que el cache TTL **nunca** pegaba — la misma serie 4h se pedia 2 veces por job de 30 min (`compute_snapshot` + `advance_universe_state`) y una tercera en /reporte; (c) el 429 se reintentaba 4 veces dentro de la llamada y el screener reintentaba por nombre encima: cada minuto castigado generaba cientos de intentos y nada persistia entre llamadas (misma clase que CoinGecko en R-LIVE-FIX). **Fix** en `modules/hl_client.py`: presupuesto de **peso por minuto** (`HL_CLIENT_WEIGHT_PER_MIN` 1000; 2 para l2Book/allMids/clearinghouseState/orderStatus/spotClearinghouseState/exchangeStatus, 60 userRole, 20 el resto) ademas del bucket req/s; **cooldown por host** compartido con `utils.http` ante 429 (Retry-After o 30s·2^n con techo 300s; durante el castigo ninguna llamada toca la red: cache vencido como stale o `HostCoolingDown`); **cooldown por tipo** 120s tras 2 × 5xx seguidos (el portfolio sigue fluyendo); **cero reintentos in-call ante 429** (doctrina invertida en `tests/test_hl_client.py` con evidencia); **cache compartido de velas** `get_candles(coin, interval, bars)` keyed (coin, interval), TTL 600s, sirve cualquier ventana mas corta como slice de la serie mas larga, `endTime` redondeado al minuto para que el dedupe in-flight funcione. Todos los callers de velas pasan por ahi (`unlock_monitor.fetch_4h_closes`, `telemetry.fetch_low_7d`, `farmdump_checks.fetch_hl_daily_closes` — que era un httpx crudo sin limiter —, `btc_weekly_indicators._fetch_hl_weekly`), pinneado por un test que escanea `modules/`. El screener **no barre** un host en cooldown: salta los fetch, excluye esos nombres (nunca fabrica) y lo anota en el barrido.
  - **3. "Atribucion de performance" y "Tracker de costos LLM" con ultimo ok nunca.** Atribucion: `attribute_basket_close` estaba importado en bot.py como `r18_perf_attribute` y **nadie lo llamaba** (ni comando, ni job, ni cierre de basket) → `compute_attribution` jamas corrio. Ahora corre al cerrar un ciclo del ledger (`run_close_alerts` empuja el bloque alpha/beta despues del subtotal, con entry = Σ margen desplegado, exit = entry + NET, BTC en apertura/cierre via velas 1h cacheadas) y a pedido con `/atribucion [tag]`; el registro lo declara `kind: "event"` y /diagnostico imprime "sin corridas aun — corre al cierre de ciclo (y /atribucion)" en vez de "nunca", y no lo cuenta como "sin exito". Tracker LLM: el unico `tracked` era `log_llm_call`, que solo corre tras una llamada LLM exitosa; /reporte es determinista (0 LLM) → la fila nunca recibio `mark_ok` aunque el job hourly `cost_alert` leia cost.db cada hora. Ahora `check_alert_thresholds` esta `tracked("cost_tracker")`: una lectura limpia del tracker es un exito del tracker (un error sqlite dentro de `_aggregate` sigue siendo `swallowed` y lo degrada).
  - **4. Contador X "0 llamadas hoy" un segundo despues de un /xrefresh pago.** `diagnostics._b_x` leia `get_api_stats()["count"]`, una clave que esa funcion **nunca devolvio** → 0 fijo para todos los callers (no era un hueco de registro: `x_api_calls` si tenia las filas). Nuevo `intel_memory.x_calls_today_summary()` (dia calendario UTC, sin filtro de caller: total, pagas, costo, split por caller) y el panel imprime `N llamadas hoy (M pagas · $X) · MTD $Y` + `por caller`. Tambien: el bloque importaba `modules.x_costs`, un modulo **inexistente**, y tragaba el ImportError → `creditos_restantes` siempre None; reemplazado por el gasto MTD/proyeccion de `x_store.usage_state()`.
  - **5. "IDLE (basket v4 closed NET $290.20, v5 pending capital)".** Venia de `fund_state.BASKET_STATUS`, una **constante editada a mano** el 2026-04-20 que ningun codigo actualizo despues de las baskets publicas v1/v2/v3 (jul-ago); `templates/formatters.py` la imprimia bajo cada wallet vacia como si fuera una medicion. Ahora `trade_ledger.idle_label_for_wallet()` deriva el texto del ledger (ultimo cierre de ESA wallet: tag, fecha, NET, patas; "sin cierres en el ledger para esta wallet" cuando no hay nada) y `BASKET_STATUS` solo conserva `active` (el gate manual que leen el reconciler y el catalyst scorer); `BASKET_NOTE` ya no afirma ningun NET. **Si existio un ciclo v4 con ese NET:** el ledger no tiene ninguna etiqueta v4 (los `cycle_tag` son `ciclo YYYY-MM-DD`) y el horizonte de HL (10k fills/wallet) no garantiza llegar a abril; el NET real del ultimo cierre de cada wallet se verifica en produccion con `/cierres` y `/trackrecord` (ver reporte de la ronda).
  - **Tests:** `tests/test_ronda_oct_deuda.py` (25) + `tests/test_ronda_oct_hl_cooldown.py` (29) + doctrina invertida en `tests/test_hl_client.py`; `scripts/mutation_check_ronda_oct.py` — **48/48 mutaciones muertas**. Suite 1,637 verde. `bug_class_scan` TOTAL 7 (sin cambios). Allowlist: +3 coerciones puras con razon escrita; `MONEY_PATH` += `debt_carry.py`, `retiros_personales.py`. Comandos nuevos: `/retiros`, `/carrycursor`, `/atribucion` (101 handlers, registry sin drift).
  - **Leccion operativa:** una API que mide por peso no se respeta con un limite por request; y un contador que lee una clave inexistente es un cero que parece medido — la clase de bug mas barata de escribir y mas cara de leer.
- **2026-10-15** — **R-LIVE-FIX** — tres defectos visibles en los logs de produccion el mismo dia.
  - **1. Tormenta 429 de CoinGecko** (`simple/price` + `/global` cada ~60s, 3 reintentos, 429 en todos, "market" degradado de corrido). **Root cause (3 partes):** (a) el job `dashboard_snapshot_refresh` corria **cada 30s** con `force_refresh=True` y arrastraba `fetch_market_data()` → CoinGecko en cada vuelta; (b) `coingecko_prices()` **solo cacheaba exitos**: cada 429 garantizaba otro pedido 30s despues — un lazo de realimentacion, no un reintento; (c) CoinGecko era la fuente **primaria** de BTC/ETH/SOL/HYPE cuando el bot ya lee el oraculo HL para todo lo que HL lista. Las dos rondas anteriores (R-429-RETRY-AFTER, R-429-TECHO) arreglaron el backoff *dentro* de la llamada; cada llamada nueva arrancaba de cero.
  - **Fix 1:** `utils/http.py` — **cooldown por host** que sobrevive a la llamada (`_HOST_COOLDOWN`): 429 → `Retry-After` o 60s·2^(strikes−1) con techo 15 min; en cooldown `request_json` levanta `HostCoolingDown` sin tocar la red; un 2xx resetea. Un 429 **sin** Retry-After ya no se reintenta en-llamada (doctrina invertida en `tests/test_http_retry_after.py`, con evidencia). `modules/market.py` — **HL oracle primero** (`source="hl_oracle"`, 24h change desde `prevDayPx` via `hl_prices.get_prev_day_prices`), CoinGecko solo para lo que HL no lista (SILVER) y para market cap; TTL ensamble 300s, gecko 600s, `/global` 900s, **cache negativo** 120s, `max_retries=1`; CoinGecko caido con HL sano **no degrada market** (solo nota `coingecko_note` en `fetch_market_data().data`); sin HL ni CoinGecko para un core → `swallowed` (si degrada). `/global` caido devuelve `{"unavailable": motivo}`, no `{}`. `unlock_monitor.fetch_btc_dominance` pasa por el mismo cache (antes hacia su propio GET crudo). `bot.py`: refresco proactivo **300s** (piso 120s).
  - **2. `hl_borrow_lend.get_collateral_ltv_map` HTTP 422** en cada llamada. **Root cause:** HyperLiquid cambio la forma del pedido — `{"type":"borrowLendReserveState"}` sin parametros **ya no existe**: ahora exige `"token": <indice spot>` y devuelve un solo reserve; el listado paso a **`allBorrowLendReserveStates`** con forma `[[token_index, state], ...]` (verificado contra api.hyperliquid.xyz el 15-oct: HYPE idx 150 ltv 0.65, UBTC idx 197 ltv 0.5). El parser viejo tampoco podia leerla (filas sin nombre de token). Y la falla era **silenciosa en el panel**: el mapa volvia `{}` y la escalera usaba `PM_MAX_BORROW_LTV` de config — el numero coincidia (0.65 = 0.65) asi que nadie lo vio, pero el money path imprimia un default como medicion. **Sin tests** para el modulo: por eso sobrevivio semanas.
  - **Fix 2:** `modules/hl_borrow_lend.py` reescrito: `allBorrowLendReserveStates` + `spotMeta` (`index→name`), `get_reserve_state(token)` para el individual; fallo → `health_registry.swallowed("pm_state", …)` + **procedencia** (`ltv_map_provenance()` / `ltv_source_label()`: `HL live` / `HL cache Nmin` / `CONFIG — HL no leido (<error>)`). `PMState.ltv_source` nuevo y el panel imprime `Capacidad borrow (LTV 0.65 · HL live)`. HL **no publica** maint LTV (solo `ltv`): la formula oficial `LTV+(1−LTV)/2` sigue siendo la fuente documentada del factor parcial.
  - **3. Credenciales:** `GITHUB_TOKEN` expuesto en transcript (22-sep). `GITHUB_REPO=blackcatdefi/pear-monitor-bot` cargado en Railway via MCP (estaba ausente). La rotacion del PAT (fine-grained, Contents RW, solo este repo) requiere la UI de GitHub en Brave: ver estado en el reporte de la ronda.
  - **Tests:** `tests/test_live_fix.py` (28 casos) + `scripts/mutation_check_live_fix.py` — **28/28 mutaciones muertas**. `tests/conftest.py` resetea el cooldown por host entre tests. Allowlist: +2 swallows no-money (`_hl_prev_day_map`, `_gecko_cooldown_note`) con razon escrita. `bug_class_scan` TOTAL 7 (sin cambios).
  - **Leccion operativa:** un backoff que se olvida al terminar la llamada es una pausa, no un backoff; y un default que coincide con el valor vivo es el mas peligroso de todos porque nadie lo ve.
- **2026-09-22** — **R-X-FLIP** — el feed de X estuvo ciego 27 dias (cache del 26-ago renderizado bajo el titulo "X Timeline (last 48h)"). La llamada viva devolvia HTTP 402 desde entonces y R-BURN-CREDITS nunca flipeo a twitterapi.io.
  - **Root cause (4 defectos; los tres primeros sesgados hacia "todavia hay plata", el cuarto es el que sirvio el cadaver):**
    1. **La senal real de agotamiento nunca se leia.** El 402 existia solo como string de diagnostico (`_DIAG_402`); ningun camino lo devolvia al selector. La Console reporta el agotamiento exacto, en el cable, y `backend_selected()` lo ignoraba.
    2. **El estimador falla abierto — ESTA es la razon por la que el backend quedo pegado en `official`.** `official_x_cost_since()` devuelve 0.0 ante cualquier excepcion; las llamadas fallidas registran `tweets_returned=0`, asi que un 402 cuesta $0 en el modelo; el gasto anterior a `X_OFFICIAL_CREDITS_SINCE` es invisible por diseno. Todo modo de error empuja `remaining` **hacia arriba**, y el saldo modelado nunca toco el piso de $0.50.
    3. **El render afirmaba "last 48h" en vez de calcularlo** desde el post mas nuevo. Una ventana afirmada y no verificada es como un cache de 27 dias se disfraza de dato vivo.
    4. **`x_store` estaba sin bindear a nivel modulo en `modules/x_intel.py`** (solo se importaba adentro de 4 funciones, pero 8 lo leian como nombre global). Tres comandos morian en el acto — `/x_status`, `/costos_x`, `/intel_sources` respondian `❌ Error: name 'x_store' is not defined`, o sea que la unica superficie que habria mostrado el estado del subsistema X estaba rota ella misma. Los otros dos casos tenian el NameError **tragado por un `except` pelado**: `get_cached_timeline()` caia al espejo legacy —**eso es literalmente lo que sirvio los tweets del 26-ago**— y `cache_banner_for_report()` perdia la edad del cache, asi que el cadaver imprimia como si fuera fresco.
  - **Correccion del registro:** la hipotesis previa (`X_FETCH_BACKEND=official` explicito salteaba el modo burn) es **falsa**. Railway confirma que la variable **no existia** en el servicio. El flip no fallo por un override de entorno sino por el defecto 2.
  - **Fix:** latch de agotamiento **durable** (`x_fetch_state.official_depleted` en el volumen de Railway, con epoch `X_OFFICIAL_CREDITS_SINCE`), marcado **desde adentro del cliente oficial** (`is_depletion_response`: 402 + 403 spend-cap/credit) para que ningun caller pueda olvidarse, y con **precedencia maxima** en `backend_selected()` — por encima del env var y por encima del estimado. El despachador re-lee el selector inmediatamente despues de un fallo oficial y el provider sirve **en la misma corrida** (flipear recien en el proximo /reporte es el apagon de 27 dias en miniatura). Unica salida del latch: mover `X_OFFICIAL_CREDITS_SINCE` (epoch de refondeo).
  - **Honestidad de render:** `modules/x_staleness.py` (nuevo) computa la edad real del post mas nuevo; `/timeline`, el titulo de la Seccion 1 de `/reporte` (`bot.x_section_title`) y `cache_banner_for_report()` imprimen **DEGRADADO con la edad real** cuando el dato cae fuera de la ventana. Una ventana es una promesa que el dato tiene que cumplir.
  - **Consejo de recarga eliminado:** el fondo dejo la API oficial. `_DIAG_402` / `_DIAG_403_SPEND_CAP` / el bloque de ayuda de `/timeline` ya no mencionan console.x.com ni subir el spend cap; un test escanea **todo el repo** para que no vuelva.
  - **Borrado:** `templates/timeline1.py` (duplicado muerto con el mismo header hardcodeado — la misma mina).
  - **Binding arreglado:** un unico `from modules import x_store` a nivel modulo en `modules/x_intel.py` mata los 8 call sites. Pinneado por un test AST (el import tiene que estar en el **cuerpo del modulo**, los anidados no cuentan), un parametrizado que corre los 7 formatters y falla ante `NameError`, y un test que exige que el banner de cache reporte edad real.
  - **Lección operativa:** el `/reporte` nunca grito. El error existia hace semanas en tres comandos y estaba tragado en dos mas. Un `except` pelado alrededor de un path de datos convierte un bug duro en un apagon silencioso — es peor que el crash. Cuando una superficie degrada, tiene que **decirlo**.
  - **Tests:** `tests/test_x_flip.py` (31 casos) + `scripts/mutation_check_x_flip.py` — **11/11 mutaciones muertas**. Suite completa 1563/1563 verde (`pytest -o asyncio_mode=auto`; el repo no tiene pytest.ini).
  - **Produccion:** `X_FETCH_BACKEND=twitterapi_io` creado en el servicio Railway `pear-monitor-bot` (162→163 vars). `X_PROVIDER_API_KEY` y `X_LIST_ID` ya estaban presentes — nada inventado.
  - **Verificado en vivo (22-sep 21:00–21:09 UTC, deploy `452283b5` sobre `f471428`):** `/x_status` responde y publica la edad real del cache (**654h 31min**) en vez de tragarse el error; `/xrefresh` → **+142 posts** (`caller=xrefresh pages=25 tweets=500 est_cost=$0.1125`), `since_id` salta de `2092620034799497633` (26-ago) a `2102503206471544980`; `/reporte` hace fetch vivo por provider **dentro de la corrida** (`caller=reporte stored_new=1`), sin errores en el tail; `/timeline` imprime el header **computado** — `post mas nuevo hace 6min — backend twitterapi_io`, 79 cuentas activas / 143 tweets, posts del dia; `/diagnostico` → `*X / costos*: backend twitterapi_io · live ON` sobre `commit f471428`. Blackout de 27 dias cerrado; costo del dia ~$0.117.
  - **Higiene pendiente:** el `GITHUB_TOKEN` del servicio se leyo desde Railway para pushear sin credenciales locales y quedo expuesto en el transcript de la sesion. Rotarlo cuando BCD quiera.
- **2026-05-03** — **R-PUBLIC-START-FIX-V3** (Node) — Bot seguía mudo después de V2. Chrome MCP no disponible para obtener el token y correr diagnóstico curl directo. Root cause analysis desde código estático:
  - **V2 tenía un gap diagnóstico crítico:** `bot.deleteWebhook()` (wrapper interno de NTBA) no loguea la respuesta real de la API de Telegram ni distingue éxito de error de red. Si la llamada falla, `catch` la absorbe y el bot sigue tratando de hacer polling contra un webhook activo → 409 perpetuo. Sin Railway logs visibles, imposible saber si el webhook se limpió.
  - **Fix V3:** Reemplazar `bot.deleteWebhook()` con 4 llamadas axios explícitas en `index.js`:
    1. `GET /getMe` → verifica token válido; si retorna 401 → `process.exit(1)` con mensaje claro
    2. `GET /getWebhookInfo` ANTES del delete → loguea url + pending_update_count + last_error_message
    3. `POST /deleteWebhook` → loguea el body completo de la respuesta (`{"ok":true,"result":true}`)
    4. `GET /getWebhookInfo` DESPUÉS del delete → confirma url="" o logea "STILL ACTIVE" como error
  - **Fix adicional (bot.js):** `polling_error` ahora logea el HTTP status code (`code=409`, `code=401`, etc.) para distinguir escenarios en Railway logs. 409 → mensaje explicativo de overlap de deploy. 401 → mensaje de token revocado.
  - **Objetivo:** El próximo Railway deploy mostrará en logs el estado exacto del webhook y del token. Esto permite saber si V2 realmente limpió el webhook, si el token es válido, o si hay otro problema.
- **2026-05-03** — **R-PUBLIC-START-FIX-V2** (Node, PR #7, commit `47ec2ce`) — Bot seguía mudo después de 0571161. Tres root causes encontrados y corregidos:
  - **BUG 1 (principal):** `createBot()` usaba `{ polling: true }` iniciando `getUpdates` inmediatamente. Si había un webhook activo, Telegram rechaza con 409 "Can not getUpdates when webhook is active" en CADA llamada. `polling_error` lo logea pero nunca para — bot vivo, health checks OK, pero recibe CERO updates. Los tests pasan porque mockean TelegramBot. Fix: `{ polling: false }` en `bot.js` + `await bot.deleteWebhook()` en `index.js` antes de `bot.startPolling()`.
  - **BUG 2:** Handlers SIGTERM/SIGINT registrados DESPUÉS de `await monitor.start()` (que hace el primer poll, puede tomar segundos). Durante ese window, si Railway envía SIGTERM al container viejo, sale sin llamar `stopPolling()` → 409 Conflict para la nueva instancia. Fix: mover `process.once('SIGTERM'/'SIGINT')` inmediatamente después de `extensions.bootstrap()`.
  - **BUG 3 (defensivo):** `commands.attachCommands(bot)` en `extensions.js` no tenía try/catch — si tiraba, `bootstrap()` crasheaba antes de que `commandsStart.attach()` corriera. Fix: wrapped in try/catch.
- **2026-05-03** — **R-DASHBOARD-FIX (Python)** — 5 bugs corregidos en `fondo-blackcat-bot/modules/dashboard.py` + `portfolio_snapshot.py` + `auto/hyperlend_reader.py`:
  - **BUG 1 (Spot tokens ausentes):** Added `spot_balances` field to `WalletSnapshot`; `_build_state()` aggregates spot balances per coin from fresh wallet data; Spot Tokens HTML card added to dashboard grid.
  - **BUG 2 (UPnL discrepancy):** `_build_state()` fetches wallets once, computes UPnL directly from `unrealized_pnl_total` fields — same source as `/posiciones`. No more stale snapshot UPnL.
  - **BUG 3 (Main flywheel "Collateral: — ?"):** `portfolio_snapshot.py` now uses `read_all_with_cache()` from `hyperlend_reader`; cache extended to persist/restore `collateral_symbol`, `collateral_balance`, `debt_symbol`, `debt_balance` alongside HF.
  - **BUG 4 (Secondary flywheel "0.0000 ?"):** Secondary flywheel balance display uses conditional `"—"` when balance is zero/missing, matching main flywheel pattern.
  - **BUG 5 (Basket label hardcoded "v4"):** `basket_display` built from `basket_id_inferido` + leg count: e.g. "Basket v6 (20 legs)".
  - **Tests:** `tests/test_dashboard_fixes.py` — 9/9 green.
- **2026-05-02** — **R-EN verified** (Node.js public bot). All user-facing strings confirmed English: `src/i18n/en.js` is the single source of truth. `/start` recurring dashboard shows "Welcome back 👋", "Your setup:", "Tracked wallets: N/M", "Bot status: active". Keyboard buttons: "Track wallet", "My wallets", "Copy Trading", "Status", "My TZ", "Learn", "Open Pear Protocol". Zero Spanish strings in src/. CLAUDE.md architecture section updated. Pushed to master to trigger Railway redeploy.
- **2026-04-13** — Feature "HyperLend Borrow Available" agregada (PR #2, merge `299c6fc`).
- **2026-04-13** — Umbral de borrow subido de $10 a $50 por pedido del usuario. Se agregó manual de procedimientos y regla de auto-update de memoria en cada mensaje.
- **2026-04-14** — Umbral TP/SL subido de $1 a $50. Umbral fondos disponibles subido de $10 a $50. Ambos por pedido del usuario para eliminar falsas alertas.
- **2026-04-15** — Build completo de **Fondo Black Cat Bot** (Python) en subdirectorio `fondo-blackcat-bot/`. Bot de Telegram en Python que actúa como analista personal del fondo: portfolio HyperLiquid (5 wallets), HyperLend on-chain (web3.py), market data (CoinGecko/CoinGlass/DefiLlama/F&G), token unlocks, Telethon intel de 24 canales, análisis con Claude Sonnet 4.5. Comandos: `/reporte`, `/posiciones`, `/hf`, `/tesis`, `/alertas`. Convive con el bot Node existente en el mismo repo. Reglas nuevas agregadas a "How Claude Should Work": omitir TODOS los permisos por default + reanudar automáticamente cuando se resetee el rate limit + revisar CLAUDE.md cada turno.
- **2026-04-28** — **HOTFIX 4-bug en bot de alertas Pear Protocol** (Node, raíz del repo). Disparado por basket close 19:07 UTC (6 SHORTs, wallet 0xc7AE) donde Telegram reportó datos incorrectos (BLUR +$47.69 vs real +$406.94, ARB con TP+SL ambos disparados, mensajes duplicados, sin resumen total). Refactor unificado en `src/closeAlerts.js` + `src/monitor.js`:
  - **BUG 1 (PnL)**: `aggregateClosePnl()` suma `closedPnl` de TODAS las fills desde `openedAt`, no solo la última. BLUR reportado correctamente como +$406.94.
  - **BUG 2 (duplicados)**: `shouldSendAlert(wallet, coin)` dedup por minuto en cache 60s. Una sola alerta por cierre.
  - **BUG 3 (TP+SL ambos)**: `classifyCloseReason()` retorna UN solo motivo (TAKE_PROFIT / STOP_LOSS / TRAILING_OR_MANUAL / MANUAL_CLOSE) usando match de exit_price con trigger_px (tolerancia 1%). ARB con TP $0.09761 y SL $0.16268, exit $0.1247 → resuelve a TRAILING_OR_MANUAL (ningún trigger matchea).
  - **BUG 4 (resumen basket)**: `trackCloseForBasket()` detecta 3+ cierres en 5min y dispara summary consolidado tras debounce 30s. Formato: `🐱‍⬛ BASKET CLOSED — {wallet}` con PnL total + breakdown ordenado.
  - El antiguo handler de TP/SL en `monitor.js` (que cargaba el último `recentFill.closedPnl`) y `notifyManualClose` (que duplicaba la alerta) FUERON REEMPLAZADOS por un único pase sobre `closedCoins` que clasifica razón + agrega PnL una sola vez.
  - **Tests de regresión**: `src/closeAlerts.test.js` con 18 casos (node:test). Caso E2E replica el cierre 28-abr-19:07 exacto. Run: `node --test src/closeAlerts.test.js`.
  - El umbral de $50 en TP/SL fue removido — el nuevo flujo solo alerta en cierres COMPLETOS (coin desaparece de positions), no en partial closes, así que no hay spam de dust.

## Fondo Black Cat Bot (Python) — `fondo-blackcat-bot/`
- **Stack:** python-telegram-bot v21 + Telethon (userbot) + APScheduler + web3.py + Anthropic SDK
- **Modelo:** `claude-sonnet-4-5` (con prompt caching en system prompt)
- **Wallets monitoreadas (5):** definidas en `fondo-blackcat-bot/config.py:FUND_WALLETS`
- **HyperLend wallet:** `0xCDdF18c16EA359C64CaBe72B25e07F4D3F22e27e` (HYPERLEND_WALLET)
- **Umbrales (config.py):**
  - HF warn < 1.20, critical < 1.10
  - HYPE warn < $34, critical < $30
  - BTC warn < $62,000
  - Liquidation proximity < 10%
  - POLL_INTERVAL_MIN = 5
- **Seguridad:** `@authorized` decorator → solo `TELEGRAM_CHAT_ID` autorizado, otros chats ignorados silenciosamente.
- **Telethon session:** generar localmente con `scripts/generate_session.py` → guardar como `TELETHON_SESSION` env var en Railway.
- **Deploy independiente en Railway:** este subdirectorio se conecta como un servicio separado (root = `fondo-blackcat-bot/`). El bot Node sigue corriendo aparte en su servicio existente.

## Deployment
- Hosted on Railway (`railway.json`, `Dockerfile`)
- Persistent data via Railway volume mounted at data directory
- Multi-wallet support: each Telegram chat can register multiple wallets with labels
- **Fondo Black Cat Bot (Python):** segundo servicio Railway, root = `fondo-blackcat-bot/`, start = `python bot.py`
