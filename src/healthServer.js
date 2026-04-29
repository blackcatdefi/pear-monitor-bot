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
}

module.exports = {
  start,
  recordSuccessfulPoll,
  recordError,
  getStatus,
  isHealthy,
  _resetForTests,
};
