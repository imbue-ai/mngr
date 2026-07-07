'use strict';

// Pure decision logic for the desktop client's cold-start landing screen.
// Kept free of any `electron` imports so it can be unit-tested under plain
// node (see ../test/unit/startup-routing.test.js). main.js computes the
// inputs from the chrome SSE snapshot + saved window-state and acts on the
// returned route.

/**
 * Decide which screen the desktop client lands on at cold start.
 *
 * Returns one of:
 *   'welcome' -> the onboarding / sign-in splash (`/welcome`)
 *   'create'  -> the home / create-agent page (`/`)
 *   'restore' -> reopen the previous session's saved windows
 *
 * Precedence (first match wins):
 *   1. Not authenticated to the local backend -> welcome. Graceful fallback;
 *      the one-time login code should already have authenticated us.
 *   2. "Functionally empty": signed out of every account AND no workspaces to
 *      return to -> welcome. This holds even when stale window-state lingers
 *      from a previous session (e.g. a leftover home/`/` window left behind
 *      after a sign-out + workspace teardown). A non-workspace saved window
 *      must NOT suppress onboarding for a signed-out, workspace-less user --
 *      we want to nudge them to sign in again before using the app. (A bare
 *      `/` window survives restore-filtering because it isn't a workspace URL,
 *      so without this clause it would silently win over the welcome screen.)
 *   3. Nothing restorable -> the home/create page.
 *   4. Otherwise -> restore the saved windows.
 *
 * @param {object} state
 * @param {boolean} state.authenticated   Local backend session is authenticated.
 * @param {boolean} state.hasAccounts     Whether >=1 signed-in account exists.
 * @param {number}  state.workspaceCount  Number of existing workspaces.
 * @param {number}  state.restorableCount Saved windows that survived restore-filtering.
 * @returns {'welcome'|'create'|'restore'}
 */
function decideStartupRoute({ authenticated, hasAccounts, workspaceCount, restorableCount }) {
  if (!authenticated) return 'welcome';
  if (!hasAccounts && workspaceCount === 0) return 'welcome';
  if (restorableCount === 0) return 'create';
  return 'restore';
}

module.exports = { decideStartupRoute };
