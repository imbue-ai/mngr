// Unit tests for the pulled-out window decisions (the pull-out-window spec).
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// Pure helpers split out of main.js (which can't be required outside
// Electron): where a popout opens, how it follows the cursor, which window a
// re-dock drag is over, and the frame a drop names.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  TITLEBAR_HEIGHT,
  POPOUT_MIN_WIDTH,
  POPOUT_MIN_HEIGHT,
  followPosition,
  popoutOpenBounds,
  overshootPastSurface,
  isTornOut,
  TEAR_OUT_DISTANCE,
  surfaceBounds,
  dropTargetFor,
  redockFrame,
  isReattachHandshakeNeeded,
  isNavigationTarget,
} = require('../../electron/popout-policy');

const WORK_AREA = { x: 0, y: 0, width: 1600, height: 900 };
const SOURCE = { x: 100, y: 80, width: 1200, height: 800 };

test('followPosition keeps the cursor at the grab point', () => {
  assert.deepEqual(followPosition({ x: 500, y: 300 }, { grabX: 40, grabY: 12 }), { x: 460, y: 288 });
  // Rounded to whole pixels: a window is never placed between them.
  assert.deepEqual(followPosition({ x: 500.6, y: 300.2 }, { grabX: 0, grabY: 0 }), { x: 501, y: 300 });
});

test('a dragged popout opens under the cursor at the size the workspace drew it', () => {
  const bounds = popoutOpenBounds(
    { mode: 'drag', width: 640, height: 480, grabX: 30, grabY: 10 },
    { x: 1000, y: 200 },
    SOURCE,
    WORK_AREA,
  );
  assert.deepEqual(bounds, { x: 970, y: 190, width: 640, height: 480 });
});

test('a popout is never smaller than the workspace shell lets a window be', () => {
  const bounds = popoutOpenBounds(
    { mode: 'drag', width: 10, height: 10, grabX: 0, grabY: 0 },
    { x: 0, y: 0 },
    SOURCE,
    WORK_AREA,
  );
  assert.equal(bounds.width, POPOUT_MIN_WIDTH);
  assert.equal(bounds.height, POPOUT_MIN_HEIGHT);
});

test('an opened (not dragged) popout lands a little inside the source window, kept on its display', () => {
  const bounds = popoutOpenBounds(
    { mode: 'open', width: 640, height: 480, grabX: 0, grabY: 0 },
    { x: 9999, y: 9999 },
    SOURCE,
    WORK_AREA,
  );
  assert.deepEqual(bounds, { x: 140, y: 120, width: 640, height: 480 });
  // A source window near the display's far edge does not push the popout off it.
  const edge = popoutOpenBounds(
    { mode: 'open', width: 640, height: 480, grabX: 0, grabY: 0 },
    { x: 0, y: 0 },
    { x: 1400, y: 700, width: 800, height: 600 },
    WORK_AREA,
  );
  assert.deepEqual(edge, { x: 960, y: 420, width: 640, height: 480 });
});

test('the workspace surface is the content less the titlebar strip', () => {
  assert.deepEqual(surfaceBounds({ x: 10, y: 20, width: 800, height: 600 }), {
    x: 10,
    y: 20 + TITLEBAR_HEIGHT,
    width: 800,
    height: 600 - TITLEBAR_HEIGHT,
  });
});

test('a re-dock drag targets the first same-workspace window whose surface holds the cursor', () => {
  const candidates = [
    { id: 'other', contentBounds: { x: 0, y: 0, width: 800, height: 600 }, isSameWorkspace: false },
    { id: 'mine', contentBounds: { x: 0, y: 0, width: 800, height: 600 }, isSameWorkspace: true },
    { id: 'mine-too', contentBounds: { x: 0, y: 0, width: 800, height: 600 }, isSameWorkspace: true },
  ];
  assert.equal(dropTargetFor({ x: 400, y: 300 }, candidates), 'mine');
  // Over the titlebar strip is not over the surface.
  assert.equal(dropTargetFor({ x: 400, y: TITLEBAR_HEIGHT - 1 }, candidates), null);
  assert.equal(dropTargetFor({ x: 900, y: 300 }, candidates), null);
  assert.equal(dropTargetFor({ x: 400, y: 300 }, []), null);
});

