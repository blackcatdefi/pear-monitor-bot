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
- **2026-04-15** — Build completo del bot "Fondo Black Cat" en Python (carpeta `fondo-blackcat-bot/`). Proyecto separado del monitor Node.js: analista con 5 wallets en HyperLiquid, HyperLend on-chain, market data (CoinGecko/F&G/CoinGlass/DefiLlama), unlocks, Telethon intel (24 canales en 3 tiers), reporte con Claude Sonnet 4.5, alertas cada 5 min. Comandos: `/reporte`, `/posiciones`, `/hf`, `/tesis`, `/alertas`.

## Sub-proyecto: Fondo Black Cat Bot (`fondo-blackcat-bot/`)
- **Stack:** Python 3 (python-telegram-bot 21, Telethon 1.36, anthropic 0.39, web3 7, apscheduler).
- **Deploy:** Railway worker independiente (Procfile + railway.toml). Root dir `fondo-blackcat-bot/`.
- **Seguridad:** solo responde al `TELEGRAM_CHAT_ID` configurado; resto se ignora silenciosamente.
- **Wallets del fondo** (hardcoded en `config.py`): 4 Alt Short Bleed + 1 DreamCash/WAR TRADE.
- **HyperLend wallet** (colateral kHYPE / debt USDH): `0xCDdF...e27e`.
- **Umbrales:** HF warning 1.20, crítico 1.10. Liq distance 10%. HYPE warn $34, crítico $30. BTC warn $62K.
- **Tesis embebida en `templates/daily_report.py`:** Dalio Stage 6, WAR TRADE, Flywheel HyperLend, HYPE "House of All Finances".
- **Canales Intel:** Tier 1 (6 canales, 200 msgs), Tier 2 (6 canales, 50 msgs), Tier 3 (12 canales, 20 msgs).
- **Telethon session:** usar `StringSession` → env var `TELETHON_SESSION` (generar localmente una vez).

## Deployment
- Hosted on Railway (`railway.json`, `Dockerfile`)
- Persistent data via Railway volume mounted at data directory
- Multi-wallet support: each Telegram chat can register multiple wallets with labels
