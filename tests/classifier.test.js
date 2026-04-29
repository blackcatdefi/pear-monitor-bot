'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isPartialClose,
  isFullClose,
  classifyCloseEvent,
  refineTrailingVsManual,
  trackPartialClose,
  getPartialHistory,
  clearPartialHistory,
  PARTIAL_THRESHOLD,
  _resetForTests,
} = require('../src/closeClassifier');

test('isPartialClose: prev=100 cur=50 → true', () => {
  assert.equal(isPartialClose({ size: 100 }, { size: 50 }), true);
});

test('isPartialClose: prev=100 cur=98 → false (just under threshold)', () => {
  assert.equal(isPartialClose({ size: 100 }, { size: 98 }), false);
});

test('isPartialClose: prev=100 cur=0 → false (full close, not partial)', () => {
  assert.equal(isPartialClose({ size: 100 }, { size: 0 }), false);
});

test('isPartialClose: prev=null → false', () => {
  assert.equal(isPartialClose(null, { size: 50 }), false);
});

test('isFullClose: prev=100 cur=null → true', () => {
  assert.equal(isFullClose({ size: 100 }, null), true);
});

test('isFullClose: prev=100 cur=0 → true', () => {
  assert.equal(isFullClose({ size: 100 }, { size: 0 }), true);
});

test('isFullClose: prev=100 cur=50 → false', () => {
  assert.equal(isFullClose({ size: 100 }, { size: 50 }), false);
});

test('classifyCloseEvent: TWAP partial slice → PARTIAL_CLOSE', () => {
  const r = classifyCloseEvent({ size: -1000 }, { size: -400 });
  assert.equal(r.type, 'PARTIAL_CLOSE');
  assert.equal(r.sizeReduction, 600);
});

test('classifyCloseEvent: full close → FULL_CLOSE', () => {
  assert.equal(classifyCloseEvent({ size: -1000 }, { size: 0 }).type, 'FULL_CLOSE');
});

test('classifyCloseEvent: still open same size → STILL_OPEN', () => {
  assert.equal(classifyCloseEvent({ size: -1000 }, { size: -1000 }).type, 'STILL_OPEN');
});

test('refineTrailingVsManual: TRAILING_OR_MANUAL + trailingStop → TRAILING_STOP', () => {
  assert.equal(
    refineTrailingVsManual('TRAILING_OR_MANUAL', { trailingStopPct: 5 }),
    'TRAILING_STOP'
  );
});

test('refineTrailingVsManual: TRAILING_OR_MANUAL + no trailing → MANUAL_CLOSE', () => {
  assert.equal(refineTrailingVsManual('TRAILING_OR_MANUAL', null), 'MANUAL_CLOSE');
});

test('refineTrailingVsManual: TAKE_PROFIT pass-through unchanged', () => {
  assert.equal(refineTrailingVsManual('TAKE_PROFIT', null), 'TAKE_PROFIT');
});

test('partial-close tracker stores and retrieves history', () => {
  _resetForTests();
  trackPartialClose('0xabc', 'BLUR', 500);
  trackPartialClose('0xabc', 'BLUR', 300);
  const h = getPartialHistory('0xabc', 'BLUR');
  assert.equal(h.length, 2);
  assert.equal(h[0].sizeReduction, 500);
});

test('partial-close tracker clears on demand', () => {
  _resetForTests();
  trackPartialClose('0xabc', 'BLUR', 500);
  clearPartialHistory('0xabc', 'BLUR');
  assert.deepEqual(getPartialHistory('0xabc', 'BLUR'), []);
});

test(`PARTIAL_THRESHOLD = ${PARTIAL_THRESHOLD} (sanity)`, () => {
  assert.ok(PARTIAL_THRESHOLD > 0 && PARTIAL_THRESHOLD < 1);
});
