'use strict';

// Unit tests for the one-main-window-per-workspace decisions
// (electron/window-policy.js). What is NOT covered here (main.js wiring):
// looking the windows up, focusing, opening and restoring them.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  decideWorkspaceWindowTarget,
  dedupeRestoreEntries,
  resolveDuplicateWorkspaceLanding,
} = require('../../electron/window-policy');

test('a workspace that already has a main window raises it, wherever the request came from', () => {
  const existing = { name: 'W window' };
  for (const source of [null, { name: 'X window' }, { name: 'blank window' }]) {
    for (const mayNavigateSource of [true, false]) {
      assert.equal(decideWorkspaceWindowTarget({ existing, source, mayNavigateSource }), 'focus-existing');
    }
  }
});

test('the window already showing the workspace handles the request itself', () => {
  const window = { name: 'W window' };
  assert.equal(
    decideWorkspaceWindowTarget({ existing: window, source: window, mayNavigateSource: false }),
    'navigate-source',
  );
});

test('with no window on the workspace, the asking window takes it only when it may', () => {
  const source = { name: 'X window' };
  assert.equal(decideWorkspaceWindowTarget({ existing: null, source, mayNavigateSource: true }), 'navigate-source');
  // Never taken over from outside: a banner for W does not move the window showing X.
  assert.equal(decideWorkspaceWindowTarget({ existing: null, source, mayNavigateSource: false }), 'open-new');
  assert.equal(decideWorkspaceWindowTarget({ existing: null, source: null, mayNavigateSource: false }), 'open-new');
});

function workspaceOfEntry(entry) {
  return entry.popout ? null : entry.workspace;
}

test('session restore keeps one main window per workspace, the most recently focused one', () => {
  const entries = [
    { workspace: 'agent-a', x: 10, popout: false },
    { workspace: null, x: 20, popout: false },
    { workspace: 'agent-b', x: 30, popout: false },
    { workspace: 'agent-a', x: 40, popout: false },
    { workspace: 'agent-a', x: 50, popout: true },
    { workspace: null, x: 60, popout: false },
  ];

  const kept = dedupeRestoreEntries(entries, workspaceOfEntry, (a, b) => a === b);

  // The later agent-a main window is dropped; its popout and every blank window stay.
  assert.deepEqual(kept.map((entry) => entry.x), [10, 20, 30, 50, 60]);
});

test('session restore treats both spellings of a workspace as one', () => {
  const aliases = new Map([['host-1', 'agent-1']]);
  const isSameWorkspace = (a, b) => a === b || aliases.get(a) === b || aliases.get(b) === a;
  const entries = [
    { workspace: 'host-1', x: 10, popout: false },
    { workspace: 'agent-1', x: 20, popout: false },
  ];

  assert.deepEqual(dedupeRestoreEntries(entries, workspaceOfEntry, isSameWorkspace).map((entry) => entry.x), [10]);
});

test('a window landing on a workspace another window holds steps back the way it came', () => {
  const landing = { route: '/workspace/agent-ab12', isArrivedByGoingBack: false, canGoBack: true, canGoForward: true };
  // A bare workspace needs nothing delivered: raising its window is the whole answer.
  assert.deepEqual(resolveDuplicateWorkspaceLanding(landing), { isRouteHandedOver: false, retreat: 'back' });
  // Arrived by the Back button: forward again is where it was.
  assert.equal(resolveDuplicateWorkspaceLanding({ ...landing, isArrivedByGoingBack: true }).retreat, 'forward');
  // Nowhere to step to: home rather than staying a second window on the workspace.
  assert.equal(resolveDuplicateWorkspaceLanding({ ...landing, canGoBack: false }).retreat, 'home');
  assert.equal(
    resolveDuplicateWorkspaceLanding({ ...landing, isArrivedByGoingBack: true, canGoForward: false }).retreat,
    'home',
  );
});

test('a landing that names more than the workspace hands that over to the window holding it', () => {
  for (const route of [
    '/workspace/agent-ab12?chat=agent-cd34',
    '/workspace/agent-ab12/options?tab=share',
    '/help?workspace=agent-ab12',
  ]) {
    const landing = { route, isArrivedByGoingBack: false, canGoBack: true, canGoForward: false };
    assert.equal(resolveDuplicateWorkspaceLanding(landing).isRouteHandedOver, true);
  }
});
