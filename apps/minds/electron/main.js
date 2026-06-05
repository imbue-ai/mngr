const { BaseWindow, WebContentsView, Menu, Notification, ipcMain, net, shell, app, session, screen, dialog, clipboard } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

// Only init the auto-updater in packaged builds: in dev, electron.autoUpdater
// is undefined on macOS, so todesktop's constructor throws.
if (app.isPackaged) {
  // Default is "never", which stages a downloaded update without ever
  // prompting the user to install it.
  todesktop.init({
    updateReadyAction: {
      showInstallAndRestartPrompt: 'always',
    },
  });
} else {
  console.log('[update] Skipping ToDesktop init (dev build -- not packaged)');
}

// Redirect Electron's userData directory to ~/.<MINDS_ROOT_NAME>/ so that dev
// and production installs are fully isolated (cookies, sessions, caches, etc.).
app.setPath('userData', paths.getDataDir());

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;
const SIDEBAR_WIDTH = 260;
const REQUESTS_PANEL_WIDTH = 320;
const CONTENT_PARTITION = 'persist:workspace-content';

// -- Per-window bundle registry --
const bundles = new Set();
const mruWindows = []; // most recently focused first
let appMenuInstalled = false;

let backendBaseUrl = null;
let mngrForwardBaseUrl = null;
let workspaceList = []; // [{id, name, account}]
// Persistent set of agent ids we have ever seen in the chrome SSE's
// ``destroying_agent_ids`` payload. Used to decide whether a workspace
// disappearing from the workspaces list is an actual user-initiated destroy
// (navigate the window to landing) or a transient discovery loss (leave the
// window alone -- the recovery flow kicks in via the system_interface_status
// event). Never cleared once added; a destroyed workspace's id is dead forever.
const everSeenDestroying = new Set();
let isShuttingDown = false;
let initialBundle = null; // the first window created at startup
let hasCompletedInitialStart = false;

// Central cache of the latest SSE state from /_chrome/events so newly-loaded
// chrome/sidebar webContents can be primed without opening their own SSE
// connection.
const latestChromeState = {
  workspaces: null, // most recent workspaces payload
  authStatus: null, // most recent auth_status payload
  requestCount: 0,  // most recent pending-request count
  requestIds: [],   // most recent ordered list of pending request ids
};

const chromeSseAbortRef = { current: null };
// Holds the in-flight connection's ``finish`` resolver so a forced reconnect
// (e.g. after the auth cookie is synced) can resolve the awaited promise
// directly. Electron's ``ClientRequest`` does not reliably emit a terminal
// event on ``abort()``, and its ``'close'`` event fires eagerly on a healthy
// streaming response (causing a reconnect storm), so neither can be relied on
// to drive reconnection.
const chromeSseFinishRef = { current: null };
let chromeSseReconnectTick = 0; // bumped to interrupt the current wait

function getSessionStatePath() {
  return path.join(paths.getDataDir(), 'window-state.json');
}

// -- URL/workspace helpers --

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

function toAbsoluteUrl(url) {
  if (!url) return url;
  if (url.startsWith('/') && backendBaseUrl) return backendBaseUrl + url;
  return url;
}

// Classify a URL as "external" -- i.e. something that should open in the
// user's default browser rather than inside the app. All in-app navigation
// (the minds backend, the mngr_forward plugin, and every
// `agent-<id>.localhost` workspace subdomain) lives on localhost, so anything
// off-localhost over http(s), plus mail/tel links, is treated as external.
// Non-web schemes (file:, about:, blob:, data:, devtools:, etc.) are internal
// app machinery and must never be handed to shell.openExternal.
function isExternalUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    // Malformed but still clearly an http(s) link -- e.g. an agent emitted
    // "https://example.com (note)" and the space/parens got encoded into the
    // host, which makes `new URL` throw. Treat it as external so it routes to
    // the browser (which shows a normal error page) instead of falling through
    // to 'allow' and spawning a chrome-less Electron popup that hangs on
    // ERR_NAME_NOT_RESOLVED. mailto:/tel: aren't handled here: a malformed one
    // can't be opened meaningfully, so we let it stay internal (a no-op).
    return /^https?:\/\//i.test(url);
  }
  if (parsed.protocol === 'mailto:' || parsed.protocol === 'tel:') return true;
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  const host = parsed.hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost')) return false;
  // URL.hostname wraps IPv6 literals in brackets, so the loopback parses as
  // `[::1]`, not `::1`.
  if (host === '127.0.0.1' || host === '[::1]') return false;
  return true;
}

// Build the auth-bridge URL that, when loaded, installs a session cookie on
// the agent's subdomain and redirects into the workspace's dockview UI.
// Returns null if the backend hasn't come up yet.
function workspaceUrlForAgent(agentId) {
  // `/goto/` lives on the mngr_forward plugin (which owns subdomain
  // forwarding), not the minds backend. Use mngrForwardBaseUrl when it has
  // been received; fall back to backendBaseUrl during the startup window
  // before the mngr_forward_started event arrives (rare -- the user can't
  // open a workspace in that window).
  if (!agentId) return null;
  const origin = mngrForwardBaseUrl || backendBaseUrl;
  if (!origin) return null;
  return `${origin}/goto/${encodeURIComponent(agentId)}/`;
}

function findBundleForWorkspace(agentId) {
  if (!agentId) return null;
  for (const b of bundles) {
    if (!b.window.isDestroyed() && b.currentWorkspaceId === agentId) return b;
  }
  return null;
}

function getBundleFromEvent(event) {
  if (!event || !event.sender) return null;
  const senderId = event.sender.id;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    const views = [b.chromeView, b.contentView, b.sidebarView, b.requestsPanelView, b.modalView];
    for (const v of views) {
      if (!v) continue;
      if (v.webContents.isDestroyed()) continue;
      if (v.webContents.id === senderId) return b;
    }
  }
  return null;
}

function getMostRecentWindow() {
  for (const b of mruWindows) {
    if (!b.window.isDestroyed()) return b;
  }
  for (const b of bundles) {
    if (!b.window.isDestroyed()) return b;
  }
  return null;
}

function focusBundle(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.window.isMinimized()) bundle.window.restore();
  if (!bundle.window.isVisible()) bundle.window.show();
  bundle.window.focus();
}


// -- Title handling --

function computeTitleFor(bundle) {
  const agentId = bundle.currentWorkspaceId;
  if (agentId) {
    const ws = workspaceList.find((w) => w.id === agentId);
    const name = ws ? (ws.name || ws.id) : null;
    return name ? `${name} \u2014 Minds` : 'Minds';
  }
  return 'Minds';
}

function updateOsTitle(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const title = computeTitleFor(bundle);
  bundle.window.setTitle(title);
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.webContents.send('window-title-changed', title);
  }
}

function updateAllOsTitles() {
  for (const b of bundles) updateOsTitle(b);
}

// -- Layout --

function updateBundleBounds(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const { width, height } = bundle.window.getContentBounds();

  if (bundle.isErrorState || bundle.isLoadingState) {
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.setBounds({ x: 0, y: 0, width, height });
    }
    if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
      bundle.contentView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    }
    return;
  }

  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.setBounds({ x: 0, y: 0, width, height: TITLEBAR_HEIGHT });
  }
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    const rightOffset = bundle.requestsPanelVisible ? REQUESTS_PANEL_WIDTH : 0;
    bundle.contentView.setBounds({
      x: 0,
      y: TITLEBAR_HEIGHT,
      width: width - rightOffset,
      height: height - TITLEBAR_HEIGHT,
    });
  }
  if (bundle.sidebarView && !bundle.sidebarView.webContents.isDestroyed()) {
    bundle.sidebarView.setBounds({
      x: 0,
      y: TITLEBAR_HEIGHT,
      width: SIDEBAR_WIDTH,
      height: height - TITLEBAR_HEIGHT,
    });
  }
  if (bundle.requestsPanelView && !bundle.requestsPanelView.webContents.isDestroyed()) {
    bundle.requestsPanelView.setBounds({
      x: width - REQUESTS_PANEL_WIDTH,
      y: TITLEBAR_HEIGHT,
      width: REQUESTS_PANEL_WIDTH,
      height: height - TITLEBAR_HEIGHT,
    });
  }
  // The modal overlays the entire content area (everything below the title
  // bar, including the sidebar and requests panel). The title bar stays
  // uncovered so window controls and the drag handle remain usable. The
  // view is transparent, so the dialog's own dim backdrop shows the
  // workspace behind it.
  if (bundle.modalView && !bundle.modalView.webContents.isDestroyed()) {
    bundle.modalView.setBounds({
      x: 0,
      y: TITLEBAR_HEIGHT,
      width,
      height: height - TITLEBAR_HEIGHT,
    });
  }
}

