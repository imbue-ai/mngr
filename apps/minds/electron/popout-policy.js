'use strict';

// Pure decisions for pulled-out workspace windows (the pull-out-window spec):
// when a watched title-bar drag has left the chrome window far enough to pull
// the window out, where a popout opens, where it follows the cursor to, which
// main window a re-dock drag is over, and the frame a drop names. Kept free of any
// `electron` import so it can be unit-tested under plain node; main.js reads
// the cursor, the window bounds, and the displays, and acts on what these
// answer.

// The chrome's titlebar (and the popout's own bar) height; the workspace
// surface starts below it in every window.
const TITLEBAR_HEIGHT = 38;
// The smallest a popout can be, matching the workspace shell's own minimum
// window size so a window never shrinks on the way out.
const POPOUT_MIN_WIDTH = 320;
const POPOUT_MIN_HEIGHT = 240;
// How far beside the chrome window an "open" (no drag) popout lands.
const OPEN_OFFSET = 40;
// How far past the workspace surface the cursor travels, with a window's
// title bar held, before the window is pulled out.
const TEAR_OUT_DISTANCE = 48;

/**
 * Where a popout's window sits so the cursor holds it at the grab point.
 *
 * @param {{x: number, y: number}} cursor  The cursor in screen coordinates.
 * @param {{grabX: number, grabY: number}} grab  Where inside the window it is held.
 * @returns {{x: number, y: number}}
 */
function followPosition(cursor, grab) {
  return { x: wholePixel(cursor.x - grab.grabX), y: wholePixel(cursor.y - grab.grabY) };
}

// A whole pixel for setPosition, which takes int32s and refuses -0 (V8's
// IsInt32 excludes it). Math.round gives -0 for anything in [-0.5, 0], which a
// fractional grab offset (a HiDPI pointer) reaches whenever the window is held
// flush against the top or left edge of the leftmost display. Adding 0 turns
// -0 into +0 and leaves NaN alone, so a bad input still fails loudly.
function wholePixel(value) {
  return Math.round(value) + 0;
}

/**
 * The bounds a popout opens at.
 *
 * In `drag` mode the window is sized as the workspace drew it and placed so
 * the cursor holds it where the title bar was grabbed (the popout's own bar
 * takes the place of the workspace's, so the grab offset carries over). In
 * `open` mode it lands a little inside the source window's top-left corner,
 * kept within that window's display.
 *
 * @param {object} request
 * @param {'drag'|'open'} request.mode
 * @param {number} request.width
 * @param {number} request.height
 * @param {number} request.grabX
 * @param {number} request.grabY
 * @param {{x: number, y: number}} cursor
 * @param {{x: number, y: number, width: number, height: number}} sourceBounds  The source window.
 * @param {{x: number, y: number, width: number, height: number}} workArea     Its display's work area.
 * @returns {{x: number, y: number, width: number, height: number}}
 */
function popoutOpenBounds(request, cursor, sourceBounds, workArea) {
  const width = Math.max(POPOUT_MIN_WIDTH, Math.round(request.width));
  const height = Math.max(POPOUT_MIN_HEIGHT, Math.round(request.height));
  if (request.mode === 'drag') {
    const position = followPosition(cursor, request);
    return { x: position.x, y: position.y, width, height };
  }
  const x = Math.min(
    Math.max(sourceBounds.x + OPEN_OFFSET, workArea.x),
    Math.max(workArea.x, workArea.x + workArea.width - width),
  );
  const y = Math.min(
    Math.max(sourceBounds.y + OPEN_OFFSET, workArea.y),
    Math.max(workArea.y, workArea.y + workArea.height - height),
  );
  return { x, y, width, height };
}

/**
 * The workspace surface of a chrome window: its content bounds less the
 * titlebar strip at the top.
 */
