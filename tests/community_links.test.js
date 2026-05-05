'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — community-links row regression suite.
 *
 * The simplified /start keyboard renders, as its LAST row, two URL-only
 * buttons that point to the public Telegram channels:
 *   • 📡 Signals Channel  → t.me/BlackCatDeFiSignals
 *   • 📚 Thesis Channel   → t.me/BlackCatDeFiThesis
 *
 * These are *informational* — the bot does NOT scrape them; they are only
 * here so a user reading the menu can dive deeper into Black Cat's voice.
 *
 * Asserts:
 *   1. The last row of the simplified /start keyboard is exactly the
 *      two community-link URL buttons (both have .url, neither has
 *      .callback_data).
 *   2. The default URLs point to t.me/BlackCatDeFiSignals + t.me/BlackCatDeFiThesis.
 *   3. SIGNALS_CHANNEL_URL / THESIS_CHANNEL_URL env vars override the
 *      defaults (deferred via module reload).
 *   4. Sanitizer's allowlist permits "Signals Channel" + "Thesis Channel"
 *      labels and the t.me/BlackCatDeFi* URLs (no FORBIDDEN_TERMS hits).
 *   5. The community-links row appears AFTER the COPY TRADING row
 *      (community is the very last row, never competing with conversion CTA).
 *   6. Empty-basket fallback also has the community-links row at the end.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rcommunity-'));
process.env.ONBOARDING_DB_PATH    = path.join(TMP, 'onboarding.json');
process.env.USER_TZ_DB_PATH       = path.join(TMP, 'tz.json');
process.env.TRACK_DB_PATH         = path.join(TMP, 'wallets.json');
process.env.STATS_DB_PATH         = path.join(TMP, 'stats.json');
process.env.SHARE_DB_PATH         = path.join(TMP, 'share.json');
process.env.ALERTS_CONFIG_DB_PATH = path.join(TMP, 'alerts.json');
process.env.COPY_AUTO_DB_PATH     = path.join(TMP, 'copy_auto.json');
process.env.HF_CACHE_PATH         = path.join(TMP, 'hf_cache.json');
process.env.PEAR_HERO_URL         = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.PEAR_REFERRAL_CODE    = 'BlackCatDeFi';
process.env.SIMPLIFY_START_ENABLED   = 'true';
process.env.SIMPLIFY_DEFAULT_CAPITAL = '100';

const sanitizer = require('../src/sanitizer');
const bcdBasketCache = require('../src/bcdBasketCache');

const BASKET_FIXTURE = [
  { coin: 'WLD',  side: 'SHORT', notional: 5000 },
  { coin: 'STRK', side: 'SHORT', notional: 3000 },
];

test.beforeEach(() => {
  bcdBasketCache._resetForTests();
  bcdBasketCache._setCacheForTests(BASKET_FIXTURE);
});

// 1. Last row has exactly 2 URL-only buttons (no callback_data).
test('community-links row: 2 URL-only buttons', async () => {
  delete require.cache[require.resolve('../src/simplifiedStart')];
  process.env.SIGNALS_CHANNEL_URL = 'https://t.me/BlackCatDeFiSignals';
  process.env.THESIS_CHANNEL_URL  = 'https://t.me/BlackCatDeFiThesis';
  const simplifiedStart = require('../src/simplifiedStart');
  const kb = await simplifiedStart._buildKeyboard(11111);
  const last = kb.inline_keyboard[kb.inline_keyboard.length - 1];
  assert.strictEqual(last.length, 2);
  for (const btn of last) {
    assert.ok(btn.url, `community-link button missing .url: ${JSON.stringify(btn)}`);
    assert.strictEqual(btn.callback_data, undefined,
      `community-link button must NOT have callback_data: ${JSON.stringify(btn)}`);
  }
});

