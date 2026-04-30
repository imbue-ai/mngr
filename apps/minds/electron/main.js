const { BaseWindow, WebContentsView, Menu, Notification, ipcMain, net, shell, app, session, screen } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');
const logger = require('./logger');

todesktop.init();

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
let workspaceList = []; // [{id, name, account}]
let isShuttingDown = false;
let initialBundle = null; // the first window created at startup
let hasCompletedInitialStart = false;

// Central cache of the latest SSE state from /_chrome/events so newly-loaded
// chrome/sidebar webContents can be primed without opening their own SSE
// connection.
const latestChromeState = {
  workspaces: null, // most recent workspaces payload
  authStatus: null, // most recent auth_status payload
  requestCount: 0,  // most recent request_count value
};

const chromeSseAbortRef = { current: null };
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

// Build the auth-bridge URL that, when loaded, installs a session cookie on
// the agent's subdomain and redirects into the workspace's dockview UI.
// Returns null if the backend hasn't come up yet.
function workspaceUrlForAgent(agentId) {
  if (!agentId || !backendBaseUrl) return null;
  return `${backendBaseUrl}/goto/${encodeURIComponent(agentId)}/`;
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
    const views = [b.chromeView, b.contentView, b.sidebarView, b.requestsPanelView];
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
      partition: CONTENT_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.contentView.addChildView(chromeView);
  win.contentView.addChildView(contentView);
  logger.attachConsoleListener(chromeView.webContents, 'chrome');
  logger.attachConsoleListener(contentView.webContents, 'content');
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
    const views = [bundle.chromeView, bundle.contentView, bundle.sidebarView, bundle.requestsPanelView];
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
      sendCurrentWorkspaceToBundleSidebar(bundle);
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
    logger.attachConsoleListener(sidebarView.webContents, 'sidebar');
    sidebarView.webContents.on('did-finish-load', () => {
      sendCurrentWorkspaceToBundleSidebar(bundle);
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
    logger.attachConsoleListener(panel.webContents, 'requests-panel');
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

// Coalesce rapid SSE-triggered reloads. A burst of request_count events
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

function sendCurrentWorkspaceToBundleSidebar(bundle) {
  if (!bundle || !bundle.sidebarView) return;
  if (bundle.sidebarView.webContents.isDestroyed()) return;
  bundle.sidebarView.webContents.send('current-workspace-changed', bundle.currentWorkspaceId);
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
    sendCurrentWorkspaceToBundleSidebar(bundle);
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
          partition: CONTENT_PARTITION,
          contextIsolation: true,
          nodeIntegration: false,
        },
      });
      bundle.contentView = contentView;
      bundle.window.contentView.addChildView(contentView);
      registerShortcutsFor(bundle, contentView.webContents);
      logger.attachConsoleListener(contentView.webContents, 'content');
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
    logger.logMain('warning', '[session] Failed to save state: ' + err.message);
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

    // Handle windows for destroyed workspaces: close them if other
    // windows exist, otherwise navigate to home so the user isn't left
    // with nothing.
    for (const oldId of oldIds) {
      if (!newIds.has(oldId)) {
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
    }

    updateAllOsTitles();
  } else if (evt.type === 'auth_status') {
    latestChromeState.authStatus = evt;
  } else if (evt.type === 'request_count') {
    latestChromeState.requestCount = evt.count || 0;
    // Requests panel HTML is static at load time. Refresh any visible panels
    // so their cards reflect the new pending list. Debounced per-bundle so
    // a burst of count changes coalesces into one reload per panel.
    for (const b of bundles) {
      scheduleRequestsPanelReload(b);
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
  wc.send('chrome-event', { type: 'request_count', count: latestChromeState.requestCount });
}

function kickChromeSSEReconnect() {
  chromeSseReconnectTick += 1;
  const req = chromeSseAbortRef.current;
  if (req) {
    try { req.abort(); } catch { /* noop */ }
  }
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
      req.setHeader('Accept', 'text/event-stream');
      req.on('response', (response) => {
        if (response.statusCode !== 200) {
          response.on('data', () => {});
          response.on('end', finish);
          response.on('error', finish);
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
        response.on('end', finish);
        response.on('error', finish);
      });
      req.on('error', finish);
      req.end();
    });
    // Brief backoff before reconnecting.
    await sleepInterruptible(1500);
  }
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
      logger.logMain('warning', '[cookie-sync] Failed to sync cookie to default session: ' + err.message);
    });
  });
}

async function syncContentCookiesToDefaultSession() {
  const contentSession = session.fromPartition(CONTENT_PARTITION);
  let cookies;
  try {
    cookies = await contentSession.cookies.get({ name: 'minds_session' });
  } catch (err) {
    logger.logMain('warning', '[cookie-sync] Failed to read cookies from content partition: ' + err.message);
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
      logger.logMain('warning', '[cookie-sync] Failed to sync cookie to default session: ' + err.message);
    }
  }
}

async function onReady() {
  logger.init();
  installApplicationMenu();
  installDockMenu();
  setupContentPartitionCookieSync();
  await syncContentCookiesToDefaultSession();

  initialBundle = createBundle();
  await runStartupSequence(initialBundle);
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
  logger.logMain('info', '[startup] Loading shell.html in chrome view...');
  bundle.isLoadingState = true;
  updateBundleBounds(bundle);
  await bundle.chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
  logger.logMain('info', '[startup] shell.html loaded');

  try {
    await runEnvSetup((status) => {
      if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
        bundle.chromeView.webContents.send('status-update', status);
      }
    });
  } catch (err) {
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
    );

    // Use `localhost` (not `127.0.0.1`) so the auth cookie, which is issued with
    // `Domain=localhost`, is valid both here and on every `<agent-id>.localhost`
    // subdomain the desktop client forwards to.
    backendBaseUrl = `http://localhost:${port}`;
    logger.setBackendPort(port);

    logger.logMain('info', `[startup] Backend ready. Loading chrome from ${backendBaseUrl}/_chrome`);

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

ipcMain.on('navigate-to-request', (event, agentId, eventId) => {
  if (!eventId) return;
  const url = toAbsoluteUrl('/requests/' + eventId);
  const sender = getBundleFromEvent(event);
  // Route to the workspace's window when one is open so the request page
  // lives alongside the workspace it's about, rather than wherever the user
  // happened to click the request card from.
  if (agentId) {
    const existing = findBundleForWorkspace(agentId);
    if (existing) {
      focusBundle(existing);
      if (existing.contentView && !existing.contentView.webContents.isDestroyed()) {
        existing.contentView.webContents.loadURL(url);
      }
      return;
    }
  }
  // Fallback: no window for this workspace -- open the request in the sender.
  if (sender && sender.contentView && !sender.contentView.webContents.isDestroyed()) {
    sender.contentView.webContents.loadURL(url);
  }
});

ipcMain.on('show-workspace-context-menu', (event, agentId, x, y) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !agentId) return;
  // Don't offer "Open in new window" if the sender's window is already on this workspace
  if (bundle.currentWorkspaceId === agentId) return;
  const menu = Menu.buildFromTemplate([
    {
      label: 'Open in new window',
      click: () => {
        openOrFocusWorkspace(agentId, workspaceUrlForAgent(agentId));
        closeSidebar(bundle);
      },
    },
  ]);
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

app.on('window-all-closed', async () => {
  logger.logMain('info', '[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  if (isShuttingDown) return;
  isShuttingDown = true;
  await shutdown();
  app.quit();
});

app.on('before-quit', async (event) => {
  logger.logMain('info', '[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
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
