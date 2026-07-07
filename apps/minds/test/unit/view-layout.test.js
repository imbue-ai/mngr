// Unit tests for the bundle view-layout bounds math.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The bounds decision is the pure ``computeBundleViewBounds`` helper, split out
// of main.js (which can't be required outside Electron) so it is testable here.
// The interactive Electron layout itself is verified manually; this locks in
// the regime decisions that a future edit could silently break -- in
// particular the error-state modal overlay vs. the quitting-state collapse.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { computeBundleViewBounds } = require('../../electron/view-layout');

const W = 1200;
const H = 800;
const TITLEBAR = 38;
const INSET = 4;

function compute(overrides) {
  return computeBundleViewBounds({
    isErrorState: false,
    isLoadingState: false,
    isQuittingState: false,
    modalVisible: false,
    width: W,
    height: H,
    titlebarHeight: TITLEBAR,
    contentInset: INSET,
    ...overrides,
  });
}

const FULL = { x: 0, y: 0, width: W, height: H };
const COLLAPSED = { x: 0, y: 0, width: 0, height: 0 };

test('normal layout: chrome fills window, content is inset, modal overlays full window', () => {
  const b = compute({});
  assert.deepEqual(b.chrome, FULL);
  assert.deepEqual(b.content, {
    x: INSET,
    y: TITLEBAR,
    width: W - INSET * 2,
    height: H - TITLEBAR - INSET,
  });
  assert.deepEqual(b.modal, FULL);
});

test('error state with an open modal: modal overlays the full window (the fix)', () => {
  // The error takeover's "Report a bug" button opens the /help modal; it must
  // overlay the full window rather than collapse to 0x0 (which made it invisible).
  const b = compute({ isErrorState: true, modalVisible: true });
  assert.deepEqual(b.chrome, FULL);
  assert.deepEqual(b.content, COLLAPSED);
  assert.deepEqual(b.modal, FULL);
});

test('error state without an open modal: modal collapses', () => {
  const b = compute({ isErrorState: true, modalVisible: false });
  assert.deepEqual(b.chrome, FULL);
  assert.deepEqual(b.content, COLLAPSED);
  assert.deepEqual(b.modal, COLLAPSED);
});

test('quitting state collapses the modal even when modalVisible is true', () => {
  // The quitting flip hides the modal via setVisible(false) but leaves
  // modalVisible true so it can be restored on cancel. The overlay must be
  // gated on isErrorState, NOT modalVisible alone, or a stale modal would
  // overlay the quitting screen.
  const b = compute({ isQuittingState: true, modalVisible: true });
  assert.deepEqual(b.chrome, FULL);
  assert.deepEqual(b.content, COLLAPSED);
  assert.deepEqual(b.modal, COLLAPSED);
});

test('loading state collapses content and modal regardless of modalVisible', () => {
  const b = compute({ isLoadingState: true, modalVisible: true });
  assert.deepEqual(b.chrome, FULL);
  assert.deepEqual(b.content, COLLAPSED);
  assert.deepEqual(b.modal, COLLAPSED);
});
