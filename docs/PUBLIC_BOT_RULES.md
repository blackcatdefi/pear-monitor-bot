# Public Bot Rules

**Date created:** 2 may 2026 (R-NOSPAM)
**Owner:** Black Cat DeFi Fund
**Service:** `gentle-luck` / `pear-monitor-bot` — public Telegram bot
**Status:** PERMANENT RULE — applies to every future round.

---

## Why this doc exists

On Sat 2 may 2026 09:29 AR (12:29 UTC), the heartbeat scheduler woke BCD
and his girlfriend mid-sleep with a sound notification reading:

```
✅ Pear Alerts Bot online
· Uptime 6.0h
· Errors 24h: 0
· Last poll: 2026-05-02 12:29 UTC
```

Heartbeats are **operator monitoring telemetry**. They belong in Railway
logs / `GET /health` — they do NOT belong in a Telegram broadcast that
vibrates phones. **A user notification that does not require user action
is a bug.**

This document codifies the rule so future changes (new modules, new
rounds, new features) cannot regress it.

---

## The Rule

**Public bot = silence by default.** The bot may only emit a Telegram
message when ONE of these three conditions is true:

### 1. Actionable for the user

The user can do something with the message, and would want to be
woken up to see it. Examples:

- A new basket opens in a wallet the user is tracking — "you may want to
  copy-trade this."
- A copy-trade signal fires — "execute this trade now if you want it."
- A position closes with profit/loss — "your position resolved."

### 2. Critical risk

The user's tracked position is at imminent risk and inaction has cost.
Examples:

- HF (Health Factor) of a tracked HyperLend wallet drops below 1.10 —
  liquidation threshold approaching.
- A tracked basket hits its stop-loss or take-profit and closes.
- The Pear API stops responding for >15 min and the user's open positions
  are now blind.

### 3. On-demand response

The user just sent a slash command. The bot responds. Examples:

- `/status`, `/portfolio`, `/dedup_status`, `/help`, `/start`.

---

## What the bot must NEVER auto-emit

Anything that fits the pattern *"the bot is fine, here's a status
update"* is a violation. Specifically banned:

- ❌ Heartbeats / "bot online" / "bot started"
- ❌ Uptime stats, error counters, polling stats
- ❌ "Successful poll" pings
- ❌ Self-announcements after deploy/restart
- ❌ Periodic summaries of internal state (cache hits, rate-limit stats)
- ❌ Boot announcements ("R-FOO deployed at HH:MM UTC")
- ❌ Anything that is monitoring telemetry rather than user-facing intel

These belong in:

- **Railway logs** — `console.log(...)` is fine, anyone with a Railway
  account can tail.
- **`GET /health` endpoint** (port 8080) — JSON status endpoint already
  exists at `src/healthServer.js`. Add fields here, not to broadcasts.