function surfaceBounds(contentBounds) {
  return {
    x: contentBounds.x,
    y: contentBounds.y + TITLEBAR_HEIGHT,
    width: contentBounds.width,
    height: Math.max(0, contentBounds.height - TITLEBAR_HEIGHT),
  };
}

/**
 * How far past ``surface`` the cursor is, in screen pixels: the largest of
 * its distances beyond the four edges, 0 inside. Main samples this while a
 * title-bar drag is watched; the shell's own pointer events cannot answer it,
 * since they stop at the window's edge on some platforms.
 */
function overshootPastSurface(cursor, surface) {
  return Math.max(
    0,
    surface.x - cursor.x,
    cursor.x - (surface.x + surface.width),
    surface.y - cursor.y,
    cursor.y - (surface.y + surface.height),
  );
}

/** Whether a watched drag's cursor is far enough past the surface for the window to be out. */
function isTornOut(cursor, surface) {
  return overshootPastSurface(cursor, surface) >= TEAR_OUT_DISTANCE;
}

function containsPoint(bounds, point) {
  return (
    point.x >= bounds.x &&
    point.x < bounds.x + bounds.width &&
    point.y >= bounds.y &&
    point.y < bounds.y + bounds.height
  );
}

/**
 * Which main window a re-dock drag is over: the first candidate (callers pass
 * them most-recently-focused first) showing the popout's workspace whose
 * surface contains the cursor, or null.
 *
 * @param {{x: number, y: number}} cursor
 * @param {Array<{id: unknown, contentBounds: object, isSameWorkspace: boolean}>} candidates
 * @returns {unknown} The matching candidate's id, or null.
 */
function dropTargetFor(cursor, candidates) {
  for (const candidate of candidates) {
    if (!candidate.isSameWorkspace) continue;
    if (containsPoint(surfaceBounds(candidate.contentBounds), cursor)) return candidate.id;
  }
  return null;
}

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/**
 * The frame (fractions of the target's workspace surface, inside the unit
 * square) a dropped popout's content lands at: the popout's content bounds
 * relative to the surface, shrunk to fit when the popout is larger than it.
 * The workspace shell clamps again against its own backdrop, which is the
 * surface less its taskbar; the small difference is not worth a round trip.
 */
function redockFrame(popoutContentBounds, targetContentBounds) {
  const surface = surfaceBounds(targetContentBounds);
  if (surface.width <= 0 || surface.height <= 0) return { x: 0, y: 0, width: 1, height: 1 };
  const width = clamp(popoutContentBounds.width / surface.width, 0, 1);
  const height = clamp(popoutContentBounds.height / surface.height, 0, 1);
  const x = clamp((popoutContentBounds.x - surface.x) / surface.width, 0, 1 - width);
  const y = clamp((popoutContentBounds.y - surface.y) / surface.height, 0, 1 - height);
  return { x, y, width, height };
}

/**
 * Whether a popout's OS close must first ask its page to return the window
 * to the desktop. Not during a quit (the popout is restored next launch), and
 * not once the page already settled the window (it reattached, or the window
 * is no longer pulled out).
 */
function isReattachHandshakeNeeded({ isShuttingDown, isQuitSequenceRunning, isReattachSettled }) {
  return !isShuttingDown && !isQuitSequenceRunning && !isReattachSettled;
}

/**
 * Whether a window may be the target of a main-driven navigation (startup
 * routing, a notification click, a deeplink, an open-help ask). A popout
 * shows exactly one pulled-out window and is never navigated elsewhere.
 */
function isNavigationTarget(kind) {
  return kind !== 'popout';
}

module.exports = {
  TITLEBAR_HEIGHT,
  POPOUT_MIN_WIDTH,
  POPOUT_MIN_HEIGHT,
  TEAR_OUT_DISTANCE,
  followPosition,
  popoutOpenBounds,
  surfaceBounds,
  overshootPastSurface,
  isTornOut,
  dropTargetFor,
  redockFrame,
  isReattachHandshakeNeeded,
  isNavigationTarget,
};
