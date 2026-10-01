// Unit tests for the popout side of a watched workspace title-bar drag.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The effects stand in for main's popout windows: a registry of open popouts
// per workspace window, so a test can assert how many popouts a window has
// after a gesture, not only which calls were made.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createTearOutWatch } = require('../../electron/tear-out-watch');

const REQUEST = { workspaceId: 'agent-5e1f', windowId: 'win-7a3c', width: 640, height: 480, grabX: 20, grabY: 10 };

function fakeWindows() {
  const open = [];
  const closed = [];
  const kept = [];
  const phases = [];
  let nextId = 1;
  const effects = {
    openPopout: (request) => {
      const popout = { id: nextId++, windowId: request.windowId };
      open.push(popout);
      return popout;
    },
    findPopout: (request) => open.find((popout) => popout.windowId === request.windowId) ?? null,
    closePopout: (popout, reason) => {
      const index = open.indexOf(popout);
      assert.notEqual(index, -1, `closed popout ${popout.id}, which is not open`);
      open.splice(index, 1);
      closed.push({ id: popout.id, reason });
    },
    keepPopout: (popout) => kept.push(popout.id),
    sendTearOut: (request, phase) => phases.push(`${request.windowId}:${phase}`),
  };
  return { effects, open, closed, kept, phases };
}

test('a drag out opens one popout and a return inside closes it, telling the shell each step', () => {
  const windows = fakeWindows();
  const watch = createTearOutWatch(REQUEST, windows.effects);
  watch.sample(false);
  watch.sample(true);
  watch.sample(true);
  assert.equal(windows.open.length, 1);
  assert.equal(watch.popout, windows.open[0]);
  watch.sample(false);
  assert.equal(windows.open.length, 0);
  assert.equal(watch.popout, null);
  assert.deepStrictEqual(windows.phases, ['win-7a3c:out', 'win-7a3c:in']);
});

test('tearing a window out again while its dropped-back popout lingers leaves exactly one popout', () => {
  const windows = fakeWindows();
  const first = createTearOutWatch(REQUEST, windows.effects);
  first.sample(true);
  first.end(true, false);
  // The popout was dropped back onto the desktop, and its close waits on the desktop's word; meanwhile the
  // window is dragged out again.
  const second = createTearOutWatch(REQUEST, windows.effects);
  second.sample(true);
  assert.equal(windows.open.length, 1);
  assert.equal(windows.open[0], second.popout);
  assert.deepStrictEqual(windows.closed, [{ id: 1, reason: 'replaced by a new tear-out' }]);
});

test('popouts of other windows of the workspace are left alone', () => {
  const windows = fakeWindows();
  const other = createTearOutWatch({ ...REQUEST, windowId: 'win-0b9d' }, windows.effects);
  other.sample(true);
  other.release();
  const watch = createTearOutWatch(REQUEST, windows.effects);
  watch.sample(true);
  assert.deepStrictEqual(
    windows.open.map((popout) => popout.windowId),
    ['win-0b9d', 'win-7a3c'],
  );
  assert.deepStrictEqual(windows.closed, []);
});

test('the release keeps a popout that is out, and says nothing when the drag never left', () => {
  const windows = fakeWindows();
  const inside = createTearOutWatch(REQUEST, windows.effects);
  inside.release();
  assert.deepStrictEqual(windows.phases, []);
  const out = createTearOutWatch(REQUEST, windows.effects);
  out.sample(true);
  out.release();
  assert.deepStrictEqual(windows.kept, [1]);
  assert.deepStrictEqual(windows.phases, ['win-7a3c:out', 'win-7a3c:released']);
  assert.equal(windows.open.length, 1);
});

test("the shell's release keeps a popout that is out, even one the shell had not heard of yet", () => {
  const windows = fakeWindows();
  const detached = createTearOutWatch(REQUEST, windows.effects);
  detached.sample(true);
  detached.end(true, false);
  // A fast release reaches the shell before "out" does, so it ends the drag as not detached; the popout stays,
  // and the shell hears "released", which it takes as the word on the drag it already ended.
  const early = createTearOutWatch({ ...REQUEST, windowId: 'win-0b9d' }, windows.effects);
  early.sample(true);
  early.end(false, false);
  assert.deepStrictEqual(windows.kept, [1, 2]);
  assert.deepStrictEqual(windows.closed, []);
  assert.deepStrictEqual(windows.phases, [
    'win-7a3c:out',
    'win-7a3c:released',
    'win-0b9d:out',
    'win-0b9d:released',
  ]);
});

test("the shell's cancel drops the popout, and a shell that does not say is taken at its isDetached", () => {
  const windows = fakeWindows();
  const cancelled = createTearOutWatch(REQUEST, windows.effects);
  cancelled.sample(true);
  cancelled.end(false, true);
  const older = createTearOutWatch({ ...REQUEST, windowId: 'win-0b9d' }, windows.effects);
  older.sample(true);
  older.end(false, null);
  const olderDetached = createTearOutWatch({ ...REQUEST, windowId: 'win-1e2f' }, windows.effects);
  olderDetached.sample(true);
  olderDetached.end(true, null);
  assert.deepStrictEqual(windows.closed, [
    { id: 1, reason: 'the drag was cancelled' },
    { id: 2, reason: 'the drag ended inside' },
  ]);
  assert.deepStrictEqual(
    windows.open.map((popout) => popout.windowId),
    ['win-1e2f'],
  );
});

test('a drag replaced before it ended keeps the popout it opened rather than leaving it following the cursor', () => {
  const windows = fakeWindows();
  const watch = createTearOutWatch(REQUEST, windows.effects);
  watch.sample(true);
  watch.abandon();
  assert.deepStrictEqual(windows.kept, [1]);
  assert.deepStrictEqual(windows.closed, []);
});

test('a popout that cannot open yet is not reported to the shell as out', () => {
  const windows = fakeWindows();
  const watch = createTearOutWatch(REQUEST, { ...windows.effects, openPopout: () => null });
  watch.sample(true);
  assert.deepStrictEqual(windows.phases, []);
  watch.sample(false);
  assert.deepStrictEqual(windows.phases, []);
});
