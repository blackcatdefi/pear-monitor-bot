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
- `src/bot.js` — Telegram commands and user interface (includes HyperLend Borrow menu)
- `src/monitor.js` — Core polling loop, TP/SL/position change detection, HyperLend borrow check
- `src/hyperliquidApi.js` — Hyperliquid API client (positions, fills, trigger orders)
- `src/hyperLendApi.js` — HyperLend API client via HyperEVM RPC (Aave v3 fork, `getUserAccountData`)
- `src/pearApi.js` — Pear Protocol API (not actively used in monitor)
- `src/store.js` — JSON file persistence in `data/` directory (wallets + borrowWallets)

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
