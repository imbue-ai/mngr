// Unit tests for the cold-start landing-screen decision.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The decision logic
// is the pure ``decideStartupRoute`` helper, deliberately split out of main.js
// (which can't be required outside Electron) so it is testable here. The e2e
// Playwright suite launches the signed bundle against live auth state and
// can't isolate "onboarding incomplete + no workspaces", so this is the only
// place the precedence is verified.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { decideStartupRoute } = require('../../electron/startup-routing');

test('unauthenticated -> start regardless of other state', () => {
  assert.equal(
    decideStartupRoute({
      authenticated: false,
      isOnboardingComplete: true,
      workspaceCount: 5,
      restorableCount: 3,
      needsConsent: true,
    }),
    'start',
  );
});

test('unacknowledged consent notice -> consent, before create and restore', () => {
  assert.equal(
    decideStartupRoute({
      authenticated: true,
      isOnboardingComplete: true,
      workspaceCount: 0,
      restorableCount: 0,
      needsConsent: true,
    }),
    'consent',
  );
  // Consent outranks restore too: the notice is once-per-install and must
  // not be skippable by having restorable windows.
  assert.equal(
    decideStartupRoute({
      authenticated: true,
      isOnboardingComplete: true,
      workspaceCount: 3,
      restorableCount: 2,
      needsConsent: true,
    }),
    'consent',
  );
});

test('an install that never finished onboarding stays on start even when consent is unacknowledged', () => {
  assert.equal(
    decideStartupRoute({
      authenticated: true,
      isOnboardingComplete: false,
      workspaceCount: 0,
      restorableCount: 0,
      needsConsent: true,
    }),
    'start',
  );
});

test('onboarding incomplete with no workspaces and no saved windows -> start', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: false, workspaceCount: 0, restorableCount: 0 }),
    'start',
  );
});

test('onboarding incomplete with a stale non-workspace window -> start', () => {
  // Zero workspaces, but a leftover `/` home window survived restore-filtering
  // (restorableCount > 0). The start flow must still win, rather than
  // restoring the stale window and landing on the home page.
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: false, workspaceCount: 0, restorableCount: 1 }),
    'start',
  );
});

test('onboarding incomplete but workspaces exist, with saved windows -> restore', () => {
  // A workspace proves the install is past the start flow whatever the flag
  // says (the backend backfills the flag from exactly this observation).
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: false, workspaceCount: 2, restorableCount: 1 }),
    'restore',
  );
});

test('onboarding incomplete but workspaces exist, no saved windows -> create', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: false, workspaceCount: 2, restorableCount: 0 }),
    'create',
  );
});

test('onboarding complete with no workspaces -> create, never start', () => {
  // "I already have one (log in)" completes onboarding without creating
  // anything; the next launch must land on the home page's empty state.
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: true, workspaceCount: 0, restorableCount: 0 }),
    'create',
  );
});

test('onboarding complete with restorable windows -> restore', () => {
  assert.equal(
    decideStartupRoute({ authenticated: true, isOnboardingComplete: true, workspaceCount: 3, restorableCount: 2 }),
    'restore',
  );
});
