'use strict';

/**
 * R-PUBLIC-BASKET-UNIFY (4 may 2026) — extensions.onWalletPolled must call
 * lockout.markClosed when a wallet's basket transitions from non-empty to
 * empty (prev legs ≥ 1 → curr legs = 0). Without this, a wallet whose
 * basket actually closed would stay locked forever and never emit a new
 * BASKET_OPEN.
 *
 * We don't drive the full extensions.bootstrap pipeline here — we exercise
 * the buildHooks() factory directly with a stubbed notify and stubbed
 * monitor patch, then call onWalletPolled twice (open snapshot, then empty
 * snapshot) and verify the lockout state.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

process.env.OPEN_ALERTS_ENABLED = 'true';
process.env.BASKET_DEDUP_ENABLED = 'true';
process.env.BASKET_DEDUP_TTL_DAYS = '7';
// Disable compounding so the test isn't dragged through that codepath.
process.env.COMPOUND_ENABLED = 'false';
process.env.COMPOUND_DETECTOR_ENABLED = 'false';

const extensions = require('../src/extensions');
const lockout = require('../src/walletBasketLockout');
const openAlerts = require('../src/openAlerts');
const basketDedup = require('../src/basketDedup');

test('onWalletPolled flips wallet to CLOSED when basket goes empty', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  lockout._resetForTests({ persist: false });

  const sent = [];
  const notify = async (chatId, msg, opts) => {
    sent.push({ chatId, msg, opts });
  };
  const hooks = extensions.buildHooks({ notify, primaryChatId: 9999 });

  const wallet = '0xCloseTransition0000000000000000000000aa';
  const initialPositions = [
    { coin: 'AAA', side: 'SHORT', size: -10, entryPrice: 1.0 },
    { coin: 'BBB', side: 'SHORT', size: -10, entryPrice: 2.0 },
    { coin: 'CCC', side: 'SHORT', size: -10, entryPrice: 3.0 },
  ];

  // Phase 1 — basket opens (silent=false, prev empty, curr=3 legs)
  await hooks.onWalletPolled({
    chatId: 1,
    wallet,
    label: 'CLOSE-TEST',
    allPositions: initialPositions,
    silent: false,
  });
  // After OPEN, lockout shows wallet as OPEN.
  assert.strictEqual(
    lockout.getState(wallet).state,
    'OPEN',
    'lockout must mark wallet OPEN after basket emit'
  );

  // Phase 2 — basket closes (curr=0)
  await hooks.onWalletPolled({
    chatId: 1,
    wallet,
    label: 'CLOSE-TEST',
    allPositions: [],
    silent: false,
  });
  // After CLOSE transition, lockout shows wallet as CLOSED.
  const s = lockout.getState(wallet);
  assert.strictEqual(
    s.state,
    'CLOSED',
    `lockout must flip to CLOSED on prev≥1 → curr=0 transition (got ${s.state})`
  );
  assert.ok(Number.isFinite(s.closedAt), 'closedAt must be set');
});

test('onWalletPolled does NOT mark closed if basket size simply changed but stayed non-empty', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  lockout._resetForTests({ persist: false });

  const notify = async () => {};
  const hooks = extensions.buildHooks({ notify, primaryChatId: 9999 });
  const wallet = '0xResizeTest000000000000000000000000000aa';

  await hooks.onWalletPolled({
    chatId: 1,
    wallet,
    label: 'RESIZE',
    allPositions: [
      { coin: 'AAA', side: 'SHORT', size: -10, entryPrice: 1.0 },
      { coin: 'BBB', side: 'SHORT', size: -10, entryPrice: 2.0 },
      { coin: 'CCC', side: 'SHORT', size: -10, entryPrice: 3.0 },
    ],
    silent: false,
  });
  assert.strictEqual(lockout.getState(wallet).state, 'OPEN');

  // Resize: drop one leg but don't go empty
  await hooks.onWalletPolled({
    chatId: 1,
    wallet,
    label: 'RESIZE',
    allPositions: [
      { coin: 'AAA', side: 'SHORT', size: -10, entryPrice: 1.0 },
      { coin: 'BBB', side: 'SHORT', size: -10, entryPrice: 2.0 },
    ],
    silent: false,
  });
  assert.strictEqual(
    lockout.getState(wallet).state,
    'OPEN',
    'still OPEN — basket merely resized'
  );
});
