'use strict';

/**
 * R-BASKET (3 may 2026) — basketEngine state-machine tests.
 *
 * Covers spec §10 test cases T1-T7 + T15:
 *   T1  10-leg basket opens                    → 1 OPEN message
 *   T2  same poll cycle returns same basket    → 0 duplicate messages
 *   T3  10-leg basket closes                   → 1 CLOSE message
 *   T4  user adds margin to one leg            → 0 messages
 *   T5  rotation within zombie window          → 0 CLOSE messages
 *   T6  true close, all legs gone > grace      → 1 CLOSE message
 *   T7  bot restart mid-PENDING                → state rehydrated from disk
 *   T15 TWAP entry takes 5 minutes             → 1 OPEN after TWAP completes
 *
 * Plus a handful of unit tests covering basketSignature determinism, the
 * baseline-suppression flag, and edge-case persistence behaviour.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const os = require('os');

const {
  BasketEngine,
  basketSignature,
  STATES,
} = require('../src/basketEngine');

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TEN_LEG_BASKET = [
  { coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 },
  { coin: 'ETH', side: 'LONG', size: 5, entryPrice: 3000 },
  { coin: 'SOL', side: 'LONG', size: 100, entryPrice: 150 },
  { coin: 'AVAX', side: 'LONG', size: 200, entryPrice: 30 },
  { coin: 'BNB', side: 'LONG', size: 50, entryPrice: 600 },
  { coin: 'WLD', side: 'SHORT', size: -1000, entryPrice: 4 },
  { coin: 'STRK', side: 'SHORT', size: -5000, entryPrice: 0.6 },
  { coin: 'ZRO', side: 'SHORT', size: -2000, entryPrice: 5 },
  { coin: 'ENA', side: 'SHORT', size: -10000, entryPrice: 0.5 },
  { coin: 'OP', side: 'SHORT', size: -3000, entryPrice: 2 },
];

const TWO_LEG_BASKET = [
  { coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 },
  { coin: 'ETH', side: 'SHORT', size: -5, entryPrice: 3000 },
];

const TWO_LEG_DIFFERENT = [
  { coin: 'SOL', side: 'LONG', size: 100, entryPrice: 150 },
  { coin: 'AVAX', side: 'SHORT', size: -200, entryPrice: 30 },
];

const WALLET = '0xabc0000000000000000000000000000000000001';

function tmpDb(name) {
  return path.join(os.tmpdir(), `basket_engine_${name}_${Date.now()}_${Math.random().toString(36).slice(2)}.json`);
}

function makeEngine({ now, persist = false, dbPath = null } = {}) {
  let clock = (typeof now === 'function')
    ? now
    : (() => (Number.isFinite(now) ? now : Date.now()));
  return new BasketEngine({
    now: () => clock(),
    zombieGraceMs: 90_000, // explicit, matches default
    persist,
    dbPath,
  });
}

// ---------------------------------------------------------------------------
// basketSignature
// ---------------------------------------------------------------------------

test('basketSignature is deterministic regardless of leg order', () => {
  const a = basketSignature([
    { coin: 'BTC', side: 'LONG', size: 1 },
    { coin: 'ETH', side: 'SHORT', size: -1 },
  ]);
  const b = basketSignature([
    { coin: 'ETH', side: 'SHORT', size: -1 },
    { coin: 'BTC', side: 'LONG', size: 1 },
  ]);
  assert.equal(a, b);
  assert.equal(a, 'L:BTC|S:ETH');
});

test('basketSignature returns "" for empty/null input', () => {
  assert.equal(basketSignature([]), '');
  assert.equal(basketSignature(null), '');
  assert.equal(basketSignature(undefined), '');
});

test('basketSignature infers side from negative size when side missing', () => {
  const sig = basketSignature([
    { coin: 'BTC', size: 1 },
    { coin: 'ETH', size: -1 },
  ]);
  assert.equal(sig, 'L:BTC|S:ETH');
});

test('basketSignature ignores entries without a coin', () => {
  const sig = basketSignature([
    { coin: 'BTC', side: 'LONG', size: 1 },
    { coin: '', side: 'LONG', size: 1 },
    null,
  ]);
  assert.equal(sig, 'L:BTC|S:');
});

// ---------------------------------------------------------------------------
// T1 — 10-leg basket opens → exactly 1 OPEN
// ---------------------------------------------------------------------------

test('T1: 10-leg basket open emits exactly 1 OPEN event', () => {
  const eng = makeEngine();
  // Baseline first (wallet just appeared, has no positions yet)
  const baselineEvents = eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  assert.equal(baselineEvents.length, 0);

  // 10 legs appear in next poll
  const events = eng.processSnapshot({ walletKey: WALLET, positions: TEN_LEG_BASKET });
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'BASKET_OPEN');
  assert.equal(events[0].legs.length, 10);
});

// ---------------------------------------------------------------------------
// T2 — re-poll while basket still active → 0 duplicate messages
// ---------------------------------------------------------------------------

test('T2: re-polling the same active basket emits zero duplicate messages', () => {
  const eng = makeEngine();
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  const open = eng.processSnapshot({ walletKey: WALLET, positions: TEN_LEG_BASKET });
  assert.equal(open.length, 1);

  // Five subsequent polls all see the same basket — must produce 0 events each.
  for (let i = 0; i < 5; i++) {
    const evs = eng.processSnapshot({ walletKey: WALLET, positions: TEN_LEG_BASKET });
    assert.equal(evs.length, 0, `poll #${i + 1} should be silent`);
  }
});

// ---------------------------------------------------------------------------
// T3 — basket fully closes (after grace) → exactly 1 CLOSE
// ---------------------------------------------------------------------------

test('T3: 10-leg basket closes (after zombie grace) emits exactly 1 CLOSE', () => {
  let t = 1_000_000;
  const eng = makeEngine({ now: () => t });
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({ walletKey: WALLET, positions: TEN_LEG_BASKET });

  // Positions vanish — first call enters PENDING_CLOSE silently
  t += 60_000;
  let evs = eng.processSnapshot({ walletKey: WALLET, positions: [] });
  assert.equal(evs.length, 0);
  assert.equal(eng.peek(WALLET).state, STATES.PENDING_CLOSE);

  // Grace elapses (90s default) — next snapshot emits CLOSE
  t += 95_000;
  evs = eng.processSnapshot({ walletKey: WALLET, positions: [] });
  assert.equal(evs.length, 1);
  assert.equal(evs[0].type, 'BASKET_CLOSE');
  assert.equal(evs[0].legs.length, 10);
  assert.equal(eng.peek(WALLET).state, STATES.EMPTY);
});

// ---------------------------------------------------------------------------
// T4 — adding margin to one leg (signature unchanged) → 0 messages
// ---------------------------------------------------------------------------

test('T4: margin add to one leg keeps signature → zero events', () => {
  const eng = makeEngine();
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });

  const withMoreMargin = TWO_LEG_BASKET.map((p, i) =>
    i === 0 ? { ...p, marginUsed: (p.marginUsed || 0) + 500 } : p
  );
  const evs = eng.processSnapshot({ walletKey: WALLET, positions: withMoreMargin });
  assert.equal(evs.length, 0);
});

// ---------------------------------------------------------------------------
// T5 — rotation: legs disappear and same signature reappears within grace
// ---------------------------------------------------------------------------

test('T5: rotation within zombie window suppresses CLOSE event', () => {
  let t = 2_000_000;
  const eng = makeEngine({ now: () => t });
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });

  // legs flicker out for one cycle (within grace)
  t += 30_000;
  let evs = eng.processSnapshot({ walletKey: WALLET, positions: [] });
  assert.equal(evs.length, 0, 'entering pending must be silent');
  assert.equal(eng.peek(WALLET).state, STATES.PENDING_CLOSE);

  // same basket reappears before grace expires
  t += 30_000;
  evs = eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });
  assert.equal(evs.length, 0, 'rotation must emit no events');
  assert.equal(eng.peek(WALLET).state, STATES.ACTIVE);
});

// ---------------------------------------------------------------------------
// T6 — true close (PnL > 0)
// ---------------------------------------------------------------------------

test('T6: true close after grace yields BASKET_CLOSE with original legs preserved', () => {
  let t = 3_000_000;
  const eng = makeEngine({ now: () => t });
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({
    walletKey: WALLET,
    positions: [
      { coin: 'BTC', side: 'LONG', size: 1, entryPrice: 60000, unrealizedPnl: 250 },
    ],
  });

  t += 120_000; // beyond grace
  // First post-vanish poll enters pending; second emits CLOSE.
  eng.processSnapshot({ walletKey: WALLET, positions: [] });
  t += 100_000;
  const evs = eng.processSnapshot({ walletKey: WALLET, positions: [] });
  assert.equal(evs.length, 1);
  assert.equal(evs[0].type, 'BASKET_CLOSE');
  assert.equal(evs[0].legs[0].coin, 'BTC');
  assert.equal(evs[0].legs[0].unrealizedPnl, 250);
});

// ---------------------------------------------------------------------------
// T7 — restart mid-PENDING_CLOSE: state rehydrates from disk
// ---------------------------------------------------------------------------

test('T7: state survives "restart" via JSON persistence', () => {
  const dbPath = tmpDb('restart');
  let t = 4_000_000;
  try {
    // First engine — drives wallet into ACTIVE then PENDING_CLOSE
    let eng = new BasketEngine({
      now: () => t,
      zombieGraceMs: 90_000,
      persist: true,
      dbPath,
    });
    eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
    eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });
    t += 30_000;
    eng.processSnapshot({ walletKey: WALLET, positions: [] });
    assert.equal(eng.peek(WALLET).state, STATES.PENDING_CLOSE);
    assert.ok(fs.existsSync(dbPath), 'engine must write its store');

    // Simulate restart — fresh instance reads the file
    const eng2 = new BasketEngine({
      now: () => t,
      zombieGraceMs: 90_000,
      persist: true,
      dbPath,
    });
    assert.equal(eng2.peek(WALLET).state, STATES.PENDING_CLOSE);
    assert.equal(eng2.peek(WALLET).legs.length, 2);

    // Resume: still in pending; let grace expire on the new instance.
    t += 100_000;
    const evs = eng2.processSnapshot({ walletKey: WALLET, positions: [] });
    assert.equal(evs.length, 1);
    assert.equal(evs[0].type, 'BASKET_CLOSE');
  } finally {
    try { fs.unlinkSync(dbPath); } catch (_) {}
  }
});

// ---------------------------------------------------------------------------
// T15 — TWAP that takes 5 minutes still emits one OPEN
// ---------------------------------------------------------------------------

test('T15: TWAP entry across multiple polls still emits exactly 1 OPEN', () => {
  let t = 5_000_000;
  const eng = makeEngine({ now: () => t });
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });

  // TWAP fills incrementally — same set of coins/sides, growing sizes.
  const partial1 = [{ coin: 'BTC', side: 'LONG', size: 0.1, entryPrice: 60000 }];
  const partial2 = [{ coin: 'BTC', side: 'LONG', size: 0.3, entryPrice: 60000 }];
  const partial3 = [{ coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 }];

  let evs = eng.processSnapshot({ walletKey: WALLET, positions: partial1 });
  assert.equal(evs.length, 1, 'first non-empty snapshot fires the OPEN');

  t += 60_000;
  evs = eng.processSnapshot({ walletKey: WALLET, positions: partial2 });
  assert.equal(evs.length, 0, 'TWAP growth must be silent');

  t += 60_000;
  evs = eng.processSnapshot({ walletKey: WALLET, positions: partial3 });
  assert.equal(evs.length, 0, 'TWAP completion must be silent');
});

// ---------------------------------------------------------------------------
// Misc: rotation across signatures (different basket inside grace) emits both
// ---------------------------------------------------------------------------

test('zombie window — DIFFERENT basket appearing emits CLOSE+OPEN pair', () => {
  let t = 6_000_000;
  const eng = makeEngine({ now: () => t });
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });

  t += 30_000;
  eng.processSnapshot({ walletKey: WALLET, positions: [] }); // → PENDING

  t += 30_000;
  const evs = eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_DIFFERENT });
  // Spec: a different basket while pending → close old + open new.
  assert.equal(evs.length, 2);
  assert.equal(evs[0].type, 'BASKET_CLOSE');
  assert.equal(evs[1].type, 'BASKET_OPEN');
});

test('forget(walletKey) clears in-memory state', () => {
  const eng = makeEngine();
  eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
  eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });
  assert.equal(eng.peek(WALLET).state, STATES.ACTIVE);
  eng.forget(WALLET);
  assert.equal(eng.peek(WALLET).state, STATES.EMPTY); // peek lazily recreates
});

test('persistence writes valid JSON we can re-read manually', () => {
  const dbPath = tmpDb('rawjson');
  try {
    const eng = new BasketEngine({
      now: () => 7_000_000,
      zombieGraceMs: 90_000,
      persist: true,
      dbPath,
    });
    eng.processSnapshot({ walletKey: WALLET, positions: [], baseline: true });
    eng.processSnapshot({ walletKey: WALLET, positions: TWO_LEG_BASKET });
    const raw = JSON.parse(fs.readFileSync(dbPath, 'utf-8'));
    assert.equal(raw.version, 1);
    assert.ok(raw.byWallet[WALLET.toLowerCase()]);
    assert.equal(raw.byWallet[WALLET.toLowerCase()].state, STATES.ACTIVE);
  } finally {
    try { fs.unlinkSync(dbPath); } catch (_) {}
  }
});
