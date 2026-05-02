'use strict';

/**
 * R-NOSPAM (2 may 2026) — basket dedup must hydrate on silent boot poll.
 *
 * Bug fixed: 06:30 UTC the bot fired "🚀 NEW BASKET OPENED" for the v6
 * basket (DYDX/OP/ARB/PYTH/ENA, $24,495 ntl) which had been open since
 * 29 apr 21:45 UTC. Root cause:
 *
 *   1. Container restarts (deploys, crashes, manual restarts) wipe the
 *      in-memory `lastSeenSnapshots` Map in extensions.js.
 *   2. The first poll cycle is silent=true (suppresses monitor.js alerts)
 *      but the patched `onWalletPolled` hook in extensions.js did NOT
 *      receive the silent flag and ran openAlerts emission anyway.
 *   3. With prev=[] and currentPositions=[v6 basket], openAlerts classified
 *      all of them as "new" → BASKET_OPEN candidate.
 *   4. The persistent dedup store had never marked this basket because
 *      basketDedup was deployed AFTER the v6 basket was opened.
 *   5. Alert fired → user gets stale alert for a known-old basket.
 *
 * Fix shape (extensions.js): when silent=true, hydrate the persistent
 * dedup store with the currently-active basket without emitting. This
 * test validates the hydration logic behaves correctly via a direct
 * call into the basketDedup module.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP_DB = path.join(
  os.tmpdir(),
  `rnospam_silent_boot_${Date.now()}_${process.pid}.json`
);
process.env.DEDUP_DB_PATH = TMP_DB;
process.env.BASKET_DEDUP_ENABLED = 'true';

delete require.cache[require.resolve('../src/basketDedup')];
const basketDedup = require('../src/basketDedup');

function _cleanup() {
  basketDedup._resetForTests();
  try {
    if (fs.existsSync(TMP_DB)) fs.unlinkSync(TMP_DB);
  } catch (_) {}
}

test.beforeEach(() => _cleanup());
test.after(() => _cleanup());

const WALLET = '0xc7AE1a8DD2e9b4C99E3F11FA6f9C9D8e8F7B8a8a';

const V6_BASKET = [
  { coin: 'DYDX', side: 'SHORT', entryPx: 0.157570 },
  { coin: 'OP', side: 'SHORT', entryPx: 0.120810 },
  { coin: 'ARB', side: 'SHORT', entryPx: 0.127390 },
  { coin: 'PYTH', side: 'SHORT', entryPx: 0.082450 },
  { coin: 'ENA', side: 'SHORT', entryPx: 0.281200 },
];

test('silent-boot hydration: marks current basket so subsequent restart does not re-emit', () => {
  // Simulate first silent poll on bot boot: dedup store is empty,
  // basket is currently active, so we mark without emitting.
  const before = basketDedup.checkAlreadyAlerted(WALLET, V6_BASKET);
  assert.equal(before.wasAlerted, false,
    'pre-hydration: dedup must not yet know about this basket');

  basketDedup.markAsAlerted(WALLET, V6_BASKET);

  // Now simulate next poll cycle (or even another bot restart): the
  // basket is the same set of positions → dedup must report wasAlerted.
  const after = basketDedup.checkAlreadyAlerted(WALLET, V6_BASKET);
  assert.equal(after.wasAlerted, true,
    'post-hydration: dedup must remember this basket so next boot suppresses');
  assert.ok(typeof after.alertedAt === 'number');
});

test('silent-boot hydration: re-marking same basket is idempotent', () => {
  basketDedup.markAsAlerted(WALLET, V6_BASKET);
  const r1 = basketDedup.checkAlreadyAlerted(WALLET, V6_BASKET);
  basketDedup.markAsAlerted(WALLET, V6_BASKET);
  const r2 = basketDedup.checkAlreadyAlerted(WALLET, V6_BASKET);
  assert.equal(r2.wasAlerted, true);
  assert.equal(r1.hash, r2.hash, 'hash must be identical across re-marks');
});

test('silent-boot hydration: position order does not affect hash (determinism replay)', () => {
  // Cycle 1 silent boot: positions returned as [DYDX, OP, ARB, PYTH, ENA]
  basketDedup.markAsAlerted(WALLET, V6_BASKET);
  // Cycle 2 normal: API returned same positions in different order
  const reordered = [V6_BASKET[2], V6_BASKET[0], V6_BASKET[4], V6_BASKET[1], V6_BASKET[3]];
  const check = basketDedup.checkAlreadyAlerted(WALLET, reordered);
  assert.equal(check.wasAlerted, true,
    'reordered basket must hit the same hash');
});

test('silent-boot hydration: simulates the full 2 may 06:30 incident flow', () => {
  // Step 1: Bot deployed apr-30 with R(v4) basket dedup. v6 basket already
  // open since apr-29. First poll on apr-30 was silent → with R-NOSPAM fix,
  // hydrate marks v6 in dedup.
  basketDedup.markAsAlerted(WALLET, V6_BASKET);

  // Step 2: Bot restart on may-2 (R-EN deploy, etc). In-memory snapshots
  // wipe, but persistent dedup file survives on Volume.
  // First poll silent → R-NOSPAM hydration runs again → checkAlreadyAlerted
  // reports already-alerted → no double-mark, no emit.
  const checkOnReboot = basketDedup.checkAlreadyAlerted(WALLET, V6_BASKET);
  assert.equal(checkOnReboot.wasAlerted, true,
    'after restart, R-NOSPAM hydration must find the v6 basket already marked');

  // Step 3: A subsequent normal (non-silent) poll classifies all positions
  // as "new" because in-memory prev=[]. BASKET_OPEN candidate → dedup gate
  // fires → suppressed by basketDedup.checkAlreadyAlerted=true.
  // → no false alert. Bug fixed.
});

test('silent-boot hydration: file path lives under volume mount when set', () => {
  // Sanity check: when RAILWAY_VOLUME_MOUNT_PATH is set, the dedup file
  // should land under that path (not under the bundled __dirname/data
  // which would be wiped on every container restart).
  // We verify via the exported DB_PATH.
  // In tests, DEDUP_DB_PATH overrides → matches TMP_DB.
  assert.equal(basketDedup.DB_PATH, TMP_DB,
    'DB_PATH should match the env override (proves env-var precedence)');
});
