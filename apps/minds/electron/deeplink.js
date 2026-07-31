'use strict';

// Pure parsing logic for minds:// deeplinks. Kept free of any `electron`
// imports so it can be unit-tested under plain node (see
// ../test/unit/deeplink.test.js). main.js routes every OS delivery channel
// (macOS `open-url`, win/linux second-instance argv, cold-start argv) through
// these helpers and acts on the result.
//
// URL shape: the host names the action.
//   minds://create?git_url=<repo>&branch=<ref>  -> an Inspiration link. When the
//     app is already INSIDE a machine, main.js pops the Create from Inspiration
//     MODAL over it (a small chooser: create a new machine, or add the
//     Inspiration to the current machine). Otherwise it navigates to the full
//     Create from Inspiration PAGE. `branch` accepts anything the create form's
//     Branch input accepts and stays blank when absent (create then resolves the
//     repo's latest version). Without a git_url the plain create page is the
//     target.
//   minds:// (or any unrecognized/malformed URL) -> just focus the app.

// Generous for a git URL plus ref, tight enough to bound log spam and
// pathological input.
const MAX_DEEPLINK_LENGTH = 2048;

/**
 * Parse a raw deeplink URL into an action.
 *
 * Returns one of:
 *   { action: 'create', gitUrl: string, branch: string }  (params default '')
 *   { action: 'focus' }
 *
 * Never throws. Anything that is not a well-formed minds:// URL with a
 * recognized action host degrades to 'focus' -- the deliberate catch-all so
 * that a bare minds:// (used by the post-login web page) and any future or
 * malformed link at worst brings the app to the front.
 */
function parseDeeplink(rawUrl) {
  const FOCUS = { action: 'focus' };
  if (typeof rawUrl !== 'string' || rawUrl.length === 0 || rawUrl.length > MAX_DEEPLINK_LENGTH) {
    return FOCUS;
  }
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return FOCUS;
  }
  if (parsed.protocol !== 'minds:') return FOCUS;
  // Non-special schemes preserve host case (minds://CREATE keeps host
  // "CREATE"), so lowercase explicitly. hostname (not host) ignores a port.
  const action = parsed.hostname.toLowerCase();
  if (action !== 'create') return FOCUS;
  // Exactly these two params; extra params and path segments are ignored.
  const gitUrl = (parsed.searchParams.get('git_url') || '').trim();
  const branch = (parsed.searchParams.get('branch') || '').trim();
  return { action: 'create', gitUrl, branch };
}

/**
 * Map a raw deeplink URL to the backend path the CONTENT VIEW should navigate
 * to, or null for focus-only. This is the allowlist boundary: the only possible
 * outputs are null or a string built from the fixed '/create' /
 * '/create/inspiration' literals plus URLSearchParams re-encoding -- raw
 * deeplink text never reaches loadURL. A repo-carrying link is an Inspiration
 * link and targets the Create from Inspiration page; without a repo the plain
 * create page is the target (a branch alone is not an Inspiration). When the app
 * is already inside a machine, main.js opens the modal (``deeplinkModalPath``)
 * instead of navigating to this page.
 */
function deeplinkTargetPath(rawUrl) {
  const parsed = parseDeeplink(rawUrl);
  if (parsed.action !== 'create') return null;
  const params = new URLSearchParams();
  if (parsed.gitUrl) params.set('git_url', parsed.gitUrl);
  if (parsed.branch) params.set('branch', parsed.branch);
  const query = params.toString();
  if (parsed.gitUrl) return `/create/inspiration?${query}`;
  return query ? `/create?${query}` : '/create';
}

/**
 * Map a raw deeplink URL to the backend path of the Create from Inspiration
 * MODAL, or null when the link is not an Inspiration link (no repo). Same
 * allowlist discipline as ``deeplinkTargetPath``: the only non-null output is
 * the fixed '/create/inspiration/modal' literal plus re-encoded params. main.js
 * opens this via ``openModal`` (rather than navigating the content view) and
 * appends the window's current machine so the modal can offer "Add to current
 * machine" without a picker.
 */
function deeplinkModalPath(rawUrl) {
  const parsed = parseDeeplink(rawUrl);
  if (parsed.action !== 'create' || !parsed.gitUrl) return null;
  const params = new URLSearchParams();
  params.set('git_url', parsed.gitUrl);
  if (parsed.branch) params.set('branch', parsed.branch);
  return `/create/inspiration/modal?${params.toString()}`;
}

/**
 * Find the deeplink URL in an argv array (win/linux second-instance and
 * cold-start delivery), where it sits among the binary path, app path, and
 * chromium switches. Returns the first minds:// argument, or null.
 */
function extractDeeplinkUrlFromArgv(argv) {
  if (!Array.isArray(argv)) return null;
  for (const arg of argv) {
    if (typeof arg === 'string' && /^minds:\/\//i.test(arg)) return arg;
  }
  return null;
}

module.exports = { parseDeeplink, deeplinkTargetPath, deeplinkModalPath, extractDeeplinkUrlFromArgv, MAX_DEEPLINK_LENGTH };
