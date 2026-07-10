// Unit tests for the size-based log rotation helper.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// log-rotation.js is plain node (no Electron), so it is testable directly. These
// lock in the load-bearing behavior: rotation triggers at the size threshold,
// rotated files are gzipped and pruned to the retention count, and the timestamp
// suffix is fixed-width so a lexicographic sort is chronological.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const { createRotatingLogStream, rotationTimestamp, pruneRotated } = require('../../electron/log-rotation');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'log-rotation-test-'));
}

// Wait for the background gzip (streamed, async) of a just-rotated file to land.
function waitForGz(dir, baseName, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const poll = () => {
      const gz = fs.readdirSync(dir).filter((n) => n.startsWith(baseName + '.') && n.endsWith('.gz'));
      if (gz.length > 0) return resolve(gz);
      if (Date.now() >= deadline) return reject(new Error('gzip did not appear in time'));
      setTimeout(poll, 20);
    };
    poll();
  });
}

// The rotating stream is async (fs.WriteStream), so a just-written line reaches
// disk on a later tick. Poll the file until it equals the expected content.
function waitForFileContent(filePath, expected, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const poll = () => {
      let content = '';
      try {
        content = fs.readFileSync(filePath, 'utf8');
      } catch {
        content = '';
      }
      if (content === expected) return resolve(content);
      if (Date.now() >= deadline) return reject(new Error(`file never became ${JSON.stringify(expected)} (was ${JSON.stringify(content)})`));
      setTimeout(poll, 20);
    };
    poll();
  });
}

test('rotationTimestamp is a fixed-width, chronologically-sortable UTC string', () => {
  const earlier = rotationTimestamp(new Date('2025-01-02T03:04:05.006Z'));
  const later = rotationTimestamp(new Date('2025-01-02T03:04:05.007Z'));
  assert.equal(earlier, '20250102030405006');
  assert.equal(earlier.length, 17);
  assert.ok(earlier < later, 'later timestamp must sort after the earlier one');
});

test('writes below the threshold do not rotate', async () => {
  const dir = tempDir();
  const filePath = path.join(dir, 'electron.log');
  const stream = createRotatingLogStream({ filePath, maxSizeBytes: 1000, maxRotatedCount: 5 });
  stream.write('a'.repeat(100) + '\n');
  stream.write('b'.repeat(100) + '\n');
  await stream.end(); // resolves once the async stream has flushed

  assert.equal(fs.readdirSync(dir).length, 1, 'no rotation expected');
  assert.ok(fs.readFileSync(filePath, 'utf8').includes('aaaa'));
});

test('crossing the threshold rotates, gzips the old file, and starts a fresh one', async () => {
  const dir = tempDir();
  const filePath = path.join(dir, 'electron.log');
  const stream = createRotatingLogStream({ filePath, maxSizeBytes: 200, maxRotatedCount: 5 });
  // The first write pushes tracked size past the threshold; the SECOND write sees
  // the over-size file and triggers rotation, then is replayed into the fresh file.
  stream.write('x'.repeat(250) + '\n');
  stream.write('fresh-line\n');

  const gz = await waitForGz(dir, 'electron.log');
  assert.equal(gz.length, 1);
  // The rotated (gzipped) file holds the pre-rotation content...
  const rotatedContent = zlib.gunzipSync(fs.readFileSync(path.join(dir, gz[0]))).toString();
  assert.ok(rotatedContent.includes('xxxx'));
  // ...and the fresh file eventually holds only what was written after rotating.
  const currentContent = await waitForFileContent(filePath, 'fresh-line\n');
  assert.equal(currentContent, 'fresh-line\n');
  await stream.end();
});

test('pruneRotated keeps only the newest N gzipped rotations', () => {
  const dir = tempDir();
  // Fixed-width timestamps => lexicographic order == chronological order.
  const names = [
    'electron.log.20250101000000001.gz',
    'electron.log.20250101000000002.gz',
    'electron.log.20250101000000003.gz',
    'electron.log.20250101000000004.gz',
  ];
  for (const name of names) fs.writeFileSync(path.join(dir, name), 'x');
  // A non-matching sibling and the live file must be left untouched.
  fs.writeFileSync(path.join(dir, 'electron.log'), 'live');
  fs.writeFileSync(path.join(dir, 'minds.log.20250101000000001.gz'), 'other');

  pruneRotated(dir, 'electron.log', 2);

  const remaining = fs.readdirSync(dir).sort();
  assert.deepEqual(remaining, [
    'electron.log',
    'electron.log.20250101000000003.gz',
    'electron.log.20250101000000004.gz',
    'minds.log.20250101000000001.gz',
  ]);
});
