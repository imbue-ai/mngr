'use strict';

// Pure decisions behind the one-main-window-per-workspace rule: which window a
// request to show a workspace lands in, where a window that landed on a
// workspace another window holds goes instead, and which saved windows a
// session restore reopens. Kept free of any `electron` import so it can be
// unit-tested under plain node; main.js looks the windows up and acts on the
// answer.
// Pulled-out windows are outside the rule: any number may show a workspace.

/**
 * Where a request to show a workspace lands.
 *
 * `existing` is the main window already showing the workspace, if any: it is
 * raised rather than a second one opened, unless it is the asking window
 * itself, which then shows the workspace on its own. `source` is the window
 * the request came from (null for one from outside the app: a banner, the
 * dock); it may take the workspace over only when `mayNavigateSource` -- a
 * navigation the user started in that window, or a window showing no
 * workspace -- and a new window opens otherwise.
 *
 * @param {object} request
 * @param {unknown|null} request.existing  The main window showing the workspace, or null.
 * @param {unknown|null} request.source  The window the request came from, or null.
 * @param {boolean} request.mayNavigateSource  Whether the source may be moved onto the workspace.
 * @returns {'focus-existing'|'navigate-source'|'open-new'}
 */
function decideWorkspaceWindowTarget({ existing, source, mayNavigateSource }) {
  if (existing) return existing === source ? 'navigate-source' : 'focus-existing';
  if (source && mayNavigateSource) return 'navigate-source';
  return 'open-new';
}

/**
 * A saved session's entries with at most one main window per workspace.
 *
 * Entries are saved most-recently-focused first, so the first entry for a
 * workspace is the most recent one and keeps its bounds; later main-window
 * entries for it are dropped. Entries that name no workspace (a home page)
 * and pulled-out windows are all kept.
 *
 * @param {object[]} entries  Persisted window entries, in save order.
 * @param {(entry: object) => string|null} mainWindowWorkspaceIdOf
 *   The workspace a main-window entry shows, or null for a pulled-out window
 *   or a page that shows none.
 * @param {(a: string, b: string) => boolean} isSameWorkspace
 *   Whether two workspace ids name the same workspace (they may be agent- or host-scoped).
 * @returns {object[]}
 */
function dedupeRestoreEntries(entries, mainWindowWorkspaceIdOf, isSameWorkspace) {
  const kept = [];
  const seenWorkspaceIds = [];
  for (const entry of entries) {
    const workspaceId = mainWindowWorkspaceIdOf(entry);
    if (workspaceId) {
      if (seenWorkspaceIds.some((seen) => isSameWorkspace(seen, workspaceId))) continue;
      seenWorkspaceIds.push(workspaceId);
    }
    kept.push(entry);
  }
  return kept;
}

/** Whether ``route`` names only a workspace: no query, no sub-screen. */
function isBareWorkspaceRoute(route) {
  return /^\/workspace\/[^/?#]+\/?$/.test(route);
}

/**
 * What happens when a main window lands on a workspace another main window
 * already holds, whichever path took it there (a history step back or forward
 * included): the holder is raised, and handed the route when it names more
 * than the bare workspace; the landing window steps back the way it came --
 * forward again when it arrived by going back -- or goes home when there is
 * nowhere to step to.
 *
 * @param {object} landing
 * @param {string} landing.route  The SPA route the window landed on (path and query).
 * @param {boolean} landing.isArrivedByGoingBack  The landing was a step back in the window's history.
 * @param {boolean} landing.canGoBack
 * @param {boolean} landing.canGoForward
 * @returns {{ isRouteHandedOver: boolean, retreat: 'back'|'forward'|'home' }}
 */
function resolveDuplicateWorkspaceLanding({ route, isArrivedByGoingBack, canGoBack, canGoForward }) {
  const isRouteHandedOver = !isBareWorkspaceRoute(route);
  if (isArrivedByGoingBack) return { isRouteHandedOver, retreat: canGoForward ? 'forward' : 'home' };
  return { isRouteHandedOver, retreat: canGoBack ? 'back' : 'home' };
}

module.exports = {
  decideWorkspaceWindowTarget,
  dedupeRestoreEntries,
  isBareWorkspaceRoute,
  resolveDuplicateWorkspaceLanding,
};
