// Unit tests for the loading document's intro schedule.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The schedule is derived from the timing numbers by the pure helper in
// electron/intro-timing.js, which shell.html drives its CSS delays from.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { INTRO_LINES, INTRO_TIMING, computeIntroSchedule } = require('../../electron/intro-timing');

test('the first line waits for the lockup to resolve, plus the beat after it', () => {
  const schedule = computeIntroSchedule(INTRO_TIMING, INTRO_LINES);
  assert.equal(
    schedule.lines[0].typedAt,
    INTRO_TIMING.lockupDelayMs + INTRO_TIMING.lockupFocusMs + INTRO_TIMING.travelGapMs,
  );
});

test('a line leaves after its last character has landed and been read', () => {
  const timing = {
    ...INTRO_TIMING,
    lockupDelayMs: 0,
    lockupFocusMs: 0,
    travelGapMs: 0,
    stepMs: 10,
    fadeMs: 100,
    holdMs: 1000,
  };
  const lines = [[{ text: 'abcd' }]];
  const schedule = computeIntroSchedule(timing, lines);
  // Four characters: the last starts 3 steps in and takes fadeMs to land.
  assert.equal(schedule.lines[0].fadesOutAt, 3 * 10 + 100 + 1000);
});

test('character counts run straight through emphasised runs', () => {
  const timing = { ...INTRO_TIMING, lockupDelayMs: 0, lockupFocusMs: 0, travelGapMs: 0, holdMs: 0 };
  const plain = computeIntroSchedule(timing, [[{ text: 'abcdef' }]]);
  const split = computeIntroSchedule(timing, [[{ text: 'ab' }, { text: 'cd', em: true }, { text: 'ef' }]]);
  assert.equal(split.lines[0].fadesOutAt, plain.lines[0].fadesOutAt);
});

test('each later line starts once the previous one is gone plus the gap', () => {
  const schedule = computeIntroSchedule(INTRO_TIMING, INTRO_LINES);
  assert.equal(
    schedule.lines[1].typedAt,
    schedule.lines[0].fadesOutAt + INTRO_TIMING.outMs + INTRO_TIMING.gapMs,
  );
});

test('the lockup travels once the last line is gone, and parks a travel later', () => {
  const schedule = computeIntroSchedule(INTRO_TIMING, INTRO_LINES);
  const last = schedule.lines[schedule.lines.length - 1];
  assert.equal(schedule.travelAt, last.fadesOutAt + INTRO_TIMING.outMs + INTRO_TIMING.travelGapMs);
  assert.equal(schedule.parkedAt, schedule.travelAt + INTRO_TIMING.travelMs);
});

test('the whole film stays under ten seconds', () => {
  // A hard ceiling on the wait a first launch adds in front of the app; a
  // third line or a longer hold must be weighed against it.
  const schedule = computeIntroSchedule(INTRO_TIMING, INTRO_LINES);
  assert.ok(schedule.parkedAt < 10000, `parked at ${schedule.parkedAt}ms`);
});
