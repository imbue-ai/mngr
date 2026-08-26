// Unit tests for the rolling renderer-console capture.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// console-capture.js takes its log dir as a parameter and touches no Electron
// API, so it is testable directly. These lock in the properties that keep the
// file bounded and readable: one record per console message however many lines
// that message spans, a per-message character cap so a single dumped object
// cannot blow up a record, size-based rotation on the same terms as the other
// logs in the folder, and history surviving a restart.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const capturePath = require.resolve('../../electron/console-capture');
const {
  DEFAULT_MAX_SIZE_BYTES,
  DEFAULT_MAX_ROTATED_COUNT,
} = require('../../electron/log-rotation');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'console-capture-test-'));
}

// Each test needs a module whose one-shot init has not run yet, so a fresh
// require is the only way to point the capture at its own log dir.
function freshCapture(logDir, bounds) {
  delete require.cache[capturePath];
  const capture = require(capturePath);
  capture.initConsoleCapture(logDir, bounds);
  return capture;
}

// The stream is async, so closing it is the deterministic point at which the
// file on disk is complete -- the same flush a report would read after.
async function readAfterClose(capture, logDir) {
  await capture.closeConsoleCapture();
  return fs.readFileSync(path.join(logDir, 'console-tail.log'), 'utf8');
}

function nonEmptyLines(contents) {
  return contents.split('\n').filter((line) => line.length > 0);
}

function rotationsOf(logDir) {
  return fs.readdirSync(logDir).filter((name) => name.startsWith('console-tail.log.'));
}

// Rotation renames synchronously but gzips in the background, so a rotation is
// briefly on disk uncompressed. Waiting for the compressed form is what lets the
// test assert the finished shape without weakening it to "either form will do".
async function waitForGzippedRotations(logDir) {
  const deadline = Date.now() + 3000;
  for (;;) {
    const rotations = rotationsOf(logDir);
    if (rotations.length > 0 && rotations.every((name) => name.endsWith('.gz'))) return rotations;
    if (Date.now() >= deadline) return rotations;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

test('a multi-line message stays one record, so a reader counts messages', async () => {
  const dir = tempDir();
  const capture = freshCapture(dir);
  capture.recordConsoleMessage({
    message: 'Error: boom\n  at a.js:1\n  at b.js:2',
    level: 'error',
    sourceId: 'a.js',
    lineNumber: 1,
  });
  const contents = await readAfterClose(capture, dir);
  assert.equal(nonEmptyLines(contents).length, 1);
  assert.match(contents, /\[console:ERROR\] Error: boom\\n {2}at a\.js:1\\n {2}at b\.js:2 \(a\.js:1\)/);
});

test('the console log rotates instead of growing without bound', async () => {
  const dir = tempDir();
  // A tiny bound stands in for the real 10MB so the test does not have to write
  // 10MB; what it proves -- that passing the cap rotates rather than appends
  // forever -- is the same behavior at either size.
  const capture = freshCapture(dir, { maxSizeBytes: 4096, maxRotatedCount: 3 });
  for (let i = 0; i < 400; i++) {
    capture.recordConsoleMessage({ message: `msg-${i}`.padEnd(100, '.'), level: 'log', sourceId: 's.js', lineNumber: i });
  }
  await capture.closeConsoleCapture();

  const rotations = await waitForGzippedRotations(dir);
  assert.ok(rotations.length > 0, `expected at least one rotation, got ${JSON.stringify(fs.readdirSync(dir))}`);
  assert.ok(rotations.length <= 3, `rotations were not pruned to the cap: ${JSON.stringify(rotations)}`);
  assert.ok(
    rotations.every((name) => name.endsWith('.gz')),
    `rotations must be gzipped like the other logs: ${JSON.stringify(rotations)}`,
  );
  // The live file is what a report reads, and it must not be the whole history.
  const liveSize = fs.statSync(path.join(dir, 'console-tail.log')).size;
  assert.ok(liveSize <= 4096 * 2, `live file was ${liveSize} bytes`);
});

test('the console log is capped on the same terms as the other logs in the folder', async () => {
  const dir = tempDir();
  // Production calls initConsoleCapture with no bounds, so the caps are
  // log-rotation.js's defaults -- the same ones logger.js gives electron.log and
  // the Python backend sink uses (10MB, 10 rotations). This is the requirement:
  // one capping rule for every log in the dir, not a bespoke one here.
  const capture = freshCapture(dir);
  // Well past any plausible bespoke cap but far short of the shared 10MB one, so
  // this fails if the console log is ever given a smaller cap of its own --
  // asserting the constants alone would not catch that.
  for (let i = 0; i < 2000; i++) {
    capture.recordConsoleMessage({ message: `msg-${i}`.padEnd(100, '.'), level: 'log', sourceId: 's.js', lineNumber: i });
  }
  const contents = await readAfterClose(capture, dir);

  assert.equal(DEFAULT_MAX_SIZE_BYTES, 10 * 1024 * 1024);
  assert.equal(DEFAULT_MAX_ROTATED_COUNT, 10);
  assert.ok(contents.length > 200_000, `expected the whole run in one file, got ${contents.length} bytes`);
  assert.deepEqual(rotationsOf(dir), [], 'a 200KB run must not rotate under the shared 10MB cap');
});

test('a restart continues the log instead of truncating it to the new session', async () => {
  const dir = tempDir();
  const first = freshCapture(dir);
  first.recordConsoleMessage({ message: 'before-restart', level: 'log', sourceId: 's.js', lineNumber: 1 });
  await first.closeConsoleCapture();

  const second = freshCapture(dir);
  second.recordConsoleMessage({ message: 'after-restart', level: 'log', sourceId: 's.js', lineNumber: 2 });
  const contents = await readAfterClose(second, dir);

  assert.match(contents, /before-restart/);
  assert.match(contents, /after-restart/);
});

test('an event with no string message is ignored rather than written as garbage', async () => {
  const dir = tempDir();
  const capture = freshCapture(dir);
  capture.recordConsoleMessage({ level: 'log' });
  capture.recordConsoleMessage(null);
  capture.recordConsoleMessage({ message: 12, level: 'log' });
  // The stream opens the file on init, so the file existing proves nothing --
  // that it is still empty is what proves the garbage was dropped.
  const contents = await readAfterClose(capture, dir);
  assert.equal(contents, '');
});

test('the rolling file is not the staged name a bug report attaches', () => {
  delete require.cache[capturePath];
  const capture = require(capturePath);
  // The staged copy is what a report uploads; if the rolling file shared that
  // name it would be attached regardless of the checkbox.
  assert.notEqual(capture.CONSOLE_TAIL_FILENAME, 'bug-report-console.log');
});
