'use strict';

// Pure URL classification for the desktop client's two content surfaces.
// Kept free of any `electron` import so it can be unit-tested under plain node
// (see ../test/unit/surface-routing.test.js). main.js requires these helpers to
// route each navigation to the right WebContentsView: an untrusted agent URL to
// the content view, a trusted local page to the chrome view.

// Extract the workspace (agent) id a URL identifies, or null. Two shapes count
// as "this URL IS a workspace":
//   - the final workspace subdomain `agent-<id>.localhost:PORT/...`
//   - the auth-bridge `localhost:PORT/goto/<agent-id>/` (the pending state
//     before the subdomain cookie is installed). Recognising it lets
//     findBundleForWorkspace de-dupe clicks during the redirect window.
function parseWorkspaceId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const hostMatch = parsed.hostname.match(/^(agent-[a-f0-9]+)\.localhost$/i);
    if (hostMatch) return hostMatch[1];
    const pathMatch = parsed.pathname.match(/^\/goto\/(agent-[a-f0-9]+)(?:\/|$)/i);
    return pathMatch ? pathMatch[1] : null;
  } catch {
    return null;
  }
}

// Wider than ``parseWorkspaceId`` -- also recognises the workspace-scoped
// minds-backend routes (``/workspace/<id>/settings``, ``/workspace/<id>/
// associate``, ``/sharing/<id>/<service>``, ``/destroying/<id>``,
// ``/agents/<id>/recovery``). Used ONLY to decide which workspace's accent
// should tint the titlebar; deliberately NOT fed into
// ``bundle.currentWorkspaceId`` / ``findBundleForWorkspace`` because those drive
// workspace uniqueness, and we want the user to be able to open
// ``/workspace/X/settings`` in one window while another window holds the actual
// workspace X.
//
// Returns null for every non-workspace minds screen (Home, Create, accounts,
// inbox, auth, ...). That null is load-bearing: the navigation handlers feed it
// straight into ``updateBundleAccentAgentId``, so leaving a workspace-scoped
// screen clears the accent back to the neutral chrome rather than stranding the
// previous workspace's color on a general screen.
function parseAccentSourceAgentId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const hostMatch = parsed.hostname.match(/^(agent-[a-f0-9]+)\.localhost$/i);
    if (hostMatch) return hostMatch[1];
    const pathMatch =
      parsed.pathname.match(/^\/(?:goto|workspace|sharing)\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      parsed.pathname.match(/^\/destroying\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      parsed.pathname.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
    return pathMatch ? pathMatch[1] : null;
  } catch {
    return null;
  }
}

// The two content surfaces. A URL that identifies a workspace (a
// `agent-<id>.localhost` subdomain or the `/goto/<id>/` auth-bridge) is
// untrusted foreign agent content and belongs on the CONTENT surface
// (contentView, caged relay preload). Every other in-app URL is a trusted
// local/native page (Landing, Create, Settings, the workspace-scoped settings /
// sharing / destroying / recovery screens, ...) and belongs on the CHROME
// surface (chromeView, full preload, which renders the titlebar + the page
// itself). The workspace-scoped local screens still TINT the titlebar via
// parseAccentSourceAgentId, but they render on the chrome surface -- only the
// workspace content itself is agent content.
const SURFACE_CONTENT = 'content';
const SURFACE_CHROME = 'chrome';

function selectSurfaceForUrl(url) {
  return parseWorkspaceId(url) ? SURFACE_CONTENT : SURFACE_CHROME;
}

// The chrome surface's HUB pages, swappable in-place inside the persistent
// chrome shell document (fetch-and-swap of #local-page-root) so the titlebar
// never rebuilds and navigation between them is instant. Deliberately a small
// allowlist: transitional / live-machinery pages (welcome, creating,
// destroying, recovery, auth, help, the full sharing page) do FULL navigations
// so their timers, pollers, and SSE subscriptions get a real document
// lifecycle. chrome.js mirrors this list (it cannot require this module);
// keep the two in sync.
function isSwappableLocalPath(pathname) {
  if (!pathname) return false;
  return (
    pathname === '/'
    || pathname === '/create'
    || pathname === '/create/inspiration'
    || pathname === '/settings'
    || pathname === '/accounts'
    || pathname === '/_chrome'
    || /^\/workspace\/agent-[a-f0-9]+\/settings$/i.test(pathname)
    // Recovery flips to/from the workspace wrapper constantly while a
    // workspace flaps; swapping it keeps the titlebar from blinking on every
    // hop. Its poll loops carry minds:page-teardown guards (see
    // _RECOVERY_SCRIPT in templates.py).
    || /^\/agents\/agent-[a-f0-9]+\/recovery$/i.test(pathname)
  );
}

module.exports = {
  parseWorkspaceId,
  parseAccentSourceAgentId,
  selectSurfaceForUrl,
  isSwappableLocalPath,
  SURFACE_CONTENT,
  SURFACE_CHROME,
};
