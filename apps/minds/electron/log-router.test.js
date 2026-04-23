'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { LogRouter } = require('./log-router');

function makeFakeWriter() {
  /** @type {object[]} */
  const writes = [];
  return {
    writes,
    write: (record) => writes.push(record),
  };
}

function makeFakeBuffer() {
  /** @type {Array<{mindId: string, port: number, record: object}>} */
  const enqueues = [];
  return {
    enqueues,
    enqueue: (mindId, port, record) => {
      enqueues.push({ mindId, port, record });
      return null;
    },
    close: () => {},
    flushAll: async () => {},
  };
}

function makeConsoleDetails(frameUrl, overrides = {}) {
  return {
    frame: { url: frameUrl },
    level: 'info',
    message: 'hello',
    sourceId: 'script.js',
    lineNumber: 1,
    ...overrides,
  };
}

test('mind-destined records enqueue to the buffer once the port is known', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer, now: () => new Date('2026-04-23T00:00:00.000Z') });
  router.setBackendPort(8420);
  const frameUrl = 'http://agent-abc.localhost:8420/service/web/';
  router.handleConsoleMessage(makeConsoleDetails(frameUrl), 'content');
  assert.equal(writer.writes.length, 0, 'writer must not see mind records');
  assert.equal(buffer.enqueues.length, 1);
  const [call] = buffer.enqueues;
  assert.equal(call.mindId, 'agent-abc');
  assert.equal(call.port, 8420);
  assert.equal(call.record.mind_id, 'agent-abc');
  assert.equal(call.record.service_name, 'web');
  assert.equal(call.record.frame_url, frameUrl);
  assert.equal(call.record.client_timestamp, '2026-04-23T00:00:00.000Z');
});

test('mind-destined records queue when the port is not yet known', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.handleConsoleMessage(
    makeConsoleDetails('http://agent-abc.localhost:8420/service/web/'),
    'content',
  );
  assert.equal(writer.writes.length, 0, 'never write mind records locally even pre-port');
  assert.equal(buffer.enqueues.length, 0, 'buffer must not see records until port is known');
  assert.equal(router._pendingMindRecords.length, 1);
});

test('setBackendPort drains pending records into the buffer in order', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.handleConsoleMessage(
    makeConsoleDetails('http://agent-abc.localhost:8420/service/web/', { message: 'first' }),
    'content',
  );
  router.handleConsoleMessage(
    makeConsoleDetails('http://agent-def.localhost:8420/service/terminal/', { message: 'second' }),
    'content',
  );
  assert.equal(router._pendingMindRecords.length, 2);
  router.setBackendPort(9000);
  assert.equal(router._pendingMindRecords.length, 0);
  assert.equal(buffer.enqueues.length, 2);
  assert.deepEqual(
    buffer.enqueues.map((c) => ({ mindId: c.mindId, message: c.record.message, port: c.port })),
    [
      { mindId: 'agent-abc', message: 'first', port: 9000 },
      { mindId: 'agent-def', message: 'second', port: 9000 },
    ],
  );
});

test('pending queue bounded by maxPendingRecords, dropping oldest', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer, maxPendingRecords: 2 });
  for (let index = 0; index < 4; index += 1) {
    router.handleConsoleMessage(
      makeConsoleDetails('http://agent-abc.localhost:8420/service/web/', {
        message: `msg-${index}`,
      }),
      'content',
    );
  }
  assert.equal(router._pendingMindRecords.length, 2);
  router.setBackendPort(8420);
  assert.equal(buffer.enqueues.length, 2);
  assert.deepEqual(
    buffer.enqueues.map((c) => c.record.message),
    ['msg-2', 'msg-3'],
  );
});

test('local-destined records go to the writer with the classification source', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.setBackendPort(8420);
  router.handleConsoleMessage(
    makeConsoleDetails('http://localhost:8420/_chrome', { message: 'local' }),
    'chrome',
  );
  assert.equal(buffer.enqueues.length, 0, 'buffer must not see local records');
  assert.equal(writer.writes.length, 1);
  const written = writer.writes[0];
  assert.equal(written.source, 'electron/renderer/local/chrome');
  assert.equal(written.message, 'local');
  assert.equal(written.mind_id, null);
  assert.equal(written.service_name, null);
});

test('logMain writes to the writer with source electron/main', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.logMain('warning', 'boom');
  assert.deepEqual(writer.writes, [{ level: 'warning', source: 'electron/main', message: 'boom' }]);
});

test('close stops routing further records to either sink', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.setBackendPort(8420);
  router.close();
  router.logMain('info', 'ignored');
  router.handleConsoleMessage(
    makeConsoleDetails('http://localhost:8420/_chrome', { message: 'also ignored' }),
    'chrome',
  );
  router.handleConsoleMessage(
    makeConsoleDetails('http://agent-abc.localhost:8420/service/web/', { message: 'mind' }),
    'content',
  );
  assert.equal(writer.writes.length, 0);
  assert.equal(buffer.enqueues.length, 0);
});

test('pending mind records are dropped on close, not flushed to the local writer', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.handleConsoleMessage(
    makeConsoleDetails('http://agent-abc.localhost:8420/service/web/', { message: 'queued' }),
    'content',
  );
  assert.equal(router._pendingMindRecords.length, 1);
  router.close();
  assert.equal(router._pendingMindRecords.length, 0);
  assert.equal(writer.writes.length, 0, 'mind records must never reach the local writer');
});

test('malformed console details default to safe values without throwing', () => {
  const writer = makeFakeWriter();
  const buffer = makeFakeBuffer();
  const router = new LogRouter({ writer, buffer });
  router.setBackendPort(8420);
  // `details.frame.url` is missing -- classification must fall through to
  // 'unclassified' and be routed to the local writer.
  router.handleConsoleMessage({ level: 'info', message: 'x' }, 'content');
  assert.equal(writer.writes.length, 1);
  assert.equal(writer.writes[0].source, 'electron/renderer/unclassified');
});
