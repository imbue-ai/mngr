'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { IframeLogBuffer } = require('./iframe-log-buffer');

// Snapshot the buffer's per-mind queue sizes by reading the internal state
// directly. Used only by these tests; the production class does not expose
// a helper for it (see CLAUDE.md: no test-only hooks in production code).
function queueSizes(buffer) {
  const out = {};
  for (const [mindId, entry] of buffer._queues.entries()) {
    out[mindId] = entry.records.length;
  }
  return out;
}

function makeRecorder() {
  /** @type {Array<{url: string, body: object}>} */
  const calls = [];
  /** @type {(url: string, init: object) => Promise<{ok: boolean, status: number}>} */
  const fetchFn = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return { ok: true, status: 200 };
  };
  return { calls, fetchFn };
}

function aRecord(overrides = {}) {
  return {
    level: 'info',
    message: 'hi',
    frame_url: 'http://agent-abc.localhost:8420/service/web/',
    service_name: 'web',
    mind_id: 'agent-abc',
    ...overrides,
  };
}

test('enqueue below threshold does not trigger a flush', () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn, flushAtSize: 3 });
  buffer.enqueue('agent-abc', 8420, aRecord());
  assert.equal(calls.length, 0);
  assert.deepEqual(queueSizes(buffer), { 'agent-abc': 1 });
});

test('enqueue at threshold auto-flushes', async () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn, flushAtSize: 2 });
  buffer.enqueue('agent-abc', 8420, aRecord({ message: 'a' }));
  const flushed = buffer.enqueue('agent-abc', 8420, aRecord({ message: 'b' }));
  assert.ok(flushed instanceof Promise);
  await flushed;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://agent-abc.localhost:8420/api/iframe-logs');
  assert.deepEqual(
    calls[0].body.records.map((/** @type {{message: string}} */ r) => r.message),
    ['a', 'b'],
  );
  assert.deepEqual(queueSizes(buffer), { 'agent-abc': 0 });
});

test('flushAll POSTs one batch per mind with the correct port', async () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn, flushAtSize: 100 });
  buffer.enqueue('agent-abc', 8420, aRecord({ message: 'from abc' }));
  buffer.enqueue('agent-xyz', 8421, aRecord({ message: 'from xyz', mind_id: 'agent-xyz' }));
  await buffer.flushAll();
  assert.equal(calls.length, 2);
  const byUrl = new Map(calls.map((c) => [c.url, c.body]));
  assert.ok(byUrl.has('http://agent-abc.localhost:8420/api/iframe-logs'));
  assert.ok(byUrl.has('http://agent-xyz.localhost:8421/api/iframe-logs'));
});

test('overflow drops oldest records, preserves newest', async () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn, flushAtSize: 100, maxQueueSize: 3 });
  for (let i = 0; i < 5; i += 1) {
    buffer.enqueue('agent-abc', 8420, aRecord({ message: `msg-${i}` }));
  }
  assert.deepEqual(queueSizes(buffer), { 'agent-abc': 3 });
  await buffer.flush('agent-abc');
  assert.equal(calls.length, 1);
  const messages = calls[0].body.records.map((/** @type {{message: string}} */ r) => r.message);
  assert.deepEqual(messages, ['msg-2', 'msg-3', 'msg-4']);
});

test('empty flush is a no-op', async () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn });
  await buffer.flush('agent-abc');
  await buffer.flushAll();
  assert.equal(calls.length, 0);
});

test('POST failures do not crash and surface via onError', async () => {
  /** @type {Array<{err: Error, mindId: string}>} */
  const errors = [];
  const fetchFn = async () => {
    throw new Error('network down');
  };
  const buffer = new IframeLogBuffer({
    fetchFn,
    flushAtSize: 100,
    onError: (err, mindId) => errors.push({ err, mindId }),
  });
  buffer.enqueue('agent-abc', 8420, aRecord());
  await buffer.flush('agent-abc');
  assert.equal(errors.length, 1);
  assert.equal(errors[0].mindId, 'agent-abc');
  assert.match(errors[0].err.message, /network down/);
});

test('non-OK HTTP response surfaces via onError', async () => {
  /** @type {Array<{err: Error, mindId: string}>} */
  const errors = [];
  const fetchFn = async () => ({ ok: false, status: 500 });
  const buffer = new IframeLogBuffer({
    fetchFn,
    flushAtSize: 100,
    onError: (err, mindId) => errors.push({ err, mindId }),
  });
  buffer.enqueue('agent-abc', 8420, aRecord());
  await buffer.flush('agent-abc');
  assert.equal(errors.length, 1);
  assert.match(errors[0].err.message, /HTTP 500/);
});

test('close blocks further enqueues', () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn });
  buffer.close();
  const result = buffer.enqueue('agent-abc', 8420, aRecord());
  assert.equal(result, null);
  assert.deepEqual(queueSizes(buffer), {});
  assert.equal(calls.length, 0);
});

test('port updates when the same mind is re-enqueued with a new port', async () => {
  const { calls, fetchFn } = makeRecorder();
  const buffer = new IframeLogBuffer({ fetchFn, flushAtSize: 100 });
  buffer.enqueue('agent-abc', 8420, aRecord({ message: 'one' }));
  buffer.enqueue('agent-abc', 9000, aRecord({ message: 'two' }));
  await buffer.flush('agent-abc');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://agent-abc.localhost:9000/api/iframe-logs');
});
