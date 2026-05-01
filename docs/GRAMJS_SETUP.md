# GramJS MTProto Fallback — Setup & Regeneration Guide

**Round:** R-GRAMJS (1 may 2026)
**Service:** `gentle-luck` / pear-monitor-bot (Node)
**Module:** `src/gramjsBackend.js` + `src/signalsChannelScraper.js` switching layer

## What this is

The public-channel signals pipeline reads new `@BlackCatDeFiSignals` posts from Telegram's public preview at `https://t.me/s/BlackCatDeFiSignals`. That endpoint is unauthenticated and works most of the time, but it can be rate-limited or blocked by Telegram for any reason at any time. When that happens, the bot would go silent.

R-GRAMJS adds a second backend: `gramjs`, which talks directly to Telegram MTProto via the user-account API. After 10 consecutive scraper failures the scheduler switches to gramjs automatically (if configured). After 3 consecutive scraper-probe successes it switches back. No manual intervention.

The gramjs fallback only activates if all three of these env vars are populated on the service:

| Env var | Where from |
|---|---|
| `TELEGRAM_API_ID` | https://my.telegram.org/apps (after creating an app) |
| `TELEGRAM_API_HASH` | https://my.telegram.org/apps (same page) |
| `TELEGRAM_SESSION_STRING` | Output of `scripts/generate_telegram_session.js` (run locally) |

If any of these are empty or `TELEGRAM_SESSION_STRING=PENDING_BCD_SETUP`, the bot continues running on the scraper alone — no errors, just no fallback if the scraper goes down.

## One-time setup (5 minutes)

### Step 1 — Get API credentials

Visit **https://my.telegram.org/apps** in a browser and log in with the phone number that owns `@BlackCatDeFiSignals` (the bot reads messages as that account, not as a bot).

Telegram will deliver a one-time code through the official Telegram app (NOT SMS). Enter the code on the website.

Once logged in, click "Create application":

- **App title:** `PearProtocolAlertsBot`
- **Short name:** `pearmonbot` (or anything 5–32 chars)
- **Platform:** Other
- **Description:** anything

Submit. Copy the resulting **api_id** (integer) and **api_hash** (long hex string).

### Step 2 — Generate the session string locally

On any machine with Node 18+ and outbound internet (your laptop is fine):

```bash
git clone https://github.com/blackcatdefi/pear-monitor-bot.git
cd pear-monitor-bot
npm install --no-save telegram input

TELEGRAM_API_ID=<id from step 1> \
TELEGRAM_API_HASH=<hash from step 1> \
node scripts/generate_telegram_session.js
```

The script will prompt for:

1. **Phone number** — same as in step 1 (e.g. `+5491155555555`)
2. **2FA password** — blank if 2FA is off; otherwise your cloud password
3. **Login code** — Telegram sends a code to the official app; type it in

On success the script prints a long base64-ish string. That's the session string. Copy it (one line).

### Step 3 — Set the three env vars in Railway

On the `gentle-luck` service (project `BlackCatDeFiPlugin Bot Railway`):

```
TELEGRAM_API_ID=<integer from step 1>
TELEGRAM_API_HASH=<hash from step 1>
TELEGRAM_SESSION_STRING=<long string from step 2>
```

Railway redeploys automatically on env-var change. Verify with:

```bash
# In Railway logs, look for one of:
#   [gramjsBackend] connected to Telegram MTProto
# (only appears if/when fallback kicks in)
#
# Or manually trigger the status check:
#   /scraper_status   ← currently shows scraper schedule + backend state
```

## Verifying the fallback actually works

The fallback is dormant by design: it only activates after 10 consecutive scraper failures. To force-test it without waiting for a real outage:

