// Unit tests for the cold-start landing-screen decision.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The decision logic
// is the pure ``decideStartupRoute`` helper, deliberately split out of main.js
// (which can't be required outside Electron) so it is testable here. The e2e
// Playwright suite launches the signed bundle against live auth state and
// can't isolate "signed out + no workspaces", so this is the only place the
// precedence is verified.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { decideStartupRoute } = require('../../electron/startup-routing');

test('unauthenticated -> welcome regardless of other state', () => {
  assert.equal(
    decideStartupRoute({ authenticated: false, hasAccounts: true, workspaceCount: 5, restorableCount: 3 }),
    'welcome',
  );
});

test('functionally empty (no accounts, no workspaces, no saved windows) -> welcome', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: false, workspaceCount: 0, restorableCount: 0 }),
    'welcome',
  );
});

test('functionally empty with a stale non-workspace window -> welcome (the fix)', () => {
  // Signed out + zero workspaces, but a leftover `/` home window survived
  // restore-filtering (restorableCount > 0). Onboarding must still win, rather
  // than restoring the stale window and landing on the create page.
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: false, workspaceCount: 0, restorableCount: 1 }),
    'welcome',
  );
});

test('signed out but workspaces exist, with saved windows -> restore (not empty)', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: false, workspaceCount: 2, restorableCount: 1 }),
    'restore',
  );
});

test('signed out but workspaces exist, no saved windows -> create (not empty, so no welcome)', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: false, workspaceCount: 2, restorableCount: 0 }),
    'create',
  );
});

test('has accounts, nothing to restore -> create', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: true, workspaceCount: 0, restorableCount: 0 }),
    'create',
  );
});

test('has accounts with restorable windows -> restore', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, hasAccounts: true, workspaceCount: 3, restorableCount: 2 }),
    'restore',
  );
});
