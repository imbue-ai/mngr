// Unit tests for the loading document's intro.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The intro is in two halves. The arrival is physics (electron/intro-pop.js)
// and has no schedule; the departure is derived from the timing numbers by the
// pure helper in electron/intro-timing.js, which shell.html drives its CSS
// delays from.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { INTRO_TIMING, computeDepartureSchedule } = require('../../electron/intro-timing');
const { createPopRig, ARRIVAL_PAD, TUNING } = require('../../electron/intro-pop');
const { LETTERS, BOX } = require('../../electron/intro-letters');
const { parsePath } = require('../../electron/intro-wobble');

test('the lockup sets off once the loader is gone, and parks a travel later', () => {
  const schedule = computeDepartureSchedule(INTRO_TIMING);
  assert.equal(schedule.travelAt, INTRO_TIMING.loaderFadeMs + INTRO_TIMING.travelGapMs);
  assert.equal(schedule.parkedAt, schedule.travelAt + INTRO_TIMING.travelMs);
});

test('the page settles once the mark has parked', () => {
  const schedule = computeDepartureSchedule(INTRO_TIMING);
  assert.equal(schedule.settledAt, schedule.parkedAt + INTRO_TIMING.settleMs);
});

test('a departure with no loader to clear sets off straight away', () => {
  // A launch that was ready before the wait became worth reporting has nothing
  // to fade out, so the mark must not sit through a fade with no subject.
  const schedule = computeDepartureSchedule({ ...INTRO_TIMING, loaderFadeMs: 0 });
  assert.equal(schedule.travelAt, INTRO_TIMING.travelGapMs);
  assert.equal(
    schedule.settledAt,
    computeDepartureSchedule(INTRO_TIMING).settledAt - INTRO_TIMING.loaderFadeMs,
  );
});

test('a loader that is shown is shown for longer than it takes to appear', () => {
  // The minimum has to outlast the fade, or the loader would still be arriving
  // when it is told to leave.
  assert.ok(
    INTRO_TIMING.loaderMinMs > INTRO_TIMING.loaderFadeMs,
    `${INTRO_TIMING.loaderMinMs}ms is not longer than the ${INTRO_TIMING.loaderFadeMs}ms fade`,
  );
});

test('the departure stays under three seconds', () => {
  // A hard ceiling on what the film adds AFTER the app is ready -- every
  // millisecond here is one the user waits with the app already up behind the
  // page.
  const schedule = computeDepartureSchedule(INTRO_TIMING);
  assert.ok(schedule.settledAt < 3000, `settled at ${schedule.settledAt}ms`);
});

test('the page is held empty for the lead before any letter lands', () => {
  // The window opens on the brand's colour and holds it for a beat. Stepped to
  // just before the lead is up, every letter must still be absent.
  const rig = createPopRig();
  for (let t = 0; t < TUNING.lead - 0.02; t += 1 / 120) rig.step(1 / 120);
  for (const letterFrame of rig.frame()) {
    assert.match(letterFrame.transform, /scale\(0 0\)/);
  }
  rig.step(0.05);
  assert.doesNotMatch(rig.frame()[0].transform, /scale\(0 0\)/, 'the first letter never arrived');
});

test('every letter arrives, and the word ends as the drawing itself', () => {
  // The rig has to come back to the Figma vector exactly: at rest it reports no
  // warp, which is what tells shell.html to restore the original path data.
  const rig = createPopRig();
  for (let t = 0; t < 10 && !rig.settled(); t += 1 / 60) rig.step(1 / 60);
  assert.ok(rig.settled(), 'the letters never settled');
  const frame = rig.frame();
  assert.equal(frame.length, LETTERS.length);
  for (const letterFrame of frame) {
    assert.equal(letterFrame.warp, null);
    assert.equal(letterFrame.transform, 'translate(0 0)');
  }
});

test('a letter that has not had its turn is absent, not full size', () => {
  // Its springs sit at zero with zero velocity, which is indistinguishable from
  // "finished" unless birth is checked first -- and a letter treated as
  // finished renders at full size before it has arrived.
  const rig = createPopRig();
  rig.step(TUNING.lead + 1 / 240);
  const frame = rig.frame();
  assert.match(frame[frame.length - 1].transform, /scale\(0 0\)/);
});

test('reduced motion lands the finished word without stepping the springs', () => {
  const rig = createPopRig();
  rig.settleAll();
  assert.ok(rig.settled());
  assert.equal(rig.clock, 0);
});

test('the arrival never reaches outside the room the lockup leaves it', () => {
  // The lockup's viewBox is the drawing's box grown by ARRIVAL_PAD, and a letter
  // that reaches past that is clipped on screen. Re-measured here rather than
  // trusted: every control point of every letter, warped and transformed, across
  // the whole run -- so re-tuning the physics fails this instead of quietly
  // cutting a corner off the mark.
  const drawings = LETTERS.map((letter) => parsePath(letter.d));
  const rig = createPopRig();
  const seen = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };

  for (let t = 0; t < 6 && !rig.settled(); t += 1 / 120) {
    rig.step(1 / 120);
    rig.frame().forEach((letterFrame, i) => {
      const parts = /translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+) ([-\d.]+)\)/.exec(
        letterFrame.transform,
      );
      for (const subpath of drawings[i]) {
        const points = [subpath.start];
        for (const curve of subpath.curves) {
          points.push([curve[0], curve[1]], [curve[2], curve[3]], [curve[4], curve[5]]);
        }
        for (let [x, y] of points) {
          if (letterFrame.warp) [x, y] = letterFrame.warp(x, y);
          if (parts) {
            const [cx, cy, sx, sy] = parts.slice(1).map(Number);
            x = cx + (x - cx) * sx;
            y = cy + (y - cy) * sy;
          }
          // The letters are drawn in the artwork's own space, which is the
          // viewBox's, so there is nothing to map through.
          seen.minX = Math.min(seen.minX, x);
          seen.minY = Math.min(seen.minY, y);
          seen.maxX = Math.max(seen.maxX, x);
          seen.maxY = Math.max(seen.maxY, y);
        }
      }
    });
  }

  assert.ok(seen.minX >= BOX.x - ARRIVAL_PAD, `reached ${(BOX.x - seen.minX).toFixed(1)} past the left`);
  assert.ok(seen.minY >= BOX.y - ARRIVAL_PAD, `reached ${(BOX.y - seen.minY).toFixed(1)} past the top`);
  assert.ok(
    seen.maxX <= BOX.x + BOX.w + ARRIVAL_PAD,
    `reached ${(seen.maxX - BOX.x - BOX.w).toFixed(1)} past the right`,
  );
  assert.ok(
    seen.maxY <= BOX.y + BOX.h + ARRIVAL_PAD,
    `reached ${(seen.maxY - BOX.y - BOX.h).toFixed(1)} past the bottom`,
  );
});
