// Unit tests for the window-state.json persistence helpers.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// session-persistence.js is plain node (no Electron), so it is testable
// directly. These lock in the two load-bearing behaviors main.js relies on:
// the debounced saver coalesces a burst of schedule() calls into a single
// write, and the empty-clobber guard refuses to overwrite a non-empty on-disk
// file with an empty snapshot (the teardown-race bug that dropped users on the
// create screen after an auto-update restart).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { shouldWriteSessionState, createDebouncedSaver, isSameSavedWindow } = require('../../electron/session-persistence');

// A deterministic stand-in for setTimeout/clearTimeout: records armed
// callbacks by id and fires them on demand, so debounce timing is exercised
// without real timers.
function makeFakeTimer() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimer(cb) {
      const id = nextId++;
      pending.set(id, cb);
      return id;
    },
    clearTimer(id) {
      pending.delete(id);
    },
    fireAll() {
      const callbacks = Array.from(pending.values());
      pending.clear();
      for (const cb of callbacks) cb();
    },
    pendingCount() {
      return pending.size;
    },
  };
}

test('shouldWriteSessionState permits any non-empty snapshot', () => {
  // A non-empty computed list is always the live truth -- write it regardless
  // of what is already on disk.
  assert.equal(shouldWriteSessionState({ computedWindowCount: 2, persistedWindowCount: 0 }), true);
  assert.equal(shouldWriteSessionState({ computedWindowCount: 1, persistedWindowCount: 3 }), true);
});

test('shouldWriteSessionState writes a genuine empty when nothing is persisted', () => {
  // Empty computed + empty/missing file: writing empty clobbers nothing, so it
  // is allowed (keeps the file consistent on a fresh install).
  assert.equal(shouldWriteSessionState({ computedWindowCount: 0, persistedWindowCount: 0 }), true);
});

test('shouldWriteSessionState rejects an empty snapshot over a non-empty file (teardown race)', () => {
  // The bug: a save computed an empty list while windows were being torn down
  // by a non-graceful quit, and it would have zeroed a good file. Skip it.
  assert.equal(shouldWriteSessionState({ computedWindowCount: 0, persistedWindowCount: 2 }), false);
});

test('isSameSavedWindow matches identical entries by value, not identity', () => {
  // The restore path uses this to skip re-applying the initial window's bounds
  // when the entry being restored is the same saved window onReady already
  // positioned it at. It must match by value so it survives a future refactor
  // that rebuilds the restorable list into fresh objects (the previous
  // reference-equality check would silently break and re-introduce the bug).
  const a = { url: '/goto/agent-1/', x: 10, y: 20, width: 800, height: 600, displayId: 1 };
  const b = { url: '/goto/agent-1/', x: 10, y: 20, width: 800, height: 600, displayId: 1 };
  assert.equal(isSameSavedWindow(a, b), true, 'distinct objects with equal fields are the same saved window');
  assert.equal(isSameSavedWindow(a, a), true, 'an entry equals itself');
});

test('isSameSavedWindow distinguishes entries that differ in url or geometry', () => {
  const base = { url: '/goto/agent-1/', x: 10, y: 20, width: 800, height: 600, displayId: 1 };
  // A differing url is a different saved window even at the same geometry (the
  // MRU workspace was filtered out and a different one now takes its place).
  assert.equal(isSameSavedWindow(base, { ...base, url: '/goto/agent-2/' }), false, 'different url');
  assert.equal(isSameSavedWindow(base, { ...base, x: 11 }), false, 'different x');
  assert.equal(isSameSavedWindow(base, { ...base, y: 21 }), false, 'different y');
  assert.equal(isSameSavedWindow(base, { ...base, width: 801 }), false, 'different width');
  assert.equal(isSameSavedWindow(base, { ...base, height: 601 }), false, 'different height');
  assert.equal(isSameSavedWindow(base, { ...base, displayId: 2 }), false, 'different display');
});

test('isSameSavedWindow treats a missing entry as not-the-same', () => {
  // The first save (or first restorable entry) may be absent; never claim a
  // match against undefined, so the caller falls back to applying bounds.
  const entry = { url: '/', x: 0, y: 0, width: 1200, height: 800, displayId: 1 };
  assert.equal(isSameSavedWindow(entry, undefined), false);
  assert.equal(isSameSavedWindow(undefined, entry), false);
  assert.equal(isSameSavedWindow(undefined, undefined), false);
});

test('createDebouncedSaver coalesces a burst into a single save', () => {
  let saves = 0;
  const timer = makeFakeTimer();
  const saver = createDebouncedSaver({
    save: () => { saves++; },
    delayMs: 1000,
    setTimer: timer.setTimer,
    clearTimer: timer.clearTimer,
  });

  saver.schedule();
  saver.schedule();
  saver.schedule();
  assert.equal(timer.pendingCount(), 1, 'a burst arms exactly one timer');
  assert.equal(saves, 0, 'nothing writes until the timer fires');

  timer.fireAll();
  assert.equal(saves, 1, 'the coalesced burst produced exactly one save');
  assert.equal(saver.isPending(), false, 'scheduler returns to idle after firing');
});

test('createDebouncedSaver re-arms for the next burst after firing', () => {
  let saves = 0;
  const timer = makeFakeTimer();
  const saver = createDebouncedSaver({
    save: () => { saves++; },
    delayMs: 1000,
    setTimer: timer.setTimer,
    clearTimer: timer.clearTimer,
  });

  saver.schedule();
  timer.fireAll();
  assert.equal(saves, 1);

  saver.schedule();
  assert.equal(saver.isPending(), true, 'a fresh schedule after firing arms a new timer');
  timer.fireAll();
  assert.equal(saves, 2, 'the second burst produced its own single save');
});

test('createDebouncedSaver cancel drops a pending save without writing', () => {
  let saves = 0;
  const timer = makeFakeTimer();
  const saver = createDebouncedSaver({
    save: () => { saves++; },
    delayMs: 1000,
    setTimer: timer.setTimer,
    clearTimer: timer.clearTimer,
  });

  saver.schedule();
  saver.cancel();
  assert.equal(saver.isPending(), false);
  timer.fireAll();
  assert.equal(saves, 0, 'a cancelled save never runs (used when a quit takes over)');
});

test('createDebouncedSaver flush writes a pending save now and is a no-op when idle', () => {
  let saves = 0;
  const timer = makeFakeTimer();
  const saver = createDebouncedSaver({
    save: () => { saves++; },
    delayMs: 1000,
    setTimer: timer.setTimer,
    clearTimer: timer.clearTimer,
  });

  saver.flush();
  assert.equal(saves, 0, 'flushing while idle writes nothing');

  saver.schedule();
  saver.flush();
  assert.equal(saves, 1, 'flushing a pending save writes it immediately');
  assert.equal(saver.isPending(), false, 'flush clears the pending timer');
});
