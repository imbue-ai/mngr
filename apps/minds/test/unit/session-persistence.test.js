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
const { shouldWriteSessionState, createDebouncedSaver } = require('../../electron/session-persistence');

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
