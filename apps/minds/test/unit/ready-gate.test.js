// Unit tests for the one-shot readiness gate.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The gate (electron/ready-gate.js) is split out of main.js so its
// timeout/idempotency semantics are testable outside Electron. It guards
// startup workspace-restore navigation on the mngr_forward preauth cookie
// being written; the properties that actually matter are: a waiter is released
// promptly once ready, a waiter never hangs when ready never fires, and
// signalling is idempotent.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createReadyGate } = require('../../electron/ready-gate');

test('waitUntilReady resolves once markReady fires, well before the timeout', async () => {
  const gate = createReadyGate();
  let resolved = false;
  const waiter = gate.waitUntilReady(10_000).then(() => {
    resolved = true;
  });
  assert.equal(resolved, false, 'should not resolve before markReady');
  gate.markReady();
  await waiter;
  assert.equal(resolved, true);
});

test('markReady before waitUntilReady resolves immediately', async () => {
  const gate = createReadyGate();
  gate.markReady();
  // A zero timeout would still resolve, so prove readiness (not the timer)
  // drives it by using a very long timeout that the test would otherwise wait.
  await gate.waitUntilReady(10_000);
});

test('waitUntilReady resolves via the timeout when markReady never fires', async () => {
  const gate = createReadyGate();
  const start = process.hrtime.bigint();
  await gate.waitUntilReady(20);
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  assert.ok(elapsedMs >= 15, `expected to wait out the ~20ms timeout, waited ${elapsedMs}ms`);
});

test('markReady is idempotent and releases every waiter', async () => {
  const gate = createReadyGate();
  const waiters = [gate.waitUntilReady(10_000), gate.waitUntilReady(10_000)];
  gate.markReady();
  gate.markReady(); // second call must not throw
  await Promise.all(waiters);
  // A fresh waiter after ready returns immediately.
  await gate.waitUntilReady(10_000);
});