// -- Bundle lifecycle --

function buildBundleWindowOptions() {
  const windowOptions = {
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Minds',
    show: false,
    autoHideMenuBar: true,
  };
  if (isMac) {
    windowOptions.titleBarStyle = 'hiddenInset';
    windowOptions.trafficLightPosition = { x: 12, y: (TITLEBAR_HEIGHT - 16) / 2 };
  } else {
    windowOptions.frame = false;
  }
  return windowOptions;
}

function createBundleWebContentsViews(win) {
  const chromeView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  const contentView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'content-relay-preload.js'),
      partition: CONTENT_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.contentView.addChildView(chromeView);
  win.contentView.addChildView(contentView);

  // Auto-open DevTools for dev-time inspection.
  if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
    contentView.webContents.once('did-finish-load', () => {
      contentView.webContents.openDevTools({ mode: 'detach' });
    });
  }

  return { chromeView, contentView };
}

function wireBundleWindowEvents(bundle) {
  const { window: win } = bundle;

  win.on('focus', () => {
    const idx = mruWindows.indexOf(bundle);
    if (idx >= 0) mruWindows.splice(idx, 1);
    mruWindows.unshift(bundle);
  });

  win.on('maximize', () => { bundle._maximizedByUs = true; });
  win.on('unmaximize', () => { bundle._maximizedByUs = false; });
  win.on('resize', () => updateBundleBounds(bundle));

  // Run cleanup on `close` (before views are detached) rather than `closed`
  // so we can still reach the child webContents. BaseWindow does not guarantee
  // destruction of child WebContentsView render processes on its own; leaking
  // them across create/close cycles eventually starves new ones of resources.
  win.on('close', () => {
    // Snapshot session state on every manual window close: by the time
    // `before-quit` fires on the `window-all-closed` path, every bundle has
    // already been removed from `bundles` by its `closed` handler, so saving
    // there would clobber the file with `[]`. Skip when we're tearing down as
    // part of a `cmd+Q` / crash quit -- `before-quit` already saved the full
    // set and we must not overwrite it with a progressively shrinking snapshot
    // as the teardown closes each window.
    if (!isShuttingDown) saveSessionState();
    if (bundle.requestsPanelReloadTimer) {
      clearTimeout(bundle.requestsPanelReloadTimer);
      bundle.requestsPanelReloadTimer = null;
    }
    const views = [bundle.chromeView, bundle.contentView, bundle.sidebarView, bundle.requestsPanelView, bundle.modalView];
    for (const view of views) {
      if (!view) continue;
      if (view.webContents.isDestroyed()) continue;
      try {
        view.webContents.close();
      } catch { /* noop */ }
    }
  });

  win.on('closed', () => {
    bundles.delete(bundle);
    const mruIdx = mruWindows.indexOf(bundle);
    if (mruIdx >= 0) mruWindows.splice(mruIdx, 1);
    if (initialBundle === bundle) initialBundle = null;
  });
}

function wireBundleShowLogic(bundle) {
  const { window: win, chromeView } = bundle;
  // Show the window once chrome has painted (avoids flashing a bare BaseWindow
  // for the half-second before the WebContentsView renders). Fall back to a
  // longer timer in case the chrome load never completes.
  chromeView.webContents.once('did-finish-load', () => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  });
  win.once('ready-to-show', () => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  });
  setTimeout(() => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  }, 3000);
}

function createBundle() {
  const win = new BaseWindow(buildBundleWindowOptions());
  const { chromeView, contentView } = createBundleWebContentsViews(win);

  const bundle = {
    window: win,
    chromeView,
    contentView,
    sidebarView: null,
    sidebarVisible: false,
    requestsPanelView: null,
    requestsPanelVisible: false,
    requestsPanelReloadTimer: null,
    modalView: null,
    modalVisible: false,
    currentContentUrl: null,
    currentWorkspaceId: null,
    preErrorUrl: null,
    isErrorState: false,
    isLoadingState: true,
    _maximizedByUs: false,
    _boundsBeforeMaximize: null,
  };
  bundles.add(bundle);
  mruWindows.unshift(bundle);

  updateBundleBounds(bundle);
  wireBundleWindowEvents(bundle);

  // Re-push the computed title when chrome finishes (re)loading; the in-window
  // title bar otherwise has no way to learn its own window's title.
  chromeView.webContents.on('did-finish-load', () => {
    updateOsTitle(bundle);
    sendCurrentWorkspaceToBundleViews(bundle);
    primeViewWithCachedChromeState(chromeView.webContents);
  });

  wireContentViewEvents(bundle, contentView);
  registerShortcutsFor(bundle, chromeView.webContents);
  registerShortcutsFor(bundle, contentView.webContents);
  wireBundleShowLogic(bundle);

  return bundle;
}

function wireContentViewEvents(bundle, contentView) {
  // Forward content view nav events to the bundle's chrome view and update state.
  // Called from both createBundle and prepareAllWindowsForRetry (which rebuilds
  // the contentView that showErrorInAllWindows tore down).
  contentView.webContents.on('page-title-updated', (_e, title) => {
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-title-changed', title);
    }
  });

  const onContentNavigate = (url) => {
    if (!bundle.isErrorState) {
      bundle.currentContentUrl = url;
      bundle.preErrorUrl = url;
    }
    const newAgentId = parseWorkspaceId(url);
    if (bundle.currentWorkspaceId !== newAgentId) {
      bundle.currentWorkspaceId = newAgentId;
      sendCurrentWorkspaceToBundleViews(bundle);
    }
    updateOsTitle(bundle);
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-url-changed', url);
    }
  };

  contentView.webContents.on('did-navigate', (_e, url) => onContentNavigate(url));
  contentView.webContents.on('did-navigate-in-page', (_e, url) => onContentNavigate(url));

  // Enforce workspace uniqueness at the Electron level so it applies to EVERY
  // path that can drive the content view to a /forwarding/X/ URL (landing-page
  // row clicks, in-page anchors, pushState, etc.), not just sidebar-driven
  // navigate-content IPC.
  contentView.webContents.on('will-navigate', (event, url) => {
    const targetAgentId = parseWorkspaceId(url);
    if (!targetAgentId) return;
    const existing = findBundleForWorkspace(targetAgentId);
    if (!existing || existing === bundle) return;
    event.preventDefault();
    focusBundle(existing);
  });

  // Workspace pages (with live websockets) often attach `beforeunload`
  // handlers. Without a dialog host, Electron stalls the unload forever,
  // so the home button and workspace-switching navigate-content calls
  // never complete. Always allow unload.
  contentView.webContents.on('will-prevent-unload', (event) => {
    event.preventDefault();
  });
  // Belt-and-suspenders: some pages install `onbeforeunload` in ways that
  // Electron's will-prevent-unload doesn't intercept. Null it out after
  // every top-level page load.
  contentView.webContents.on('did-finish-load', () => {
    contentView.webContents
      .executeJavaScript('window.onbeforeunload = null;')
      .catch(() => {});
  });
}

function registerShortcutsFor(bundle, wc) {
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const key = input.key ? input.key.toLowerCase() : '';
    const modifier = isMac ? input.meta : input.control;
    // Match on `input.code` (physical key) rather than `input.key`: on macOS,
    // holding Option transforms `key` into the Option-composed character
    // (e.g. 'ˆ' or 'Dead' for Option+I), so `key === 'i'` never matches.
    const devTools =
      (isMac && input.meta && input.alt && input.code === 'KeyI') ||
      (!isMac && input.control && input.shift && input.code === 'KeyC');
    if (devTools) {
      event.preventDefault();
      if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
        bundle.contentView.webContents.toggleDevTools();
      }
      return;
    }
    // When the app menu is installed, it owns cmd+W / cmd+Q / cmd+N; handling
    // them here too would double-fire (e.g. two new windows per cmd+N).
    if (appMenuInstalled) return;
    // Ctrl+W on non-macOS: do NOT close the window. The keystroke should
    // reach the web content (terminal, editor) where it means "delete word"
    // or "close tab" depending on the app.
    if (modifier && !input.shift && !input.alt && key === 'q') {
      event.preventDefault();
      initiateFullQuit();
      return;
    }
    if (modifier && !input.shift && !input.alt && key === 'n') {
      event.preventDefault();
      openHomeInNewWindow();
      return;
    }
  });
}

