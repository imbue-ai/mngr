'use strict';

// Pure workspace-URL classification for the desktop shell. Kept free of any
// `electron` import so it can be unit-tested under plain node (see
// ../test/unit/surface-routing.test.js). Workspace content renders inside
// the SPA's sandboxed iframe (frontend/src/views/shell/WorkspaceFrame.ts
// owns that); main.js uses parseWorkspaceId only for shell bookkeeping --
// window titles, session persistence, notification routing, and the guard
// that blocks TOP-LEVEL navigations to workspace origins.

// Extract the workspace (host) id a URL identifies, or null. Two shapes count
// as "this URL IS a workspace":
//   - the workspace origins `[<service>.]host-<id>.localhost:PORT/...` (the
//     bare origin is the shell; service labels are that workspace's other
//     registered services, all keyed by the same host id)
//   - the auth-bridge `localhost:PORT/goto/<host-id>/` (the pending state
//     before the workspace-domain cookie is installed).
// The SPA's parseWorkspaceIdFromUrl (frontend/src/router.ts) mirrors these
// shapes (plus a few SPA-only ones); keep the two in sync.
function parseWorkspaceId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const hostMatch = parsed.hostname.match(/^(?:[a-z0-9_-]+\.)*(host-[a-f0-9]+)\.localhost$/i);
    if (hostMatch) return hostMatch[1];
    const pathMatch = parsed.pathname.match(/^\/goto\/(host-[a-f0-9]+)(?:\/|$)/i);
    return pathMatch ? pathMatch[1] : null;
  } catch {
    return null;
  }
}

// Extract the workspace id from the SPA's own /workspace/<id> route path --
// the shape notification deep links use (/workspace/<agent-id>?review=...).
// Distinct from parseWorkspaceId: these are chrome-page routes, not workspace
// origins, so the origin//goto matcher above cannot see them. Path-only on
// purpose (callers only consult it for URLs parseWorkspaceId rejected);
// workspace-SCOPED sub-screens like /workspace/<id>/settings do not count.
function parseSpaWorkspaceRouteId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/workspace\/((?:agent|host)-[a-f0-9]+)\/?$/i);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

module.exports = {
  parseWorkspaceId,
  parseSpaWorkspaceRouteId,
};
