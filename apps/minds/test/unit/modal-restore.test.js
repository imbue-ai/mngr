// Unit tests for the displaced-modal memory.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// modal-restore.js is plain node (no Electron), so it is testable directly.
// These lock in the three rules a dismissal depends on: a detour that displaced
// a modal hands back to it, handing back happens once, and a plain close
// forgets it so no stale reopen can fire later.

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  rememberDisplacedModal,
  forgetDisplacedModal,
  planDismissal,
} = require('../../electron/modal-restore.js');

const PANEL_URL = 'http://localhost:1234/workspace/agent-abc/options/modal?tab=share&x=305&y=5&h=28';

test('a dismissal hands back to the modal the detour displaced', () => {
  const state = {};
  rememberDisplacedModal(state, PANEL_URL);
  // The whole URL comes back: the shell uses it to work out which overlay it is
  // showing again once the detour ends.
  assert.deepEqual(planDismissal(state), { action: 'restore', url: PANEL_URL });
});

test('a dismissal closes outright when the detour displaced nothing', () => {
  const state = {};
  rememberDisplacedModal(state, null);
  assert.deepEqual(planDismissal(state), { action: 'close' });
});

test('a fresh state closes outright', () => {
  assert.deepEqual(planDismissal({}), { action: 'close' });
});

test('handing back happens once, so a second dismissal closes', () => {
  const state = {};
  rememberDisplacedModal(state, PANEL_URL);
  assert.equal(planDismissal(state).action, 'restore');
  // Without consuming the memory the panel would reopen every time anything
  // was dismissed from then on.
  assert.deepEqual(planDismissal(state), { action: 'close' });
});

test('a plain close forgets the displaced modal', () => {
  const state = {};
  rememberDisplacedModal(state, PANEL_URL);
  // main.js closes the modal from many places that are not dismissals
  // (navigation, workspace switches, teardown); each abandons the detour.
  forgetDisplacedModal(state);
  assert.deepEqual(planDismissal(state), { action: 'close' });
});

test('a later detour that displaces nothing clears an earlier memory', () => {
  const state = {};
  rememberDisplacedModal(state, PANEL_URL);
  forgetDisplacedModal(state);
  rememberDisplacedModal(state, undefined);
  assert.deepEqual(planDismissal(state), { action: 'close' });
});