// Route external links to the user's default browser instead of navigating
// the in-app view (which would clobber the workspace UI) or spawning a bare
// chrome-less Electron window. Covers both `target="_blank"` / `window.open`
// (via setWindowOpenHandler) and ordinary link clicks / JS navigations (via
// will-frame-navigate, which -- unlike will-navigate -- also fires for clicks
// inside the iframes that the workspace UI embeds agent content in). In-app
// (localhost) navigation is left untouched so workspace, home, and
// request-page links keep working. The `setImmediate` defer around
// openExternal follows Electron's security guide.
function applyExternalLinkHandling(wc) {
  // Defer per Electron's security guide. shell.openExternal returns a Promise
  // that rejects when the OS has no handler for the scheme -- realistic for
  // mailto:/tel: on machines with no mail client or dialer configured. We must
  // catch (an unhandled rejection terminates the main process under the bundled
  // Node runtime), but rather than silently no-op we log and surface the failure
  // to the user so the click is recoverable instead of vanishing.
  const openInBrowser = (url) => {
    setImmediate(() => {
      shell.openExternal(url).catch((err) => {
        console.warn('[external-link] failed to open', url, err);
        notifyOpenFailed(url);
      });
    });
  };
  wc.setWindowOpenHandler(({ url }) => {
    if (isExternalUrl(url)) {
      openInBrowser(url);
      return { action: 'deny' };
    }
    // Internal popups keep Electron's default behavior (unchanged from before
    // this handler existed).
    return { action: 'allow' };
  });
  // Use will-frame-navigate (not will-navigate) so external link clicks inside
  // embedded iframes are caught too. It is a superset of will-navigate -- it
  // fires for the main frame as well -- so listening to both would double-fire
  // and open two browser tabs for a top-level external navigation.
  wc.on('will-frame-navigate', (details) => {
    if (!isExternalUrl(details.url)) return;
    details.preventDefault();
    openInBrowser(details.url);
  });
}

// Surface a failed shell.openExternal to the user instead of letting the click
// vanish. For mailto:/tel: the useful payload is the bare address (to paste into
// webmail or a dialer), not the scheme-prefixed URL, so copy that.
function notifyOpenFailed(url) {
  let scheme = '';
  try {
    scheme = new URL(url).protocol.replace(':', '');
  } catch {
    // Unparseable url -- fall through with an empty scheme and copy it verbatim.
  }
  const isAddressScheme = scheme === 'mailto' || scheme === 'tel';
  const payload = isAddressScheme ? url.slice(url.indexOf(':') + 1) : url;
  clipboard.writeText(payload);
  const what = scheme === 'mailto' ? 'email address'
    : scheme === 'tel' ? 'phone number'
    : 'link';
  new Notification({
    title: "Couldn't open link",
    body: `No app is set up to handle this ${what}. It has been copied to your clipboard.`,
  }).show();
}

// -- Sidebar / requests panel helpers (per-bundle) --

// Sidebar and requests-panel views are created lazily the first time the
// user toggles them on, then reused for all subsequent toggles via
// setVisible(true/false). Destroying and recreating a WebContentsView on
// every click means spawning a fresh render process + preload + loadURL
// round-trip; on rapid clicks these queue up and take seconds to drain.

function openSidebar(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.sidebarView) {
    const sidebarView = new WebContentsView({
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    bundle.sidebarView = sidebarView;
    bundle.window.contentView.addChildView(sidebarView);
    registerShortcutsFor(bundle, sidebarView.webContents);
    sidebarView.webContents.on('did-finish-load', () => {
      sendCurrentWorkspaceToBundleViews(bundle);
      primeViewWithCachedChromeState(sidebarView.webContents);
    });
    if (backendBaseUrl) {
      sidebarView.webContents.loadURL(backendBaseUrl + '/_chrome/sidebar');
    }
  } else {
    // Re-add to the parent to raise to the top of z-order, then make visible.
    bundle.window.contentView.removeChildView(bundle.sidebarView);
    bundle.window.contentView.addChildView(bundle.sidebarView);
    bundle.sidebarView.setVisible(true);
  }
  bundle.sidebarVisible = true;
  updateBundleBounds(bundle);
}

function closeSidebar(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.sidebarView || !bundle.sidebarVisible) return;
  bundle.sidebarView.setVisible(false);
  bundle.sidebarVisible = false;
}

function toggleSidebar(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.sidebarVisible) closeSidebar(bundle);
  else openSidebar(bundle);
}

function openRequestsPanel(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.requestsPanelView) {
    const panel = new WebContentsView({
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    bundle.requestsPanelView = panel;
    bundle.window.contentView.addChildView(panel);
    registerShortcutsFor(bundle, panel.webContents);
    if (backendBaseUrl) {
      panel.webContents.loadURL(backendBaseUrl + '/_chrome/requests-panel');
    }
  } else {
    bundle.window.contentView.removeChildView(bundle.requestsPanelView);
    bundle.window.contentView.addChildView(bundle.requestsPanelView);
    bundle.requestsPanelView.setVisible(true);
    // The panel's HTML is rendered server-side and doesn't subscribe to SSE,
    // so its cards go stale while hidden. Refresh on show, and cancel any
    // debounced SSE-driven reload that was pending so we don't double-load.
    if (bundle.requestsPanelReloadTimer) {
      clearTimeout(bundle.requestsPanelReloadTimer);
      bundle.requestsPanelReloadTimer = null;
    }
    if (!bundle.requestsPanelView.webContents.isDestroyed()) {
      bundle.requestsPanelView.webContents.reload();
    }
  }
  bundle.requestsPanelVisible = true;
  updateBundleBounds(bundle);
}

function closeRequestsPanel(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.requestsPanelView || !bundle.requestsPanelVisible) return;
  bundle.requestsPanelView.setVisible(false);
  bundle.requestsPanelVisible = false;
  updateBundleBounds(bundle);
}

// Coalesce rapid SSE-triggered reloads. A burst of requests events
// (e.g. count 1 -> 2 -> 3 within a few ms) would otherwise restart the
// panel load multiple times in flight, potentially preventing it from
// ever settling on a rendered state, and multiplying backend HTTP load
// by (open windows) x (events).
const REQUESTS_PANEL_RELOAD_DEBOUNCE_MS = 50;
function scheduleRequestsPanelReload(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.requestsPanelView || !bundle.requestsPanelVisible) return;
  if (bundle.requestsPanelReloadTimer) {
    clearTimeout(bundle.requestsPanelReloadTimer);
  }
  bundle.requestsPanelReloadTimer = setTimeout(() => {
    bundle.requestsPanelReloadTimer = null;
    if (bundle.window.isDestroyed()) return;
    if (!bundle.requestsPanelView || !bundle.requestsPanelVisible) return;
    if (bundle.requestsPanelView.webContents.isDestroyed()) return;
    bundle.requestsPanelView.webContents.reload();
  }, REQUESTS_PANEL_RELOAD_DEBOUNCE_MS);
}

function toggleRequestsPanel(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.requestsPanelVisible) closeRequestsPanel(bundle);
  else openRequestsPanel(bundle);
}

// -- Modal overlay (per-bundle) --
//
// The modal is a full-content-area overlay used for transient dialogs (the
// permission request page) that should not replace the user's workspace in
// the content view. Like the sidebar / requests panel it is created lazily
// and reused via setVisible(true/false). It uses the default session (so it
// carries the auth cookie, like the chrome / requests-panel views) plus the
// preload bridge, so the page inside can call `window.minds.closeModal()`.

