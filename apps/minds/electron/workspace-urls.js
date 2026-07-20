// Pure URL <-> persisted-window-state helpers, split out of main.js (which can't
// be required outside Electron) so the persist/restore canonicalization is
// unit-testable, mirroring the split-out ``decideStartupRoute`` helper.
//
// These carry no module state (no live ports / origins); ``main.js`` keeps the
// stateful inverses (``toRestoredContentUrl`` / ``workspaceUrlForAgent``) that
// rebuild an absolute URL against the CURRENT run's mngr_forward origin.

function parseWorkspaceId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    // Final workspace URL: `<agent-id>.localhost:PORT/...`
    const hostMatch = parsed.hostname.match(/^(agent-[a-f0-9]+)\.localhost$/i);
    if (hostMatch) return hostMatch[1];
    // Auth-bridge URL: `localhost:PORT/goto/<agent-id>/` is the pending
    // state before the subdomain cookie is installed. Recognising it lets
    // findBundleForWorkspace de-dupe clicks during the redirect window.
    const pathMatch = parsed.pathname.match(/^\/goto\/(agent-[a-f0-9]+)(?:\/|$)/i);
    return pathMatch ? pathMatch[1] : null;
  } catch {
    return null;
  }
}

// The workspace id from a minds-backend recovery URL
// (``/agents/<agent-id>/recovery``). Kept distinct from ``parseWorkspaceId``
// (which only knows the live workspace + auth-bridge shapes) because the
// recovery page is a minds-backend screen, not a workspace origin. Used by the
// persist/restore path so a window left on the recovery page is canonicalised to
// its underlying workspace rather than persisted verbatim -- the recovery URL
// embeds an absolute ``return_to`` with the run's ephemeral mngr_forward port,
// which is dead after any restart (see toPersistedContentUrl).
function parseRecoveryAgentId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

function toRelativeBackendUrl(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return parsed.pathname + parsed.search + parsed.hash;
  } catch {
    return null;
  }
}

// Canonicalise a window's live content URL into the form persisted in
// ``window-state.json``. A workspace window's URL is on the agent subdomain
// (``agent-<id>.localhost:<mngr_forward_port>/...``); stripping that to a bare
// relative path loses the agent identity (the subdomain host) and would reopen
// against the minds backend (the landing page) on restore. Persist the
// port-independent ``/goto/<agent>/`` auth-bridge path instead -- it carries the
// agent id, is recognised by ``parseWorkspaceId`` (so dead-workspace filtering
// works), and ``toRestoredContentUrl`` rebuilds the live origin from it.
//
// A recovery-page URL (``/agents/<agent>/recovery?return_to=<absolute forward
// url>``) is treated the same way: its ``return_to`` embeds an absolute URL with
// the run's ephemeral mngr_forward port, which is dead after any restart, so
// persisting it verbatim reopens to a dead link (a blank page). Canonicalising
// it to the workspace's ``/goto/<agent>/`` drops the stale port entirely; on
// restore the workspace loads directly, and if it is still unhealthy the normal
// health flow re-issues a recovery page with a FRESH return_to.
//
// Everything else round-trips as a minds-backend-relative path.
function toPersistedContentUrl(url) {
  const agentId = parseWorkspaceId(url) || parseRecoveryAgentId(url);
  if (agentId) return `/goto/${encodeURIComponent(agentId)}/`;
  return toRelativeBackendUrl(url);
}

module.exports = {
  parseWorkspaceId,
  parseRecoveryAgentId,
  toRelativeBackendUrl,
  toPersistedContentUrl,
};
