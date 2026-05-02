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
    `Your current TZ: \`${currentTz}\``,
    '',
    'To change:',
    '  `/timezone <IANA>` (e.g. `/timezone America/New_York`)',
    '  `/timezone auto` — detect from Telegram',
    '',
    '*Popular:*',
  ];
  for (const tz of POPULAR_TZS) lines.push(`  • \`${tz}\``);
  lines.push('');
  lines.push(
    'Full IANA list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones'
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
          `⚠️ Could not set detected TZ (${detected}): ${e.message}`
        );
        return;
      }
      await bot.sendMessage(
        chatId,
        `✅ TZ detected: \`${detected}\`\n` +
          `_(based on language_code=${lang || 'unknown'})_\n\n` +
          `If wrong, use \`/timezone <IANA>\` to fix it.`,
        { parse_mode: 'Markdown' }
      );
      return;
    }

    try {
      tzMgr.setUserTz(userId, arg);
      const sample = tzMgr.formatLocalTime(userId);
      await bot.sendMessage(
        chatId,
        `✅ TZ updated: \`${arg}\`\n\nExample: ${sample}`,
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      await bot.sendMessage(
        chatId,
        `⚠️ ${e.message || 'Invalid timezone'}.\n\n` +
          `Use an IANA name like \`America/New_York\`.`,
        { parse_mode: 'Markdown' }
      );
    }
  });

  console.log('[commandsTimezone] attached: /timezone');
}

module.exports = { attach, POPULAR_TZS };