function openModal(bundle, url) {
  if (!bundle || bundle.window.isDestroyed() || !url) return;
  if (!bundle.modalView) {
    const modal = new WebContentsView({
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    // Transparent background so the dialog's own dim backdrop reveals the
    // workspace underneath instead of an opaque rectangle.
    modal.setBackgroundColor('#00000000');
    bundle.modalView = modal;
    bundle.window.contentView.addChildView(modal);
    registerShortcutsFor(bundle, modal.webContents);
    // Escape closes the modal even if the page's own key handling fails.
    modal.webContents.on('before-input-event', (event, input) => {
      if (input.type === 'keyDown' && input.key === 'Escape') {
        event.preventDefault();
        closeModal(bundle);
      }
    });
  } else {
    // Re-add to the parent to raise to the top of z-order, then make visible.
    bundle.window.contentView.removeChildView(bundle.modalView);
    bundle.window.contentView.addChildView(bundle.modalView);
    bundle.modalView.setVisible(true);
  }
  bundle.modalVisible = true;
  if (!bundle.modalView.webContents.isDestroyed()) {
    bundle.modalView.webContents.loadURL(url);
  }
  updateBundleBounds(bundle);
}

function closeModal(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.modalView || !bundle.modalVisible) return;
  bundle.modalView.setVisible(false);
  bundle.modalVisible = false;
  // Drop the page so its websockets/timers stop and a stale dialog isn't
  // briefly visible the next time the modal opens.
  if (!bundle.modalView.webContents.isDestroyed()) {
    bundle.modalView.webContents.loadURL('about:blank').catch(() => {});
  }
}

function sendCurrentWorkspaceToBundleViews(bundle) {
  if (!bundle) return;
  // Both the titlebar (chrome view) and the sidebar key UI off the current
  // workspace -- the titlebar uses it to scope the per-agent accent swatch
  // and the auto-redirect to the recovery page (which only fires when a
  // system_interface_status event matches the currently-displayed agent).
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.webContents.send('current-workspace-changed', bundle.currentWorkspaceId);
  }
  if (bundle.sidebarView && !bundle.sidebarView.webContents.isDestroyed()) {
    bundle.sidebarView.webContents.send('current-workspace-changed', bundle.currentWorkspaceId);
  }
}

// -- Window opening / focusing --

function loadUrlIntoBundleContentView(bundle, url) {
  // Stamp the intended workspace synchronously so subsequent
  // findBundleForWorkspace lookups see this bundle as occupying the workspace
  // BEFORE its content view has fired did-navigate. Otherwise a second
  // openOrFocusWorkspace / landing-click / notification-click arriving during
  // the load window wouldn't see the pending bundle and would spawn a duplicate.
  // Applies to every content-view loadURL aimed at a workspace URL, including
  // session restore into the initial bundle.
  if (!bundle) return;
  const intendedAgentId = parseWorkspaceId(url);
  if (intendedAgentId) {
    bundle.currentWorkspaceId = intendedAgentId;
    bundle.currentContentUrl = url;
    bundle.preErrorUrl = url;
    updateOsTitle(bundle);
    sendCurrentWorkspaceToBundleViews(bundle);
  }
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed() && url) {
    bundle.contentView.webContents.loadURL(url);
  }
}

function openOrFocusWorkspace(agentId, url) {
  const existing = findBundleForWorkspace(agentId);
  if (existing) {
    focusBundle(existing);
    return existing;
  }
  const absolute = toAbsoluteUrl(url || workspaceUrlForAgent(agentId));
  return openNewWindow(absolute);
}

function openNewWindow(url) {
  const bundle = createBundle();
  bundle.isLoadingState = false;
  updateBundleBounds(bundle);
  if (bundle.chromeView && backendBaseUrl) {
    bundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
  }
  loadUrlIntoBundleContentView(bundle, url);
  return bundle;
}

function openHomeInNewWindow() {
  // Backend isn't up yet (still in the shell.html loading state): just focus
  // the existing initial window instead of creating a disconnected second one.
  if (!backendBaseUrl) {
    const target = getMostRecentWindow();
    if (target) focusBundle(target);
    return target;
  }
  return openNewWindow(backendBaseUrl + '/');
}

// -- Error / retry flow --

function showErrorInAllWindows(message, details) {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isErrorState = true;

    if (bundle.sidebarView) closeSidebar(bundle);
    if (bundle.requestsPanelView) closeRequestsPanel(bundle);
    if (bundle.modalView) closeModal(bundle);

    if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
      bundle.window.contentView.removeChildView(bundle.contentView);
      bundle.contentView.webContents.close();
      bundle.contentView = null;
    }
    updateBundleBounds(bundle);

    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      const url = bundle.chromeView.webContents.getURL();
      if (!url.startsWith('file://')) {
        bundle.chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
        bundle.chromeView.webContents.once('did-finish-load', () => {
          if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
            bundle.chromeView.webContents.send('error-details', { message, details });
          }
        });
      } else {
        bundle.chromeView.webContents.send('error-details', { message, details });
      }
    }
  }
}

function prepareAllWindowsForRetry() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    if (!bundle.contentView) {
      const contentView = new WebContentsView({
        webPreferences: {
          preload: path.join(__dirname, 'content-relay-preload.js'),
          partition: CONTENT_PARTITION,
          contextIsolation: true,
          nodeIntegration: false,
        },
      });
      bundle.contentView = contentView;
      bundle.window.contentView.addChildView(contentView);
      registerShortcutsFor(bundle, contentView.webContents);
      wireContentViewEvents(bundle, contentView);
    }

    bundle.isLoadingState = true;
    updateBundleBounds(bundle);
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
    }
  }
}

function reloadAllWindowsAfterRetry() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isErrorState = false;
    bundle.isLoadingState = false;
    updateBundleBounds(bundle);
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed() && backendBaseUrl) {
      bundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
    }
    if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
      const target = bundle.preErrorUrl || (backendBaseUrl ? backendBaseUrl + '/' : null);
      if (target) bundle.contentView.webContents.loadURL(target);
    }
  }
}

function readLastLogLines(lineCount) {
  try {
    const logPath = path.join(paths.getLogDir(), 'minds.log');
    if (!fs.existsSync(logPath)) return '';
    const content = fs.readFileSync(logPath, 'utf-8');
    const lines = content.split('\n');
    return lines.slice(-lineCount).join('\n');
  } catch {
    return '';
  }
}

// -- Session state --

function loadSessionState() {
  try {
    const p = getSessionStatePath();
    if (!fs.existsSync(p)) return [];
    const raw = fs.readFileSync(p, 'utf-8');
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((e) => typeof e === 'object' && typeof e.url === 'string');
  } catch {
    return [];
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

function saveSessionState() {
  try {
    const state = [];
    for (const b of bundles) {
      if (b.window.isDestroyed()) continue;
      const url = b.preErrorUrl || b.currentContentUrl;
      const relative = toRelativeBackendUrl(url);
      if (!relative) continue;
      const bounds = b.window.getBounds();
      const display = screen.getDisplayMatching(bounds);
      state.push({
        url: relative,
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
        displayId: display ? display.id : null,
      });
    }
    const p = getSessionStatePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, JSON.stringify(state, null, 2));
  } catch (err) {
    console.log('[session] Failed to save state:', err.message);
  }
}

function filterRestorableUrls(state, knownAgentIdsSet) {
  // If we have no agent list yet, pass everything through.
  if (!knownAgentIdsSet) return state.slice();
  const results = [];
  for (const entry of state) {
    const agentId = parseWorkspaceId(entry.url);
    if (agentId && !knownAgentIdsSet.has(agentId)) {
      continue; // workspace no longer exists, skip silently
    }
    results.push(entry);
  }
  return results;
}

function restoreWindowBounds(bundle, entry) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (typeof entry.x !== 'number' || typeof entry.y !== 'number') return;
  const width = typeof entry.width === 'number' ? entry.width : 1200;
  const height = typeof entry.height === 'number' ? entry.height : 800;
  const savedBounds = { x: entry.x, y: entry.y, width, height };

  // Check if the saved display still exists
  const displays = screen.getAllDisplays();
  let targetDisplay = null;
  if (entry.displayId) {
    targetDisplay = displays.find((d) => d.id === entry.displayId);
  }
  if (!targetDisplay) {
    // Saved monitor gone -- check if bounds are visible on any display
    targetDisplay = screen.getDisplayMatching(savedBounds);
    const db = targetDisplay.bounds;
    const isVisible = savedBounds.x < db.x + db.width && savedBounds.x + savedBounds.width > db.x &&
                      savedBounds.y < db.y + db.height && savedBounds.y + savedBounds.height > db.y;
    if (!isVisible) {
      // Place on primary display at a reasonable offset
      const primary = screen.getPrimaryDisplay();
      savedBounds.x = primary.bounds.x + 50;
      savedBounds.y = primary.bounds.y + 50;
    }
  }

  bundle.window.setBounds(savedBounds);
}

// ---------- Centralized chrome SSE ----------
// Every chromeView and sidebarView used to open its own EventSource to
// /_chrome/events. Chromium caps same-host HTTP/1.1 connections at 6, so
// with a couple of workspace windows + sidebars, ALL subsequent requests
// (/_chrome/sidebar, /_chrome/requests-panel, home navigation) queue
// behind SSE streams -- you'd see load-finish latencies creep from 50ms
// to 8+ seconds. Running one SSE connection in the main process and
// broadcasting events via IPC avoids the exhaustion entirely.

