'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { LogWriter } = require('./log-writer');

// Matches the rotation suffix produced by log-writer.js (20-digit timestamp,
// optional `.<tie>` numeric disambiguator). Mirrors the Python side's
// `ROTATED_JSONL_PATTERN` which is `^events\.jsonl\.\d+$` and therefore
// accepts both the primary suffix and a tiebreaker continuation.
const ROTATED_NAME_PATTERN = /^electron\.jsonl\.\d{20}(?:\.\d+)?$/;

function makeTmpDir(label) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `${label}-`));
}

function readLines(filePath) {
  return fs
    .readFileSync(filePath, 'utf8')
    .split('\n')
    .filter((line) => line.length > 0)
    .map((line) => JSON.parse(line));
}

function listRotatedSiblings(dir) {
  return fs
    .readdirSync(dir)
    .filter((name) => ROTATED_NAME_PATTERN.test(name))
    .sort();
}

test('write creates the file and appends a JSONL record with envelope', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  const writer = new LogWriter(file, { now: () => new Date('2026-04-23T00:00:00.000Z') });
  writer.write({
    level: 'info',
    message: 'hello',
    source: 'electron/main',
  });
  writer.close();

  const lines = readLines(file);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].level, 'info');
  assert.equal(lines[0].message, 'hello');
  assert.equal(lines[0].source, 'electron/main');
  assert.equal(lines[0].type, 'electron');
  assert.equal(lines[0].timestamp, '2026-04-23T00:00:00.000Z');
  assert.equal(typeof lines[0].pid, 'number');
});

test('successive writes append to the same file', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  const writer = new LogWriter(file);
  writer.write({ level: 'info', message: 'first', source: 'electron/main' });
  writer.write({ level: 'warn', message: 'second', source: 'electron/main' });
  writer.close();

  const lines = readLines(file);
  assert.deepEqual(
    lines.map((l) => l.message),
    ['first', 'second'],
  );
});

test('rotation: rename uses the 20-digit UTC timestamp suffix and active file restarts', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  const writer = new LogWriter(file, {
    maxSizeBytes: 64,
    now: () => new Date('2026-04-23T12:00:00.000Z'),
  });
  // First write is ~100 bytes and pushes the file over the cap.
  writer.write({
    level: 'info',
    message: 'a'.repeat(80),
    source: 'electron/main',
  });
  // Second write triggers the rotation (pre-write check).
  writer.write({
    level: 'info',
    message: 'short',
    source: 'electron/main',
  });
  writer.close();

  const rotated = listRotatedSiblings(dir);
  assert.equal(rotated.length, 1, 'exactly one rotated sibling exists');
  // Expected suffix for 2026-04-23T12:00:00.000Z: YYYYMMDDHHMMSS + ms*1000.
  assert.equal(rotated[0], 'electron.jsonl.20260423120000000000');
  assert.ok(fs.existsSync(file), 'active file exists');
  const rotatedLines = readLines(path.join(dir, rotated[0]));
  const currentLines = readLines(file);
  assert.equal(rotatedLines.length, 1);
  assert.equal(currentLines.length, 1);
  assert.equal(currentLines[0].message, 'short');
});

test('rotation: a collision on the same millisecond falls back to a numeric tiebreaker', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  // Pre-create the primary suffix for the fixed clock so the writer is forced
  // to use the tiebreaker branch.
  fs.writeFileSync(path.join(dir, 'electron.jsonl.20260423120000000000'), 'stale\n');
  const writer = new LogWriter(file, {
    maxSizeBytes: 32,
    now: () => new Date('2026-04-23T12:00:00.000Z'),
  });
  writer.write({ level: 'info', message: 'a'.repeat(40), source: 'electron/main' });
  writer.write({ level: 'info', message: 'tiny', source: 'electron/main' });
  writer.close();

  assert.equal(
    fs.readFileSync(path.join(dir, 'electron.jsonl.20260423120000000000'), 'utf8'),
    'stale\n',
  );
  assert.ok(fs.existsSync(path.join(dir, 'electron.jsonl.20260423120000000000.1')));
});

test('rotation: retention cap prunes the oldest siblings beyond maxRotatedCount', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  // Monotonic fake clock: each time the writer asks for `now`, advance by
  // one second so every rotation lands on a distinct suffix.
  let tick = 0;
  const writer = new LogWriter(file, {
    maxSizeBytes: 64,
    maxRotatedCount: 2,
    now: () => {
      tick += 1;
      return new Date(Date.UTC(2026, 0, 1, 0, 0, tick));
    },
  });
  // Four rotations: each pair is one large write that pushes over the cap
  // plus one short write that triggers the pre-write rotation check.
  for (let index = 0; index < 4; index += 1) {
    writer.write({ level: 'info', message: 'x'.repeat(80), source: 'electron/main' });
    writer.write({ level: 'info', message: 'short', source: 'electron/main' });
  }
  writer.close();

  const rotated = listRotatedSiblings(dir);
  assert.equal(
    rotated.length,
    2,
    `expected 2 retained rotations, found ${rotated.length}: ${rotated.join(', ')}`,
  );
});

test('close is idempotent and subsequent writes reopen the file', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  const writer = new LogWriter(file);
  writer.write({ level: 'info', message: 'one', source: 'electron/main' });
  writer.close();
  writer.close(); // should not throw
  writer.write({ level: 'info', message: 'two', source: 'electron/main' });
  writer.close();

  const lines = readLines(file);
  assert.equal(lines.length, 2);
});

test('parent directory is created automatically', () => {
  const dir = makeTmpDir('logwriter');
  const nested = path.join(dir, 'a', 'b', 'c');
  const file = path.join(nested, 'electron.jsonl');
  const writer = new LogWriter(file);
  writer.write({ level: 'info', message: 'hello', source: 'electron/main' });
  writer.close();
  assert.ok(fs.existsSync(file));
});
