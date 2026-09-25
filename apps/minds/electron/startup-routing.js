'use strict';

// Pure decision logic for the desktop client's cold-start landing screen,
// which is also where the app lands when it is reopened with no window open.
// Kept free of any `electron` imports so it can be unit-tested under plain
// node (see ../test/unit/startup-routing.test.js). main.js computes the
// inputs from the /ui/api/app-status probe + saved window-state and acts on
// the returned route.

/**
 * Decide which screen the desktop client lands on at cold start, or when it
 * is reopened (dock-icon click, second launch) with no window open.
 *
 * Returns one of:
 *   'start'   -> the first-run start flow (`/start`): the manifesto exchange
 *                and the first workspace's questions, asked as a chat
 *   'consent' -> the once-per-install error-reporting notice (`/consent`)
 *   'create'  -> the home / create-agent page (`/`)
 *   'restore' -> reopen the previous session's saved windows
 *
 * Precedence (first match wins):
 *   1. Not authenticated to the local backend -> start. Graceful fallback;
 *      the one-time login code should already have authenticated us.
 *   2. Onboarding not complete AND no workspaces exist -> start. Onboarding
 *      is complete once the install has started creating a workspace or
 *      signed in from the start flow (the backend owns the flag); an install
 *      that already has workspaces is past it whatever the flag says. This
 *      holds even when stale window-state lingers from a previous session
 *      (e.g. a leftover home/`/` window after a workspace teardown): a
 *      non-workspace saved window must NOT suppress the start flow for an
 *      install that has never made a workspace. (A bare `/` window survives
 *      restore-filtering because it isn't a workspace URL, so without this
 *      clause it would silently win over the start flow.)
 *   3. The error-reporting notice was never acknowledged -> consent. Sits
 *      after the start branches and before the landing content, matching
 *      the legacy server-side gate. ConsentPage's accept action records the
 *      acknowledgement (POST /ui/api/onboarding/consent) and lands home, so
 *      the route never recurs.
 *   4. Nothing restorable -> the home/create page.
 *   5. Otherwise -> restore the saved windows.
 *
 * @param {object} state
 * @param {boolean} state.authenticated          Local backend session is authenticated.
 * @param {boolean} state.isOnboardingComplete   The install is past the first-run start flow.
 * @param {number}  state.workspaceCount         Number of existing workspaces.
 * @param {number}  state.restorableCount        Saved windows that survived restore-filtering.
 * @param {boolean} state.needsConsent           The error-reporting notice is unacknowledged.
 * @returns {'start'|'consent'|'create'|'restore'}
 */
function decideStartupRoute({ authenticated, isOnboardingComplete, workspaceCount, restorableCount, needsConsent }) {
  if (!authenticated) return 'start';
  if (!isOnboardingComplete && workspaceCount === 0) return 'start';
  if (needsConsent) return 'consent';
  if (restorableCount === 0) return 'create';
  return 'restore';
}

module.exports = { decideStartupRoute };