1. **In Railway, temporarily change** `SIGNALS_SCRAPER_URL` to a deliberately-broken URL, e.g. `https://t.me/s/this-channel-does-not-exist-xyz123`.
2. **Wait ~5 min** (10 polls × 30s default interval).
3. **Watch logs.** You should see, in order:
   ```
   [signalsChannelScraper] poll failed via scraper (#3/10): …
   [signalsChannelScraper] poll failed via scraper (#10/10): …
   [signalsChannelScraper] backend switch: scraper → gramjs (10 consecutive scraper failures)
   [gramjsBackend] connected to Telegram MTProto
   ```
4. **Check Telegram** — you should receive the owner alert "🚨 SIGNALS SCRAPER DOWN".
5. **Restore** `SIGNALS_SCRAPER_URL` to its original value (delete the override; the default is `https://t.me/s/BlackCatDeFiSignals`).
6. **Watch logs.** After 3 successful scraper probes:
   ```
   [signalsChannelScraper] scraper probe ok (#1/3) while gramjs primary
   [signalsChannelScraper] scraper probe ok (#2/3) while gramjs primary
   [signalsChannelScraper] scraper probe ok (#3/3) while gramjs primary
   [signalsChannelScraper] backend switch: gramjs → scraper (probe success x3)
   ```
7. **Telegram** — owner alert "✅ SIGNALS SCRAPER RECOVERED".

## Regenerating the session string

The session string is long-lived (Telegram doesn't auto-expire it), but it can be invalidated by:

- Logging out of the account from another device
- Telegram's own security system marking the session suspicious
- Changing the 2FA password

If the bot logs `[gramjsBackend] connect failed: AUTH_KEY_…` repeatedly, regenerate:

1. Repeat **Step 2** above (you don't need to redo Step 1 — `api_id` and `api_hash` are reusable).
2. Update `TELEGRAM_SESSION_STRING` in Railway to the new value.
3. The next poll will reconnect.

## Security notes

- **Never commit** `TELEGRAM_SESSION_STRING`, `TELEGRAM_API_HASH`, or any value derived from them to the repo. The repo is checked with `git grep` on every CI run for known secret patterns.
- **Never echo** the session string into chat, logs, or screenshots. The generator script prints it once to stdout — redirect to a temp file only if necessary, then `rm -P` after copying.
- The session string grants full account access (read DMs, post messages, change settings). Treat it like a password.
- The bot only ever calls `client.getMessages('@BlackCatDeFiSignals', { limit: 20 })` — read-only and channel-scoped. It does not subscribe, post, or modify anything.

## Module reference

- `src/gramjsBackend.js` — the MTProto client wrapper. Lazy-loads the `telegram` package so unit tests don't need it installed. Exports `isAvailable()`, `fetchRecentMessages()`, `disconnect()`, `statusLines()`.
- `src/signalsChannelScraper.js` — backend state machine. Holds `_backend` ('scraper' | 'gramjs') and routes `pollOnce()` accordingly. After 10 scraper failures → switch to gramjs (if available). After 3 scraper probes succeed in gramjs mode → switch back.
- `scripts/generate_telegram_session.js` — local-only CLI for generating the session string.
- `tests/gramjsBackend.test.js` + `tests/scraperBackendSwitch.test.js` — unit coverage of normalization, env handling, and the state machine (no live network).

## Env vars summary

| Var | Default | Purpose |
|---|---|---|
| `TELEGRAM_API_ID` | (empty) | from my.telegram.org/apps |
| `TELEGRAM_API_HASH` | (empty) | from my.telegram.org/apps |
| `TELEGRAM_SESSION_STRING` | (empty) | from generator script |
| `BCD_SIGNALS_CHANNEL` | `BlackCatDeFiSignals` | channel username (no @) |
| `GRAMJS_PROBE_OK_THRESHOLD` | `3` | scraper probe successes to switch back |
| `SCRAPER_FAILURES_HARD_ALERT` | `10` | scraper failures to switch to gramjs |

If you only set the placeholder `TELEGRAM_SESSION_STRING=PENDING_BCD_SETUP`, the bot logs that the fallback is unconfigured and continues on the scraper alone. No errors. The fallback simply won't activate until you complete steps 1–3 above.