// 2. Default URLs point to BlackCatDeFiSignals + BlackCatDeFiThesis.
test('community-links default URLs: t.me/BlackCatDeFiSignals + Thesis', async () => {
  delete require.cache[require.resolve('../src/simplifiedStart')];
  delete process.env.SIGNALS_CHANNEL_URL;
  delete process.env.THESIS_CHANNEL_URL;
  const simplifiedStart = require('../src/simplifiedStart');
  const kb = await simplifiedStart._buildKeyboard(11111);
  const last = kb.inline_keyboard[kb.inline_keyboard.length - 1];
  const signalsBtn = last.find((b) => /Signals Channel/i.test(b.text));
  const thesisBtn  = last.find((b) => /Thesis Channel/i.test(b.text));
  assert.ok(signalsBtn, 'Signals Channel button missing');
  assert.ok(thesisBtn,  'Thesis Channel button missing');
  assert.match(signalsBtn.url, /t\.me\/BlackCatDeFiSignals/i);
  assert.match(thesisBtn.url,  /t\.me\/BlackCatDeFiThesis/i);
});

// 3. Env vars override the defaults.
test('community-links: SIGNALS_CHANNEL_URL + THESIS_CHANNEL_URL env vars override', async () => {
  delete require.cache[require.resolve('../src/simplifiedStart')];
  process.env.SIGNALS_CHANNEL_URL = 'https://t.me/CustomSignals';
  process.env.THESIS_CHANNEL_URL  = 'https://t.me/CustomThesis';
  const simplifiedStart = require('../src/simplifiedStart');
  const kb = await simplifiedStart._buildKeyboard(11111);
  const last = kb.inline_keyboard[kb.inline_keyboard.length - 1];
  const urls = last.map((b) => b.url);
  assert.ok(urls.some((u) => /CustomSignals/.test(u)));
  assert.ok(urls.some((u) => /CustomThesis/.test(u)));
  // Restore defaults so subsequent tests don't pollute env.
  delete process.env.SIGNALS_CHANNEL_URL;
  delete process.env.THESIS_CHANNEL_URL;
});

// 4. Sanitizer allows community-link labels + URLs, no FORBIDDEN hits.
test('sanitizer allowlist passes the community-link strings', () => {
  const accepted = [
    '📡 Signals Channel',
    '📚 Thesis Channel',
    'https://t.me/BlackCatDeFiSignals',
    'https://t.me/BlackCatDeFiThesis',
    '@BlackCatDeFiSignals',
    '@BlackCatDeFiThesis',
  ];
  for (const s of accepted) {
    const hits = sanitizer.findForbiddenInString(s);
    assert.deepStrictEqual(
      hits,
      [],
      `sanitizer flagged community-link literal "${s}": ${JSON.stringify(hits)}`
    );
  }
});

// 5. Community row is the LAST row, AFTER copy_trading.
test('community-links row sits AFTER copytrading + tracking rows', async () => {
  delete require.cache[require.resolve('../src/simplifiedStart')];
  const simplifiedStart = require('../src/simplifiedStart');
  const kb = await simplifiedStart._buildKeyboard(11111);
  const copyIdx = kb.inline_keyboard.findIndex(
    (row) => row.some((b) => b.callback_data === 'simple:copy_trading')
  );
  const communityIdx = kb.inline_keyboard.findIndex((row) =>
    row.length === 2 && row.every((b) => b.url) &&
    row.some((b) => /Signals Channel/i.test(b.text))
  );
  assert.ok(copyIdx >= 0, 'copy_trading row must exist');
  assert.ok(communityIdx >= 0, 'community-links row must exist');
  assert.ok(
    communityIdx > copyIdx,
    `community-links must come AFTER copy_trading (got copy=${copyIdx}, community=${communityIdx})`
  );
  // And must be the very last row.
  assert.strictEqual(communityIdx, kb.inline_keyboard.length - 1);
});

// 6. Empty-basket fallback still has community-links last.
test('community-links row present in empty-basket fallback', async () => {
  delete require.cache[require.resolve('../src/simplifiedStart')];
  const simplifiedStart = require('../src/simplifiedStart');
  bcdBasketCache._resetForTests();
  const original = bcdBasketCache.getActiveBasket;
  bcdBasketCache.getActiveBasket = async () => [];
  try {
    const kb = await simplifiedStart._buildKeyboard(11111);
    const last = kb.inline_keyboard[kb.inline_keyboard.length - 1];
    assert.strictEqual(last.length, 2);
    for (const btn of last) assert.ok(btn.url);
    assert.ok(last.some((b) => /Signals Channel/i.test(b.text)));
    assert.ok(last.some((b) => /Thesis Channel/i.test(b.text)));
  } finally {
    bcdBasketCache.getActiveBasket = original;
  }
});
