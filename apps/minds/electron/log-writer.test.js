'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { LogWriter } = require('./log-writer');

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

test('rotation: when size exceeds cap, current file renames to .1 and next write starts fresh', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  const writer = new LogWriter(file, { maxSizeBytes: 64 });
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

  assert.ok(fs.existsSync(path.join(dir, 'electron.jsonl.1')), 'rotated file exists');
  assert.ok(fs.existsSync(file), 'active file exists');
  const rotated = readLines(path.join(dir, 'electron.jsonl.1'));
  const current = readLines(file);
  assert.equal(rotated.length, 1);
  assert.equal(current.length, 1);
  assert.equal(current[0].message, 'short');
});

test('rotation finds next free numeric suffix when .1 already exists', () => {
  const dir = makeTmpDir('logwriter');
  const file = path.join(dir, 'electron.jsonl');
  fs.writeFileSync(path.join(dir, 'electron.jsonl.1'), 'stale\n');
  const writer = new LogWriter(file, { maxSizeBytes: 32 });
  writer.write({ level: 'info', message: 'a'.repeat(40), source: 'electron/main' });
  writer.write({ level: 'info', message: 'tiny', source: 'electron/main' });
  writer.close();

  assert.equal(fs.readFileSync(path.join(dir, 'electron.jsonl.1'), 'utf8'), 'stale\n');
  assert.ok(fs.existsSync(path.join(dir, 'electron.jsonl.2')));
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