test('a drop names the popout content as fractions of the target surface, kept inside it', () => {
  const target = { x: 100, y: 100, width: 1000, height: 500 + TITLEBAR_HEIGHT };
  const frame = redockFrame({ x: 350, y: 100 + TITLEBAR_HEIGHT + 125, width: 500, height: 250 }, target);
  assert.deepEqual(frame, { x: 0.25, y: 0.25, width: 0.5, height: 0.5 });
  // Larger than the surface: full width, clamped into the square.
  const big = redockFrame({ x: -500, y: -500, width: 3000, height: 3000 }, target);
  assert.deepEqual(big, { x: 0, y: 0, width: 1, height: 1 });
  // Dropped partly past the right edge: slid back in.
  const slid = redockFrame({ x: 900, y: 100 + TITLEBAR_HEIGHT, width: 500, height: 250 }, target);
  assert.deepEqual(slid, { x: 0.5, y: 0, width: 0.5, height: 0.5 });
  // A degenerate target answers the whole surface rather than dividing by zero.
  assert.deepEqual(redockFrame({ x: 0, y: 0, width: 10, height: 10 }, { x: 0, y: 0, width: 0, height: 10 }), {
    x: 0,
    y: 0,
    width: 1,
    height: 1,
  });
});

test('an OS close asks the page to reattach unless the app is quitting or the page already settled it', () => {
  assert.equal(
    isReattachHandshakeNeeded({ isShuttingDown: false, isQuitSequenceRunning: false, isReattachSettled: false }),
    true,
  );
  assert.equal(
    isReattachHandshakeNeeded({ isShuttingDown: true, isQuitSequenceRunning: false, isReattachSettled: false }),
    false,
  );
  assert.equal(
    isReattachHandshakeNeeded({ isShuttingDown: false, isQuitSequenceRunning: true, isReattachSettled: false }),
    false,
  );
  assert.equal(
    isReattachHandshakeNeeded({ isShuttingDown: false, isQuitSequenceRunning: false, isReattachSettled: true }),
    false,
  );
});

test('popouts are never navigation targets; main windows are', () => {
  assert.equal(isNavigationTarget('main'), true);
  assert.equal(isNavigationTarget('popout'), false);
});

test('overshootPastSurface is the largest distance past an edge, 0 inside', () => {
  const surface = { x: 100, y: 138, width: 800, height: 600 };
  assert.strictEqual(overshootPastSurface({ x: 500, y: 400 }, surface), 0);
  assert.strictEqual(overshootPastSurface({ x: 950, y: 400 }, surface), 50);
  assert.strictEqual(overshootPastSurface({ x: 60, y: 400 }, surface), 40);
  // Above the surface counts from the surface's top, which sits under the chrome's titlebar.
  assert.strictEqual(overshootPastSurface({ x: 500, y: 100 }, surface), 38);
  assert.strictEqual(overshootPastSurface({ x: 500, y: 800 }, surface), 62);
  // A corner answers the larger overshoot.
  assert.strictEqual(overshootPastSurface({ x: 960, y: 760 }, surface), 60);
});

test('isTornOut turns on at the tear-out distance and not a pixel before', () => {
  const surface = { x: 100, y: 138, width: 800, height: 600 };
  assert.strictEqual(isTornOut({ x: 900 + TEAR_OUT_DISTANCE - 1, y: 400 }, surface), false);
  assert.strictEqual(isTornOut({ x: 900 + TEAR_OUT_DISTANCE, y: 400 }, surface), true);
  assert.strictEqual(isTornOut({ x: 500, y: 138 - TEAR_OUT_DISTANCE }, surface), true);
});
