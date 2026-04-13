# Pear Monitor Bot - Project Memory

## What This Bot Does
Telegram bot that monitors Hyperliquid positions for multiple wallets and sends alerts for:
- New positions opened
- Take Profit (TP) hit
- Stop Loss (SL) triggered
- Position manually closed
- Funds available (balance >= $10)
- **HyperLend Borrow Available** (borrow power >= $10 on HyperEVM)

## Architecture
- `index.js` — Entry point, wires bot + monitor + HyperLend API
- `src/bot.js` — Telegram commands and user interface (includes HyperLend Borrow menu)
- `src/monitor.js` — Core polling loop, TP/SL/position change detection, HyperLend borrow check
- `src/hyperliquidApi.js` — Hyperliquid API client (positions, fills, trigger orders)
- `src/hyperLendApi.js` — HyperLend API client via HyperEVM RPC (Aave v3 fork, `getUserAccountData`)
- `src/pearApi.js` — Pear Protocol API (not actively used in monitor)
- `src/store.js` — JSON file persistence in `data/` directory (wallets + borrowWallets)

## Key Configuration (src/monitor.js)
- `this.minAvailableBalance = 10` — Minimum $10 balance to trigger "funds available" alert
- `this.minBorrowAvailable = 10` — Minimum $10 borrowable on HyperLend to trigger alert
- `POLL_INTERVAL` env var — Seconds between polling cycles (default 30s)
- `HYPEREVM_RPC_URL` env var — HyperEVM RPC (default `https://rpc.hyperliquid.xyz/evm`, chainId 999)
- `HYPERLEND_POOL_ADDRESS` env var — HyperLend Pool (default `0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b`)

## Alert Logic (src/monitor.js:83-131)
TP/SL alerts fire when a trigger order disappears from `getAllTriggerOrders()`.
PnL comes from `getUserFills()` → `recentFill.closedPnl`.

## Rules & Decisions Made
### Minimum PnL threshold for TP/SL alerts = $1
- **Date:** 2026-04-06
- **Reason:** Bot was sending false/dust alerts with PnL: $0.00 (partial closes on tiny positions)
- **Fix:** `src/monitor.js` line ~102: `if (Math.abs(closedPnl) < 1) continue;`
- Alerts only fire if |closedPnl| >= $1 (gain or loss)

### HyperLend Borrow Available feature added
- **Date:** 2026-04-13
- **What:** Independent wallet list (`borrowWallets` in `user_<chatId>.json`) that monitors
  HyperLend borrow power on HyperEVM. Fires alert when `availableBorrowsUsd` crosses from
  `< $10` to `>= $10` (edge-triggered, same pattern as `hadFunds`).
- **How:** `src/hyperLendApi.js` calls the Pool contract's `getUserAccountData(address)` via
  ethers + HyperEVM RPC. Aave v3 base-currency is USD with 8 decimals.
- **UI:** New `/borrow` command and "🏦 HyperLend Borrow Available" main-menu button with
  add/remove/list/check-status sub-menu.

## How Claude Should Work on This Project
- **Siempre completar el ciclo entero:** fix → commit → push → crear PR → mergear a master. Sin esperar confirmación del usuario.
- Railway redeploya automaticamente al detectar cambios en master.
- No dejar pasos pendientes para el usuario. Si algo se puede hacer, hacerlo.

## Deployment
- Hosted on Railway (`railway.json`, `Dockerfile`)
- Persistent data via Railway volume mounted at data directory
- Multi-wallet support: each Telegram chat can register multiple wallets with labels
