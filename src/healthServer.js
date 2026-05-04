'use strict';

/**
 * Round v2 — Health & status HTTP server + heartbeat tracker.
 *
 * /health  → 200 if last successful poll < 5 minutes ago, 503 otherwise
 * /status  → JSON with uptime, last_poll, errors_24h
 * /metrics → simple text metrics for scraping
 *
 * Default port HEALTH_PORT=8080. Railway maps this automatically.
 */

const http = require('http');

const _state = {
  startedAt: Date.now(),
  lastSuccessfulPoll: null,
  errors: [], // {timestamp, message}
  // R-PUBLIC-START-NUCLEAR — Telegram polling-specific telemetry so future
  // regressions are detectable in seconds via curl /health (instead of
  // grepping Railway logs by hand). Polling can crash silently or drop
  // updates without changing wallet-monitor pulse — these counters separate
  // the two failure modes.
  telegramUpdatesLifetime: 0,
  lastTelegramUpdateAt: null,
  lastStartCommandAt: null,
  lastStartCommandFromUserId: null,
  pollingStartedAt: null,
  registeredHandlers: [],
  bootDeployId: process.env.RAILWAY_DEPLOYMENT_ID || null,
  bootCommitSha:
    process.env.RAILWAY_GIT_COMMIT_SHA ||
    process.env.RAILWAY_GIT_COMMIT ||
    null,
  // R-PUBLIC-BASKET-SPAM-NUCLEAR (4 may 2026) — forensic counters for the
  // anti-spam architecture. Each emit-suppressing path (basketDedup hit,
  // 60s wallet debounce, isCloseEmittable refusal, monitor.js legacy close
  // drop) increments one of these. If `events_deduplicated_lifetime` stays
  // at 0 in a busy hour, the dedup wiring is broken. If
  // `phantom_events_suppressed_lifetime` stays at 0 across an HL margin
  // recompute, the phantom guard is broken.
  eventsDeduplicatedLifetime: 0,
  phantomEventsSuppressedLifetime: 0,
  lastDedupEventAt: null,
  lastDedupReason: null,
  lastPhantomSuppressedAt: null,
  lastPhantomReason: null,
  // R-PUBLIC-SPAM-FINAL (4 may 2026) — per-leg INDIVIDUAL_OPEN kill switch
  // counter. Increments every time openAlerts.emitAlerts refuses to dispatch
  // an INDIVIDUAL_OPEN because PER_LEG_ALERTS_DISABLED=true (default). One
  // increment per *leg* (so a 2-leg INDIVIDUAL_OPEN snapshot artifact yields
  // perLegAlertsBlockedLifetime += 2). If this counter grows while
  // events_deduplicated_lifetime stays flat, snapshot churn is happening.
  perLegAlertsBlockedLifetime: 0,
  lastPerLegBlockedAt: null,
  lastPerLegBlockedReason: null,
};

const ERRORS_BUFFER_MAX = 200;

function recordSuccessfulPoll() {
  _state.lastSuccessfulPoll = Date.now();
}

function recordError(err) {
  _state.errors.push({
    timestamp: Date.now(),
    message: err && err.message ? err.message : String(err),
  });
  if (_state.errors.length > ERRORS_BUFFER_MAX) {
    _state.errors.splice(0, _state.errors.length - ERRORS_BUFFER_MAX);
  }
}

// R-PUBLIC-START-NUCLEAR — instrumented from src/extensions.js bootstrap so
// the lifetime counter increments on every Telegram update the bot consumes.
// If pending_update_count > 0 on Telegram side AND lifetime stays flat → the
// bot is not polling.
function recordTelegramUpdate(msg) {
  _state.telegramUpdatesLifetime += 1;
  _state.lastTelegramUpdateAt = Date.now();
}

// Called from commandsStart.js whenever /start is consumed. Lets the bot
// prove end-to-end that the /start handler fires, not just that polling
// returns updates.
function recordStartCommand(userId) {
  _state.lastStartCommandAt = Date.now();
  if (userId != null) _state.lastStartCommandFromUserId = String(userId);
}

function recordPollingStarted() {
  _state.pollingStartedAt = Date.now();
}

function registerHandler(name) {
  if (!_state.registeredHandlers.includes(name)) {
    _state.registeredHandlers.push(name);
  }
}

// R-PUBLIC-BASKET-SPAM-NUCLEAR — invoked from src/basketDedup.js whenever
// `markAsAlerted` finds the SHA-256 hash already stored. The reason string
// is opaque (e.g. "basketDedup.hit:0xc7ae:abcd1234") so we keep just the
// last one for /health forensic spot-checks; the running total is what
// matters for regression dashboards.
function recordEventDeduplicated(reason) {
  _state.eventsDeduplicatedLifetime += 1;
  _state.lastDedupEventAt = Date.now();
  _state.lastDedupReason = reason ? String(reason).slice(0, 200) : null;
}

