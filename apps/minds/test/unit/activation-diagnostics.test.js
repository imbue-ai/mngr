// Unit tests for the activate / focus-relay log lines main.js emits.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  formatActivateStart,
  formatActivateEnd,
  formatFocusRelayStart,
  formatFocusRelayEnd,
} = require('../../electron/activation-diagnostics');

test('an activate with a live window records that no window is opened', () => {
  assert.equal(
    formatActivateStart({ hasLiveWindow: true, isShuttingDown: false, isQuitSequenceRunning: false, opensWindow: false }),
    '[activate] start (hasLiveWindow=true, isShuttingDown=false, isQuitSequenceRunning=false, opensWindow=false)',
  );
  assert.equal(formatActivateEnd({ openedWindow: false }), '[activate] end (openedWindow=false)');
});

test('an activate with no window records that one is opened', () => {
  assert.equal(
    formatActivateStart({ hasLiveWindow: false, isShuttingDown: false, isQuitSequenceRunning: false, opensWindow: true }),
    '[activate] start (hasLiveWindow=false, isShuttingDown=false, isQuitSequenceRunning=false, opensWindow=true)',
  );
  assert.equal(formatActivateEnd({ openedWindow: true }), '[activate] end (openedWindow=true)');
});

test('the focus relay lines carry the window id and its destroyed and popout state', () => {
  assert.equal(
    formatFocusRelayStart({ windowId: 3, isWindowDestroyed: false, isContentsDestroyed: true, isPopout: true }),
    '[focus] window 3 focused, relaying (windowDestroyed=false, contentsDestroyed=true, popout=true)',
  );
  assert.equal(formatFocusRelayEnd({ windowId: 3 }), '[focus] window 3 focus relayed');
});
