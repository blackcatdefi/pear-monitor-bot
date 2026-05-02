'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

// R-NOSPAM (2 may 2026): borrow alert dedup gate.
// Bug fixed: 09:10 + 09:11 UTC duplicate "🏦 HyperLend — Borrow Available!"
// alerts (1 minute apart, identical wallet/amount/HF). The legacy
// edge-trigger in monitor.js fired again because available oscillated
// around the $50 threshold. This gate suppresses re-emits within 30 min,
// <5% available delta, <0.05 HF delta, force-emits on HF<1.10 cross or
// >50% available delta.

// Each test gets its own temp DB to avoid cross-test pollution.
function setupGate() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rnospam-'));
  process.env.BORROW_ALERT_GATE_PATH = path.join(tmpDir, 'state.json');
  delete require.cache[require.resolve('../src/borrowAlertGate')];
  return require('../src/borrowAlertGate');
}

function teardown(gate) {
  try { gate._resetForTests(); } catch (_) {}
  delete process.env.BORROW_ALERT_GATE_PATH;
}

const W = '0xDDD0000000000000000000000000000000000001';

test('R-NOSPAM borrow gate: first alert always fires (no prior state)', () => {
  const gate = setupGate();
  const r = gate.shouldEmitBorrowAlert(W, { available: 174.25, healthFactor: 1.26 });
  assert.strictEqual(r.shouldEmit, true);
  assert.strictEqual(r.reason, 'FIRST_ALERT');
  teardown(gate);
});

test('R-NOSPAM borrow gate: duplicate within cooldown is suppressed', () => {
  const gate = setupGate();
  const now = Date.now();
  // First emit at t=0
  gate.markAlertEmitted(W, { available: 174.25, healthFactor: 1.26 }, now);
  // 1 minute later, same data → must suppress (cooldown 30 min)
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 174.25, healthFactor: 1.26 }, now + 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, false);
  assert.strictEqual(r.reason, 'COOLDOWN');
  teardown(gate);
});

test('R-NOSPAM borrow gate: <5% available delta suppressed (small change post-cooldown)', () => {
  const gate = setupGate();
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 100, healthFactor: 1.26 }, now);
  // 35 min later, $1 change (1%) → cooldown clear but delta too small
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 101, healthFactor: 1.26 }, now + 35 * 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, false);
  assert.strictEqual(r.reason, 'AVAILABLE_DELTA_TOO_SMALL');
  teardown(gate);
});

test('R-NOSPAM borrow gate: <0.05 HF delta suppressed', () => {
  const gate = setupGate();
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 100, healthFactor: 1.26 }, now);
  // 35 min later, available changed by $10 (10% — passes), but HF
  // changed by 0.02 (below 0.05 threshold) → suppress
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 110, healthFactor: 1.28 }, now + 35 * 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, false);
  assert.strictEqual(r.reason, 'HF_DELTA_TOO_SMALL');
  teardown(gate);
});

test('R-NOSPAM borrow gate: HF cross below 1.10 force-emits even within cooldown', () => {
  const gate = setupGate();
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 100, healthFactor: 1.15 }, now);
  // 1 minute later, HF dropped to 1.08 → CRITICAL, must emit despite cooldown
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 100, healthFactor: 1.08 }, now + 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, true);
  assert.strictEqual(r.reason, 'HF_CROSSED_CRITICAL');
  teardown(gate);
});

test('R-NOSPAM borrow gate: >50% available delta force-emits even within cooldown', () => {
  const gate = setupGate();
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 100, healthFactor: 1.26 }, now);
  // 1 minute later, available jumped to $200 (+100%) → force emit
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 200, healthFactor: 1.26 }, now + 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, true);
  assert.strictEqual(r.reason, 'AVAILABLE_DELTA_FORCE');
  teardown(gate);
});

test('R-NOSPAM borrow gate: emits cleanly when cooldown clear AND deltas material', () => {
  const gate = setupGate();
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 100, healthFactor: 1.26 }, now);
  // 35 min later, $20 increase (20%) AND HF moved 0.10 → emit
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 120, healthFactor: 1.36 }, now + 35 * 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, true);
  assert.strictEqual(r.reason, 'OK');
  teardown(gate);
});

test('R-NOSPAM borrow gate: state persists across module reload (Volume-style)', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rnospam-persist-'));
  process.env.BORROW_ALERT_GATE_PATH = path.join(tmpDir, 'state.json');

  // First instance: mark
  delete require.cache[require.resolve('../src/borrowAlertGate')];
  let gate = require('../src/borrowAlertGate');
  const now = Date.now();
  gate.markAlertEmitted(W, { available: 174.25, healthFactor: 1.26 }, now);

  // Simulate restart: reload module fresh — file still on disk
  delete require.cache[require.resolve('../src/borrowAlertGate')];
  gate = require('../src/borrowAlertGate');

  // Same alert 1 min later → must still suppress despite cold reload
  const r = gate.shouldEmitBorrowAlert(
    W, { available: 174.25, healthFactor: 1.26 }, now + 60 * 1000
  );
  assert.strictEqual(r.shouldEmit, false);
  assert.strictEqual(r.reason, 'COOLDOWN');

  teardown(gate);
});

test('R-NOSPAM borrow gate: replay of exact 09:10/09:11 incident is suppressed', () => {
  // Replay the actual production incident from 2 may 2026:
  //   09:10 UTC: HyperLend Borrow Available wallet=DDS, $174.25, HF=1.26
  //   09:11 UTC: HyperLend Borrow Available wallet=DDS, $174.25, HF=1.26
  // Second alert MUST be suppressed.
  const gate = setupGate();
  const t0910 = new Date('2026-05-02T09:10:00Z').getTime();
  const t0911 = new Date('2026-05-02T09:11:00Z').getTime();

  // First alert at 09:10 — fires (FIRST_ALERT)
  const a = gate.shouldEmitBorrowAlert(
    W, { available: 174.25, healthFactor: 1.26 }, t0910
  );
  assert.strictEqual(a.shouldEmit, true);
  gate.markAlertEmitted(W, { available: 174.25, healthFactor: 1.26 }, t0910);

  // Second alert at 09:11 — must be suppressed
  const b = gate.shouldEmitBorrowAlert(
    W, { available: 174.25, healthFactor: 1.26 }, t0911
  );
  assert.strictEqual(b.shouldEmit, false,
    'duplicate alert 1 minute apart MUST be suppressed (R-NOSPAM bug fix)');
  teardown(gate);
});
