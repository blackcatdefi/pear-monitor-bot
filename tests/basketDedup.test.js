'use strict';

/**
 * R(v4) — basketDedup tests.
 *
 * Validates: hash determinism, persistence across module re-require,
 * TTL expiry, ENABLED kill switch, and the apr-30 v6-basket regression
 * (2x duplicate "NUEVA BASKET ABIERTA" within 3h must collapse to 1).
 *
 * Tests use a temp DB file via DEDUP_DB_PATH so they don't touch the
 * production volume path. _resetForTests() clears state between tests.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Force a temp DB BEFORE requiring the module — env vars are read at module-load.
const TMP_DB = path.join(
  os.tmpdir(),
  `basket_dedup_test_${Date.now()}_${process.pid}.json`
);
process.env.DEDUP_DB_PATH = TMP_DB;
process.env.BASKET_DEDUP_ENABLED = 'true';
process.env.BASKET_DEDUP_TTL_DAYS = '7';

const basketDedup = require('../src/basketDedup');

function _cleanup() {
  basketDedup._resetForTests();
  try {
    if (fs.existsSync(TMP_DB)) fs.unlinkSync(TMP_DB);
  } catch (_) {}
}

test.beforeEach(() => _cleanup());
test.after(() => _cleanup());

// --- hash determinism ---

test('computeBasketHash es determinístico — orden de positions no cambia hash', () => {
  const positions = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
    { coin: 'ARB', side: 'SHORT', entryPx: 0.127390 },
  ];
  const wallet = '0xc7AE1a8DD2e9b4C99E3F11FA6f9C9D8e8F7B8a8a';
  const h1 = basketDedup.computeBasketHash(wallet, positions);
  const h2 = basketDedup.computeBasketHash(wallet, [...positions].reverse());
  const h3 = basketDedup.computeBasketHash(wallet, [
    positions[2], positions[0], positions[1],
  ]);
  assert.equal(h1, h2);
  assert.equal(h1, h3);
});

test('computeBasketHash — wallet case-insensitive (lowercase)', () => {
  const positions = [
    { coin: 'BTC', side: 'LONG', entryPx: 100000 },
  ];
  const h1 = basketDedup.computeBasketHash('0xABCD', positions);
  const h2 = basketDedup.computeBasketHash('0xabcd', positions);
  assert.equal(h1, h2);
});

test('computeBasketHash — coin/side case-insensitive (uppercase normalized)', () => {
  const wallet = '0xabcd';
  const h1 = basketDedup.computeBasketHash(wallet, [
    { coin: 'BTC', side: 'LONG', entryPx: 100 },
  ]);
  const h2 = basketDedup.computeBasketHash(wallet, [
    { coin: 'btc', side: 'long', entryPx: 100 },
  ]);
  assert.equal(h1, h2);
});

test('computeBasketHash — diferente entryPx → diferente hash', () => {
  const wallet = '0xabcd';
  const h1 = basketDedup.computeBasketHash(wallet, [
    { coin: 'BTC', side: 'LONG', entryPx: 100 },
  ]);
  const h2 = basketDedup.computeBasketHash(wallet, [
    { coin: 'BTC', side: 'LONG', entryPx: 101 },
  ]);
  assert.notEqual(h1, h2);
});

test('computeBasketHash — diferente wallet → diferente hash', () => {
  const positions = [{ coin: 'BTC', side: 'LONG', entryPx: 100 }];
  const h1 = basketDedup.computeBasketHash('0xaaaa', positions);
  const h2 = basketDedup.computeBasketHash('0xbbbb', positions);
  assert.notEqual(h1, h2);
});

test('computeBasketHash — acepta entryPrice además de entryPx (compat)', () => {
  const wallet = '0xabcd';
  const h1 = basketDedup.computeBasketHash(wallet, [
    { coin: 'BTC', side: 'LONG', entryPx: 100 },
  ]);
  const h2 = basketDedup.computeBasketHash(wallet, [
    { coin: 'BTC', side: 'LONG', entryPrice: 100 },
  ]);
  assert.equal(h1, h2);
});

test('computeBasketHash — throws on empty positions', () => {
  assert.throws(() => basketDedup.computeBasketHash('0xabcd', []));
  assert.throws(() => basketDedup.computeBasketHash('0xabcd', null));
});

// --- check + mark flow ---

test('checkAlreadyAlerted — basket nuevo → wasAlerted=false', () => {
  const wallet = '0xc7AE';
  const positions = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
  ];
  const r = basketDedup.checkAlreadyAlerted(wallet, positions);
  assert.equal(r.wasAlerted, false);
  assert.equal(r.alertedAt, null);
  assert.ok(r.hash); // hash siempre devuelto
});

test('markAsAlerted + checkAlreadyAlerted — segundo check bloqueado', () => {
  const wallet = '0xc7AE';
  const positions = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
  ];
  const c1 = basketDedup.checkAlreadyAlerted(wallet, positions);
  assert.equal(c1.wasAlerted, false);
  basketDedup.markAsAlerted(wallet, positions);
  const c2 = basketDedup.checkAlreadyAlerted(wallet, positions);
  assert.equal(c2.wasAlerted, true);
  assert.ok(c2.alertedAt > 0);
});

// --- REGRESSION ---

test('REGRESSION apr30: basket v6 duplicate fire suprimido', () => {
  // Reproducción exacta del bug que BCD reportó:
  // mismas 5 posiciones de v6 alertadas 2x con 3h 15min de diferencia.
  const wallet = '0xc7AE1a8DD2e9b4C99E3F11FA6f9C9D8e8F7B8a8a';
  const v6Positions = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
    { coin: 'ARB', side: 'SHORT', entryPx: 0.127390 },
    { coin: 'PYTH', side: 'SHORT', entryPx: 0.046933 },
    { coin: 'ENA', side: 'SHORT', entryPx: 0.104340 },
  ];

  // Alerta 1 (15:50 UTC — post R(v3) deploy)
  const fire1 = basketDedup.checkAlreadyAlerted(wallet, v6Positions);
  assert.equal(fire1.wasAlerted, false, 'primera alerta debe pasar');
  basketDedup.markAsAlerted(wallet, v6Positions);

  // Alerta 2 (19:05 UTC — post otro restart, mismas posiciones)
  // En el bug original esto disparó otra "NUEVA BASKET ABIERTA"
  const fire2 = basketDedup.checkAlreadyAlerted(wallet, v6Positions);
  assert.equal(
    fire2.wasAlerted,
    true,
    'segunda alerta DEBE bloquearse (regresión apr-30)'
  );
  assert.equal(fire1.hash, fire2.hash, 'mismo basket → mismo hash');
});

test('REGRESSION: positions reordered post-restart todavía matchean', () => {
  // Hyperliquid API a veces devuelve positions en distinto orden cycle-to-cycle.
  // El hash debe ser order-invariant.
  const wallet = '0xc7AE';
  const fire1 = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
    { coin: 'ARB', side: 'SHORT', entryPx: 0.127390 },
  ];
  const fire2 = [
    { coin: 'ARB', side: 'SHORT', entryPx: 0.127390 },
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
  ];
  basketDedup.markAsAlerted(wallet, fire1);
  const c = basketDedup.checkAlreadyAlerted(wallet, fire2);
  assert.equal(c.wasAlerted, true);
});

// --- TTL ---

test('TTL expiry: entry de 8 días deja de bloquear (TTL=7d)', () => {
  const wallet = '0xc7AE';
  const positions = [
    { coin: 'BTC', side: 'LONG', entryPx: 100000 },
  ];
  const hash = basketDedup.markAsAlerted(wallet, positions);
  assert.ok(hash);

  // Backdate 8 días — más allá del TTL
  basketDedup._backdateForTests(hash, 8);

  const c = basketDedup.checkAlreadyAlerted(wallet, positions);
  assert.equal(c.wasAlerted, false, 'entry vieja > TTL no debe bloquear');
});

test('TTL: entry de 6 días todavía bloquea (TTL=7d)', () => {
  const wallet = '0xc7AE';
  const positions = [
    { coin: 'BTC', side: 'LONG', entryPx: 100000 },
  ];
  const hash = basketDedup.markAsAlerted(wallet, positions);
  basketDedup._backdateForTests(hash, 6);
  const c = basketDedup.checkAlreadyAlerted(wallet, positions);
  assert.equal(c.wasAlerted, true);
});

test('cleanupExpired remueve entries vencidos', () => {
  const wallet = '0xc7AE';
  const fresh = [{ coin: 'BTC', side: 'LONG', entryPx: 100 }];
  const old = [{ coin: 'ETH', side: 'LONG', entryPx: 4000 }];
  basketDedup.markAsAlerted(wallet, fresh);
  const oldHash = basketDedup.markAsAlerted(wallet, old);
  basketDedup._backdateForTests(oldHash, 30);

  const cleaned = basketDedup.cleanupExpired();
  assert.equal(cleaned, 1);

  const entries = basketDedup.getAllEntries();
  assert.equal(entries.length, 1);
});

// --- persistence ---

test('Persistencia: escribir y leer mismo file produce misma data', () => {
  const wallet = '0xc7AE';
  const positions = [
    { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
    { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
  ];
  basketDedup.markAsAlerted(wallet, positions);

  // Forzar re-read del file (simula reinicio del proceso)
  const raw = fs.readFileSync(TMP_DB, 'utf8');
  const db = JSON.parse(raw);
  const hashes = Object.keys(db);
  assert.equal(hashes.length, 1);
  const stored = db[hashes[0]];
  assert.equal(stored.wallet, wallet.toLowerCase());
  assert.equal(stored.positions.length, 2);
  assert.equal(stored.ttlDays, 7);
});

test('getAllEntries: ordenado newest-first', () => {
  const wallet = '0xc7AE';
  const h1 = basketDedup.markAsAlerted(wallet, [
    { coin: 'BTC', side: 'LONG', entryPx: 100 },
  ]);
  // Pequeño jitter de tiempo para garantizar sentAt distinto
  const orig = Date.now;
  Date.now = () => orig() + 1000;
  const h2 = basketDedup.markAsAlerted(wallet, [
    { coin: 'ETH', side: 'LONG', entryPx: 4000 },
  ]);
  Date.now = orig;

  const entries = basketDedup.getAllEntries();
  assert.equal(entries.length, 2);
  assert.equal(entries[0].hash, h2, 'más reciente primero');
  assert.equal(entries[1].hash, h1);
});

// --- ENABLED kill switch ---
// We can't toggle ENABLED at runtime (it's frozen at module load), but we
// can verify that the exported flag matches what's set above.
test('ENABLED flag refleja env var', () => {
  assert.equal(basketDedup.ENABLED, true);
});

test('TTL_DAYS flag refleja env var', () => {
  assert.equal(basketDedup.TTL_DAYS, 7);
});

// --- floating-point jitter resilience ---

test('entryPx con jitter de 1e-9 todavía matchea (rounding 6dp)', () => {
  const wallet = '0xc7AE';
  const fire1 = [{ coin: 'BTC', side: 'LONG', entryPx: 0.157570 }];
  const fire2 = [{ coin: 'BTC', side: 'LONG', entryPx: 0.1575700001 }]; // jitter
  basketDedup.markAsAlerted(wallet, fire1);
  const c = basketDedup.checkAlreadyAlerted(wallet, fire2);
  assert.equal(c.wasAlerted, true, 'rounding a 6dp absorbe el jitter');
});
