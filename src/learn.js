'use strict';

/**
 * R-AUTOCOPY — Tutorials. Pure data + a pagination helper.
 *
 * Each lesson is a Markdown body. Pagination buttons are wired by
 * commandsLearn.js — this module is content-only so tests don't need
 * a bot instance.
 *
 * R-EN — Migrated to English. Lesson titles & bodies are now native English
 * (not translated Spanish). Tone: concise, action-oriented, trader vernacular.
 */

const LESSONS = [
  {
    id: 1,
    title: '📘 How to track a wallet (30s)',
    body: [
      '*How to track a wallet*',
      '',
      '1️⃣ Tap /track or the *🎯 Track wallet* button on /start.',
      '2️⃣ Paste the wallet address (format `0x...` with 40 hex chars).',
      '3️⃣ Optional: give it an alias (e.g. "Whale 1") or tap /skip.',
      '4️⃣ Done — you\'ll get alerts when that wallet opens/closes baskets.',
      '',
      '_You can track up to 10 wallets (25 with Premium when you hit 3 referrals)._',
    ].join('\n'),
  },
  {
    id: 2,
    title: '📗 How to set up copy auto (1min)',
    body: [
      '*How to set up copy auto*',
      '',
      '1️⃣ Tap /copy_auto.',
      '2️⃣ Set your *capital per signal* with `/capital 500` (between $10 and $50K).',
      '3️⃣ Pick a mode:',
      '  • *MANUAL* — alert with a "Copy on Pear" button',
      '  • *AUTO* — pre-armed alert ready for a 1-tap signature',
      '4️⃣ Toggle *🚦 ON / OFF*.',
      '',
      'When a signal hits @BlackCatDeFiSignals, you get the direct Pear link with your capital pre-loaded and the exact basket.',
      '',
      '⚠️ *You always sign from your wallet* — Pear has no public execution API. That\'s the only legit way.',
    ].join('\n'),
  },
  {
    id: 3,
    title: '📕 What is a basket on Pear (2min)',
    body: [
      '*What is a basket*',
      '',
      'A basket on Pear is a set of same-side positions (all SHORT or all LONG) across several tokens of the same "type" (e.g. memecoins, L2s, AI, etc.).',
      '',
      '*Edge over standalone trades:*',
      '  • Intra-theme diversification',
      '  • Reduces idiosyncratic risk of a single token',
      '  • Stops and trailing apply to the whole basket',
      '',
      '*Example:* L2s SHORT basket = SHORT on ARB+OP+DYDX+PYTH+ENA. If the narrative breaks, all drop together → bigger upside; if not, your SL protects you.',
    ].join('\n'),
  },
  {
    id: 4,
    title: '📙 Risk management 101 (3min)',
    body: [
      '*Risk management 101*',
      '',
      '🎯 *SL (Stop Loss)*: max % you\'re willing to lose. Default 50% of basket capital.',
      '',
      '📈 *Trailing Stop*: SL that moves in your favor when you\'re in profit.',
      '  • *Activation*: profit % at which trailing kicks in (default 30%).',
      '  • *Distance*: how much room before closing (default 10%).',
      '',
      '⚡ *Leverage*: multiplies gain and loss equally. Default 4x = you win/lose 4x the price move.',
      '',
      '⏱️ *TWAP* (Time-Weighted Avg Price): split your entry into N "bullets" over X hours → better average price than a single market order.',
      '',
      '_Rule: never risk more than 1-2% of your total capital on a single trade._',
    ].join('\n'),
  },
  {
    id: 5,
    title: '📒 How to read official signals (1min)',
    body: [
      '*How to read official signals*',
      '',
      'When @BlackCatDeFiSignals posts a signal, you\'ll see:',
      '',
      '🚀 *OFFICIAL SIGNAL #N*',
      '📊 *Basket*: list of tokens with their side (LONG/SHORT)',
      '⚡ *Leverage*: how many x to apply',
      '🎯 *SL / Trailing*: risk config',
      '⏱️ *TWAP*: how to enter (hours + bullets)',
      '',
      'If copy auto is ON, you get the personalized signal with your capital and a one-tap Pear button.',
      '',
      'If not, tap /signals to subscribe to the channel manually.',
    ].join('\n'),
  },
];

function getLessonCount() {
  return LESSONS.length;
}

function getLesson(idx) {
  if (idx < 0 || idx >= LESSONS.length) return null;
  return LESSONS[idx];
}

function getAllTitles() {
  return LESSONS.map((l, i) => ({ idx: i, id: l.id, title: l.title }));
}

function buildKeyboard(idx) {
  const total = LESSONS.length;
  const row = [];
  if (idx > 0) row.push({ text: '◀️ Previous', callback_data: `learn:nav:${idx - 1}` });
  if (idx < total - 1) row.push({ text: 'Next ▶️', callback_data: `learn:nav:${idx + 1}` });
  const rows = [];
  if (row.length > 0) rows.push(row);
  rows.push([{ text: '✖️ Exit', callback_data: 'learn:exit' }]);
  return { inline_keyboard: rows };
}

function formatLesson(idx) {
  const l = getLesson(idx);
  if (!l) return '⚠️ Lesson not found.';
  return `${l.body}\n\n_Lesson ${idx + 1} of ${LESSONS.length}_`;
}

function formatIndex() {
  const lines = [
    '📚 *Learn the bot*',
    '',
    'Short tutorials — tap one to start:',
    '',
  ];
  LESSONS.forEach((l, idx) => {
    lines.push(`${idx + 1}. ${l.title}`);
  });
  return lines.join('\n');
}

function buildIndexKeyboard() {
  const rows = LESSONS.map((l, idx) => [
    { text: l.title, callback_data: `learn:nav:${idx}` },
  ]);
  return { inline_keyboard: rows };
}

module.exports = {
  LESSONS,
  getLessonCount,
  getLesson,
  getAllTitles,
  buildKeyboard,
  formatLesson,
  formatIndex,
  buildIndexKeyboard,
};
