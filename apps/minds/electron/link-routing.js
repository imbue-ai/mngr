'use strict';

// Where a link a page opens goes: the user's default browser, back into the
// workspace the window shows (the embed contract's minds:open-link, which the
// workspace routes to its own browser or app windows), or a window of its own.
// Kept free of any `electron` import so it can be unit-tested under plain
// node; the caller supplies the app's own origins, the workspace id aliases,
// and the side effects.

const POPUP_ACTIONS = Object.freeze({
  OPEN_EXTERNALLY: 'open-externally',
  FORWARD: 'forward',
  ALLOW: 'allow',
});

// A workspace's own app addresses: `[<app>.]agent-<hex>.localhost`, and the
// legacy host-keyed family a stale address may still name. The same family as
// WORKSPACE_ORIGIN_FAMILY in frontend/src/views/shell/WorkspaceFrame.ts, less
// its `.127.0.0.1` form, which no URL can carry (it does not parse).
const WORKSPACE_HOST_PATTERN = /^(?:[a-z0-9_-]+\.)*((?:agent|host)-[a-f0-9]+)\.localhost$/i;
// A workspace id as the renderer reports it; mirrors the embed contract's
// AGENT_ID_PATTERN (never trust the renderer).
const WORKSPACE_ID_PATTERN = /^(?:agent|host)-[a-f0-9]{1,64}$/i;

// Classify a URL as "external" (open in the user's default browser). All
// in-app navigation (the Imbue Studio backend, the mngr_forward plugin, and every
// `host-<id>.localhost` workspace origin) lives on localhost.
// link-externality-vectors.json pins these answers; the workspace template's
// link classifier asserts it agrees with them.
function isExternalUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    // Malformed but clearly an http(s) link: route it to the browser rather
    // than spawning a chrome-less popup that hangs on ERR_NAME_NOT_RESOLVED.
    return /^https?:\/\//i.test(url);
  }
  if (parsed.protocol === 'mailto:' || parsed.protocol === 'tel:') return true;
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  const host = parsed.hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost')) return false;
  if (host === '127.0.0.1' || host === '[::1]') return false;
  return true;
}

function parseHttpUrl(url) {
  if (typeof url !== 'string' || url === '') return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed : null;
}

// The workspace id an app address names, lowercased, or null.
function workspaceIdOfHost(hostname) {
  const match = hostname.match(WORKSPACE_HOST_PATTERN);
  return match ? match[1].toLowerCase() : null;
}

/**
 * What to do with a popup a window's page asked for.
 *
 * Electron names no frame for a popup, only its URL and referrer, and the
 * Imbue Studio page opens local popups of its own, so a popup is taken to be
 * the mounted workspace's when its URL names a workspace, or when it came from
 * the workspace's own origin family or with no referrer at all (a
 * `noreferrer` link).
 *
 * @param {object} popup
 * @param {string} popup.url  The resolved URL the popup would open.
 * @param {string} popup.referrer  The popup's referrer URL, '' when it has none.
 * @param {string[]} popup.appOrigins  The Imbue Studio backend's and the forwarder's origins.
 * @param {{workspaceIds: string[], opensLinks: boolean} | null} popup.mount  The workspace
 *   mounted in the window (every alias of its id), or null when none is.
 * @returns {string} One of POPUP_ACTIONS.
 */
