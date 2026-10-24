'use strict';

// Where a link a page opens goes: the user's default browser (or mail or phone
// app, or the app a custom scheme belongs to, once the user agrees), back into
// the workspace the window shows (the embed contract's minds:open-link, which
// the workspace routes to its own browser or app windows, or to an app of its
// own registered for an external link, or back out with minds:open-external),
// or a window of its own.
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

// Schemes the app's own browser engine loads, or refuses, itself. A URL of any
// other scheme is handed to another app of the user's system.
const IN_APP_SCHEMES = new Set([
  'http:',
  'https:',
  'ws:',
  'wss:',
  'file:',
  'data:',
  'blob:',
  'about:',
  'javascript:',
  'filesystem:',
  'chrome:',
  'chrome-untrusted:',
  'chrome-extension:',
  'devtools:',
  'view-source:',
]);

// A URL of a custom scheme (`paseo:`, `slack:`, `vscode:`), which the app it
// belongs to opens, unless an app of the workspace takes it.
// link-externality-vectors.json pins these answers too.
function isCustomSchemeUrl(url) {
  let protocol;
  try {
    protocol = new URL(url).protocol;
  } catch {
    return false;
  }
  return !IN_APP_SCHEMES.has(protocol) && protocol !== 'mailto:' && protocol !== 'tel:';
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

// Whether `url` (a page's URL, or a referrer) is on the mounted workspace's
// own origin family.
function isOfMountedWorkspace(url, mount) {
  const parsed = parseHttpUrl(url);
  const workspaceId = parsed === null ? null : workspaceIdOfHost(parsed.hostname);
  if (workspaceId === null) return false;
  return mount.workspaceIds.map((id) => id.toLowerCase()).includes(workspaceId);
}

// Whether a popup is the mounted workspace's: its own origin family asked for
// it, or it is a noreferrer custom-scheme link, since the Imbue Studio page
// opens no custom-scheme link itself.
function isPopupOfMountedWorkspace(url, referrer, mount) {
  if (mount === null) return false;
  return isOfMountedWorkspace(referrer, mount) || (referrer === '' && isCustomSchemeUrl(url));
}

// Whether the mounted workspace takes the custom-scheme links handed to it.
function routesCustomSchemeLinks(mount) {
  return mount !== null && mount.opensLinks && mount.opensCustomSchemeLinks;
}

/**
 * What to do with a popup a window's page asked for.
 *
 * Electron names no frame for a popup, only its URL and referrer, and the
 * Imbue Studio page opens popups of its own, so a local popup is taken to be
 * the mounted workspace's when its URL names a workspace, or when it came from
 * the workspace's own origin family or with no referrer at all (a
 * `noreferrer` link). An external one is the workspace's only when its own
 * origin family asked for it: the Imbue Studio page opens noreferrer external
 * links of its own, which are not the workspace's to route. A custom-scheme
 * link is the workspace's as isPopupOfMountedWorkspace says, and goes to it
 * only when it announced `opensCustomSchemeLinks`; any other opens outside
 * the app.
 *
 * @param {object} popup
 * @param {string} popup.url  The resolved URL the popup would open.
 * @param {string} popup.referrer  The popup's referrer URL, '' when it has none.
 * @param {string[]} popup.appOrigins  The Imbue Studio backend's and the forwarder's origins.
 * @param {{workspaceIds: string[], opensLinks: boolean, opensCustomSchemeLinks: boolean} | null} popup.mount
 *   The workspace mounted in the window (every alias of its id), or null when none is.
 * @returns {string} One of POPUP_ACTIONS.
 */
function decidePopup({ url, referrer, appOrigins, mount }) {
  if (isCustomSchemeUrl(url)) {
    const isForwarded = routesCustomSchemeLinks(mount) && isPopupOfMountedWorkspace(url, referrer, mount);
    return isForwarded ? POPUP_ACTIONS.FORWARD : POPUP_ACTIONS.OPEN_EXTERNALLY;
  }
  if (isExternalUrl(url)) {
    if (mount !== null && mount.opensLinks && isOfMountedWorkspace(referrer, mount)) return POPUP_ACTIONS.FORWARD;
    return POPUP_ACTIONS.OPEN_EXTERNALLY;
  }
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
  return isOfMountedWorkspace(referrer, mount) ? POPUP_ACTIONS.FORWARD : POPUP_ACTIONS.ALLOW;
}

/**
 * What to do with a frame of a window's page navigating in place: ALLOW
 * anything not external; an external URL goes to the mounted workspace when
 * the frame is one of its pages (a page without the workspace's app contract,
 * whose plain link click navigates its own frame) and it announced
 * `opensLinks`, and opens outside the app otherwise. A custom-scheme URL goes
 * to the workspace from any frame but the window's own page (a workspace app
 * launches one from an `about:blank` frame of its own) when it announced
 * `opensCustomSchemeLinks`; otherwise Chromium hands it to its app, through
 * openExternalRequested.
 *
 * @param {object} navigation
 * @param {string} navigation.url  Where the frame is navigating.
 * @param {string} navigation.frameUrl  The URL of the frame's current page, '' when unknown.
 * @param {boolean} navigation.isMainFrame  Whether it is the window's own page.
 * @param {{workspaceIds: string[], opensLinks: boolean, opensCustomSchemeLinks: boolean} | null} navigation.mount
 *   As for decidePopup.
 * @returns {string} One of POPUP_ACTIONS.
 */
function decideFrameNavigation({ url, frameUrl, isMainFrame, mount }) {
  if (isCustomSchemeUrl(url)) {
    return !isMainFrame && routesCustomSchemeLinks(mount) ? POPUP_ACTIONS.FORWARD : POPUP_ACTIONS.ALLOW;
  }
  if (!isExternalUrl(url)) return POPUP_ACTIONS.ALLOW;
  if (isMainFrame || mount === null || !mount.opensLinks) return POPUP_ACTIONS.OPEN_EXTERNALLY;
  return isOfMountedWorkspace(frameUrl, mount) ? POPUP_ACTIONS.FORWARD : POPUP_ACTIONS.OPEN_EXTERNALLY;
}

// Electron can hand over a frame it has already disposed (an iframe reused
// after a cancelled navigation), and reading its url then throws.
function frameUrlOf(frame) {
  return frame && !frame.isDestroyed() ? frame.url : '';
}

function decodedOrRaw(text) {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/**
 * The question to ask before handing `url` to another app of the user's
 * system, or null for a web link, which opens in the browser unasked: a
 * mailto: or tel: link starts writing an email or a call, and a custom-scheme
 * link opens the app it belongs to. `rememberLabel` offers to stop asking for
 * the scheme's links from the workspace that asked.
 *
 * @param {string} url
 * @param {string} appName  The app the system opens `url` with, '' when unknown.
 * @returns {{message: string, detail: string, rememberLabel: string} | null}
 */
function externalOpenPrompt(url, appName) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  // A mailto: link may carry its address in the query alone (`mailto:?to=...`).
  const detail = decodedOrRaw(parsed.pathname) || url;
  if (parsed.protocol === 'mailto:') {
    return {
      message: 'Open this email address in your mail app?',
      detail,
      rememberLabel: 'Always open email addresses from this workspace without asking',
    };
  }
  if (parsed.protocol === 'tel:') {
    return {
      message: 'Call this number with your phone app?',
      detail,
      rememberLabel: 'Always call numbers from this workspace without asking',
    };
  }
  if (!isCustomSchemeUrl(url)) return null;
  return {
    message: appName ? `Open this link in ${appName}?` : 'Open this link in another app?',
    detail: url,
    rememberLabel: `Always open ${parsed.protocol.slice(0, -1)} links from this workspace without asking`,
  };
}

/**
 * Open an external link outside the app: a web link at once, any other only
 * once the user agrees to the externalOpenPrompt, or without asking when they
 * chose to always allow the scheme for the workspace the link came from. A
 * link of a scheme the window already has a question open for is dropped.
 *
 * @param {object} deps
 * @param {(prompt: {message: string, detail: string, rememberLabel: string | null}, webContents: object)
 *   => Promise<{accepted: boolean, remember: boolean}>} deps.confirm
 *   Ask the user, over the window `webContents` belongs to; resolves whether they agreed, and whether to
 *   stop asking (offered only when `rememberLabel` is set).
 * @param {(url: string, webContents: object) => void} deps.open  Hand the URL to the system.
 * @param {(url: string) => string} deps.appNameFor  The app the system opens `url` with, '' when unknown.
 * @param {{isAllowed: (workspaceId: string, scheme: string) => boolean,
 *   allow: (workspaceId: string, scheme: string) => void}} deps.permissions  The "Always allow" choices.
 * @returns {(url: string, webContents: object, workspaceId: string | null) => Promise<void>}
 *   `workspaceId` is the workspace the link came from, null when it came from no workspace.
 */
function createExternalOpener({ confirm, open, appNameFor, permissions }) {
  // Per webContents, the schemes it has a question open for: a page launching
  // links in a loop gets one dialog, not a stack of them.
  const asking = new Map();
  return async (url, webContents, workspaceId) => {
    const prompt = externalOpenPrompt(url, isCustomSchemeUrl(url) ? appNameFor(url) : '');
    if (prompt === null) {
      open(url, webContents);
      return;
    }
    const scheme = new URL(url).protocol;
    if (workspaceId !== null && permissions.isAllowed(workspaceId, scheme)) {
      open(url, webContents);
      return;
    }
    const schemesAsked = asking.get(webContents) || new Set();
    if (schemesAsked.has(scheme)) return;
    schemesAsked.add(scheme);
    asking.set(webContents, schemesAsked);
    let answer;
    try {
      answer = await confirm({ ...prompt, rememberLabel: workspaceId !== null ? prompt.rememberLabel : null }, webContents);
    } finally {
      schemesAsked.delete(scheme);
      if (schemesAsked.size === 0) asking.delete(webContents);
    }
    if (!answer.accepted) return;
    if (answer.remember && workspaceId !== null) permissions.allow(workspaceId, scheme);
    open(url, webContents);
  };
}

/**
 * The link routing for every web contents the app creates, keeping which
 * workspace each window's page has mounted.
 *
 * @param {object} deps
 * @param {(url: string, webContents: object, workspaceId: string | null) => void} deps.openExternal
 *   Open a URL outside the app (see createExternalOpener), naming the mounted workspace when one of
 *   its pages asked for it.
 * @param {(webContents: object, url: string) => void} deps.forward  Hand a popup's, a frame
 *   navigation's or a custom-scheme launch's URL to the window's page, which sends it to its mounted
 *   workspace as minds:open-link.
 * @param {() => string[]} deps.appOrigins  The backend's and the forwarder's origins, as known now.
 * @param {(workspaceId: string) => string[]} deps.workspaceAliases  Every id the workspace goes by.
 */
function createLinkRouter({ openExternal, forward, appOrigins, workspaceAliases }) {
  const mounts = new WeakMap();

  function mountFor(webContents) {
    const mounted = mounts.get(webContents);
    if (mounted === undefined) return null;
    return {
      workspaceIds: workspaceAliases(mounted.workspaceId),
      opensLinks: mounted.opensLinks,
      opensCustomSchemeLinks: mounted.opensCustomSchemeLinks,
    };
  }

  function mountedWorkspaceId(webContents) {
    const mounted = mounts.get(webContents);
    return mounted === undefined ? null : mounted.workspaceId;
  }

  // Any frame but the window's own page is the mounted workspace's or one it embeds.
  function workspaceIdOfFrame(webContents, isMainFrame) {
    return isMainFrame === true ? null : mountedWorkspaceId(webContents);
  }

  return {
    /**
     * Record the workspace a window's page mounted (`{ workspaceId,
     * opensLinks, opensCustomSchemeLinks }`), or that it mounts none (null).
     * Answers whether the report was well-formed; an off-shape one changes
     * nothing.
     */
    recordMount(webContents, report) {
      if (report === null) {
        mounts.delete(webContents);
        return true;
      }
      if (typeof report !== 'object') return false;
      if (typeof report.workspaceId !== 'string' || !WORKSPACE_ID_PATTERN.test(report.workspaceId)) return false;
      if (typeof report.opensLinks !== 'boolean' || typeof report.opensCustomSchemeLinks !== 'boolean') return false;
      mounts.set(webContents, {
        workspaceId: report.workspaceId,
        opensLinks: report.opensLinks,
        opensCustomSchemeLinks: report.opensCustomSchemeLinks,
      });
      return true;
    },

    /** The window's page is gone (reloaded, navigated away, crashed). */
    forgetMount(webContents) {
      mounts.delete(webContents);
    },

    /** The workspace the window's page has mounted, or null. */
    workspaceOf(webContents) {
      return mountedWorkspaceId(webContents);
    },

    /**
     * Chromium's `openExternal` permission request, which it makes before
     * every launch of another app for a page (a frame navigation or a server
     * redirect to a mailto:, tel: or custom-scheme link, however it got
     * there). The caller denies the request; a custom-scheme link a frame of
     * a workspace that takes them asked for goes to the workspace, and any
     * other through openExternal, which asks first.
     */
    openExternalRequested(webContents, details) {
      const url = details.externalURL;
      if (typeof url !== 'string') return;
      if (isCustomSchemeUrl(url) && details.isMainFrame !== true && routesCustomSchemeLinks(mountFor(webContents))) {
        forward(webContents, url);
        return;
      }
      openExternal(url, webContents, workspaceIdOfFrame(webContents, details.isMainFrame));
    },

    install(webContents) {
      webContents.setWindowOpenHandler((details) => {
        const referrer = details.referrer ? details.referrer.url : '';
        const mount = mountFor(webContents);
        const action = decidePopup({ url: details.url, referrer, appOrigins: appOrigins(), mount });
        if (action === POPUP_ACTIONS.OPEN_EXTERNALLY) {
          const isFromWorkspace = isPopupOfMountedWorkspace(details.url, referrer, mount);
          openExternal(details.url, webContents, isFromWorkspace ? mountedWorkspaceId(webContents) : null);
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
      // cancelled rather than rendering a foreign site inside the chrome (the
      // iframe's frame-ancestors would usually refuse anyway), and goes where
      // decideFrameNavigation says.
      webContents.on('will-frame-navigate', (details) => {
        const action = decideFrameNavigation({
          url: details.url,
          frameUrl: frameUrlOf(details.frame),
          isMainFrame: details.isMainFrame === true,
          mount: mountFor(webContents),
        });
        if (action === POPUP_ACTIONS.ALLOW) return;
        details.preventDefault();
        if (action === POPUP_ACTIONS.FORWARD) forward(webContents, details.url);
        else openExternal(details.url, webContents, workspaceIdOfFrame(webContents, details.isMainFrame));
      });
    },
  };
}

module.exports = {
  POPUP_ACTIONS,
  isExternalUrl,
  isCustomSchemeUrl,
  decidePopup,
  decideFrameNavigation,
  externalOpenPrompt,
  createExternalOpener,
  createLinkRouter,
};