// R-PUBLIC-BASKET-SPAM-NUCLEAR — invoked from monitor.js (legacy close drop),
// from messageFormattersV2.isCloseEmittable refusals, and from openAlerts.js
// 60s wallet debounce. Reason strings stay namespaced so it's obvious which
// gate fired.
function recordPhantomSuppressed(reason) {
  _state.phantomEventsSuppressedLifetime += 1;
  _state.lastPhantomSuppressedAt = Date.now();
  _state.lastPhantomReason = reason ? String(reason).slice(0, 200) : null;
}

// R-PUBLIC-SPAM-FINAL — invoked from src/openAlerts.js whenever the
// INDIVIDUAL_OPEN path is refused by the PER_LEG_ALERTS_DISABLED kill
// switch. One increment per leg refused.
function recordPerLegBlocked(reason) {
  _state.perLegAlertsBlockedLifetime += 1;
  _state.lastPerLegBlockedAt = Date.now();
  _state.lastPerLegBlockedReason = reason ? String(reason).slice(0, 200) : null;
}

function _formatDurationMs(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '0m';
  const days = Math.floor(ms / (24 * 60 * 60 * 1000));
  const hours = Math.floor((ms % (24 * 60 * 60 * 1000)) / (60 * 60 * 1000));
  const minutes = Math.floor((ms % (60 * 60 * 1000)) / (60 * 1000));
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

// R-PUBLIC-BASKET-UNIFY — tap walletBasketLockout for /health visibility.
// We require lazily so a missing/broken module doesn't take healthServer
// down with it.
function _lockoutSnapshot() {
  try {
    const lockout = require('./walletBasketLockout');
    return lockout.snapshot({ verbose: false });
  } catch (e) {
    return { error: e && e.message ? e.message : String(e) };
  }
}

function getStatus() {
  const now = Date.now();
  const errors24h = _state.errors.filter(
    (e) => now - e.timestamp < 24 * 60 * 60 * 1000
  );
  return {
    uptime_ms: now - _state.startedAt,
    uptime_human: _formatDurationMs(now - _state.startedAt),
    started_at: new Date(_state.startedAt).toISOString(),
    last_successful_poll: _state.lastSuccessfulPoll
      ? new Date(_state.lastSuccessfulPoll).toISOString()
      : null,
    last_poll_age_ms: _state.lastSuccessfulPoll
      ? now - _state.lastSuccessfulPoll
      : null,
    errors_24h_count: errors24h.length,
    errors_24h_recent: errors24h.slice(-10),
    // R-PUBLIC-START-NUCLEAR — Telegram-specific health
    telegram: {
      polling_started_at: _state.pollingStartedAt
        ? new Date(_state.pollingStartedAt).toISOString()
        : null,
      updates_lifetime: _state.telegramUpdatesLifetime,
      last_update_at: _state.lastTelegramUpdateAt
        ? new Date(_state.lastTelegramUpdateAt).toISOString()
        : null,
      last_update_age_ms: _state.lastTelegramUpdateAt
        ? now - _state.lastTelegramUpdateAt
        : null,
      last_start_command_at: _state.lastStartCommandAt
        ? new Date(_state.lastStartCommandAt).toISOString()
        : null,
      last_start_command_from_user_id: _state.lastStartCommandFromUserId,
      registered_handlers: _state.registeredHandlers.slice(),
      handlers_count: _state.registeredHandlers.length,
    },
    deploy: {
      deploy_id: _state.bootDeployId,
      commit_sha: _state.bootCommitSha,
    },
    // R-PUBLIC-BASKET-SPAM-NUCLEAR — dedup + phantom-suppression telemetry.
    // Read these to confirm the anti-spam wiring is live in prod after
    // deploy. Both should grow under any meaningful BCD trading load.
    spam_guard: {
      events_deduplicated_lifetime: _state.eventsDeduplicatedLifetime,
      phantom_events_suppressed_lifetime: _state.phantomEventsSuppressedLifetime,
      last_dedup_at: _state.lastDedupEventAt
        ? new Date(_state.lastDedupEventAt).toISOString()
        : null,
      last_dedup_reason: _state.lastDedupReason,
      last_phantom_suppressed_at: _state.lastPhantomSuppressedAt
        ? new Date(_state.lastPhantomSuppressedAt).toISOString()
        : null,
      last_phantom_reason: _state.lastPhantomReason,
      // R-PUBLIC-BASKET-UNIFY — Gate-0 wallet-level absolute lockout.
      // open_count = wallets currently holding an OPEN basket (cannot
      // emit another BASKET_OPEN until they close). open_wallets is
      // truncated to 10 to keep /health small.
      wallet_lockout: _lockoutSnapshot(),
      // R-PUBLIC-SPAM-FINAL — per-leg INDIVIDUAL_OPEN kill switch. If
      // per_leg_alerts_blocked_lifetime grows while
      // events_deduplicated_lifetime stays flat, the snapshot diff is
      // churning (legs flickering in/out of allPositions across polls).
      per_leg_alerts_blocked_lifetime: _state.perLegAlertsBlockedLifetime,
      last_per_leg_blocked_at: _state.lastPerLegBlockedAt
        ? new Date(_state.lastPerLegBlockedAt).toISOString()
        : null,
      last_per_leg_blocked_reason: _state.lastPerLegBlockedReason,
    },
  };
}

function isHealthy() {
  if (!_state.lastSuccessfulPoll) return false;
  return Date.now() - _state.lastSuccessfulPoll < 5 * 60 * 1000;
}

function start(port) {
  const PORT = port || parseInt(process.env.HEALTH_PORT || '8080', 10);
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      const ok = isHealthy();
      res.writeHead(ok ? 200 : 503, {
        'Content-Type': 'application/json',
      });
      res.end(
        JSON.stringify({
          status: ok ? 'healthy' : 'unhealthy',
          ...getStatus(),
        })
      );
      return;
    }
    if (req.url === '/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(getStatus(), null, 2));
      return;
    }
    if (req.url === '/metrics') {
      const s = getStatus();
      const lines = [
        `# HELP pear_alerts_uptime_seconds Bot uptime in seconds`,
        `# TYPE pear_alerts_uptime_seconds counter`,
        `pear_alerts_uptime_seconds ${Math.floor(s.uptime_ms / 1000)}`,
        `# HELP pear_alerts_errors_24h Errors in last 24 hours`,
        `# TYPE pear_alerts_errors_24h gauge`,
        `pear_alerts_errors_24h ${s.errors_24h_count}`,
        `# HELP pear_alerts_last_poll_age_seconds Age of last successful poll`,
        `# TYPE pear_alerts_last_poll_age_seconds gauge`,
        `pear_alerts_last_poll_age_seconds ${s.last_poll_age_ms ? Math.floor(s.last_poll_age_ms / 1000) : -1}`,
        // R-PUBLIC-BASKET-SPAM-NUCLEAR
        `# HELP pear_alerts_events_deduplicated_lifetime Basket-open emits suppressed by SHA-256 dedup since boot`,
        `# TYPE pear_alerts_events_deduplicated_lifetime counter`,
        `pear_alerts_events_deduplicated_lifetime ${s.spam_guard.events_deduplicated_lifetime}`,
        `# HELP pear_alerts_phantom_events_suppressed_lifetime Phantom $0/$0 events refused by isCloseEmittable + monitor.js + 60s wallet debounce`,
        `# TYPE pear_alerts_phantom_events_suppressed_lifetime counter`,
        `pear_alerts_phantom_events_suppressed_lifetime ${s.spam_guard.phantom_events_suppressed_lifetime}`,
        // R-PUBLIC-SPAM-FINAL
        `# HELP pear_alerts_per_leg_alerts_blocked_lifetime INDIVIDUAL_OPEN legs refused by PER_LEG_ALERTS_DISABLED kill switch`,
        `# TYPE pear_alerts_per_leg_alerts_blocked_lifetime counter`,
        `pear_alerts_per_leg_alerts_blocked_lifetime ${s.spam_guard.per_leg_alerts_blocked_lifetime}`,
      ];
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end(lines.join('\n'));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  server.listen(PORT, () => {
    console.log(`[healthServer] listening on :${PORT}`);
  });
  return server;
}

function _resetForTests() {
  _state.startedAt = Date.now();
  _state.lastSuccessfulPoll = null;
  _state.errors.length = 0;
  _state.telegramUpdatesLifetime = 0;
  _state.lastTelegramUpdateAt = null;
  _state.lastStartCommandAt = null;
  _state.lastStartCommandFromUserId = null;
  _state.pollingStartedAt = null;
  _state.registeredHandlers.length = 0;
  _state.eventsDeduplicatedLifetime = 0;
  _state.phantomEventsSuppressedLifetime = 0;
  _state.lastDedupEventAt = null;
  _state.lastDedupReason = null;
  _state.lastPhantomSuppressedAt = null;
  _state.lastPhantomReason = null;
  _state.perLegAlertsBlockedLifetime = 0;
  _state.lastPerLegBlockedAt = null;
  _state.lastPerLegBlockedReason = null;
}

module.exports = {
  start,
  recordSuccessfulPoll,
  recordError,
  recordTelegramUpdate,
  recordStartCommand,
  recordPollingStarted,
  registerHandler,
  recordEventDeduplicated,
  recordPhantomSuppressed,
  recordPerLegBlocked,
  getStatus,
  isHealthy,
  _resetForTests,
};