function decidePopup({ url, referrer, appOrigins, mount }) {
  if (isExternalUrl(url)) return POPUP_ACTIONS.OPEN_EXTERNALLY;
  // Not external and not http(s) (about:blank, blob:, data:, file:): no link
  // the workspace could route.
  const target = parseHttpUrl(url);
  if (target === null) return POPUP_ACTIONS.ALLOW;
  if (appOrigins.includes(target.origin)) return POPUP_ACTIONS.ALLOW;
  if (mount === null) return POPUP_ACTIONS.ALLOW;
  // CLEANUP: drop this fallback (the bare window for a workspace that never
  // announced opensLinks) once every workspace template Imbue Studio supports
  // announces it, i.e. once the template release carrying opensLinks is the
  // oldest supported one.
  if (!mount.opensLinks) return POPUP_ACTIONS.ALLOW;
  // Another workspace's address goes to the mounted one too, which refuses it
  // with a notice rather than a window opening onto it.
  if (workspaceIdOfHost(target.hostname) !== null) return POPUP_ACTIONS.FORWARD;
  if (referrer === '') return POPUP_ACTIONS.FORWARD;
  const referrerUrl = parseHttpUrl(referrer);
  const referrerWorkspaceId = referrerUrl === null ? null : workspaceIdOfHost(referrerUrl.hostname);
  if (referrerWorkspaceId === null) return POPUP_ACTIONS.ALLOW;
  const mountedIds = mount.workspaceIds.map((id) => id.toLowerCase());
  return mountedIds.includes(referrerWorkspaceId) ? POPUP_ACTIONS.FORWARD : POPUP_ACTIONS.ALLOW;
}

/**
 * The link routing for every web contents the app creates, keeping which
 * workspace each window's page has mounted.
 *
 * @param {object} deps
 * @param {(url: string, webContents: object) => void} deps.openExternal  Open a URL in the default browser.
 * @param {(webContents: object, url: string) => void} deps.forward  Hand a popup's URL to the
 *   window's page, which sends it to its mounted workspace as minds:open-link.
 * @param {() => string[]} deps.appOrigins  The backend's and the forwarder's origins, as known now.
 * @param {(workspaceId: string) => string[]} deps.workspaceAliases  Every id the workspace goes by.
 */
function createLinkRouter({ openExternal, forward, appOrigins, workspaceAliases }) {
  const mounts = new WeakMap();

  function mountFor(webContents) {
    const mounted = mounts.get(webContents);
    if (mounted === undefined) return null;
    return { workspaceIds: workspaceAliases(mounted.workspaceId), opensLinks: mounted.opensLinks };
  }

  return {
    /**
     * Record the workspace a window's page mounted (`{ workspaceId,
     * opensLinks }`), or that it mounts none (null). Answers whether the
     * report was well-formed; an off-shape one changes nothing.
     */
    recordMount(webContents, report) {
      if (report === null) {
        mounts.delete(webContents);
        return true;
      }
      if (typeof report !== 'object') return false;
      if (typeof report.workspaceId !== 'string' || !WORKSPACE_ID_PATTERN.test(report.workspaceId)) return false;
      if (typeof report.opensLinks !== 'boolean') return false;
      mounts.set(webContents, { workspaceId: report.workspaceId, opensLinks: report.opensLinks });
      return true;
    },

    /** The window's page is gone (reloaded, navigated away, crashed). */
    forgetMount(webContents) {
      mounts.delete(webContents);
    },

    install(webContents) {
      webContents.setWindowOpenHandler((details) => {
        const action = decidePopup({
          url: details.url,
          referrer: details.referrer ? details.referrer.url : '',
          appOrigins: appOrigins(),
          mount: mountFor(webContents),
        });
        if (action === POPUP_ACTIONS.OPEN_EXTERNALLY) {
          openExternal(details.url, webContents);
          return { action: 'deny' };
        }
        if (action === POPUP_ACTIONS.FORWARD) {
          forward(webContents, details.url);
          return { action: 'deny' };
        }
        return { action: 'allow' };
      });
      // Fires for every frame, the workspace iframe and the service iframes
      // it embeds included, so an in-place navigation to an external site is
      // cancelled and opened externally instead of rendering a foreign site
      // inside the chrome (the iframe's frame-ancestors would usually refuse
      // anyway).
      webContents.on('will-frame-navigate', (details) => {
        if (!isExternalUrl(details.url)) return;
        details.preventDefault();
        openExternal(details.url, webContents);
      });
    },
  };
}

module.exports = {
  POPUP_ACTIONS,
  isExternalUrl,
  decidePopup,
  createLinkRouter,
};
