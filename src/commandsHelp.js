'use strict';

/**
 * R-AUTOCOPY — /help command (consolidated reference).
 */

function _body() {
  return [
    '🆘 *Commands*',
    '',
    '/start — Welcome',
    '/track — Track external wallets',
    '/signals — Official signals channel',
    '/copy_auto — Auto copy (MANUAL/AUTO)',
    '/capital — Capital per signal',
    '/timezone — Timezone',
    '/portfolio — Your portfolio (read-only HL)',
    '/leaderboard — Top tracked wallets',
    '/alerts_config — Alert granularity',
    '/stats — Your personal stats',
    '/share — Invite friends (Premium after 3 refs)',
    '/learn — Tutorials (5 lessons)',
    '/feedback — Support / suggestions',
    '',
    'Something off? /feedback',
  ].join('\n');
}

function attach(bot) {
  bot.onText(/^\/help(?:@\w+)?$/i, async (msg) => {
    try {
      await bot.sendMessage(msg.chat.id, _body(), { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[commandsHelp] failed:', e && e.message ? e.message : e);
    }
  });
  console.log('[commandsHelp] attached: /help');
}

module.exports = { attach, _body };
