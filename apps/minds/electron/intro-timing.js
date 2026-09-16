'use strict';

// The loading document's first-launch intro, as numbers: what the two lines
// under the lockup say, how each character arrives, how long a line is read,
// and when the lockup travels up to its parked place. Pure and electron-free
// so the derived schedule is unit-testable under plain node (see
// ../test/unit/intro-timing.test.js); shell.html loads it with a script tag
// and drives its CSS delays from the result.

/**
 * The lines the lockup carries, in order. Each is an array of runs so the
 * document can set the emphasised words in italic; the per-character delays
 * count straight through a run boundary.
 */
const INTRO_LINES = [
  [{ text: 'Create an ' }, { text: 'intentional', em: true }, { text: ' life' }],
  [{ text: 'An ' }, { text: 'honest software', em: true }],
];

const INTRO_TIMING = {
  // The lockup resolves from blurred to sharp over this long, after this delay.
  lockupDelayMs: 100,
  lockupFocusMs: 2000,
  // A typed line: one character starts every stepMs and fades in over fadeMs.
  // Four characters are mid-fade at any moment, so the line rolls in as a
  // wave rather than as separate keystrokes.
  stepMs: 20,
  fadeMs: 80,
  // A finished line is read for holdMs, then rises and fades out over outMs,
  // and the next line waits gapMs of empty space before it starts.
  holdMs: 1000,
  outMs: 250,
  gapMs: 500,
  // The beat after the last line is gone before the lockup sets off, and how
  // long its travel to the parked place takes.
  travelGapMs: 150,
  travelMs: 500,
};

function countCharacters(runs) {
  return runs.reduce((count, run) => count + Array.from(run.text).length, 0);
}

/**
 * Walk the intro forward from the lockup's own resolve: each line arrives once
 * the previous one is gone plus the gap, lands when its last character has
 * finished fading, holds, and leaves; then the lockup travels.
 *
 * @returns {{ lines: Array<{ typedAt: number, fadesOutAt: number }>, travelAt: number, parkedAt: number }}
 *   every instant in milliseconds from the document's load.
 */
function computeIntroSchedule(timing = INTRO_TIMING, lines = INTRO_LINES) {
  const firstLineAt = timing.lockupDelayMs + timing.lockupFocusMs + timing.travelGapMs;
  const schedule = [];
  for (const runs of lines) {
    const previous = schedule[schedule.length - 1];
    const typedAt = previous ? previous.fadesOutAt + timing.outMs + timing.gapMs : firstLineAt;
    const landedAt = typedAt + (countCharacters(runs) - 1) * timing.stepMs + timing.fadeMs;
    schedule.push({ typedAt, fadesOutAt: landedAt + timing.holdMs });
  }
  const lastLine = schedule[schedule.length - 1];
  const travelAt = lastLine.fadesOutAt + timing.outMs + timing.travelGapMs;
  return { lines: schedule, travelAt, parkedAt: travelAt + timing.travelMs };
}

const introTiming = { INTRO_LINES, INTRO_TIMING, computeIntroSchedule };

// Loaded by shell.html with a plain script tag (the renderer has no require),
// and required by the node unit tests.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = introTiming;
}
if (typeof window !== 'undefined') {
  window.mindsIntroTiming = introTiming;
}
