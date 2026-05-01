'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'sig-scraper-'));
process.env.COPY_TRADING_DB_DIR = TMP;
process.env.SIGNALS_SCRAPER_ENABLED = 'true';

// Fresh module instances using sandbox.
delete require.cache[require.resolve('../src/copyTradingStore')];
delete require.cache[require.resolve('../src/signalsChannelScraper')];
const store = require('../src/copyTradingStore');
const scraper = require('../src/signalsChannelScraper');

const HTML_FIXTURE = `
<html><body>
<div class="tgme_widget_message" data-post="BlackCatDeFiSignals/1" data-time="2026-04-15T15:00:00+00:00">
  <div class="tgme_widget_message_text">
    Test #1 — basket WLD STRK
    <a href="https://app.pear.garden/trade/hl/USDC-WLD+STRK+ENA+TIA+ARB?referral=BlackCatDeFi">link</a>
  </div>
  <time datetime="2026-04-15T15:00:00+00:00"></time>
</div>
<div class="tgme_widget_message" data-post="BlackCatDeFiSignals/5">
  <div class="tgme_widget_message_text">
    Test #5 — LONG HYPE
    <a href="https://app.pear.garden/trade/hl/HYPE+LIT-WLFI?referral=somecompetitor">x</a>
  </div>
</div>
<div class="tgme_widget_message" data-post="BlackCatDeFiSignals/9">
  <div class="tgme_widget_message_text">just text, no Pear link</div>
</div>
</body></html>
`;

test('parseHtml extracts data-post id + Pear URL', () => {
  const posts = scraper.parseHtml(HTML_FIXTURE);
  assert.equal(posts.length, 3);
  assert.equal(posts[0].channel, 'BlackCatDeFiSignals');
  assert.equal(posts[0].messageId, 1);
  assert.match(posts[0].pearUrl, /USDC-WLD\+STRK/);
  assert.equal(posts[1].messageId, 5);
  assert.match(posts[1].pearUrl, /HYPE\+LIT-WLFI/);
  assert.equal(posts[2].pearUrl, null);
});

test('parseHtml extracts text body stripped of tags', () => {
  const posts = scraper.parseHtml(HTML_FIXTURE);
  assert.match(posts[0].text, /Test #1/);
  assert.match(posts[0].text, /basket WLD STRK/);
  assert.ok(!posts[0].text.includes('<a'));
});

test('parseHtml handles empty / malformed input', () => {
  assert.deepEqual(scraper.parseHtml(''), []);
  assert.deepEqual(scraper.parseHtml(null), []);
  assert.deepEqual(scraper.parseHtml('<html></html>'), []);
});

test('processNewPosts dedupes via store + forces referral', async () => {
  store._resetForTests();
  const dispatched = [];
  const posts = scraper.parseHtml(HTML_FIXTURE);
  const n = await scraper.processNewPosts(posts, async (sig) => {
    dispatched.push(sig);
  });
  // 2 valid Pear URLs, 1 message without (skipped seen)
  assert.equal(n, 2);
  // message #5 had referral=somecompetitor — must be forced
  const msg5 = dispatched.find((s) => s.messageId === 5);
  assert.match(msg5.pearUrl, /referral=BlackCatDeFi$/);
});

test('processNewPosts skips already-seen messages', async () => {
  store._resetForTests();
  const posts = scraper.parseHtml(HTML_FIXTURE);
  const dispatched1 = [];
  await scraper.processNewPosts(posts, async (sig) => dispatched1.push(sig));
  const dispatched2 = [];
  await scraper.processNewPosts(posts, async (sig) => dispatched2.push(sig));
  // second call should dispatch nothing
  assert.equal(dispatched2.length, 0);
});

test('processNewPosts marks non-Pear posts as seen so they do not retry', async () => {
  store._resetForTests();
  const posts = [
    { messageId: 100, channel: 'BlackCatDeFiSignals', pearUrl: null, text: 'no link' },
  ];
  let calls = 0;
  await scraper.processNewPosts(posts, async () => {
    calls += 1;
  });
  assert.equal(calls, 0);
  // dispatched again
  await scraper.processNewPosts(posts, async () => {
    calls += 1;
  });
  assert.equal(calls, 0);
  // and store records it as seen so it never re-dispatches
  assert.equal(store.hasSignalBeenSeen('BlackCatDeFiSignals', 100), true);
});

test('processNewPosts onSignal failure does not poison the rest', async () => {
  store._resetForTests();
  const posts = scraper.parseHtml(HTML_FIXTURE);
  let attempts = 0;
  await scraper.processNewPosts(posts, async (sig) => {
    attempts += 1;
    if (sig.messageId === 1) throw new Error('boom');
  });
  // we still attempted message 5 even though message 1 threw.
  assert.equal(attempts, 2);
});

test('isEnabled honors SIGNALS_SCRAPER_ENABLED env', () => {
  process.env.SIGNALS_SCRAPER_ENABLED = 'false';
  assert.equal(scraper.isEnabled(), false);
  process.env.SIGNALS_SCRAPER_ENABLED = 'true';
  assert.equal(scraper.isEnabled(), true);
});