function handleChromeSSEEvent(evt) {
  if (evt.type === 'workspaces' && Array.isArray(evt.workspaces)) {
    const oldIds = new Set(workspaceList.map((w) => w.id));
    latestChromeState.workspaces = evt.workspaces;
    workspaceList = evt.workspaces.map((w) => ({
      id: String(w.id),
      name: w.name ? String(w.name) : '',
      account: w.account ? String(w.account) : '',
    }));
    const newIds = new Set(workspaceList.map((w) => w.id));
    // Anything currently in destroying state stays in the ever-seen set so
    // we can recognize it as a real destroy once it later disappears from
    // the workspaces list.
    if (Array.isArray(evt.destroying_agent_ids)) {
      for (const aid of evt.destroying_agent_ids) {
        everSeenDestroying.add(String(aid));
      }
    }

    // Handle windows whose workspace disappeared. We ONLY navigate the user
    // away when we have positive evidence the workspace was destroyed (the
    // id was in destroying state at some prior tick). Otherwise (a transient
    // discovery hiccup) we leave the content view alone -- the recovery flow
    // handles the unresponsive workspace via the system_interface_status SSE
    // event, no nav required.
    for (const oldId of oldIds) {
      if (newIds.has(oldId)) continue;
      if (!everSeenDestroying.has(oldId)) continue;
      const affected = [];
      for (const b of bundles) {
        if (!b.window.isDestroyed() && b.currentWorkspaceId === oldId) {
          affected.push(b);
        }
      }
      const liveBundleCount = [...bundles].filter((b) => !b.window.isDestroyed()).length;
      for (const b of affected) {
        if (liveBundleCount - affected.length >= 1) {
          b.window.close();
        } else {
          b.currentWorkspaceId = null;
          if (b.contentView && !b.contentView.webContents.isDestroyed() && backendBaseUrl) {
            b.contentView.webContents.loadURL(backendBaseUrl + '/');
          }
          updateOsTitle(b);
        }
      }
    }

    updateAllOsTitles();
  } else if (evt.type === 'auth_status') {
    latestChromeState.authStatus = evt;
  } else if (evt.type === 'requests') {
    const prevIds = latestChromeState.requestIds || [];
    const newIds = Array.isArray(evt.request_ids) ? evt.request_ids.map(String) : [];
    const newCount = evt.count || 0;
    // Backend defaults auto_open to true; treat a missing field the same way.
    const autoOpen = evt.auto_open !== false;
    // Diff the pending *set* (ordered ids), not the count, so a swap at
    // constant size still refreshes the panel. Auto-open keys off a
    // genuinely new id appearing (not a count increase, which is blind to
    // replacements), so approving/denying never reopens a panel the user
    // closed.
    const prevSet = new Set(prevIds);
    const hasNewRequest = newIds.some((id) => !prevSet.has(id));
    const idsChanged = newIds.length !== prevIds.length || hasNewRequest;
    latestChromeState.requestIds = newIds;
    latestChromeState.requestCount = newCount;
    const shouldAutoOpen = autoOpen && hasNewRequest;
    // Requests panel HTML is static at load time. Refresh visible panels so
    // their cards reflect the new pending set whenever it changed, OR open
    // hidden ones when shouldAutoOpen is set. ``openRequestsPanel`` reloads
    // the panel itself for the visible-bundle case, so we never need to
    // schedule a reload on top of an open call. Debounced per-bundle so a
    // burst of changes coalesces into one reload per panel.
    if (idsChanged || shouldAutoOpen) {
      for (const b of bundles) {
        if (shouldAutoOpen && !b.requestsPanelVisible) {
          openRequestsPanel(b);
        } else {
          scheduleRequestsPanelReload(b);
        }
      }
    }
  }
  broadcastChromeEvent(evt);
}

function broadcastChromeEvent(evt) {
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    for (const view of [b.chromeView, b.sidebarView]) {
      if (!view) continue;
      if (view.webContents.isDestroyed()) continue;
      try {
        view.webContents.send('chrome-event', evt);
      } catch { /* noop */ }
    }
  }
}

function primeViewWithCachedChromeState(wc) {
  if (!wc || wc.isDestroyed()) return;
  if (latestChromeState.workspaces !== null) {
    wc.send('chrome-event', { type: 'workspaces', workspaces: latestChromeState.workspaces });
  }
  if (latestChromeState.authStatus) {
    wc.send('chrome-event', latestChromeState.authStatus);
  }
  wc.send('chrome-event', {
    type: 'requests',
    count: latestChromeState.requestCount,
    request_ids: latestChromeState.requestIds,
  });
}

function kickChromeSSEReconnect() {
  chromeSseReconnectTick += 1;
  const req = chromeSseAbortRef.current;
  if (req) {
    try { req.abort(); } catch { /* noop */ }
  }
  // ``req.abort()`` does not reliably emit a terminal event on Electron's
  // ClientRequest, so resolve the in-flight connection promise directly to
  // guarantee the loop reconnects rather than wedging on an unresolved await.
  const finish = chromeSseFinishRef.current;
  if (finish) finish();
}

async function runChromeSSELoop() {
  // Runs until the app is shutting down. Maintains exactly one SSE
  // connection to /_chrome/events, reconnecting on end/error with backoff.
  while (!isShuttingDown) {
    if (!backendBaseUrl) {
      await sleepInterruptible(500);
      continue;
    }
    await new Promise((resolve) => {
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        chromeSseAbortRef.current = null;
        chromeSseFinishRef.current = null;
        resolve();
      };
      let req;
      try {
        req = net.request({
          url: backendBaseUrl + '/_chrome/events',
          method: 'GET',
          useSessionCookies: true,
        });
      } catch {
        finish();
        return;
      }
      chromeSseAbortRef.current = req;
      chromeSseFinishRef.current = finish;
      req.setHeader('Accept', 'text/event-stream');
      req.on('response', (response) => {
        if (response.statusCode !== 200) {
          response.on('data', () => {});
          response.on('end', () => finish());
          response.on('error', () => finish());
          return;
        }
        let buffer = '';
        response.on('data', (chunk) => {
          buffer += chunk.toString();
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';
          for (const part of parts) {
            const dataLines = part.split('\n').filter((l) => l.startsWith('data:'));
            if (dataLines.length === 0) continue;
            const payload = dataLines.map((l) => l.slice(5).trim()).join('');
            if (!payload) continue;
            try {
              handleChromeSSEEvent(JSON.parse(payload));
            } catch { /* ignore bad frames */ }
          }
        });
        response.on('end', () => finish());
        response.on('error', () => finish());
        response.on('aborted', () => finish());
      });
      req.on('error', () => finish());
      req.end();
    });
    // Brief backoff before reconnecting.
    await sleepInterruptible(1500);
  }
}

// POST a restart endpoint (``restart-system-interface`` or ``restart-host``)
// and resolve once the server has acknowledged the 202 dispatch (or the
// request errors / times out). The endpoints return 202 immediately and
// drive recovery asynchronously; the 202 also means the health tracker is
// already RESTARTING, so callers navigate to the recovery page afterward,
// which shows restart progress and returns to the workspace once healthy.
//
// Always resolves (never rejects) so callers can chain navigation
// regardless of network outcome.
const RESTART_REQUEST_TIMEOUT_MS = 10000;
function postRestart(agentId, endpointPath) {
  return new Promise((resolve) => {
    if (!agentId || !backendBaseUrl) {
      resolve();
      return;
    }
    let req;
    try {
      req = net.request({
        url: `${backendBaseUrl}/api/agents/${encodeURIComponent(agentId)}/${endpointPath}`,
        method: 'POST',
        useSessionCookies: true,
      });
    } catch (e) {
      console.warn('[restart] failed to construct restart request:', e);
      resolve();
      return;
    }
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const timer = setTimeout(() => {
      console.warn(`[restart] restart API timed out for ${agentId} after ${RESTART_REQUEST_TIMEOUT_MS}ms`);
      try { req.abort(); } catch (_) { /* ignore */ }
      settle();
    }, RESTART_REQUEST_TIMEOUT_MS);
    req.on('response', (response) => {
      response.on('data', () => {});
      response.on('end', () => {
        clearTimeout(timer);
        settle();
      });
      response.on('error', () => {
        clearTimeout(timer);
        settle();
      });
      if (response.statusCode >= 400) {
        console.warn(`[restart] restart API returned ${response.statusCode} for ${agentId}`);
      }
    });
    req.on('error', (err) => {
      console.warn(`[restart] restart API request failed for ${agentId}:`, err);
      clearTimeout(timer);
      settle();
    });
    req.end();
  });
}

