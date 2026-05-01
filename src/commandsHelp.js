'use strict';

/**
 * R-AUTOCOPY — /help command (consolidated reference).
 */

function _body() {
  return [
    '🆘 *Comandos*',
    '',
    '/start — Inicio',
    '/track — Trackear wallets externas',
    '/signals — Canal oficial de signals',
    '/copy_auto — Copy automático (MANUAL/AUTO)',
    '/capital — Capital por signal',
    '/timezone — Zona horaria',
    '/portfolio — Tu portfolio (read-only HL)',
    '/leaderboard — Top wallets trackeadas',
    '/alerts_config — Granularidad de alertas',
    '/stats — Tus stats personales',
    '/share — Invitar amigos (Premium tras 3 refs)',
    '/learn — Tutoriales (5 lessons)',
    '/feedback — Soporte / sugerencias',
    '',
    '¿Algo no anda? /feedback',
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
