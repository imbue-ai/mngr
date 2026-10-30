// Unit tests for the main process's stdout/stderr 'error' listener.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// A write to the main process's own stdio reports failure as an 'error' event,
// and anything thrown from there is an uncaught exception that crashes the
// main process. These pin that no write failure is rethrown (ENOSPC included),
// that a broken pipe stays silent, and that any other failure is noted once per
// stream and not again.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { dropStdioWriteErrors } = require('../../electron/stdio-write-errors');

// Shaped like the errors node:fs raises for a failed write.
function writeError(code, description) {
  return Object.assign(new Error(`${code}: ${description}, write`), { code, syscall: 'write' });
}

function guardedStreams() {
  const streams = { stdout: new EventEmitter(), stderr: new EventEmitter() };
  const warnings = [];
  dropStdioWriteErrors(streams, (message) => warnings.push(message));
  return { streams, warnings };
}

test('ENOSPC on stderr is not rethrown and is noted once', () => {
  const { streams, warnings } = guardedStreams();
  assert.doesNotThrow(() => streams.stderr.emit('error', writeError('ENOSPC', 'no space left on device')));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /stderr failed/);
  assert.match(warnings[0], /ENOSPC/);
});

test('later failures on the same stream are dropped without another note', () => {
  const { streams, warnings } = guardedStreams();
  streams.stderr.emit('error', writeError('ENOSPC', 'no space left on device'));
  assert.doesNotThrow(() => streams.stderr.emit('error', writeError('EIO', 'i/o error')));
  assert.doesNotThrow(() => streams.stderr.emit('error', writeError('ENOSPC', 'no space left on device')));
  assert.equal(warnings.length, 1);
});

test('EPIPE is dropped silently', () => {
  const { streams, warnings } = guardedStreams();
  assert.doesNotThrow(() => streams.stdout.emit('error', writeError('EPIPE', 'broken pipe')));
  assert.doesNotThrow(() => streams.stderr.emit('error', writeError('EPIPE', 'broken pipe')));
  assert.deepEqual(warnings, []);
});

test('each stream gets its own note', () => {
  const { streams, warnings } = guardedStreams();
  streams.stderr.emit('error', writeError('ENOSPC', 'no space left on device'));
  streams.stdout.emit('error', writeError('EIO', 'i/o error'));
  assert.equal(warnings.length, 2);
  assert.match(warnings[0], /stderr failed/);
  assert.match(warnings[1], /stdout failed/);
});
