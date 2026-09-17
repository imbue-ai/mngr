'use strict';

// The loading document's first-launch intro, as numbers: what the two lines
// under the lockup say, how each line arrives and leaves, how long it is read,
// and when the lockup travels up to its parked place. Pure and electron-free
// so the derived schedule is unit-testable under plain node (see
// ../test/unit/intro-timing.test.js); shell.html loads it with a script tag
// and drives its CSS delays from the result.

/**
 * The lines the lockup carries, in order. Each is an array of runs so the
 * document can set the emphasised words in italic -- and so the emphasis can
 * be animated on its own, which is what the arrival is built around.
 */
const INTRO_LINES = [
  [{ text: 'Create an ' }, { text: 'intentional', em: true }, { text: ' life' }],
  [{ text: 'An ' }, { text: 'honest software', em: true }],
];

const INTRO_TIMING = {
  // The lockup resolves from blurred to sharp over this long, after this delay.
  lockupDelayMs: 100,
  lockupFocusMs: 2000,
  // A line arrives whole, on the lockup's own curve and at a length that
  // matches it: ease-out-circ leaves fast and then spends most of the second
  // creeping the last fraction, so the type reads as the same gesture as the
  // mark rather than as a different animation underneath it.
  fadeMs: 900,
  fadeEase: 'cubic-bezier(0, 0.55, 0.45, 1)',
  // The italics do what the lockup does -- start out of focus and resolve --
  // while the plain words only brighten, so it is the emphasis that arrives.
  emBlur: '0.5cqw',
  // A finished line is read for holdMs, then rises and fades out over outMs,
  // blurring as it goes so it leaves the way the mark arrived; the next line
  // waits gapMs of empty space before it starts.
  holdMs: 1000,
  outMs: 400,
  outBlur: '0.4cqw',
  gapMs: 500,
  // The beat after the last line is gone before the lockup sets off, and how
  // long its travel to the parked place takes.
  travelGapMs: 150,
  travelMs: 500,
};

/**
 * Walk the intro forward from the lockup's own resolve: each line arrives once
 * the previous one is gone plus the gap, lands a fade later, holds, and
 * leaves; then the lockup travels.
 *
 * @returns {{ lines: Array<{ shownAt: number, fadesOutAt: number }>, travelAt: number, parkedAt: number }}
 *   every instant in milliseconds from the document's load.
 */
function computeIntroSchedule(timing = INTRO_TIMING, lines = INTRO_LINES) {
  const firstLineAt = timing.lockupDelayMs + timing.lockupFocusMs + timing.travelGapMs;
  const schedule = [];
  // One entry per line; what a line says does not decide how long it takes.
  lines.forEach(() => {
    const previous = schedule[schedule.length - 1];
    const shownAt = previous ? previous.fadesOutAt + timing.outMs + timing.gapMs : firstLineAt;
    schedule.push({ shownAt, fadesOutAt: shownAt + timing.fadeMs + timing.holdMs });
  });
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
