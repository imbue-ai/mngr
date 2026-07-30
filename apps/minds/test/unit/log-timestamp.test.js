// Unit tests for the line-timestamping helpers shared by minds.log and
// electron.log.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// log-timestamp.js is plain node (no Electron), so it is testable directly.
// These lock in the load-bearing behavior: every captured line gets a stamp,
// the payload survives byte-for-byte, and a line split across chunk boundaries
// is reassembled rather than stamped as a fragment (with the trailing partial
// line surfaced at exit instead of dropped).

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { formatTimestampedLine, createLineSplitter } = require('../../electron/log-timestamp');

// A fixed instant, so the formatted prefix is asserted exactly rather than by
// a shape-matching regex that a malformed stamp could still satisfy.
const FIXED_NOW = new Date('2026-07-20T20:12:12.987Z');

test('formatTimestampedLine prefixes an ISO-8601 UTC stamp and terminates the line', () => {
  const formatted = formatTimestampedLine('Waiting for discovery events', FIXED_NOW);
  // Exactly one trailing newline: one record per line, and a second newline
  // would fabricate a blank record.
  assert.equal(formatted, '2026-07-20T20:12:12.987Z Waiting for discovery events\n');
});

test('formatTimestampedLine stamps a blank line rather than dropping it', () => {
  assert.equal(formatTimestampedLine('', FIXED_NOW), '2026-07-20T20:12:12.987Z \n');
});

test('formatTimestampedLine leaves the payload byte-for-byte intact', () => {
  // Loguru console output carries ANSI colour codes and non-ASCII; mangling
  // either would stop the capture matching what the backend actually emitted.
  const payload = '\x1b[38;5;33mDEBUG\x1b[0m café — 你好';
  assert.equal(formatTimestampedLine(payload, FIXED_NOW), `2026-07-20T20:12:12.987Z ${payload}\n`);
});

test('formatTimestampedLine defaults to the current time when none is injected', () => {
  const before = Date.now();
  const formatted = formatTimestampedLine('now', undefined);
  const stamped = Date.parse(formatted.slice(0, formatted.indexOf(' ')));
  assert.ok(stamped >= before && stamped <= Date.now());
});

test('createLineSplitter returns only complete lines and holds the partial remainder', () => {
  const splitter = createLineSplitter();
  // The chunk boundary lands mid-line: the tail must be withheld, not emitted.
  assert.deepEqual(splitter.push('alpha\nbeta\ngam'), ['alpha', 'beta']);
  assert.deepEqual(splitter.push('ma\ndelta\n'), ['gamma', 'delta']);
  assert.equal(splitter.flush(), null);
});

test('createLineSplitter reassembles a line split across many chunks', () => {
  const splitter = createLineSplitter();
  assert.deepEqual(splitter.push('Trace'), []);
  assert.deepEqual(splitter.push('back ('), []);
  assert.deepEqual(splitter.push('most recent'), []);
  assert.deepEqual(splitter.push(' call last):\n'), ['Traceback (most recent call last):']);
});

test('createLineSplitter flush surfaces a newline-less trailing fragment exactly once', () => {
  const splitter = createLineSplitter();
  assert.deepEqual(splitter.push('partial traceback line'), []);
  // A backend dying mid-line still gets its last output into the log.
  assert.equal(splitter.flush(), 'partial traceback line');
  assert.equal(splitter.flush(), null);
});

test('createLineSplitter treats blank lines as real lines', () => {
  const splitter = createLineSplitter();
  assert.deepEqual(splitter.push('\n\n'), ['', '']);
});

test('createLineSplitter keeps carriage returns so CRLF output is not silently altered', () => {
  const splitter = createLineSplitter();
  assert.deepEqual(splitter.push('windows\r\n'), ['windows\r']);
});