function sleepInterruptible(ms) {
  const tick = chromeSseReconnectTick;
  return new Promise((resolve) => {
    const interval = 200;
    let elapsed = 0;
    const timer = setInterval(() => {
      elapsed += interval;
      if (isShuttingDown || tick !== chromeSseReconnectTick || elapsed >= ms) {
        clearInterval(timer);
        resolve();
      }
    }, interval);
  });
}

function fetchInitialChromeState(timeoutMs = 4000) {
  // Drives one round-trip to /_chrome/events (SSE) to learn both auth status
  // and the current workspace list. Returns:
  //   { authenticated: true, workspaces: [...] }  on authenticated success
  //   { authenticated: false }                     when the backend says auth_required
  //   null                                          on timeout / network error
  return new Promise((resolve) => {
    if (!backendBaseUrl) {
      resolve(null);
      return;
    }
    let done = false;
    let req;
    const finish = (value) => {
      if (done) return;
      done = true;
      if (req) {
        try { req.abort(); } catch { /* noop */ }
      }
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    try {
      req = net.request({
        url: backendBaseUrl + '/_chrome/events',
        method: 'GET',
        useSessionCookies: true,
      });
    } catch {
      clearTimeout(timer);
      resolve(null);
      return;
    }
    req.setHeader('Accept', 'text/event-stream');
    let buffer = '';
    req.on('response', (response) => {
      if (response.statusCode !== 200) {
        clearTimeout(timer);
        finish(null);
        return;
      }
      response.on('data', (chunk) => {
        buffer += chunk.toString();
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const part of parts) {
          const dataLines = part.split('\n').filter((l) => l.startsWith('data:'));
          if (dataLines.length === 0) continue;
          const payload = dataLines.map((l) => l.slice(5).trim()).join('');
          if (!payload) continue;
          try {
            const parsed = JSON.parse(payload);
            if (parsed.type === 'workspaces' && Array.isArray(parsed.workspaces)) {
              clearTimeout(timer);
              finish({ authenticated: true, workspaces: parsed.workspaces, hasAccounts: !!parsed.has_accounts });
              return;
            }
            if (parsed.type === 'auth_required') {
              clearTimeout(timer);
              finish({ authenticated: false });
              return;
            }
          } catch { /* ignore invalid frames */ }
        }
      });
      response.on('end', () => {
        clearTimeout(timer);
        finish(null);
      });
      response.on('error', () => {
        clearTimeout(timer);
        finish(null);
      });
    });
    req.on('error', () => {
      clearTimeout(timer);
      finish(null);
    });
    req.end();
  });
}

// -- Single instance lock --

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const mru = getMostRecentWindow();
    if (mru) focusBundle(mru);
  });
  app.whenReady().then(onReady);
}

// -- Content partition cookie sync --
// Auth happens in the contentView (which uses CONTENT_PARTITION). The main
// process SSE and chrome/sidebar views use the default session. We sync
// minds_session cookies from the content partition to the default session
// so that chrome-level auth checks work.

function setupContentPartitionCookieSync() {
  const contentSession = session.fromPartition(CONTENT_PARTITION);
  contentSession.cookies.on('changed', (_event, cookie, _cause, removed) => {
    if (cookie.name !== 'minds_session' || removed) return;
    const domain = (cookie.domain || 'localhost').replace(/^\./, '');
    const url = `http://${domain}`;
    session.defaultSession.cookies.set({
      url,
      name: cookie.name,
      value: cookie.value,
      httpOnly: cookie.httpOnly,
      path: cookie.path || '/',
      sameSite: cookie.sameSite || 'lax',
    }).then(() => {
      kickChromeSSEReconnect();
    }).catch((err) => {
      console.warn('[cookie-sync] Failed to sync cookie to default session:', err);
    });
  });
}

async function syncContentCookiesToDefaultSession() {
  const contentSession = session.fromPartition(CONTENT_PARTITION);
  let cookies;
  try {
    cookies = await contentSession.cookies.get({ name: 'minds_session' });
  } catch (err) {
    console.warn('[cookie-sync] Failed to read cookies from content partition:', err);
    return;
  }
  for (const cookie of cookies) {
    const domain = (cookie.domain || 'localhost').replace(/^\./, '');
    const url = `http://${domain}`;
    try {
      await session.defaultSession.cookies.set({
        url,
        name: cookie.name,
        value: cookie.value,
        httpOnly: cookie.httpOnly,
        path: cookie.path || '/',
        sameSite: cookie.sameSite || 'lax',
      });
    } catch (err) {
      console.warn('[cookie-sync] Failed to sync cookie to default session:', err);
    }
  }
}

async function onReady() {
  // Send external links to the user's default browser for every WebContents
  // the app ever creates (all four bundle views plus any popup windows),
  // rather than wiring each view individually. Registered before the first
  // bundle is created so it covers the initial chrome/content views too.
  app.on('web-contents-created', (_event, contents) => {
    applyExternalLinkHandling(contents);
  });
  installApplicationMenu();
  installDockMenu();
  setupContentPartitionCookieSync();
  await syncContentCookiesToDefaultSession();

  initialBundle = createBundle();
  await runStartupSequence(initialBundle);
}

// User-initiated update check from the app menu's Check for Updates item.
// autoUpdater.checkForUpdates() resolves to { updateInfo }: present when a
// newer version exists (then downloaded in the background), absent when current.
async function triggerUpdateCheck() {
  const autoUpdater = todesktop.autoUpdater;
  if (!autoUpdater || typeof autoUpdater.checkForUpdates !== 'function') {
    dialog.showMessageBox({
      type: 'info',
      message: 'Update check unavailable.',
      detail: app.isPackaged
        ? 'The auto-updater is disabled until this build is released to the latest channel.'
        : 'Updates are only available in installed builds.',
    });
    return;
  }
  try {
    const result = await autoUpdater.checkForUpdates();
    const updateInfo = result && result.updateInfo;
    if (updateInfo) {
      const v = updateInfo.version || updateInfo.releaseName;
      dialog.showMessageBox({
        type: 'info',
        message: v ? `Update ${v} found.` : 'Update found.',
        detail: 'Downloading in the background. You will be prompted to restart when it is ready.',
      });
    } else {
      dialog.showMessageBox({
        type: 'info',
        message: "You're up to date.",
        detail: 'No newer version is available.',
      });
    }
  } catch (err) {
    dialog.showMessageBox({
      type: 'error',
      message: 'Update check failed.',
      detail: String(err && err.message ? err.message : err),
    });
  }
}

