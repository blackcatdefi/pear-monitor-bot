'use strict';

/**
 * Round v2 — extra Telegram bot commands.
 *
 * Commands registered by attachCommands(bot):
 *   /history [N]        — last N closes (default 10)
 *   /pnl [period]       — PnL by period (today/week/month/ytd/all)
 *   /status             — bot health (uptime, last poll, errors)
 *   /export [period]    — CSV export of closes
 *   /summary            — force trigger weekly summary
 *
 * All commands respect the existing wallet ownership: a chat's history
 * only includes events whose chatId matches.
 */

const eventLog = require('./eventLog');
const healthServer = require('./healthServer');
const weeklySummary = require('./weeklySummary');

function isEnabled() {
  return (
    (process.env.HISTORY_COMMANDS_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0.00';
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

function _short(d) {
  if (!d) return '?';
  return String(d).replace('T', ' ').slice(0, 16);
}

function _formatHours(ms) {
  return (ms / (60 * 60 * 1000)).toFixed(1);
}

function attachCommands(bot, opts = {}) {
  if (!isEnabled()) {
    console.log('[commands] disabled');
    return;
  }
  const exportEnabled =
    (process.env.EXPORT_ENABLED || 'true').toLowerCase() !== 'false';

  // /history [N]
  bot.onText(/^\/history(?:\s+(\d+))?$/, async (msg, match) => {
    const chatId = msg.chat.id;
    const limit = parseInt(match[1] || '10', 10);
    const closes = eventLog.recentCloses(limit, {
      chatId: String(chatId),
    });
    if (closes.length === 0) {
      await bot.sendMessage(chatId, 'Sin cierres registrados todavía.');
      return;
    }
    const lines = [`📜 *ÚLTIMOS ${closes.length} CIERRES*`, ''];
    for (const c of closes) {
      const e = (c.pnl || 0) >= 0 ? '🟢' : '🔴';
      const side = c.side ? ` ${c.side}` : '';
      lines.push(
        `${e} ${c.coin}${side}: ${_fmtUsd(c.pnl || 0)} _(${_short(c.timestamp)})_`
      );
    }
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
  });

  // /pnl [period]
  bot.onText(/^\/pnl(?:\s+(\w+))?$/, async (msg, match) => {
    const chatId = msg.chat.id;
    const period = (match[1] || 'today').toLowerCase();
    const valid = ['today', 'week', 'month', 'ytd', 'all'];
    if (!valid.includes(period)) {
      await bot.sendMessage(
        chatId,
        `Período inválido. Usá: ${valid.join(' / ')}`
      );
      return;
    }
    const closes = eventLog.closesByPeriod(period);
    const s = eventLog.summarize(closes);
    const lines = [
      `💰 *PnL ${period.toUpperCase()}*`,
      '',
      `Total: ${_fmtUsd(s.total_pnl)}`,
      `Trades: ${s.count} (${s.wins}W / ${s.losses}L)`,
      `Win Rate: ${s.win_rate_pct.toFixed(1)}%`,
    ];
    if (s.best && Number.isFinite(s.best.pnl)) {
      lines.push(
        `Best: ${s.best.coin} ${_fmtUsd(s.best.pnl)}`
      );
    }
    if (s.worst && Number.isFinite(s.worst.pnl) && s.worst !== s.best) {
      lines.push(
        `Worst: ${s.worst.coin} ${_fmtUsd(s.worst.pnl)}`
      );
    }
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
  });

  // /status
  bot.onText(/^\/status$/, async (msg) => {
    const chatId = msg.chat.id;
    const s = healthServer.getStatus();
    const lines = [
      `✅ *Bot status*`,
      '',
      `Uptime: ${_formatHours(s.uptime_ms)}h`,
      `Último poll: ${_short(s.last_successful_poll)}`,
      `Errores 24h: ${s.errors_24h_count}`,
    ];
    if (s.errors_24h_count > 0 && Array.isArray(s.errors_24h_recent)) {
      const last = s.errors_24h_recent[s.errors_24h_recent.length - 1];
      if (last) {
        lines.push('', `Último error: ${(last.message || '').slice(0, 120)}`);
      }
    }
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
  });

  // /export [period]
  if (exportEnabled) {
    bot.onText(/^\/export(?:\s+(\w+))?$/, async (msg, match) => {
      const chatId = msg.chat.id;
      const period = (match[1] || 'all').toLowerCase();
      const closes = eventLog.closesByPeriod(period);
      if (closes.length === 0) {
        await bot.sendMessage(
          chatId,
          `Sin eventos en período ${period}.`
        );
        return;
      }
      const header = [
        'timestamp',
        'wallet',
        'coin',
        'side',
        'entry_px',
        'exit_px',
        'size',
        'pnl_usd',
        'fees_usd',
        'reason',
        'duration_hours',
      ].join(',');
      const rows = closes.map((c) => {
        const opened = c.openedAt ? Date.parse(c.openedAt) : 0;
        const closed = Date.parse(c.timestamp || '') || 0;
        const dur = opened && closed ? (closed - opened) / 3600000 : 0;
        return [
          c.timestamp || '',
          c.wallet || '',
          c.coin || '',
          c.side || '',
          c.entryPrice ?? '',
          c.exitPrice ?? '',
          c.size ?? '',
          (c.pnl ?? 0).toFixed ? c.pnl.toFixed(2) : c.pnl,
          (c.fees ?? 0).toFixed ? c.fees.toFixed(2) : c.fees,
          c.reason || 'UNKNOWN',
          dur.toFixed(2),
        ]
          .map((v) =>
            String(v).includes(',') ? `"${String(v).replace(/"/g, '""')}"` : v
          )
          .join(',');
      });
      const csv = [header, ...rows].join('\n');
      const buffer = Buffer.from(csv, 'utf-8');
      const filename = `pear_alerts_${period}_${Date.now()}.csv`;
      try {
        await bot.sendDocument(
          chatId,
          buffer,
          {
            caption: `Export ${period}: ${closes.length} cierres`,
          },
          {
            filename,
            contentType: 'text/csv',
          }
        );
      } catch (e) {
        console.error(
          '[commands] export send failed:',
          e && e.message ? e.message : e
        );
        await bot.sendMessage(
          chatId,
          `⚠️ No pude generar el CSV: ${e && e.message ? e.message : 'error'}`
        );
      }
    });
  }

  // /summary
  bot.onText(/^\/summary$/, async (msg) => {
    const chatId = msg.chat.id;
    await bot.sendMessage(chatId, 'Forzando weekly summary...');
    const text = weeklySummary.buildSummaryMessage();
    if (!text) {
      await bot.sendMessage(
        chatId,
        'Sin cierres en la semana actual. Volvé el domingo 18:00 UTC.'
      );
      return;
    }
    await bot.sendMessage(chatId, text, { parse_mode: 'Markdown' });
  });

  // /healthcheck (alias)
  bot.onText(/^\/healthcheck$/, async (msg) => {
    const chatId = msg.chat.id;
    const ok = healthServer.isHealthy();
    await bot.sendMessage(
      chatId,
      ok ? '✅ healthy' : '⚠️ unhealthy — revisar /status'
    );
  });

  console.log(
    '[commands] attached: /history /pnl /status /export /summary /healthcheck'
  );
}

module.exports = {
  isEnabled,
  attachCommands,
};
