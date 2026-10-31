'use strict';

// Pure parsing logic for imbue-studio:// deeplinks. Kept free of any
// `electron` imports so it can be unit-tested under plain node (see
// ../test/unit/deeplink.test.js). main.js routes every OS delivery channel
// (macOS `open-url`, win/linux second-instance argv, cold-start argv) through
// these helpers and acts on the result.
//
// URL shape: the host names the action.
//   imbue-studio://create?git_url=<repo>&branch=<ref>  -> a Template link:
//     main.js navigates to the SPA's Create from Template PAGE, which
//     offers both ways forward (create a new machine, or add the Template
//     to an existing one). `branch` accepts anything the create form's
//     Branch input accepts and stays blank when absent (create then resolves
//     the repo's latest version). Without a git_url the plain create page is
//     the target.
//   imbue-studio:// (or any unrecognized/malformed URL) -> just focus the app.

// The schemes the app registers with the OS and accepts here, primary first.
// CLEANUP: drop 'minds' once two stable releases have shipped with both
// schemes (specs/imbue-studio-rename/05_cleanup.md).
const DEEPLINK_SCHEMES = Object.freeze(['imbue-studio', 'minds']);
const DEEPLINK_PROTOCOLS = new Set(DEEPLINK_SCHEMES.map((scheme) => `${scheme}:`));
const DEEPLINK_ARGV_PATTERN = new RegExp(`^(?:${DEEPLINK_SCHEMES.join('|')})://`, 'i');

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
 * Never throws. Anything that is not a well-formed URL on one of the
 * DEEPLINK_SCHEMES with a recognized action host degrades to 'focus' -- the
 * deliberate catch-all so that a bare imbue-studio:// (used by the post-login
 * web page) and any future or malformed link at worst brings the app to the
 * front.
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
  if (!DEEPLINK_PROTOCOLS.has(parsed.protocol.toLowerCase())) return FOCUS;
  // Non-special schemes preserve host case (imbue-studio://CREATE keeps host
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
 * '/create/template' literals plus URLSearchParams re-encoding -- raw
 * deeplink text never reaches loadURL. A repo-carrying link is a Template
 * link and targets the Create from Template page; without a repo the plain
 * create page is the target (a branch alone is not a Template).
 */
function deeplinkTargetPath(rawUrl) {
  const parsed = parseDeeplink(rawUrl);
  if (parsed.action !== 'create') return null;
  const params = new URLSearchParams();
  if (parsed.gitUrl) params.set('git_url', parsed.gitUrl);
  if (parsed.branch) params.set('branch', parsed.branch);
  const query = params.toString();
  if (parsed.gitUrl) return `/create/template?${query}`;
  return query ? `/create?${query}` : '/create';
}

/**
 * Find the deeplink URL in an argv array (win/linux second-instance and
 * cold-start delivery), where it sits among the binary path, app path, and
 * chromium switches. Returns the first argument on one of the
 * DEEPLINK_SCHEMES, or null.
 */
function extractDeeplinkUrlFromArgv(argv) {
  if (!Array.isArray(argv)) return null;
  for (const arg of argv) {
    if (typeof arg === 'string' && DEEPLINK_ARGV_PATTERN.test(arg)) return arg;
  }
  return null;
}

/**
 * The url schemes an XML Info.plist declares (every ``CFBundleURLSchemes``
 * entry). Anything that is not an XML plist declares none.
 */
function declaredUrlSchemes(infoPlistXml) {
  const schemes = new Set();
  if (typeof infoPlistXml !== 'string') return schemes;
  const arrays = infoPlistXml.matchAll(/<key>CFBundleURLSchemes<\/key>\s*<array>([\s\S]*?)<\/array>/g);
  for (const [, body] of arrays) {
    for (const [, scheme] of body.matchAll(/<string>\s*([^<]*?)\s*<\/string>/g)) schemes.add(scheme);
  }
  return schemes;
}

/**
 * The ``app.setAsDefaultProtocolClient`` calls this run should make, as
 * argument lists.
 *
 * A packaged app registers itself. A dev run (``process.defaultApp``) is the
 * bare electron binary, which has to be told which app to load, and only
 * Windows takes the executable and arguments that say so. macOS ignores them
 * and registers the running bundle: the stock Electron.app declares no schemes
 * and would open every link in an empty window, taking them from an installed
 * app, so a dev run there registers only the schemes its own bundle declares
 * (``bundleSchemes``, from a bundle patched for deeplink testing). Linux
 * honors a protocol only for a packaged app, so a dev run there registers
 * nothing.
 */
function protocolClientRegistrations({ isDefaultApp, platform, execPath, appPath, bundleSchemes }) {
  if (!isDefaultApp) return DEEPLINK_SCHEMES.map((scheme) => [scheme]);
  if (platform === 'darwin') {
    return DEEPLINK_SCHEMES.filter((scheme) => bundleSchemes.has(scheme)).map((scheme) => [scheme]);
  }
  if (platform === 'win32' && appPath) return DEEPLINK_SCHEMES.map((scheme) => [scheme, execPath, [appPath]]);
  return [];
}

module.exports = {
  parseDeeplink,
  deeplinkTargetPath,
  extractDeeplinkUrlFromArgv,
  protocolClientRegistrations,
  declaredUrlSchemes,
  DEEPLINK_SCHEMES,
  MAX_DEEPLINK_LENGTH,
};