function installApplicationMenu() {
  if (!isMac || process.env.MINDS_HIDE_MENU === '1') {
    // On Windows/Linux the frame is custom-drawn; on macOS with MINDS_HIDE_MENU
    // the user explicitly asked for no menu. cmd/ctrl+N still works via
    // `registerShortcutsFor` in each bundle.
    Menu.setApplicationMenu(null);
    appMenuInstalled = false;
    return;
  }
  appMenuInstalled = true;
  const template = [
    {
      label: app.name || 'Minds',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { label: 'Check for Updates...', click: triggerUpdateCheck },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'File',
      submenu: [
        {
          label: 'New Window',
          accelerator: 'CmdOrCtrl+N',
          click: () => openHomeInNewWindow(),
        },
        { type: 'separator' },
        {
          label: 'Close Window',
          accelerator: 'CmdOrCtrl+W',
          click: () => {
            const target = getMostRecentWindow();
            if (target && !target.window.isDestroyed()) target.window.close();
          },
        },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        {
          label: 'Toggle Developer Tools',
          // role: 'toggleDevTools' targets a BrowserWindow; we use BaseWindow,
          // so toggle the focused bundle's content view explicitly.
          accelerator: 'Alt+Cmd+I',
          click: () => {
            const bundle = getMostRecentWindow();
            if (!bundle || bundle.window.isDestroyed()) return;
            const cv = bundle.contentView;
            if (cv && !cv.webContents.isDestroyed()) {
              cv.webContents.toggleDevTools();
            }
          },
        },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function installDockMenu() {
  if (!isMac || !app.dock) return;
  app.dock.setMenu(Menu.buildFromTemplate([
    {
      label: 'New Window',
      click: () => openHomeInNewWindow(),
    },
  ]));
}

async function runStartupSequence(bundle) {
  console.log('[startup] Loading shell.html in chrome view...');
  bundle.isLoadingState = true;
  updateBundleBounds(bundle);
  await bundle.chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
  console.log('[startup] shell.html loaded');

  try {
    await runEnvSetup((status) => {
      if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
        bundle.chromeView.webContents.send('status-update', status);
      }
    });
  } catch (err) {
    console.error('[startup] env-setup failed:', err.message);
    showErrorInAllWindows(
      'Setup failed -- you may not be connected to the internet',
      err.message,
    );
    return;
  }

  await startBackendWithRetry();
}

function broadcastStatusToLoadingWindows(status) {
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    if (!b.isLoadingState) continue;
    if (b.chromeView && !b.chromeView.webContents.isDestroyed()) {
      b.chromeView.webContents.send('status-update', status);
    }
  }
}

async function startBackendWithRetry() {
  broadcastStatusToLoadingWindows('Starting Minds...');

  try {
    const { loginUrl, port } = await startBackend(
      (status) => broadcastStatusToLoadingWindows(status),
      (event) => handleNotification(event),
      (event) => handleAuthEvent(event),
      (event) => handleMngrForwardStarted(event),
    );

    // Use `localhost` (not `127.0.0.1`) so the auth cookie, which is issued with
    // `Domain=localhost`, is valid both here and on every `<agent-id>.localhost`
    // subdomain the desktop client forwards to.
    backendBaseUrl = `http://localhost:${port}`;

    console.log('[startup] Backend ready. Loading chrome from', backendBaseUrl + '/_chrome');

    // Kick off the shared chrome-events SSE consumer (idempotent: only starts once).
    if (!runChromeSSELoop._started) {
      runChromeSSELoop._started = true;
      runChromeSSELoop();
    } else {
      // On retry after backend restart, force the live connection to reconnect.
      kickChromeSSEReconnect();
    }

    const isFirstStart = !hasCompletedInitialStart;
    hasCompletedInitialStart = true;

    if (isFirstStart && initialBundle && !initialBundle.window.isDestroyed()) {
      const savedState = loadSessionState();

      // Consume the one-time login code via net.request BEFORE checking
      // chrome state. This hits /authenticate which sets the minds_session
      // cookie in the default session (used by net.request, chromeView, and
      // SSE). We follow the redirect so Electron processes the Set-Cookie
      // header, then copy the cookie to the content partition.
      await new Promise((resolve) => {
        const authenticateUrl = loginUrl.replace('/login?', '/authenticate?');
        console.log('[startup] Consuming one-time code via', authenticateUrl);
        const req = net.request({ url: authenticateUrl, method: 'GET', useSessionCookies: true });
        req.on('response', async (resp) => {
          console.log('[startup] /authenticate response status:', resp.statusCode);
          resp.on('data', () => {});
          resp.on('end', async () => {
            try {
              const defaultCookies = await session.defaultSession.cookies.get({ name: 'minds_session' });
              console.log('[startup] Default session cookies after /authenticate:', defaultCookies.length);
              const contentSession = session.fromPartition(CONTENT_PARTITION);
              for (const c of defaultCookies) {
                const domain = (c.domain || 'localhost').replace(/^\./, '');
                await contentSession.cookies.set({
                  url: `http://${domain}`,
                  name: c.name, value: c.value,
                  httpOnly: c.httpOnly, path: c.path || '/',
                  sameSite: c.sameSite || 'lax',
                });
              }
              console.log('[startup] Cookie synced to content partition');
            } catch (err) {
              console.warn('[startup] Failed to sync cookie to content partition:', err);
            }
            resolve();
          });
        });
        req.on('error', (err) => {
          console.warn('[startup] /authenticate request failed:', err);
          resolve();
        });
        req.end();
      });

      const chromeState = await fetchInitialChromeState();
      const authenticated = chromeState && chromeState.authenticated;

      if (authenticated && chromeState.workspaces) {
        workspaceList = chromeState.workspaces.map((w) => ({
          id: String(w.id),
          name: w.name ? String(w.name) : '',
          account: w.account ? String(w.account) : '',
        }));
      }

      const knownAgentIdsSet = authenticated
        ? new Set(workspaceList.map((w) => w.id))
        : null;
      const restorable = authenticated
        ? filterRestorableUrls(savedState, knownAgentIdsSet)
        : [];

      initialBundle.isLoadingState = false;
      updateBundleBounds(initialBundle);
      if (initialBundle.chromeView && !initialBundle.chromeView.webContents.isDestroyed()) {
        initialBundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
      }

      if (!authenticated) {
        // The one-time code was already consumed above but fetchInitialChromeState
        // still returned unauthenticated (should not happen, but handle gracefully).
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(backendBaseUrl + '/welcome');
        }
      } else if (!chromeState.hasAccounts && restorable.length === 0) {
        // Locally authenticated but user has never signed in with SuperTokens
        // and has no saved windows -- show the welcome/onboarding page.
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(backendBaseUrl + '/welcome');
        }
      } else if (restorable.length === 0) {
        // Has accounts but nothing to restore -- land on the create page.
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(backendBaseUrl + '/');
        }
      } else {
        // Restore saved windows with their positions and sizes
        const [first, ...rest] = restorable;
        restoreWindowBounds(initialBundle, first);
        loadUrlIntoBundleContentView(initialBundle, toAbsoluteUrl(first.url));
        for (const entry of rest) {
          const bundle = openNewWindow(toAbsoluteUrl(entry.url));
          restoreWindowBounds(bundle, entry);
        }
      }
    } else {
      // Retry path: re-load every existing window
      reloadAllWindowsAfterRetry();
    }

    const proc = getBackendProcess();
    if (proc) {
      proc.on('exit', (code) => {
        if (code !== 0 && code !== null && bundles.size > 0) {
          const logContent = readLastLogLines(50);
          showErrorInAllWindows(
            'Minds stopped unexpectedly',
            logContent || `Process exited with code ${code}`,
          );
        }
      });
    }
  } catch (err) {
    showErrorInAllWindows('Failed to start Minds', err.message);
  }
}

function handleNotification(event) {
  const agentName = event.agent_name || 'Agent';
  const title = event.title || `Notification from ${agentName}`;
  const notification = new Notification({
    title,
    body: event.message,
  });
  notification.on('click', () => {
    const url = event.url;
    if (!url) {
      const mru = getMostRecentWindow();
      if (mru) focusBundle(mru);
      return;
    }
    const absolute = toAbsoluteUrl(url);
    const agentId = parseWorkspaceId(absolute);
    if (agentId) {
      openOrFocusWorkspace(agentId, absolute);
    } else {
      const mru = getMostRecentWindow();
      if (mru && mru.contentView && !mru.contentView.webContents.isDestroyed()) {
        focusBundle(mru);
        mru.contentView.webContents.loadURL(absolute);
      }
    }
  });
  notification.show();
}

// Pre-set the plugin's session cookie on `localhost:<mngr_forward_port>` so
// the user is already authenticated to the `mngr forward` plugin before any
// agent-subdomain navigation. The plugin's server treats a cookie value
// matching the freshly-minted preauth token as authenticated -- see
// `libs/mngr_forward/imbue/mngr_forward/cookie.py::verify_session_cookie`.
//
// Mirrors the cookie into the workspace content partition so any
// chrome/iframe / WebContentsView using that partition is authenticated too.
async function handleMngrForwardStarted(event) {
  const port = event.mngr_forward_port;
  const preauth = event.preauth_cookie;
  if (!port || !preauth) {
    console.warn('[startup] mngr_forward_started missing port or preauth_cookie:', event);
    return;
  }
  const url = `http://localhost:${port}`;
  // Cache the plugin origin so workspaceUrlForAgent() can build /goto/ URLs
  // against the correct port (the plugin, not minds).
  mngrForwardBaseUrl = url;
  const baseSpec = {
    url,
    name: 'mngr_forward_session',
    value: preauth,
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
  };
  try {
    await session.defaultSession.cookies.set(baseSpec);
    const contentSession = session.fromPartition(CONTENT_PARTITION);
    await contentSession.cookies.set(baseSpec);
    console.log('[startup] mngr_forward_session cookie pre-set on', url);
  } catch (err) {
    console.warn('[startup] Failed to set mngr_forward_session cookie:', err);
  }
}


function handleAuthEvent(event) {
  if (event.event === 'auth_success') {
    for (const b of bundles) {
      if (b.window.isDestroyed()) continue;
      if (b.chromeView && !b.chromeView.webContents.isDestroyed()) {
        b.chromeView.webContents.reload();
      }
    }
  } else if (event.event === 'auth_required') {
    const mru = getMostRecentWindow();
    if (!mru) return;
    focusBundle(mru);
    if (mru.contentView && !mru.contentView.webContents.isDestroyed() && backendBaseUrl) {
      const authUrl = `${backendBaseUrl}/auth/login?message=` +
        encodeURIComponent('You need to sign in to Imbue in order to share');
      mru.contentView.webContents.loadURL(authUrl);
    }
  }
}

