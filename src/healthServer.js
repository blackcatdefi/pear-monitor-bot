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

function _formatDurationMs(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '0m';
  const days = Math.floor(ms / (24 * 60 * 60 * 1000));
  const hours = Math.floor((ms % (24 * 60 * 60 * 1000)) / (60 * 60 * 1000));
  const minutes = Math.floor((ms % (60 * 60 * 1000)) / (60 * 1000));
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
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
}

module.exports = {
  start,
  recordSuccessfulPoll,
  recordError,
  recordTelegramUpdate,
  recordStartCommand,
  recordPollingStarted,
  registerHandler,
  getStatus,
  isHealthy,
  _resetForTests,
};
