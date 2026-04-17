const { BaseWindow, WebContentsView, Menu, Notification, ipcMain, net, shell, app } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

todesktop.init();

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;
const SIDEBAR_WIDTH = 260;
const REQUESTS_PANEL_WIDTH = 320;

// -- Per-window bundle registry --
const bundles = new Set();
const mruWindows = []; // most recently focused first
let appMenuInstalled = false;

let backendBaseUrl = null;
let backendPort = null;
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

let chromeSseAbortRef = { current: null };
let chromeSseReconnectTick = 0; // bumped to interrupt the current wait

function getSessionStatePath() {
  return path.join(paths.getDataDir(), 'window-state.json');
}

// -- URL/workspace helpers --

function parseWorkspaceId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const m = parsed.pathname.match(/^\/forwarding\/([^\/]+)(?:\/|$)/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function toAbsoluteUrl(url) {
  if (!url) return url;
  if (url.startsWith('/') && backendBaseUrl) return backendBaseUrl + url;
  return url;
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

function createBundle() {
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

  const win = new BaseWindow(windowOptions);

  const chromeView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  const contentView = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.contentView.addChildView(chromeView);
  win.contentView.addChildView(contentView);

  const bundle = {
    window: win,
    chromeView,
    contentView,
    sidebarView: null,
    sidebarVisible: false,
    requestsPanelView: null,
    requestsPanelVisible: false,
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
  // Run cleanup on `close` (before views are detached) rather than `closed`
  // so we can still reach the child webContents. BaseWindow does not guarantee
  // destruction of child WebContentsView render processes on its own; leaking
  // them across create/close cycles eventually starves new ones of resources.
  win.on('close', () => {
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

  // Re-push the computed title when chrome finishes (re)loading; the in-window
  // title bar otherwise has no way to learn its own window's title.
  chromeView.webContents.on('did-finish-load', () => {
    updateOsTitle(bundle);
    primeViewWithCachedChromeState(chromeView.webContents);
  });

  // Forward content view nav events to the bundle's chrome view and update state
  contentView.webContents.on('page-title-updated', (_e, title) => {
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-title-changed', title);
    }
  });

  const onContentNavigate = (url, reason) => {
    if (!bundle.isErrorState) {
      bundle.currentContentUrl = url;
      bundle.preErrorUrl = url;
    }
    const newAgentId = parseWorkspaceId(url);
    const changed = bundle.currentWorkspaceId !== newAgentId;
    if (changed) {
      bundle.currentWorkspaceId = newAgentId;
      sendCurrentWorkspaceToBundleSidebar(bundle);
    }
    console.log(
      '[content-nav] reason=' + reason +
      ' url=' + url +
      ' parsedWs=' + (newAgentId || '-') +
      ' changed=' + changed +
      ' bundleWs=' + (bundle.currentWorkspaceId || '-'),
    );
    updateOsTitle(bundle);
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-url-changed', url);
    }
  };

  contentView.webContents.on('did-navigate', (_e, url) => onContentNavigate(url, 'did-navigate'));
  contentView.webContents.on('did-navigate-in-page', (_e, url) => onContentNavigate(url, 'did-navigate-in-page'));

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

  registerShortcutsFor(bundle, chromeView.webContents);
  registerShortcutsFor(bundle, contentView.webContents);

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

  return bundle;
}

function registerShortcutsFor(bundle, wc) {
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const key = input.key ? input.key.toLowerCase() : '';
    const modifier = isMac ? input.meta : input.control;
    const devTools =
      (isMac && input.meta && input.alt && key === 'i') ||
      (!isMac && input.control && input.shift && key === 'c');
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
    if (modifier && !input.shift && !input.alt && key === 'w') {
      event.preventDefault();
      if (!bundle.window.isDestroyed()) bundle.window.close();
      return;
    }
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
    if (backendBaseUrl) {
      panel.webContents.loadURL(backendBaseUrl + '/_chrome/requests-panel');
    }
  } else {
    bundle.window.contentView.removeChildView(bundle.requestsPanelView);
    bundle.window.contentView.addChildView(bundle.requestsPanelView);
    bundle.requestsPanelView.setVisible(true);
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

function openOrFocusWorkspace(agentId, url) {
  const existing = findBundleForWorkspace(agentId);
  if (existing) {
    focusBundle(existing);
    return existing;
  }
  const absolute = toAbsoluteUrl(url || ('/forwarding/' + agentId + '/'));
  return openNewWindow(absolute);
}

function openNewWindow(url) {
  const bundle = createBundle();
  bundle.isLoadingState = false;
  updateBundleBounds(bundle);
  if (bundle.chromeView && backendBaseUrl) {
    bundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
  }
  if (bundle.contentView && url) {
    bundle.contentView.webContents.loadURL(url);
  }
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
          contextIsolation: true,
          nodeIntegration: false,
        },
      });
      bundle.contentView = contentView;
      bundle.window.contentView.addChildView(contentView);
      registerShortcutsFor(bundle, contentView.webContents);

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
      contentView.webContents.on('will-navigate', (event, url) => {
        const targetAgentId = parseWorkspaceId(url);
        if (!targetAgentId) return;
        const existing = findBundleForWorkspace(targetAgentId);
        if (!existing || existing === bundle) return;
        event.preventDefault();
        focusBundle(existing);
      });
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
      state.push({ url: relative });
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
    latestChromeState.workspaces = evt.workspaces;
    workspaceList = evt.workspaces.map((w) => ({
      id: String(w.id),
      name: w.name ? String(w.name) : '',
      account: w.account ? String(w.account) : '',
    }));
    updateAllOsTitles();
  } else if (evt.type === 'auth_status') {
    latestChromeState.authStatus = evt;
  } else if (evt.type === 'request_count') {
    latestChromeState.requestCount = evt.count || 0;
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
              finish({ authenticated: true, workspaces: parsed.workspaces });
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

async function onReady() {
  installApplicationMenu();
  installDockMenu();

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

    backendBaseUrl = `http://127.0.0.1:${port}`;
    backendPort = port;

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
        // No valid session cookie -- route through loginUrl to consume the
        // one-time code. Keep saved state on disk so the next quit-and-relaunch
        // after auth can restore. Don't open any additional restored windows
        // because they'd all 403.
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(loginUrl);
        }
      } else if (restorable.length === 0) {
        // Authenticated, but nothing to restore -- land on the home page.
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(backendBaseUrl + '/');
        }
      } else {
        const [first, ...rest] = restorable;
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(toAbsoluteUrl(first.url));
        }
        for (const entry of rest) {
          openNewWindow(toAbsoluteUrl(entry.url));
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
    if (mru.contentView && !mru.contentView.webContents.isDestroyed() && backendPort) {
      const authUrl = `http://127.0.0.1:${backendPort}/auth/login?message=` +
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
  openOrFocusWorkspace(agentId, '/forwarding/' + agentId + '/');
  // The sidebar is the sender for both the hover-icon click and the native
  // context-menu "Open in new window" item; close it now that the action is done.
  const bundle = getBundleFromEvent(event);
  if (bundle) closeSidebar(bundle);
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
        openOrFocusWorkspace(agentId, '/forwarding/' + agentId + '/');
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
  console.log('[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  if (isShuttingDown) return;
  isShuttingDown = true;
  await shutdown();
  app.quit();
});

app.on('before-quit', async (event) => {
  console.log('[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
  // Capture session state for every open window before teardown.
  saveSessionState();
  if (getBackendProcess() && !isShuttingDown) {
    isShuttingDown = true;
    event.preventDefault();
    await shutdown();
    app.quit();
  }
});
