'use strict';

/**
 * R-PUBLIC — /timezone command.
 *
 *   /timezone               → muestra TZ actual + lista de opciones populares
 *   /timezone <IANA>        → setea TZ
 *   /timezone auto          → intenta detectar via Telegram language_code
 */

const tzMgr = require('./timezoneManager');

const POPULAR_TZS = [
  'America/Argentina/Buenos_Aires',
  'America/Mexico_City',
  'America/Santiago',
  'America/Bogota',
  'America/Sao_Paulo',
  'America/New_York',
  'Europe/Madrid',
  'Europe/London',
  'Asia/Tokyo',
  'UTC',
];

function _renderHelp(currentTz) {
  const lines = [
    '🌐 *TIMEZONE*',
    '',
    `Tu TZ actual: \`${currentTz}\``,
    '',
    'Para cambiar:',
    '  `/timezone <IANA>` (ej. `/timezone America/Argentina/Buenos_Aires`)',
    '  `/timezone auto` — detectar desde Telegram',
    '',
    '*Populares:*',
  ];
  for (const tz of POPULAR_TZS) lines.push(`  • \`${tz}\``);
  lines.push('');
  lines.push(
    'Lista completa de IANA: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones'
  );
  return lines.join('\n');
}

function attach(bot) {
  bot.onText(/^\/timezone(?:\s+(.+))?$/i, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from && msg.from.id ? msg.from.id : chatId;
    const arg = (match[1] || '').trim();
    const currentTz = tzMgr.getUserTz(userId);

    if (!arg) {
      await bot.sendMessage(chatId, _renderHelp(currentTz), {
        parse_mode: 'Markdown',
        disable_web_page_preview: true,
      });
      return;
    }

    if (arg.toLowerCase() === 'auto') {
      const lang = msg.from && msg.from.language_code;
      const detected = tzMgr.detectFromLangCode(lang);
      try {
        tzMgr.setUserTz(userId, detected);
      } catch (e) {
        await bot.sendMessage(
          chatId,
          `⚠️ No pude setear TZ detectada (${detected}): ${e.message}`
        );
        return;
      }
      await bot.sendMessage(
        chatId,
        `✅ TZ detectada: \`${detected}\`\n` +
          `_(basado en language_code=${lang || 'unknown'})_\n\n` +
          `Si está mal, usá \`/timezone <IANA>\` para corregir.`,
        { parse_mode: 'Markdown' }
      );
      return;
    }

    try {
      tzMgr.setUserTz(userId, arg);
      const sample = tzMgr.formatLocalTime(userId);
      await bot.sendMessage(
        chatId,
        `✅ TZ actualizada: \`${arg}\`\n\nEjemplo: ${sample}`,
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      await bot.sendMessage(
        chatId,
        `⚠️ ${e.message || 'Timezone inválida'}.\n\n` +
          `Usá un IANA name como \`America/Argentina/Buenos_Aires\`.`,
        { parse_mode: 'Markdown' }
      );
    }
  });

  console.log('[commandsTimezone] attached: /timezone');
}

module.exports = { attach, POPULAR_TZS };