- **Internal admin chat** (BCD's private chat) — IF AND ONLY IF the
  message is something BCD specifically opted into via env var
  (`HEARTBEAT_ENABLED=true`, `BOOT_ANNOUNCEMENT_ENABLED=true`, etc.) AND
  the message has `disable_notification=true` set.

---

## Notification urgency tiers

When a message DOES qualify for emission, classify it:

| Tier | When | Telegram flag |
|------|------|---------------|
| **CRITICAL** | HF < 1.10, basket SL/TP triggered, copy-trade signal | sound (default) |
| **ACTIONABLE** | New basket opened, position closed | sound (default) |
| **INFORMATIONAL** | Borrow available, funds available, daily digest | `disable_notification: true` |

Borrow-available alerts are explicitly INFORMATIONAL — a wallet having
$174 of borrow capacity is not urgent. Setting `disable_notification: true`
on these alerts means the message still arrives in the chat history but
does not vibrate phones at 3 AM. (Implemented in `monitor.js` for
HyperLend borrow alerts, R-NOSPAM commit.)

---

## Dedup is mandatory for any auto-emit

If the bot is emitting the same alert more than once for the same
underlying state, it's spam. Every auto-emit path must have a dedup gate:

- **Basket open** — `src/basketDedup.js` (SHA-256 hash, persistent JSON
  on Railway Volume, TTL 7d). Must hydrate on silent boot poll so
  pre-existing baskets don't re-emit.
- **Borrow available** — `src/borrowAlertGate.js` (per-wallet state on
  Railway Volume, 30 min cooldown, 5% available delta gate, 0.05 HF
  delta gate). Force-emits on HF cross < 1.10 or > 50% available delta.
- **Funds available** — `src/fundsAvailableGate.js` (R(v3): TWAP-aware,
  1h dedup window, $200 min residual).
- **Compounding** — `src/compoundingGate.js` (TWAP-aware, account-grew
  required).
- **Close alerts** — `src/closeAlerts.js` `shouldSendAlert()` (60s
  cooldown per `wallet:coin`).

If you add a new auto-emit path, you must add a dedup gate. Period.

---

## Persistence: dedup state must survive restarts

In-memory `Map`s reset on every container restart. Railway redeploys,
crashes, manual restarts all wipe in-memory state. Therefore every dedup
state must persist to:

```
${RAILWAY_VOLUME_MOUNT_PATH}/data/<feature>.json
```

Railway's Volume on `gentle-luck` is mounted at `/data`, so files land
at `/data/data/<feature>.json`. The double `data/` is intentional — the
inner `data/` is a subdir of the volume mountpoint so test fixtures and
production deploys share the same relative layout.

Atomic writes (write-tmp-then-rename) are required to avoid torn reads
if the process is killed mid-write.

---

## Test plan for every new auto-emit module

Before merging a new alert path, prove:

1. **First emit works** — fresh state, alert fires.
2. **Repeat emit suppressed** — same input N seconds later, no fire.
3. **State persists across module reload** — `delete require.cache[path]`,
   re-require, repeat input still suppresses.
4. **State survives restart** — `_resetForTests()` wipes; without that
   call, JSON file is preserved on disk.
5. **Critical override works** — force-emit conditions bypass cooldown.
6. **Off switch works** — `<MODULE>_ENABLED=false` short-circuits to "no
   emit" without crashing.

All of these patterns are demonstrated in `tests/basketDedup.test.js`,
`tests/fundsAvailableGate.test.js`, `tests/compoundingGate.test.js`,
and now `tests/borrowAlertGate.test.js`.

---

## Anyone who wakes a sleeping user owes the bug a postmortem

If a future round emits a non-critical alert that wakes someone up, the
fix is not "tune the threshold." The fix is:

1. Confirm the alert was non-critical (per the rule above).
2. Apply `disable_notification: true` so it never wakes anyone again.
3. Add a dedup gate if missing.
4. Add a test that proves the dedup works.
5. Update this doc with the new rule (if a new pattern was uncovered).

R-NOSPAM is a permanent reference for this process.

---

## R-PUBLIC-SPAM-FINAL — per-leg INDIVIDUAL_OPEN is forbidden by default

**Date:** 4 may 2026
**Trigger:** 21 identical "🟢 NEW POSITION OPENED — vntl:ANTHROPIC" messages
between 13:07-15:41 UTC for wallet 0xc7AE. Same entry $1114.80, same size
1.401, same notional $1,562. BCD did not touch the position.

**Root cause:** `openAlerts.emitAlerts` had two execution paths:
1. `BASKET_OPEN` — protected by 4 stacked gates (Gate-0 wallet lockout +
   60s wallet debounce + SHA-256 dedup + shouldSendAlert). Working perfectly.
2. `INDIVIDUAL_OPEN` (1-2 legs) — only protected by `shouldSendAlert(wallet,
   OPEN_${coin})`, a 60s per-coin window. When `findNewPositions` keeps
   re-flagging the same leg as new every 2-7 minutes (snapshot churn —
   the leg's `dex` field flips between `pear`/`Native`/undefined across
   polls, OR Hyperliquid surfaces the leg intermittently), shouldSendAlert
   lets each one through after the 60s window elapses. 21 emits in 2.5h.

**Fix:** Per-leg `INDIVIDUAL_OPEN` path is now killed by default.

```javascript
function isPerLegDisabled() {
  const v = (process.env.PER_LEG_ALERTS_DISABLED || 'true').toLowerCase();
  return v !== 'false';
}
```

When `isPerLegDisabled()` is true (the default), `emitAlerts` returns
`{type: 'INDIVIDUAL_OPEN_BLOCKED', dispatched: 0}` and increments
`healthServer.perLegAlertsBlockedLifetime`.

**Why this is safe:** Pear basket trading places 3+ legs in a TWAP burst,
so any *real* new-leg event for the BCD fund is `BASKET_OPEN`.
`INDIVIDUAL_OPEN` in this flow is by definition a snapshot artifact, not
real trading activity.

**Forensic mode:** Set `PER_LEG_ALERTS_DISABLED=false` in Railway env to
restore the legacy per-leg path (e.g. while diagnosing snapshot churn).

**Telemetry:** `GET /health` exposes `spam_guard.per_leg_alerts_blocked_lifetime`.
If it grows but `events_deduplicated_lifetime` stays flat, the snapshot diff
is churning — investigate the underlying `lastSeenSnapshots` instability.

**Permanent rule for future rounds:**
- Any new "open"-type alert path MUST go through the same 4-gate stack as
  `BASKET_OPEN`, OR be gated by an explicit kill switch defaulting to OFF.
- "60s per-coin shouldSendAlert" is NOT sufficient on its own — snapshot
  churn periods are minutes, not seconds.
- New code paths must increment a `healthServer` counter on suppression so
  /health remains the single forensic source of truth.
- Tests in `tests/regression_per_leg_kill_switch.test.js` are the canonical
  regression for this rule.

---

## R-PUBLIC-SIMPLIFY — brand-as-trader-attribution exception (5 may 2026)

**Strategic shift:** the bot is no longer fully anonymous. Conversion data
(11M tweet impressions, 0 conversions) showed users will not copy unknown
signals. The simplified `/start` UX (R-PUBLIC-SIMPLIFY) deliberately
attributes the trades to "Black Cat" so users see who they're copying.

**What is now allowed:**
- The exact strings `Black Cat — Pear Copy Trading` and
  `Black Cat — Live Performance` may appear in the `/start` hero message
  and in the simple:perf callback message.
- The 🐈‍⬛ emoji used as a brand mark in those titles.

**What stays forbidden:**
- "Black Cat" / "BCD" / "BlackCat" in any other context — commentary,
  thesis, operational chatter, alert footers, error messages, etc.
- Any reference to the persona behind the brand (real name, geographies,
  fund-internal terms — `tesis del fondo`, `el fondo`, `modus operandi`,
  `war trade`, etc. are still hard-banned).
- The full list of forbidden terms in `src/sanitizer.js` is unchanged
  except for the targeted allowlist patch.

**Permanent rule for future rounds:**
- New copy-trading UX surfaces may include the `Black Cat — <surface>`
  attribution where it directly drives a copy-trade decision (hero CTA,
  performance summary, signal-source label).
- Anything else that mentions "Black Cat" remains a sanitizer violation.
- The allowlist regex in `src/sanitizer.js` — `Black Cat — (Pear Copy
  Trading|Live Performance)` — is the source of truth. Adding new
  attribution surfaces requires extending the regex AND this section.
- Tests in `tests/sanitizer.test.js` + `tests/round_autocopy.test.js`
  guard the allowlist boundary.

---

## R-PUBLIC-V3-TRACKING — secondary tracking row + Pyrus copy-purge (5 may 2026)

**Pyrus removed from copy.** The Pyrus team has not delivered an updated
referral link as of 5 may 2026. Promoting a 20% rebate (10% Pear + 10%
Pyrus) when only Pear is verifiably live is misleading. Until Pyrus ships
a working referral, the rebate copy is **Pear-only**:

```
10% fee rebate via Pear (referral: BlackCatDeFi)
```

When Pyrus delivers, override `FUND_REBATE_LINE` env var without redeploy.
Do NOT re-introduce the Pyrus mention in source defaults until that link
is live.

**Secondary tracking row added to /start.** Two buttons sit BELOW the
hero / size / perf / alerts rows:

- `👁 TRACK MY OWN WALLET` → opens the existing `/track` flow via a
  callback submenu (Add / List / Remove). Reuses `walletTracker.js` —
  10-wallet limit per user preserved. Notifications from a user's own
  tracked wallets remain ON-by-default once they add at least one wallet.
- `🛡 MY HEALTH FACTOR` → portable read-only HyperLend HF reader. User
  pastes an address, bot replies with `HF + bucket + collateral + debt +
  LTV`. Fully off-by-default, no alerts unless the user separately
  /tracks the wallet.

**Permanent rules:**
- Secondary buttons must NEVER outrank the hero CTA. Always last in the
  keyboard, always a single row, always 2 buttons max.
- The HF reader is read-only. It must never write to alertsConfig or
  walletTracker without an explicit user action.
- Time-to-copy must remain <1.5s cold. The HF module is `require()`-d
  lazily (only on first HF callback) so the /start cold-boot path never
  pays the cost.
- Tests in `tests/tracking_secondary.test.js` + `tests/health_factor.test.js`
  guard this section.

