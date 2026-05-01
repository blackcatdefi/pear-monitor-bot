'use strict';

/**
 * R-GRAMJS — unit tests for the MTProto fallback backend.
 *
 * The real `telegram` package is NOT loaded by these tests — we exercise
 * pure functions (env reading, message normalization) and the lazy-loader
 * indirection. Live MTProto behaviour is verified manually via
 * docs/GRAMJS_SETUP.md → "Verifying the fallback actually works".
 */

const test = require('node:test');
const assert = require('node:assert');

// Fresh module instance per test (env reading is module-level).
function freshBackend() {
  delete require.cache[require.resolve('../src/gramjsBackend')];
  return require('../src/gramjsBackend');
}

function withEnv(vars, fn) {
  const saved = {};
  for (const k of Object.keys(vars)) {
    saved[k] = process.env[k];
    if (vars[k] === undefined) delete process.env[k];
    else process.env[k] = vars[k];
  }
  try {
    return fn();
  } finally {
    for (const k of Object.keys(saved)) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

// ─── _readEnv ────────────────────────────────────────────────────────────

test('_readEnv — empty env returns ready=false', () => {
  withEnv(
    {
      TELEGRAM_API_ID: undefined,
      TELEGRAM_API_HASH: undefined,
      TELEGRAM_SESSION_STRING: undefined,
    },
    () => {
      const g = freshBackend();
      const env = g._readEnv();
      assert.strictEqual(env.ready, false);
    }
  );
});

test('_readEnv — placeholder PENDING_BCD_SETUP returns ready=false', () => {
  withEnv(
    {
      TELEGRAM_API_ID: '12345',
      TELEGRAM_API_HASH: 'abcdef0123456789',
      TELEGRAM_SESSION_STRING: 'PENDING_BCD_SETUP',
    },
    () => {
      const g = freshBackend();
      const env = g._readEnv();
      assert.strictEqual(env.ready, false);
    }
  );
});

test('_readEnv — fully populated returns ready=true with parsed apiId', () => {
  withEnv(
    {
      TELEGRAM_API_ID: '987654',
      TELEGRAM_API_HASH: 'deadbeef',
      TELEGRAM_SESSION_STRING: 'long-session-string-here',
    },
    () => {
      const g = freshBackend();
      const env = g._readEnv();
      assert.strictEqual(env.ready, true);
      assert.strictEqual(env.apiId, 987654);
      assert.strictEqual(env.apiHash, 'deadbeef');
      assert.strictEqual(env.sessionString, 'long-session-string-here');
    }
  );
});

test('_readEnv — non-numeric TELEGRAM_API_ID returns ready=false', () => {
  withEnv(
    {
      TELEGRAM_API_ID: 'not-a-number',
      TELEGRAM_API_HASH: 'deadbeef',
      TELEGRAM_SESSION_STRING: 'session',
    },
    () => {
      const g = freshBackend();
      const env = g._readEnv();
      assert.strictEqual(env.ready, false);
    }
  );
});

test('_readEnv — defaults BCD_SIGNALS_CHANNEL to BlackCatDeFiSignals', () => {
  withEnv({ BCD_SIGNALS_CHANNEL: undefined }, () => {
    const g = freshBackend();
    const env = g._readEnv();
    assert.strictEqual(env.channel, 'BlackCatDeFiSignals');
  });
});

test('_readEnv — honours BCD_SIGNALS_CHANNEL override', () => {
  withEnv({ BCD_SIGNALS_CHANNEL: 'CustomChannel' }, () => {
    const g = freshBackend();
    const env = g._readEnv();
    assert.strictEqual(env.channel, 'CustomChannel');
  });
});

// ─── _normalizeMessage ────────────────────────────────────────────────────

test('_normalizeMessage — extracts pearUrl from text', () => {
  const g = freshBackend();
  const post = g._normalizeMessage('TestChannel', {
    id: 42,
    date: 1714000000,
    message: 'New basket: https://app.pear.garden/basket/long-eth-short-btc some text',
  });
  assert.strictEqual(post.channel, 'TestChannel');
  assert.strictEqual(post.messageId, 42);
  assert.strictEqual(post.postedAt, 1714000000);
  assert.strictEqual(
    post.pearUrl,
    'https://app.pear.garden/basket/long-eth-short-btc'
  );
});

test('_normalizeMessage — extracts pearUrl from typed entities', () => {
  const g = freshBackend();
  const post = g._normalizeMessage('TestChannel', {
    id: 100,
    date: new Date('2026-05-01T12:00:00Z'),
    message: 'Click here',
    entities: [
      { url: 'https://example.com/not-pear' },
      { url: 'https://app.pear.garden/basket/abc' },
    ],
  });
  assert.strictEqual(post.pearUrl, 'https://app.pear.garden/basket/abc');
  assert.strictEqual(post.postedAt, Math.floor(new Date('2026-05-01T12:00:00Z').getTime() / 1000));
});

test('_normalizeMessage — null pearUrl when no URL present', () => {
  const g = freshBackend();
  const post = g._normalizeMessage('TestChannel', {
    id: 5,
    date: 1714000000,
    message: 'Just text, no URL.',
  });
  assert.strictEqual(post.pearUrl, null);
  assert.strictEqual(post.text, 'Just text, no URL.');
});

test('_normalizeMessage — handles Date object on .date', () => {
  const g = freshBackend();
  const d = new Date('2026-05-01T00:00:00Z');
  const post = g._normalizeMessage('C', { id: 1, date: d, message: '' });
  assert.strictEqual(post.postedAt, Math.floor(d.getTime() / 1000));
});

test('_normalizeMessage — returns null on garbage input', () => {
  const g = freshBackend();
  assert.strictEqual(g._normalizeMessage('C', null), null);
  assert.strictEqual(g._normalizeMessage('C', { id: 'bad' }), null);
  assert.strictEqual(g._normalizeMessage('C', undefined), null);
});

test('_normalizeMessage — falls back to .text when .message absent', () => {
  const g = freshBackend();
  const post = g._normalizeMessage('C', {
    id: 7,
    date: 1714000000,
    text: 'fallback text https://app.pear.garden/x',
  });
  assert.strictEqual(post.text, 'fallback text https://app.pear.garden/x');
  assert.strictEqual(post.pearUrl, 'https://app.pear.garden/x');
});

// ─── isAvailable / statusLines ────────────────────────────────────────────

test('isAvailable — returns false when env empty', () => {
  withEnv(
    { TELEGRAM_API_ID: undefined, TELEGRAM_API_HASH: undefined, TELEGRAM_SESSION_STRING: undefined },
    () => {
      const g = freshBackend();
      assert.strictEqual(g.isAvailable(), false);
    }
  );
});

test('statusLines — surfaces missing env clearly', () => {
  withEnv(
    {
      TELEGRAM_API_ID: undefined,
      TELEGRAM_API_HASH: undefined,
      TELEGRAM_SESSION_STRING: undefined,
    },
    () => {
      const g = freshBackend();
      const lines = g.statusLines();
      assert.ok(lines.some((l) => l.includes('api_id: missing')));
      assert.ok(lines.some((l) => l.includes('api_hash: missing')));
      assert.ok(lines.some((l) => l.includes('session: missing')));
      assert.ok(lines.some((l) => l.includes('available: NO')));
    }
  );
});

test('statusLines — surfaces PENDING placeholder', () => {
  withEnv(
    {
      TELEGRAM_API_ID: '1',
      TELEGRAM_API_HASH: 'x',
      TELEGRAM_SESSION_STRING: 'PENDING_BCD_SETUP',
    },
    () => {
      const g = freshBackend();
      const lines = g.statusLines();
      assert.ok(lines.some((l) => l.includes('session: pending')));
    }
  );
});

test('fetchRecentMessages — returns [] when unavailable', async () => {
  await withEnv(
    {
      TELEGRAM_API_ID: undefined,
      TELEGRAM_API_HASH: undefined,
      TELEGRAM_SESSION_STRING: undefined,
    },
    async () => {
      const g = freshBackend();
      const out = await g.fetchRecentMessages();
      assert.deepStrictEqual(out, []);
    }
  );
});