// -- IPC handlers --

ipcMain.on('go-home', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !backendBaseUrl) return;
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.webContents.loadURL(backendBaseUrl + '/');
  }
});

ipcMain.on('navigate-content', (event, url) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle) return;
  const absolute = toAbsoluteUrl(url);
  const targetAgentId = parseWorkspaceId(absolute);

  if (targetAgentId) {
    const existing = findBundleForWorkspace(targetAgentId);
    if (existing) {
      focusBundle(existing);
      closeSidebar(bundle);
      return;
    }
  }

  // Nobody is on this workspace (or it's a non-workspace URL): navigate sender
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.webContents.loadURL(absolute);
  }
  closeSidebar(bundle);
});

ipcMain.on('content-go-back', (event) => {
  const bundle = getBundleFromEvent(event);
  if (bundle && bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.webContents.goBack();
  }
});

ipcMain.on('content-go-forward', (event) => {
  const bundle = getBundleFromEvent(event);
  if (bundle && bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.webContents.goForward();
  }
});

ipcMain.on('toggle-sidebar', (event) => {
  toggleSidebar(getBundleFromEvent(event));
});

ipcMain.on('toggle-requests-panel', (event) => {
  toggleRequestsPanel(getBundleFromEvent(event));
});

ipcMain.on('open-requests-panel', (event) => {
  const bundle = getBundleFromEvent(event);
  openRequestsPanel(bundle);
});

ipcMain.on('open-workspace-in-new-window', (event, agentId) => {
  if (!agentId) return;
  openOrFocusWorkspace(agentId, workspaceUrlForAgent(agentId));
  // The sidebar is the sender for both the hover-icon click and the native
  // context-menu "Open in new window" item; close it now that the action is done.
  const bundle = getBundleFromEvent(event);
  if (bundle) closeSidebar(bundle);
});

ipcMain.on('navigate-to-request', (event, _agentId, eventId) => {
  if (!eventId) return;
  const url = toAbsoluteUrl('/requests/' + eventId);
  // Open the request in a modal overlay in the window the user clicked from,
  // rather than navigating its content view. This keeps the user's workspace
  // exactly as they left it -- closing the dialog returns them to their work
  // with no context lost, and no window switching.
  const sender = getBundleFromEvent(event);
  if (sender) openModal(sender, url);
});

// Open a permission-request modal on behalf of the (otherwise unprivileged)
// workspace content view. Only content-relay-preload.js can emit this channel
// -- the page itself never sees ipcRenderer -- and it does so only for an
// allowlisted `minds:open-request-modal` postMessage. We re-validate the id
// here (never trust the renderer) before building the `/requests/<id>` URL,
// then reuse the same modal path as the requests-panel card click above.
ipcMain.on('open-request-modal', (event, requestId) => {
  if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
  const sender = getBundleFromEvent(event);
  if (sender) openModal(sender, toAbsoluteUrl('/requests/' + requestId));
});

ipcMain.on('close-modal', (event) => {
  closeModal(getBundleFromEvent(event));
});

ipcMain.on('show-workspace-context-menu', (event, agentId, x, y) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !agentId) return;
  const isCurrent = bundle.currentWorkspaceId === agentId;
  const workspaceUrl = workspaceUrlForAgent(agentId);
  const template = [];
  // Don't offer "Open in new window" if the sender's window is already on this workspace.
  if (!isCurrent) {
    template.push({
      label: 'Open in new window',
      click: () => {
        openOrFocusWorkspace(agentId, workspaceUrl);
        closeSidebar(bundle);
      },
    });
    template.push({ type: 'separator' });
  }
  const goToRecoveryView = () => {
    if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
      // The restart POST has already moved the health tracker to RESTARTING,
      // so the recovery page renders its "Restarting…" progress state and
      // auto-refreshes itself until the workspace is healthy again, then
      // navigates back to ``workspaceUrl``. Reloading the workspace URL directly would
      // instead race the restart: the container is still up at dispatch
      // time, so the reload would just show the pre-restart workspace.
      bundle.contentView.webContents.loadURL(
        toAbsoluteUrl(
          '/agents/' + encodeURIComponent(agentId)
          + '/recovery?return_to=' + encodeURIComponent(workspaceUrl || ''),
        ),
      );
    }
  };
  template.push({
    label: 'Restart system interface',
    click: async () => {
      // Close the sidebar first so the user gets immediate visual feedback
      // while the restart dispatch is acknowledged.
      closeSidebar(bundle);
      await postRestart(agentId, 'restart-system-interface');
      goToRecoveryView();
    },
  });
  template.push({
    label: 'Restart workspace…',
    click: async () => {
      // A host restart interrupts every agent in the workspace, so confirm
      // before dispatching it.
      const { response } = await dialog.showMessageBox(bundle.window, {
        type: 'warning',
        buttons: ['Cancel', 'Restart workspace'],
        defaultId: 0,
        cancelId: 0,
        message: 'Restart this workspace?',
        detail: 'This restarts the whole workspace. In-progress work in all agents will be interrupted.',
      });
      if (response !== 1) return;
      closeSidebar(bundle);
      await postRestart(agentId, 'restart-host');
      goToRecoveryView();
    },
  });
  const menu = Menu.buildFromTemplate(template);
  // sidebar coords are relative to the sidebar view, which sits at (0, TITLEBAR_HEIGHT)
  const px = Math.round(x || 0);
  const py = Math.round((y || 0) + TITLEBAR_HEIGHT);
  menu.popup({ window: bundle.window, x: px, y: py });
});

ipcMain.on('retry', async (event) => {
  // User clicked retry from one window's error screen. Shut down the old
  // backend (if any), put all windows back in loading state, then restart.
  const senderBundle = getBundleFromEvent(event);
  if (senderBundle) focusBundle(senderBundle);
  await shutdown();
  prepareAllWindowsForRetry();
  await startBackendWithRetry();
});

ipcMain.on('close-workspace-windows', (_event, agentId) => {
  if (!agentId) return;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    if (b.currentWorkspaceId === agentId) {
      b.window.close();
    }
  }
});

ipcMain.on('open-log-file', () => {
  const logPath = path.join(paths.getLogDir(), 'minds.log');
  shell.openPath(logPath);
});

ipcMain.on('window-minimize', (event) => {
  const bundle = getBundleFromEvent(event);
  if (bundle && !bundle.window.isDestroyed()) bundle.window.minimize();
});

ipcMain.on('window-maximize', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || bundle.window.isDestroyed()) return;
  const win = bundle.window;
  if (win.isMaximized() || bundle._maximizedByUs) {
    win.unmaximize();
    if (bundle._boundsBeforeMaximize) {
      win.setBounds(bundle._boundsBeforeMaximize);
      bundle._boundsBeforeMaximize = null;
    }
    bundle._maximizedByUs = false;
  } else {
    bundle._boundsBeforeMaximize = win.getBounds();
    win.maximize();
  }
});

ipcMain.on('window-close', (event) => {
  const bundle = getBundleFromEvent(event);
  if (bundle && !bundle.window.isDestroyed()) bundle.window.close();
});

// -- App lifecycle --

function initiateFullQuit() {
  app.quit();
}

// Route POSIX SIGTERM / SIGINT through `app.quit()` so they trigger the
// same `before-quit` chain that window-close uses (which already runs
// `backend.shutdown()`, SIGTERMing the python backend and waiting for
// uvicorn's graceful exit). Without these handlers Node's default for
// these signals is to exit immediately, which orphans the python backend
// and the `mngr forward` / `observe` subprocesses. The `just minds-stop`
// recipe sends SIGTERM to this process so the clean-shutdown chain can run.
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    console.log(`[lifecycle] ${signal} received, requesting app.quit()`);
    app.quit();
  });
}

app.on('window-all-closed', async () => {
  console.log('[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  if (isShuttingDown) return;
  isShuttingDown = true;
  await shutdown();
  app.quit();
});

app.on('before-quit', async (event) => {
  console.log('[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
  // Capture session state for every open window before teardown. Only save
  // when bundles is non-empty: on the `window-all-closed` -> `app.quit()`
  // path, every bundle has already been removed from the Set by its `closed`
  // handler (and the per-window `close` handler already wrote the last
  // non-empty snapshot), so saving here would just clobber it with `[]`.
  if (bundles.size > 0) saveSessionState();
  if (getBackendProcess() && !isShuttingDown) {
    isShuttingDown = true;
    event.preventDefault();
    await shutdown();
    app.quit();
  }
});
