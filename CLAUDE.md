# Pear Monitor Bot - Project Memory

## What This Bot Does
Telegram bot that monitors Hyperliquid positions for multiple wallets and sends alerts for:
- New positions opened
- Take Profit (TP) hit
- Stop Loss (SL) triggered
- Position manually closed
- Funds available (balance >= $10)

## Architecture
- `index.js` — Entry point, wires bot + monitor
- `src/bot.js` — Telegram commands and user interface
- `src/monitor.js` — Core polling loop, TP/SL/position change detection
- `src/hyperliquidApi.js` — Hyperliquid API client (positions, fills, trigger orders)
- `src/pearApi.js` — Pear Protocol API (not actively used in monitor)
- `src/store.js` — JSON file persistence in `data/` directory

## Key Configuration (src/monitor.js)
- `this.minAvailableBalance = 10` — Minimum $10 balance to trigger "funds available" alert
- `POLL_INTERVAL` env var — Seconds between polling cycles (default 30s)

## Alert Logic (src/monitor.js:83-131)
TP/SL alerts fire when a trigger order disappears from `getAllTriggerOrders()`.
PnL comes from `getUserFills()` → `recentFill.closedPnl`.

## Rules & Decisions Made
### Minimum PnL threshold for TP/SL alerts = $1
- **Date:** 2026-04-06
- **Reason:** Bot was sending false/dust alerts with PnL: $0.00 (partial closes on tiny positions)
- **Fix:** `src/monitor.js` line ~102: `if (Math.abs(closedPnl) < 1) continue;`
- Alerts only fire if |closedPnl| >= $1 (gain or loss)

## Deployment
- Hosted on Railway (`railway.json`, `Dockerfile`)
- Persistent data via Railway volume mounted at data directory
- Multi-wallet support: each Telegram chat can register multiple wallets with labels
