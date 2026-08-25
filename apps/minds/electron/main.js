const { BrowserWindow, Menu, Notification, clipboard, dialog, ipcMain, net, shell, app, session, screen, nativeImage, powerMonitor } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const paths = require('./paths');
const { initElectronLogging } = require('./logger');
const { initConsoleCapture, recordConsoleMessage, closeConsoleCapture } = require('./console-capture');
const { initSentry, captureManualReport } = require('./sentry');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');
const { decideStartupRoute } = require('./startup-routing');
const { deeplinkTargetPath, extractDeeplinkUrlFromArgv } = require('./deeplink');
// Workspace-URL classification lives in ./surface-routing so it can be
// unit-tested under plain node (main.js can't be required outside Electron).
const { parseWorkspaceId, parseSpaWorkspaceRouteId } = require('./surface-routing');
const { shouldWriteSessionState, createDebouncedSaver, isSameSavedWindow } = require('./session-persistence');
const updater = require('./updater');
// Window / quit lifecycle decisions live in ./lifecycle-policy so they can be
// unit-tested under plain node (main.js can't be required outside Electron).
const {
  shouldQuitOnWindowAllClosed,
  shouldInterceptLastWindowClose,
  shouldOpenWindowOnActivate,
  decideNewWindowTarget,
} = require('./lifecycle-policy');

// After the single-web-context collapse each window is ONE BrowserWindow whose
// page is the minds SPA (titlebar + hub pages + the sandboxed workspace
// iframe + in-DOM modals, all Mithril-rendered). The main process no longer
// consumes any event stream of its own: each renderer owns a /ui/ws
// WebSocket and relays the few events main acts on over the 'shell-event'
// IPC channel. Main owns the backend lifecycle, native dialogs/menus,
// session restore, deeplinks, and the quit sequence.

// Tee console output into ~/.minds/logs/electron.log and record uncaught
// main-process failures BEFORE anything else runs.
initElectronLogging();

// Open the rolling renderer-console tail beside it, so every window created
// below has somewhere to record what its page printed.
initConsoleCapture(paths.getLogDir());

// Initialize Sentry as early as possible. The SDK only sends when the user has
// enabled error reporting (read live per event) -- see electron/sentry.js.
initSentry({ getRendererName: rendererNameForWebContents });

// Expose Chromium's DevTools protocol (CDP) when MINDS_REMOTE_DEBUGGING_PORT is
// set, for driving the real client with Playwright/CDP during development.
if (process.env.MINDS_REMOTE_DEBUGGING_PORT) {
  app.commandLine.appendSwitch('remote-debugging-port', process.env.MINDS_REMOTE_DEBUGGING_PORT);
}

// Imported for ToDesktop's smoke test, which the import itself arms when
// TODESKTOP_SMOKE_TEST is set. Never `init()`ed: that builds an updater agent
// whose constructor sets `allowDowngrade = true` on the shared electron-updater
// singleton, and electron/updater.js drives updates instead.
require('@todesktop/runtime');

// Surface the git SHA the build was cut from in the standard macOS About panel.
if (app.isPackaged) {
  try {
    const { gitSha } = JSON.parse(fs.readFileSync(path.join(__dirname, 'build-info.json'), 'utf8'));
    const pkg = require('../package.json');
    const shortSha = gitSha.slice(0, 8);
    app.setAboutPanelOptions({
      applicationName: pkg.productName,
      applicationVersion: pkg.version,
      version: pkg.tdBuildId ? `${pkg.tdBuildId} · ${shortSha}` : shortSha,
    });
  } catch (err) {
    console.warn(`[about-panel] Could not load build-info.json: ${err.message}`);
  }
}

// Redirect Electron's userData directory to ~/.<MINDS_ROOT_NAME>/ so that dev
// and production installs are fully isolated (cookies, sessions, caches, etc.).
app.setPath('userData', paths.getDataDir());

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;

// Local crash strip shown when a window's renderer process dies.
const CHROME_CRASHED_PAGE_FILE = path.join(__dirname, 'chrome-crashed.html');

// Retry budget for failed top-level loads: the dominant failure mode is
// transient (ERR_NETWORK_CHANGED from a docker/lima teardown flapping an
// interface mid-navigation), so a short escalating pause recovers invisibly.
const CHROME_LOAD_MAX_RETRIES = 2;
const CHROME_LOAD_RETRY_DELAY_MS = 500;

// -- Per-window bundle registry --
const bundles = new Set();
const mruWindows = []; // most recently focused first
let appMenuInstalled = false;

let backendBaseUrl = null;
let mngrForwardBaseUrl = null;
let workspaceList = []; // [{id, name, account}]
// Agent ids ever seen in a relayed ``workspaces`` shell event's
// ``destroying_agent_ids`` payload: a workspace disappearing from the list is
// only treated as destroyed (windows detached) when it was seen destroying
// first.
const everSeenDestroying = new Set();
// Latest per-agent system-interface health as relayed by the renderers'
// ``health`` shell events.
const systemInterfaceStatusByAgent = new Map();
let isShuttingDown = false;
let initialBundle = null;
let hasCompletedInitialStart = false;
// The app's FIRST-window route (session restore / welcome / consent) has not
// landed on a window yet: either it is still being computed, or the window it
// was for was closed mid-startup. Set once per launch and cleared by
// applyStartupRouting, which IS that landing.
let isStartupRoutingPending = false;
// The launch's own computation of that route, while it is still running.
// startBackendWithRetry lands its result on the most recent window -- including
// one opened while it runs -- so such a window must not compute a route for
// itself as well (see openStartupRoutedWindow).
let isStartupRoutingBeingComputed = false;
// A minds:// URL that arrived before the app could act on it.
let pendingDeeplinkUrl = null;
let canApplyDeeplinks = false;

// Dedupe for renderer-relayed one-shot events: every window's WS receives the
// same broadcast and every window relays it, so main keeps a tiny recent-key
// set and acts once per payload.
const recentShellEventKeys = new Map(); // key -> timestamp
const SHELL_EVENT_DEDUPE_WINDOW_MS = 3000;

function getSessionStatePath() {
  return path.join(paths.getDataDir(), 'window-state.json');
}

function toAbsoluteUrl(url) {
  if (!url) return url;
  if (url.startsWith('/') && backendBaseUrl) return backendBaseUrl + url;
  return url;
}

// Classify a URL as "external" (open in the user's default browser). All
// in-app navigation (the minds backend, the mngr_forward plugin, and every
// `host-<id>.localhost` workspace origin) lives on localhost.
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

// Coordinate aliases from the relayed ``workspaces`` shell events: content
// URLs are HOST-keyed while minds records and channel events stay
// AGENT-keyed.
const workspaceHostIdByAgentId = new Map();
const workspaceAgentIdByHostId = new Map();
function sameWorkspaceId(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  return workspaceHostIdByAgentId.get(a) === b || workspaceAgentIdByHostId.get(a) === b;
}

function toAgentScopedWorkspaceId(workspaceId) {
  if (!workspaceId) return workspaceId;
  return workspaceAgentIdByHostId.get(workspaceId) || workspaceId;
}

function toHostScopedWorkspaceId(workspaceId) {
  if (!workspaceId) return workspaceId;
  return workspaceHostIdByAgentId.get(workspaceId) || workspaceId;
}

function updateWorkspaceAliasMaps(workspaces) {
  for (const w of workspaces) {
    if (w && w.id && w.host_id) {
      workspaceHostIdByAgentId.set(String(w.id), String(w.host_id));
      workspaceAgentIdByHostId.set(String(w.host_id), String(w.id));
    }
  }
}

// The SPA URL that opens a window displaying ``workspaceId``. The host-scoped
// coordinate rides through when no agent alias is known yet (cold-start
// restore of a workspace the last-good topology keeps restorable but
// discovery has not re-confirmed) -- the SPA's workspace route accepts either
// coordinate and resolves once its snapshot lands.
function wrapperUrlForWorkspace(workspaceId) {
  if (!workspaceId || !backendBaseUrl) return null;
  const agentScoped = toAgentScopedWorkspaceId(workspaceId);
  if (!/^(?:agent|host)-[a-f0-9]+$/i.test(agentScoped)) return backendBaseUrl + '/';
  return backendBaseUrl + '/workspace/' + encodeURIComponent(agentScoped);
}

// Windows currently showing ``workspaceId`` (there may be several: the
// one-window-per-workspace rule was deliberately dropped with the collapse --
// a browser user can always open the same workspace in two tabs).
function findBundlesForWorkspace(workspaceId) {
  const found = [];
  if (!workspaceId) return found;
  for (const b of bundles) {
    if (!b.window.isDestroyed() && sameWorkspaceId(b.currentWorkspaceId, workspaceId)) found.push(b);
  }
  return found;
}

// The most-recently-focused window currently showing ``workspaceId``, or null.
// Unlike ``findBundlesForWorkspace``, which scans ``bundles`` (Set insertion /
// window-creation order), this scans ``mruWindows`` (kept in actual
// most-recently-focused order) -- the ordering a "focus the window already
// showing this" gesture needs when more than one window is showing it.
function mostRecentBundleForWorkspace(workspaceId) {
  if (!workspaceId) return null;
  for (const b of mruWindows) {
    if (!b.window.isDestroyed() && sameWorkspaceId(b.currentWorkspaceId, workspaceId)) return b;
  }
  return null;
}

function getBundleFromEvent(event) {
  if (!event || !event.sender) return null;
  const senderId = event.sender.id;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    if (!b.window.webContents.isDestroyed() && b.window.webContents.id === senderId) return b;
  }
  return null;
}

// Label renderer-death Sentry events. With one webContents per window the only
// distinction left is which window's renderer died.
function rendererNameForWebContents(contents) {
  if (!contents) return undefined;
  const id = contents.id;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    if (!b.window.webContents.isDestroyed() && b.window.webContents.id === id) return 'chrome';
  }
  return undefined;
}

// After the machine wakes from sleep, a renderer can survive but stop
// painting; webContents.invalidate() schedules a full repaint without a
// reload (see the old multi-view incarnation for the full war story).
function repaintAllWindowsAfterWake(trigger) {
  let repainted = 0;
  for (const b of bundles) {
    if (b.window.isDestroyed() || b.window.webContents.isDestroyed()) continue;
    b.window.webContents.invalidate();
    repainted += 1;
  }
  console.log(`[wake-repaint] ${trigger}: forced repaint of ${repainted} window(s)`);
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

function isLastLiveWindow(bundle) {
  let liveCount = 0;
  for (const b of bundles) {
    if (!b.window.isDestroyed()) liveCount += 1;
  }
  return liveCount <= 1 && !bundle.window.isDestroyed();
}

function focusBundle(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.window.isMinimized()) bundle.window.restore();
  if (!bundle.window.isVisible()) bundle.window.show();
  bundle.window.focus();
}

// focusBundle alone only focuses among the app's OWN windows -- on macOS it
// does not reliably activate the app over whichever other app currently has
// focus. Stealing focus first (mac only; the concept doesn't exist on other
// platforms, where a window-level focus is already enough) covers both the
// renderer-triggered 'bring-app-to-front' IPC and a notification click: both
// are the reader acting from OUTSIDE the app, so bringing "a" window to
// front is not enough if the app itself never comes forward.
function stealFocusAndFocusBundle(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (isMac) app.focus({ steal: true });
  focusBundle(bundle);
}

// A notification click needs the same treatment as bring-app-to-front (see
// stealFocusAndFocusBundle) -- the reader clicked from outside the app.
function focusBundleFromNotificationClick(bundle) {
  stealFocusAndFocusBundle(bundle);
}

// -- Title handling --

function computeTitleFor(bundle) {
  const agentId = bundle.currentWorkspaceId;
  if (agentId) {
    const ws = workspaceList.find((w) => sameWorkspaceId(w.id, agentId));
    const name = ws ? (ws.name || ws.id) : null;
    return name ? `${name} — Minds` : 'Minds';
  }
  return 'Minds';
}

function updateOsTitle(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  bundle.window.setTitle(computeTitleFor(bundle));
}

function updateAllOsTitles() {
  for (const b of bundles) updateOsTitle(b);
}

// Tear down every live window currently open to ``agentId`` (the workspace was
// destroyed or stopped). Never leaves the app windowless: when no window
// showing something ELSE would survive, the affected windows are all
// navigated home instead of closed (closing them would commit an app
// shutdown).
function detachWindowsForWorkspace(workspaceId) {
  if (!workspaceId) return;
  const affected = findBundlesForWorkspace(workspaceId);
  if (affected.length === 0) return;
  const liveBundleCount = [...bundles].filter((b) => !b.window.isDestroyed()).length;
  for (const b of affected) {
    if (liveBundleCount - affected.length >= 1) {
      b.window.close();
    } else if (backendBaseUrl) {
      navigateBundle(b, backendBaseUrl + '/');
    }
  }
}

// -- Window navigation --
//
// The SPA owns in-app navigation; main only drives windows for its own flows
// (startup routing, session restore, deeplinks, notifications, detaches
// triggered by relayed shell events). A live SPA page gets a 'shell-navigate'
// IPC (which lands in the SPA's navigateExternalUrl -- route change or
// workspace-iframe entry); a window not on a backend page gets a full load of
// the right document.
function isOnBackendPage(bundle) {
  if (!backendBaseUrl || bundle.window.isDestroyed() || bundle.window.webContents.isDestroyed()) return false;
  try {
    return new URL(bundle.window.webContents.getURL()).origin === new URL(backendBaseUrl).origin;
  } catch {
    return false;
  }
}

function navigateBundle(bundle, url) {
  if (!bundle || bundle.window.isDestroyed() || !url) return;
  const absolute = toAbsoluteUrl(url);
  const workspaceId = parseWorkspaceId(absolute);
  if (isOnBackendPage(bundle) && !bundle.window.webContents.isLoading()) {
    // The page's navigateContent handles both local paths and workspace entry.
    const target = workspaceId ? '/goto/' + toHostScopedWorkspaceId(workspaceId) + '/' : absolute;
    try {
      bundle.window.webContents.send('shell-navigate', target);
      return;
    } catch { /* fall through to a full load */ }
  }
  const loadTarget = workspaceId ? wrapperUrlForWorkspace(workspaceId) : absolute;
  if (loadTarget) bundle.window.webContents.loadURL(loadTarget).catch(() => {});
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
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  };
  if (isMac) {
    windowOptions.titleBarStyle = 'hiddenInset';
    windowOptions.trafficLightPosition = { x: 12, y: (TITLEBAR_HEIGHT - 16) / 2 };
  } else {
    windowOptions.frame = false;
  }
  return windowOptions;
}

function createBundle() {
  const win = new BrowserWindow(buildBundleWindowOptions());

  const bundle = {
    window: win,
    // The URL this window's content is "at" for session persistence and
    // titles: the displayed workspace's /goto path, or the local page path.
    currentContentUrl: null,
    currentWorkspaceId: null,
    preErrorUrl: null,
    isErrorState: false,
    isLoadingState: true,
    isQuittingState: false,
    isChromeCrashed: false,
    chromeLoadRetryCount: 0,
    chromeLoadRetryPendingUrl: null,
    chromeLoadFailedUrl: null,
    showInactiveOnFirstShow: false,
    _maximizedByUs: false,
    _boundsBeforeMaximize: null,
  };
  bundles.add(bundle);
  mruWindows.unshift(bundle);

  wireBundleWindowEvents(bundle);
  wireBundleNavigationEvents(bundle);
  registerShortcutsFor(bundle, win.webContents);
  wireBundleShowLogic(bundle);

  win.webContents.on('did-finish-load', () => {
    updateOsTitle(bundle);
  });

  // Every level from every frame of this window -- the SPA's own output and the
  // workspace iframe's, which share this webContents -- into the rolling
  // console tail, so a bug report can carry what the UI actually printed.
  win.webContents.on('console-message', (details) => {
    recordConsoleMessage(details);
  });

  if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
    win.webContents.once('did-finish-load', () => {
      if (!win.webContents.isDestroyed()) {
        win.webContents.openDevTools({ mode: 'detach' });
      }
    });
  }

  return bundle;
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
  win.on('resize', () => scheduleSessionSave());
  win.on('move', () => scheduleSessionSave());

  win.on('close', (event) => {
    // Off macOS, closing the LAST window quits the app; route that close
    // through the quit sequence so the local-mind shutdown prompt appears
    // BEFORE the window disappears. If the user cancels, the window stays open.
    // On macOS the app keeps running with no windows, so the last close is an
    // ordinary window close and the prompt fires only on an explicit Quit.
    if (
      shouldInterceptLastWindowClose({
        isMac,
        isShuttingDown,
        isQuitSequenceRunning,
        hasBackend: !!getBackendProcess(),
        isLastLiveWindow: isLastLiveWindow(bundle),
      })
    ) {
      event.preventDefault();
      runQuitSequence();
      return;
    }
    if (!isShuttingDown) saveSessionState();
  });

  win.on('closed', () => {
    bundles.delete(bundle);
    const mruIdx = mruWindows.indexOf(bundle);
    if (mruIdx >= 0) mruWindows.splice(mruIdx, 1);
    if (initialBundle === bundle) initialBundle = null;
  });
}

function wireBundleNavigationEvents(bundle) {
  const wc = bundle.window.webContents;

  // Top-level navigation: the SPA's routes (hub pages and /workspace/<id>).
  // The SPA owns the UI; main just records what it needs for session
  // persistence, titles, and error recovery.
  const onTopLevelNavigate = (url) => {
    if (bundle.isErrorState) return;
    let parsed = null;
    try { parsed = new URL(url); } catch { return; }
    // Ignore the loading/quitting/error takeover pages (shell.html): they
    // must not clobber preErrorUrl (the page restored on a quit backout).
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return;
    bundle.chromeLoadRetryCount = 0;
    bundle.chromeLoadRetryPendingUrl = null;
    bundle.chromeLoadFailedUrl = null;
    bundle.isChromeCrashed = false;
    console.log(`[nav] window committed ${url}`);
    // The SPA's workspace routes (/workspace/<id>, and its /options overlay,
    // which keeps the workspace surface mounted underneath -- but NOT
    // /workspace/<id>/settings, a legacy redirect route) show a workspace:
    // the path id is the authoritative record until the iframe commits.
    // Other SPA routes clear the displayed workspace. Either workspace
    // coordinate counts (a cold-start restore may carry the host-scoped id
    // before discovery re-confirms the agent).
    const workspaceRouteMatch = parsed.pathname.match(/^\/workspace\/((?:agent|host)-[a-f0-9]+)(?:\/options)?\/?$/i);
    if (workspaceRouteMatch) {
      bundle.currentWorkspaceId = toHostScopedWorkspaceId(workspaceRouteMatch[1]);
      bundle.currentContentUrl = '/goto/' + bundle.currentWorkspaceId + '/';
    } else {
      bundle.currentWorkspaceId = null;
      bundle.currentContentUrl = parsed.pathname + parsed.search;
    }
    bundle.preErrorUrl = bundle.currentContentUrl;
    scheduleSessionSave();
    updateOsTitle(bundle);
  };
  wc.on('did-navigate', (_e, url) => onTopLevelNavigate(url));
  wc.on('did-navigate-in-page', (_e, url) => onTopLevelNavigate(url));

  // Subframe navigation: the workspace iframe. This is main's tamper-proof
  // record of which workspace the window displays (used for session
  // persistence, the OS title, and notification routing) -- the chrome page
  // derives its own UI state from its navigation intents instead.
  wc.on('did-frame-navigate', (_e, url, _code, _status, isMainFrame) => {
    if (isMainFrame || bundle.isErrorState) return;
    const workspaceId = parseWorkspaceId(url);
    if (!workspaceId) return;
    if (bundle.currentWorkspaceId !== workspaceId) {
      bundle.currentWorkspaceId = workspaceId;
      bundle.currentContentUrl = '/goto/' + toHostScopedWorkspaceId(workspaceId) + '/';
      bundle.preErrorUrl = bundle.currentContentUrl;
      scheduleSessionSave();
      updateOsTitle(bundle);
    }
  });

  // The window hosts ONLY trusted backend pages top-level; workspace content
  // lives in the sandboxed iframe. Block top-level navigations to workspace
  // origins (a compromised page trying to escape the sandbox boundary).
  // Both events fire for EVERY frame in this Electron, so the guard must
  // exempt subframes: the workspace iframe's own redirect chain
  // (/forward-bridge -> /_bridge -> /goto/<host-id>/ -> workspace origin) is
  // precisely a subframe navigation to a workspace origin.
  wc.on('will-navigate', (event, url) => {
    if (!event.isMainFrame) return;
    if (parseWorkspaceId(url)) {
      event.preventDefault();
      console.warn('[chrome-guard] Blocked a top-level workspace-origin navigation:', url);
    }
  });
  wc.on('will-redirect', (event, url) => {
    if (!event.isMainFrame) return;
    if (parseWorkspaceId(url)) {
      event.preventDefault();
      console.warn('[chrome-guard] Blocked a top-level workspace-origin redirect:', url);
    }
  });

  // When the window's renderer dies, show a local crash strip with a Reload
  // button. Never navigate synchronously inside this handler (electron#19887).
  wc.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason;
    if (reason === 'clean-exit') return;
    if (isShuttingDown || bundle.window.isDestroyed()) return;
    if (bundle.isErrorState) return;
    console.error(`[chrome-crash] window renderer gone (reason=${reason})`);
    bundle.isChromeCrashed = true;
    setImmediate(() => {
      if (bundle.window.isDestroyed() || wc.isDestroyed()) return;
      if (!bundle.isChromeCrashed) return;
      wc.loadFile(CHROME_CRASHED_PAGE_FILE).catch((err) => {
        console.error('[chrome-crash] failed to load crash page:', err && err.message);
      });
    });
  });

  // A failed top-level load commits Electron's blank error document over the
  // entire UI; retry (the dominant causes are transient), then fall back to
  // the local error page whose Reload button re-loads the failed URL.
  wc.on('did-fail-load', (_e, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (!isMainFrame) return;
    if (errorCode === -3) return; // ERR_ABORTED: superseded, not a failure
    if (isShuttingDown || bundle.window.isDestroyed() || bundle.isErrorState) return;
    if (!backendBaseUrl || !validatedURL || !validatedURL.startsWith(backendBaseUrl + '/')) return;
    if (bundle.chromeLoadRetryCount < CHROME_LOAD_MAX_RETRIES) {
      bundle.chromeLoadRetryCount += 1;
      bundle.chromeLoadRetryPendingUrl = validatedURL;
      const attempt = bundle.chromeLoadRetryCount;
      console.warn(
        `[chrome-load-failed] load failed (errorCode=${errorCode}, error=${errorDescription || 'unknown'}, ` +
          `url=${validatedURL}); retrying (${attempt}/${CHROME_LOAD_MAX_RETRIES})`
      );
      setTimeout(() => {
        if (isShuttingDown || bundle.window.isDestroyed() || bundle.isErrorState) return;
        if (wc.isDestroyed()) return;
        if (bundle.chromeLoadRetryPendingUrl !== validatedURL) return;
        if (wc.isLoading()) return;
        wc.loadURL(validatedURL).catch(() => {});
      }, CHROME_LOAD_RETRY_DELAY_MS * attempt);
      return;
    }
    console.error(
      `[chrome-load-failed] load failed after ${CHROME_LOAD_MAX_RETRIES} retries ` +
        `(errorCode=${errorCode}, url=${validatedURL}); showing the error page`
    );
    bundle.chromeLoadFailedUrl = validatedURL;
    bundle.chromeLoadRetryPendingUrl = null;
    bundle.isChromeCrashed = true;
    setImmediate(() => {
      if (bundle.window.isDestroyed() || wc.isDestroyed()) return;
      if (!bundle.isChromeCrashed) return;
      wc.loadFile(CHROME_CRASHED_PAGE_FILE, { query: { reason: 'load-failed' } }).catch((err) => {
        console.error('[chrome-load-failed] failed to load error page:', err && err.message);
      });
    });
  });
}

function registerShortcutsFor(bundle, wc) {
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const key = input.key ? input.key.toLowerCase() : '';
    const modifier = isMac ? input.meta : input.control;
    // Match on `input.code` (physical key): on macOS, Option transforms
    // `input.key` into the composed character.
    const devTools =
      (isMac && input.meta && input.alt && input.code === 'KeyI') ||
      (!isMac && input.control && input.shift && input.code === 'KeyC');
    if (devTools) {
      event.preventDefault();
      if (!wc.isDestroyed()) wc.toggleDevTools();
      return;
    }
    // Escape backstop for the in-DOM overlay: keydowns inside the workspace
    // iframe (or its nested service iframes) don't reach the chrome page's
    // own listeners, but before-input-event sees every keystroke.
    if (input.key === 'Escape') {
      try { wc.send('escape-pressed'); } catch { /* noop */ }
      return;
    }
    // Cmd+W (macOS) / Ctrl+W closes the active dockview tab INSIDE the
    // displayed workspace, not the window. before-input-event sees the
    // keystroke even when focus is inside the workspace iframe; the chrome
    // page relays it into the workspace through the embed contract.
    const closeTabCombo = isMac
      ? input.meta && !input.shift && !input.alt && !input.control
      : input.control && !input.shift && !input.alt && !input.meta;
    if (closeTabCombo && key === 'w' && bundle.currentWorkspaceId) {
      event.preventDefault();
      try { wc.send('close-active-tab'); } catch { /* noop */ }
      return;
    }
    // When the app menu is installed, it owns cmd+Q / cmd+N.
    if (appMenuInstalled) return;
    if (modifier && !input.shift && !input.alt && key === 'q') {
      event.preventDefault();
      initiateFullQuit();
      return;
    }
    if (modifier && !input.shift && !input.alt && key === 'n') {
      event.preventDefault();
      openOrFocusWindow();
      return;
    }
  });
}

// Route external links to the user's default browser. will-frame-navigate
// fires for every frame -- including the workspace iframe and the service
// iframes it embeds -- so an in-place navigation to an external site is
// cancelled and opened externally instead of rendering a foreign site inside
// the chrome (the iframe's frame-ancestors would usually refuse anyway).
function applyExternalLinkHandling(wc) {
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
    return { action: 'allow' };
  });
  wc.on('will-frame-navigate', (details) => {
    if (!isExternalUrl(details.url)) return;
    details.preventDefault();
    openInBrowser(details.url);
  });
}

function notifyOpenFailed(url) {
  let scheme = '';
  try {
    scheme = new URL(url).protocol.replace(':', '');
  } catch {
    // Unparseable url -- fall through with an empty scheme and copy verbatim.
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

function wireBundleShowLogic(bundle) {
  const { window: win } = bundle;
  const surface = () => {
    if (win.isDestroyed() || win.isVisible()) return;
    if (bundle.showInactiveOnFirstShow) win.showInactive();
    else win.show();
  };
  win.webContents.once('did-finish-load', surface);
  win.once('ready-to-show', surface);
  setTimeout(surface, 3000);
}

function openNewWindow(url, { showInactive = false } = {}) {
  const bundle = createBundle();
  if (showInactive) bundle.showInactiveOnFirstShow = true;
  bundle.isLoadingState = false;
  const workspaceId = parseWorkspaceId(toAbsoluteUrl(url));
  const target = workspaceId ? wrapperUrlForWorkspace(workspaceId) : toAbsoluteUrl(url);
  if (target) bundle.window.webContents.loadURL(target).catch(() => {});
  return bundle;
}

// Open a window on the shell.html takeover screen: the recorded error, whose
// Retry button restarts the backend, when one is current; else the loading
// screen the startup sequence broadcasts into. This is what makes a windowless
// app recoverable -- an app whose backend failed or died has no page to load
// and no window to focus, so without this it could never open a window again.
function openTakeoverWindow() {
  const bundle = createBundle();
  const takeover = lastErrorTakeover;
  bundle.isErrorState = !!takeover;
  bundle.isLoadingState = !takeover;
  const wc = bundle.window.webContents;
  wc.loadFile(path.join(__dirname, 'shell.html')).catch(() => {});
  wc.once('did-finish-load', () => {
    if (wc.isDestroyed()) return;
    if (takeover) wc.send('error-details', takeover);
  });
  return bundle;
}

// Open a window and land the app's first-window route on it: the launch that
// computed it lost its window (closed mid-startup), or the route has not been
// computed yet. The route is recomputed here rather than replayed from a
// snapshot taken at startup, so a session restored an hour later reflects the
// workspaces that exist NOW.
function openStartupRoutedWindow() {
  const bundle = openTakeoverWindow(); // the loading screen, while we ask
  // The launch is already computing that route and lands it on the most recent
  // window, which is this one. Computing a second here would apply two routes
  // to it -- duplicating every window of a multi-window session restore -- off
  // an app-status read before the one-time login code is consumed, which
  // answers "signed out".
  if (isStartupRoutingBeingComputed) return bundle;
  computeStartupRouting()
    .then((routing) => {
      // Closed while we were asking: leave the route pending so the next
      // window the user opens still gets it.
      if (bundle.window.isDestroyed()) return;
      // Likewise for the states this request was decided against but that
      // arrived while we asked. Both have already taken this window over --
      // the error screen after a crash, the quitting screen after a committed
      // quit -- and landing the route now would navigate it off that, for a
      // crash onto the dead port the error screen exists to replace.
      if (lastErrorTakeover || isShuttingDown || isQuitSequenceRunning) return;
      applyStartupRouting(bundle, routing);
      // A deeplink that arrived while this window sat on the loading screen was
      // held for a window ready to take it, and this is the only loading state
      // the backend start does not itself flush.
      flushPendingDeeplink();
    })
    .catch((err) => {
      console.warn('[startup] could not compute the first-window route:', err);
    });
  return bundle;
}

// Every "give me a window" request (dock activate, Cmd+N, File > New Window,
// the dock menu, a second launch, a deeplink with nothing open) lands here, and
// always resolves to a window unless a quit has committed. See
// decideNewWindowTarget for what each state produces.
function openOrFocusWindow() {
  const target = decideNewWindowTarget({
    hasBackendUrl: !!backendBaseUrl,
    hasErrorTakeover: !!lastErrorTakeover,
    hasLiveWindow: getMostRecentWindow() != null,
    isStartupRoutingPending,
    isShuttingDown,
    isQuitSequenceRunning,
  });
  if (target === 'none') return null;
  if (target === 'focus-existing') {
    const existing = getMostRecentWindow();
    focusBundle(existing);
    return existing;
  }
  if (target === 'home') return openNewWindow(backendBaseUrl + '/');
  if (target === 'startup-route') return openStartupRoutedWindow();
  return openTakeoverWindow();
}

// -- Error / retry flow --

// The error currently taking every window over, replayed into any window
// opened afterwards. Cleared once a backend start succeeds.
//
// It is deliberately load-bearing beyond the error screen. After a crash
// backendBaseUrl is knowingly left naming the DEAD port -- windows record their
// content URL relative to it and toPersistedContentUrl needs that URL absolute
// to recognize a workspace, so nulling it would persist every open window as
// plain home and lose the workspace it was on -- and this flag is what stops
// the app routing there anyway. So the pair reads: backendBaseUrl = "the port
// the backend last served on"; lastErrorTakeover = "and it is not serving".
// The consumers that must read it are the ones that can fire with no live
// backend behind them -- a window request (decideNewWindowTarget) and a
// deeplink (handleDeeplink); every other navigation to backendBaseUrl runs off
// a live backend's events, or off a start that just succeeded and cleared this.
let lastErrorTakeover = null;

function showErrorInAllWindows(message, details, actionLabel) {
  const takeover = { message, details, actionLabel };
  lastErrorTakeover = takeover;
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isErrorState = true;
    const wc = bundle.window.webContents;
    if (wc.isDestroyed()) continue;
    const url = wc.getURL();
    if (!url.startsWith('file://')) {
      wc.loadFile(path.join(__dirname, 'shell.html'));
      wc.once('did-finish-load', () => {
        if (!wc.isDestroyed()) {
          wc.send('error-details', takeover);
        }
      });
    } else {
      wc.send('error-details', takeover);
    }
  }
}

function prepareAllWindowsForRetry() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isLoadingState = true;
    if (!bundle.window.webContents.isDestroyed()) {
      bundle.window.webContents.loadFile(path.join(__dirname, 'shell.html'));
    }
  }
}

function reloadAllWindowsAfterRetry() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isErrorState = false;
    bundle.isLoadingState = false;
    // A window is landing on real content, so the first-window route no longer
    // describes anything. Inside the loop because with no live window nothing
    // lands and the route is still owed.
    isStartupRoutingPending = false;
    const target = bundle.preErrorUrl || (backendBaseUrl ? backendBaseUrl + '/' : null);
    if (target) navigateBundle(bundle, target);
  }
}

// -- Quitting takeover --

let latestQuittingStatus = 'Quitting…';

function showQuittingInAllWindows() {
  latestQuittingStatus = 'Quitting…';
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isQuittingState = true;
    const wc = bundle.window.webContents;
    if (wc.isDestroyed()) continue;
    wc.loadFile(path.join(__dirname, 'shell.html'), { hash: 'quitting' });
    wc.once('did-finish-load', () => {
      if (!wc.isDestroyed()) wc.send('status-update', latestQuittingStatus);
    });
  }
}

function updateQuittingStatus(message) {
  latestQuittingStatus = message;
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed() || !bundle.isQuittingState) continue;
    if (!bundle.window.webContents.isDestroyed()) {
      bundle.window.webContents.send('status-update', message);
    }
  }
}

function restoreFromQuittingInAllWindows() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isQuittingState = false;
    const target = bundle.preErrorUrl || bundle.currentContentUrl
      || (backendBaseUrl ? backendBaseUrl + '/' : null);
    if (target) navigateBundle(bundle, target);
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
//
// On-disk shape is ``{ windows: [{ url, x, y, width, height, displayId },
// ...] }``. A workspace window persists as the port-independent
// ``/goto/<host-id>/`` path and is restored through the SPA's
// /workspace/<id> route; every other screen persists as home ("/").

function loadSessionState() {
  try {
    const p = getSessionStatePath();
    if (!fs.existsSync(p)) return { windows: [] };
    const raw = fs.readFileSync(p, 'utf-8');
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return {
        windows: parsed.filter((e) => typeof e === 'object' && typeof e.url === 'string'),
      };
    }
    if (parsed && typeof parsed === 'object') {
      const windows = Array.isArray(parsed.windows)
        ? parsed.windows.filter((e) => typeof e === 'object' && typeof e.url === 'string')
        : [];
      return { windows };
    }
    return { windows: [] };
  } catch {
    return { windows: [] };
  }
}

function toRelativeBackendUrl(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url, backendBaseUrl || 'http://localhost');
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return parsed.pathname + parsed.search + parsed.hash;
  } catch {
    return null;
  }
}

function parseRecoveryPageAgentId(url) {
  if (!url) return null;
  try {
    const match = new URL(url, backendBaseUrl || 'http://localhost').pathname
      .match(/^\/agents\/((?:agent|host)-[a-f0-9]+)\/recovery(?:\/|$)/i);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

function toPersistedContentUrl(url) {
  if (!url) return null;
  const absolute = toAbsoluteUrl(url);
  const workspaceId = parseWorkspaceId(absolute) || parseRecoveryPageAgentId(absolute);
  if (workspaceId) return `/goto/${encodeURIComponent(toHostScopedWorkspaceId(workspaceId))}/`;
  return toRelativeBackendUrl(absolute) ? '/' : null;
}

function parseLegacyGotoAgentId(url) {
  if (!url) return null;
  try {
    const match = new URL(url, backendBaseUrl || 'http://localhost').pathname
      .match(/^\/goto\/(agent-[a-f0-9]+)(?:\/|$)/i);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

function toRestoredContentUrl(entry) {
  const absolute = toAbsoluteUrl(entry.url);
  const workspaceId =
    parseWorkspaceId(absolute) || parseRecoveryPageAgentId(absolute) || parseLegacyGotoAgentId(absolute);
  if (workspaceId) {
    const wrapperUrl = wrapperUrlForWorkspace(workspaceId);
    if (wrapperUrl) return wrapperUrl;
  }
  return absolute;
}

function saveSessionState() {
  try {
    const windows = [];
    for (const b of mruWindows) {
      if (b.window.isDestroyed()) continue;
      const url = b.preErrorUrl || b.currentContentUrl;
      const persisted = toPersistedContentUrl(url);
      if (!persisted) continue;
      const bounds = b.window.getBounds();
      const display = screen.getDisplayMatching(bounds);
      windows.push({
        url: persisted,
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
        displayId: display ? display.id : null,
      });
    }
    const persistedWindowCount = loadSessionState().windows.length;
    if (!shouldWriteSessionState({ computedWindowCount: windows.length, persistedWindowCount })) {
      console.log(`[session] Skipping empty save; ${persistedWindowCount} window(s) already persisted (teardown race guard)`);
      return;
    }
    const p = getSessionStatePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, JSON.stringify({ windows }, null, 2));
  } catch (err) {
    console.log('[session] Failed to save state:', err.message);
  }
}

const SESSION_SAVE_DEBOUNCE_MS = 1000;
const debouncedSessionSaver = createDebouncedSaver({ save: saveSessionState, delayMs: SESSION_SAVE_DEBOUNCE_MS });

function scheduleSessionSave() {
  if (isShuttingDown) return;
  debouncedSessionSaver.schedule();
}

function filterRestorableUrls(state, knownAgentIdsSet) {
  const results = [];
  for (const entry of state) {
    const absolute = toAbsoluteUrl(entry.url);
    const workspaceId =
      parseWorkspaceId(absolute) || parseRecoveryPageAgentId(absolute) || parseLegacyGotoAgentId(absolute);
    if (workspaceId && (!knownAgentIdsSet || !knownAgentIdsSet.has(workspaceId))) {
      continue; // workspace no longer exists (or none are known)
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

  const displays = screen.getAllDisplays();
  let targetDisplay = null;
  if (entry.displayId) {
    targetDisplay = displays.find((d) => d.id === entry.displayId);
  }
  if (!targetDisplay) {
    targetDisplay = screen.getDisplayMatching(savedBounds);
    const db = targetDisplay.bounds;
    const isVisible = savedBounds.x < db.x + db.width && savedBounds.x + savedBounds.width > db.x &&
                      savedBounds.y < db.y + db.height && savedBounds.y + savedBounds.height > db.y;
    if (!isVisible) {
      const primary = screen.getPrimaryDisplay();
      savedBounds.x = primary.bounds.x + 50;
      savedBounds.y = primary.bounds.y + 50;
    }
  }

  bundle.window.setBounds(savedBounds);
}

// ---------- Renderer-relayed shell events ----------
// Each window's SPA owns its own /ui/ws channel and relays the events main
// still acts on. Every window relays the same broadcasts, so handlers must be
// idempotent; genuinely once-only reactions dedupe via recentShellEventKeys.

function isDuplicateShellEvent(key) {
  const now = Date.now();
  for (const [k, ts] of recentShellEventKeys) {
    if (now - ts > SHELL_EVENT_DEDUPE_WINDOW_MS) recentShellEventKeys.delete(k);
  }
  if (recentShellEventKeys.has(key)) return true;
  recentShellEventKeys.set(key, now);
  return false;
}

function handleShellEvent(evt, senderBundle) {
  if (!evt || typeof evt.type !== 'string') return;
  if (evt.type === 'workspaces' && Array.isArray(evt.workspaces)) {
    const oldIds = new Set(workspaceList.map((w) => w.id));
    workspaceList = evt.workspaces.map((w) => ({
      id: String(w.id),
      name: w.name ? String(w.name) : '',
      account: w.account ? String(w.account) : '',
    }));
    updateWorkspaceAliasMaps(evt.workspaces);
    const newIds = new Set(workspaceList.map((w) => w.id));
    if (Array.isArray(evt.destroying_agent_ids)) {
      for (const aid of evt.destroying_agent_ids) {
        everSeenDestroying.add(String(aid));
      }
    }
    // Detach windows whose workspace was genuinely destroyed (seen destroying
    // at a prior tick); transient discovery hiccups are left to the page's
    // recovery flow.
    for (const oldId of oldIds) {
      if (newIds.has(oldId)) continue;
      if (!everSeenDestroying.has(oldId)) continue;
      detachWindowsForWorkspace(oldId);
    }
    updateAllOsTitles();
  } else if (evt.type === 'health') {
    if (evt.agent_id) {
      const status = evt.status ? String(evt.status).toLowerCase() : '';
      if (!status || status === 'healthy') {
        systemInterfaceStatusByAgent.delete(String(evt.agent_id));
      } else {
        systemInterfaceStatusByAgent.set(String(evt.agent_id), status);
      }
    }
  } else if (evt.type === 'workspace_stopped') {
    // The page navigates its own window home; main closes any EXTRA windows
    // still open to the stopped workspace. Skip while mid-restart.
    if (evt.agent_id && systemInterfaceStatusByAgent.get(String(evt.agent_id)) !== 'restarting') {
      detachWindowsForWorkspace(String(evt.agent_id));
    }
  } else if (evt.type === 'notifications_count') {
    // Unresolved-notification count for the macOS dock / Linux launcher (a
    // no-op on Windows; the in-app bell suffices there). Idempotent, so the
    // every-window relay of the same broadcast needs no dedupe.
    const count = Math.max(0, Math.floor(Number(evt.count)) || 0);
    app.setBadgeCount(count);
  } else if (evt.type === 'open_help') {
    // An in-workspace /assist agent asked to open the report-a-bug modal with
    // its diagnosis. Surface it in ONE window: the one showing that workspace,
    // else the most recent. Deduped because every window relays it.
    const description = typeof evt.description === 'string' ? evt.description : '';
    const wsId = evt.workspace_agent_id ? String(evt.workspace_agent_id) : '';
    if (isDuplicateShellEvent('open_help:' + wsId + ':' + description)) return;
    const target = (wsId && mostRecentBundleForWorkspace(wsId)) || getMostRecentWindow();
    if (target && !target.window.isDestroyed() && !target.window.webContents.isDestroyed()) {
      target.window.webContents.send('open-overlay', { kind: 'help', workspace: wsId, description });
    }
  }
}

// The frame types main acts on; anything else is dropped before dispatch.
// discovery_health is deliberately absent: a dead consumer is surfaced by the
// SPA's own band now, so main has nothing to do with it. focus_window is
// likewise absent: the request popup only opens on an explicit click, so
// nothing asks main to raise a window on its behalf.
const KNOWN_SHELL_EVENT_TYPES = new Set([
  'workspaces',
  'health',
  'workspace_stopped',
  'open_help',
  'notifications_count',
]);

// Only the SPA page itself may drive window management: the sender must be a
// window main created AND the sending frame's origin must be the app server
// main launched. The workspace iframe and its nested service frames share the
// webContents but host untrusted content; before the collapse this state
// arrived on a main-owned stream, and the origin check restores that trust
// property. Returns the sender's bundle when trusted, else null.
function trustedShellEventSenderBundle(event) {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !backendBaseUrl) return null;
  const frame = event.senderFrame;
  if (!frame) return null;
  try {
    return new URL(frame.url).origin === new URL(backendBaseUrl).origin ? bundle : null;
  } catch {
    return null;
  }
}

ipcMain.on('shell-event', (event, evt) => {
  try {
    const senderBundle = trustedShellEventSenderBundle(event);
    if (!senderBundle) {
      console.warn('[shell-event] dropped an event from an untrusted sender frame');
      return;
    }
    if (!evt || typeof evt.type !== 'string' || !KNOWN_SHELL_EVENT_TYPES.has(evt.type)) {
      console.warn('[shell-event] dropped a malformed or unknown event');
      return;
    }
    handleShellEvent(evt, senderBundle);
  } catch (err) {
    console.warn('[shell-event] handler failed:', err);
  }
});

// ---------- Mind shutdown on quit ----------

const MIND_HTTP_TIMEOUT_MS = 10000;
const MIND_COMMAND_TIMEOUT_MS = 150000;

function getRunningMinds() {
  return new Promise((resolve) => {
    if (!backendBaseUrl) {
      console.warn('[mind-shutdown] no backend URL; cannot list running minds');
      resolve({ ok: false, running: [] });
      return;
    }
    let req;
    try {
      req = net.request({ url: backendBaseUrl + '/api/v1/desktop/running-workspaces', method: 'GET', useSessionCookies: true });
    } catch (e) {
      console.warn('[mind-shutdown] failed to construct running-minds request:', e);
      resolve({ ok: false, running: [] });
      return;
    }
    let body = '';
    let settled = false;
    let statusOk = false;
    const settle = (value) => { if (!settled) { settled = true; resolve(value); } };
    const timer = setTimeout(() => {
      console.warn(`[mind-shutdown] running-minds request timed out after ${MIND_HTTP_TIMEOUT_MS}ms`);
      try { req.abort(); } catch { /* noop */ }
      settle({ ok: false, running: [] });
    }, MIND_HTTP_TIMEOUT_MS);
    req.on('response', (response) => {
      statusOk = response.statusCode < 400;
      if (!statusOk) console.warn(`[mind-shutdown] running-minds returned HTTP ${response.statusCode}`);
      response.on('data', (chunk) => { body += chunk.toString(); });
      response.on('end', () => {
        clearTimeout(timer);
        if (!statusOk) { settle({ ok: false, running: [] }); return; }
        try {
          const parsed = JSON.parse(body);
          settle({ ok: true, running: Array.isArray(parsed.running) ? parsed.running : [] });
        } catch (e) {
          console.warn('[mind-shutdown] failed to parse running-minds response:', e);
          settle({ ok: false, running: [] });
        }
      });
      response.on('error', (err) => { console.warn('[mind-shutdown] running-minds response error:', err); clearTimeout(timer); settle({ ok: false, running: [] }); });
    });
    req.on('error', (err) => { console.warn('[mind-shutdown] running-minds request failed:', err); clearTimeout(timer); settle({ ok: false, running: [] }); });
    req.end();
  });
}

function postStopMinds(agentIds) {
  return new Promise((resolve) => {
    if (!backendBaseUrl || !agentIds || agentIds.length === 0) {
      console.warn('[mind-shutdown] no backend URL or no agent ids; cannot bulk-stop minds');
      resolve({ ok: false, stillRunning: [] });
      return;
    }
    const query = agentIds.map((id) => 'agent_id=' + encodeURIComponent(id)).join('&');
    let req;
    try {
      req = net.request({ url: `${backendBaseUrl}/api/v1/desktop/stop-hosts?${query}`, method: 'POST', useSessionCookies: true });
    } catch (e) {
      console.warn('[mind-shutdown] failed to construct bulk-stop request:', e);
      resolve({ ok: false, stillRunning: [] });
      return;
    }
    let body = '';
    let settled = false;
    let statusOk = false;
    const settle = (value) => { if (!settled) { settled = true; resolve(value); } };
    const timer = setTimeout(() => {
      console.warn(`[mind-shutdown] bulk-stop request timed out after ${MIND_COMMAND_TIMEOUT_MS}ms`);
      try { req.abort(); } catch { /* noop */ }
      settle({ ok: false, stillRunning: [] });
    }, MIND_COMMAND_TIMEOUT_MS);
    req.on('response', (response) => {
      statusOk = response.statusCode < 400;
      if (!statusOk) console.warn(`[mind-shutdown] bulk-stop returned HTTP ${response.statusCode}`);
      response.on('data', (chunk) => { body += chunk.toString(); });
      response.on('end', () => {
        clearTimeout(timer);
        if (!statusOk) { settle({ ok: false, stillRunning: [] }); return; }
        try {
          const parsed = JSON.parse(body);
          settle({ ok: true, stillRunning: Array.isArray(parsed.still_running) ? parsed.still_running : [] });
        } catch (e) {
          console.warn('[mind-shutdown] failed to parse bulk-stop response:', e);
          settle({ ok: false, stillRunning: [] });
        }
      });
      response.on('error', (err) => { console.warn('[mind-shutdown] bulk-stop response error:', err); clearTimeout(timer); settle({ ok: false, stillRunning: [] }); });
    });
    req.on('error', (err) => { console.warn('[mind-shutdown] bulk-stop request failed:', err); clearTimeout(timer); settle({ ok: false, stillRunning: [] }); });
    req.end();
  });
}

function postStopStateContainer() {
  return new Promise((resolve) => {
    if (!backendBaseUrl) {
      console.warn('[mind-shutdown] no backend URL; cannot stop state container');
      resolve();
      return;
    }
    let req;
    try {
      req = net.request({ url: backendBaseUrl + '/api/v1/desktop/state-container/stop', method: 'POST', useSessionCookies: true });
    } catch (e) {
      console.warn('[mind-shutdown] failed to construct stop-state-container request:', e);
      resolve();
      return;
    }
    let settled = false;
    const settle = () => { if (!settled) { settled = true; resolve(); } };
    const timer = setTimeout(() => {
      console.warn(`[mind-shutdown] stop-state-container request timed out after ${MIND_COMMAND_TIMEOUT_MS}ms`);
      try { req.abort(); } catch { /* noop */ }
      settle();
    }, MIND_COMMAND_TIMEOUT_MS);
    req.on('response', (response) => {
      if (response.statusCode >= 400) console.warn(`[mind-shutdown] stop-state-container returned HTTP ${response.statusCode}`);
      response.on('data', () => {});
      response.on('end', () => { clearTimeout(timer); settle(); });
      response.on('error', (err) => { console.warn('[mind-shutdown] stop-state-container response error:', err); clearTimeout(timer); settle(); });
    });
    req.on('error', (err) => { console.warn('[mind-shutdown] stop-state-container request failed:', err); clearTimeout(timer); settle(); });
    req.end();
  });
}

async function stopAllMindsThenDecide(running) {
  let remaining = running;
  while (true) {
    updateQuittingStatus(remaining.length === 1 ? 'Stopping 1 mind…' : `Stopping ${remaining.length} minds…`);
    const stopIds = remaining.map((mind) => mind.id);
    console.log('[mind-shutdown] posting bulk stop for', JSON.stringify(stopIds));
    const { ok, stillRunning } = await postStopMinds(stopIds);
    console.log(`[mind-shutdown] bulk stop result: ok=${ok} stillRunning=${JSON.stringify(stillRunning)}`);
    if (ok && stillRunning.length === 0) {
      await postStopStateContainer();
      return true;
    }
    const blocked = stillRunning.length > 0 ? stillRunning : remaining;
    const names = blocked.map((mind) => mind.name).join(', ');
    const { response } = await dialog.showMessageBox({
      type: 'warning',
      buttons: ['Cancel quit', 'Quit anyway', 'Retry'],
      defaultId: 2,
      cancelId: 0,
      message: blocked.length === 1 ? 'A mind could not be stopped' : 'Some minds could not be stopped',
      detail: `${names}\n\nRetry stopping them, quit anyway (they keep running and using resources), or cancel and stay open.`,
    });
    if (response === 0) return false;
    if (response === 1) return true;
    remaining = blocked;
  }
}

async function promptMindShutdown() {
  if (!getBackendProcess() || !backendBaseUrl) return { proceed: true, stop: false, running: [] };
  const { ok, running } = await getRunningMinds();
  if (!ok) {
    const { response } = await dialog.showMessageBox({
      type: 'warning',
      buttons: ['Cancel', 'Quit anyway'],
      defaultId: 1,
      cancelId: 0,
      message: 'Could not check for running minds',
      detail: 'Any local minds still running would keep using your computer\'s resources. '
        + 'Quit anyway (they may keep running in the background), or cancel and stay open.',
    });
    if (response === 0) return { proceed: false, stop: false, running: [] };
    return { proceed: true, stop: false, running: [] };
  }
  console.log('[mind-shutdown] prompt: running minds =', JSON.stringify(running));
  if (running.length === 0) return { proceed: true, stop: false, running: [] };
  const names = running.map((mind) => mind.name).join(', ');
  const { response } = await dialog.showMessageBox({
    type: 'question',
    buttons: ['Cancel', 'Leave running', 'Shut down all'],
    defaultId: 2,
    cancelId: 0,
    message: running.length === 1
      ? '1 local mind is still running'
      : `${running.length} local minds are still running`,
    detail: `${names}\n\nLeaving them running keeps using your computer's resources. `
      + 'Shutting them down stops their agents and makes their services inaccessible '
      + '(your data is preserved and you can start them again).',
  });
  console.log(`[mind-shutdown] prompt: user chose ${['Cancel', 'Leave running', 'Shut down all'][response]} (response=${response})`);
  if (response === 0) return { proceed: false, stop: false, running: [] };
  if (response === 1) return { proceed: true, stop: false, running: [] };
  return { proceed: true, stop: true, running };
}

function fetchAppStatus(timeoutMs = 25000) {
  // One GET to /ui/api/app-status to learn auth status, the restore inputs
  // (has_accounts, workspace_count, restorable ids), and whether the
  // error-reporting notice still needs acknowledging. See the startup
  // sequence for how the result routes the cold-start landing screen.
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
        url: backendBaseUrl + '/ui/api/app-status',
        method: 'GET',
        useSessionCookies: true,
      });
    } catch {
      clearTimeout(timer);
      resolve(null);
      return;
    }
    let body = '';
    req.on('response', (response) => {
      if (response.statusCode !== 200) {
        response.on('data', () => {});
        response.on('end', () => { clearTimeout(timer); finish(null); });
        response.on('error', () => { clearTimeout(timer); finish(null); });
        return;
      }
      response.on('data', (chunk) => { body += chunk.toString(); });
      response.on('end', () => {
        clearTimeout(timer);
        try {
          const parsed = JSON.parse(body);
          finish({
            authenticated: !!parsed.is_authenticated,
            hasAccounts: !!parsed.has_accounts,
            workspaceCount: typeof parsed.workspace_count === 'number' ? parsed.workspace_count : 0,
            restorableWorkspaceIds: Array.isArray(parsed.restorable_workspace_ids)
              ? parsed.restorable_workspace_ids.map(String)
              : [],
            needsErrorReportingConsent: !!parsed.needs_error_reporting_consent,
          });
        } catch {
          finish(null);
        }
      });
      response.on('error', () => { clearTimeout(timer); finish(null); });
    });
    req.on('error', () => { clearTimeout(timer); finish(null); });
    req.end();
  });
}

// -- Deeplinks (minds://) protocol registration + single instance lock --

if (process.defaultApp) {
  if (process.argv.length >= 2) {
    app.setAsDefaultProtocolClient('minds', process.execPath, [path.resolve(process.argv[1])]);
  }
} else {
  app.setAsDefaultProtocolClient('minds');
}

app.on('open-url', (event, url) => {
  event.preventDefault();
  handleDeeplink(url);
});

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    const url = extractDeeplinkUrlFromArgv(argv || []);
    if (url) {
      handleDeeplink(url);
      return;
    }
    // Launching the app again brings the app forward, so focus the window it
    // has. With none open there is nothing to focus, so open one rather than
    // dropping the launch. (Not openOrFocusWindow() unconditionally: with the
    // backend serving that opens a SECOND window, which is right for Cmd+N and
    // wrong for a re-launch.)
    const mru = getMostRecentWindow();
    if (mru) focusBundle(mru);
    else openOrFocusWindow();
  });
  const coldStartDeeplinkUrl = extractDeeplinkUrlFromArgv(process.argv);
  if (coldStartDeeplinkUrl) handleDeeplink(coldStartDeeplinkUrl);
  app.whenReady().then(onReady);
}

async function onReady() {
  // Send external links to the user's default browser for every WebContents
  // the app ever creates.
  app.on('web-contents-created', (_event, contents) => {
    applyExternalLinkHandling(contents);
  });
  installApplicationMenu();
  installDockMenu();
  installDevDockIcon();
  powerMonitor.on('resume', () => repaintAllWindowsAfterWake('resume'));
  powerMonitor.on('unlock-screen', () => repaintAllWindowsAfterWake('unlock-screen'));

  initialBundle = createBundle();
  const initialSavedState = loadSessionState();
  if (initialSavedState.windows.length > 0) {
    restoreWindowBounds(initialBundle, initialSavedState.windows[0]);
  }
  updater.init({ onStatus: broadcastUpdateStatus });
  await runStartupSequence(initialBundle);
}

function broadcastUpdateStatus(status) {
  for (const bundle of bundles) {
    if (!bundle.window.isDestroyed() && !bundle.window.webContents.isDestroyed()) {
      bundle.window.webContents.send('update-status', status);
    }
  }
}

/**
 * The menu bar's "Check for Updates...".
 *
 * Opens the panel and runs a check, rather than answering in dialogs of its
 * own: the panel already reports the result, the version each channel serves,
 * and when the check ran. Two surfaces answering the same question is how they
 * drift apart.
 */
async function triggerUpdateCheck() {
  const target = getMostRecentWindow();
  if (target) {
    focusBundle(target);
    navigateBundle(target, '/settings?section=updates');
  }
  await updater.check();
}

function installApplicationMenu() {
  if (!isMac || process.env.MINDS_HIDE_MENU === '1') {
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
          click: () => openOrFocusWindow(),
        },
        { type: 'separator' },
        {
          // Deliberately NO Cmd+W accelerator: inside a workspace the dockview
          // UI closes its active tab with it (via the embed contract).
          label: 'Close Window',
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
          accelerator: 'Alt+Cmd+I',
          click: () => {
            const bundle = getMostRecentWindow();
            if (!bundle || bundle.window.isDestroyed()) return;
            if (!bundle.window.webContents.isDestroyed()) {
              bundle.window.webContents.toggleDevTools();
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
      click: () => openOrFocusWindow(),
    },
  ]));
}

function installDevDockIcon() {
  if (app.isPackaged || !isMac || !app.dock) return;
  const devIcon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon-dev.png'));
  if (!devIcon.isEmpty()) app.dock.setIcon(devIcon);
}

async function runStartupSequence(bundle) {
  console.log('[startup] Loading shell.html...');
  bundle.isLoadingState = true;
  await bundle.window.webContents.loadFile(path.join(__dirname, 'shell.html'));
  console.log('[startup] shell.html loaded');

  try {
    await runEnvSetup((status) => broadcastStatusToLoadingWindows(status));
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
    if (!b.window.webContents.isDestroyed()) {
      b.window.webContents.send('status-update', status);
    }
  }
}

// Consume the backend's one-time login code so the default session -- used by
// every window's SPA page (including its /ui/ws channel) and by main's own
// net.request calls -- gets the minds_session cookie.
function consumeOneTimeLoginCode(loginUrl) {
  return new Promise((resolve) => {
    const authenticateUrl = loginUrl.replace('/login?', '/authenticate?');
    console.log('[startup] Consuming one-time code via', authenticateUrl);
    const req = net.request({ url: authenticateUrl, method: 'GET', useSessionCookies: true });
    req.on('response', (resp) => {
      console.log('[startup] /authenticate response status:', resp.statusCode);
      resp.on('data', () => {});
      resp.on('end', () => resolve());
    });
    req.on('error', (err) => {
      console.warn('[startup] /authenticate request failed:', err);
      resolve();
    });
    req.end();
  });
}

// Where the app's first window should land, from the backend's app-status and
// the saved session. Deliberately independent of having a window to put it in,
// and cheap enough to re-run: openStartupRoutedWindow calls it again when the
// route is claimed later, so a session restored well after launch reflects the
// workspaces that exist then rather than a snapshot from startup.
async function computeStartupRouting() {
  const savedState = loadSessionState();
  const appStatus = await fetchAppStatus();
  const authenticated = appStatus && appStatus.authenticated;
  const hasAccounts = !!(appStatus && appStatus.hasAccounts);

  const restorableWorkspaceIds = (authenticated && appStatus.restorableWorkspaceIds) || [];
  const knownAgentIdsSet = restorableWorkspaceIds.length > 0
    ? new Set(restorableWorkspaceIds.map(String))
    : null;
  const restorable = authenticated
    ? filterRestorableUrls(savedState.windows, knownAgentIdsSet)
    : [];

  const workspaceCount = appStatus ? appStatus.workspaceCount : 0;
  const needsConsent = !!(appStatus && appStatus.needsErrorReportingConsent);
  const route = decideStartupRoute({
    authenticated,
    hasAccounts,
    workspaceCount,
    restorableCount: restorable.length,
    needsConsent,
  });
  console.log(
    `[startup] route=${route} authenticated=${authenticated} hasAccounts=${hasAccounts} workspaceCount=${workspaceCount} restorableCount=${restorable.length} needsConsent=${needsConsent}`,
  );
  return { route, restorable, savedState };
}

// Land ``bundle`` on a computed startup route, opening the extra windows a
// multi-window session restore needs. ``boundsAlreadyApplied`` is set for the
// window onReady already sized from the saved state, so its bounds are only
// re-applied when the route picked a different entry.
function applyStartupRouting(bundle, { route, restorable, savedState }, { boundsAlreadyApplied = false } = {}) {
  // The window is leaving the takeover for real content. Clearing the error
  // flag matters when this window IS the takeover a Retry restarted the backend
  // from (a first start that only succeeded on the retry lands here rather than
  // in reloadAllWindowsAfterRetry): while it is set, navigation bookkeeping,
  // session persistence, deeplink delivery and load-failure recovery are all
  // suppressed for the window.
  bundle.isErrorState = false;
  bundle.isLoadingState = false;
  // This IS the first-window route landing, so nothing is owed any more.
  isStartupRoutingPending = false;
  const wc = bundle.window.webContents;
  if (route === 'welcome') {
    wc.loadURL(backendBaseUrl + '/welcome').catch(() => {});
    return;
  }
  if (route === 'consent') {
    wc.loadURL(backendBaseUrl + '/consent').catch(() => {});
    return;
  }
  if (route === 'create') {
    wc.loadURL(backendBaseUrl + '/').catch(() => {});
    return;
  }
  const [first, ...rest] = restorable;
  if (!boundsAlreadyApplied || !isSameSavedWindow(first, savedState.windows[0])) {
    restoreWindowBounds(bundle, first);
  }
  wc.loadURL(toRestoredContentUrl(first)).catch(() => {});
  const restoredBundles = [];
  for (const entry of rest) {
    const restored = openNewWindow(toRestoredContentUrl(entry), { showInactive: true });
    restoreWindowBounds(restored, entry);
    restoredBundles.push(restored);
  }
  mruWindows.length = 0;
  mruWindows.push(bundle, ...restoredBundles);
  const raiseFirst = () => {
    if (!bundle.window.isDestroyed()) bundle.window.focus();
  };
  for (const restored of restoredBundles) {
    if (restored.window.isVisible()) raiseFirst();
    else restored.window.once('show', raiseFirst);
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

    // `localhost` (not `127.0.0.1`) so the auth cookie, issued with
    // `Domain=localhost`, is valid here.
    backendBaseUrl = `http://localhost:${port}`;

    console.log('[startup] Backend ready at', backendBaseUrl);
    // The backend is serving again, so any recorded failure is stale: a window
    // opened from here on gets the app, not a replay of the old error screen.
    lastErrorTakeover = null;

    const isFirstStart = !hasCompletedInitialStart;
    hasCompletedInitialStart = true;

    if (isFirstStart) {
      // The first-window route is now owed. Marked BEFORE the awaits below so a
      // window requested while they run waits on the loading screen instead of
      // landing on home and being yanked off it a moment later.
      isStartupRoutingPending = true;
      isStartupRoutingBeingComputed = true;
      let routing;
      try {
        // Unconditional, and deliberately NOT inside the window guard below:
        // this is the only place the one-time code is consumed and a fresh one
        // is minted per backend run, so skipping it because the startup window
        // was closed strands the app on /login for the rest of the run.
        await consumeOneTimeLoginCode(loginUrl);
        routing = await computeStartupRouting();
      } finally {
        isStartupRoutingBeingComputed = false;
      }
      // Not necessarily the window startup began in: the user may have closed
      // that one and re-opened another (which is sitting on the loading
      // takeover waiting for exactly this).
      const isInitialAlive = initialBundle && !initialBundle.window.isDestroyed();
      const target = isInitialAlive ? initialBundle : getMostRecentWindow();
      if (target) {
        applyStartupRouting(target, routing, { boundsAlreadyApplied: isInitialAlive });
      } else {
        // Nothing is open at all. macOS keeps the app alive, so leave the route
        // owed rather than popping windows up unprompted a minute after the
        // user deliberately closed one: the next window they ask for is routed
        // (against a freshly computed session, not this stale one).
        console.log('[startup] no window survived startup; holding the route for the next one');
      }
    } else {
      reloadAllWindowsAfterRetry();
    }

    flushPendingDeeplink();

    const proc = getBackendProcess();
    if (proc) {
      proc.on('exit', (code) => {
        // The direct child is `uv run`, which traps SIGTERM (forwarding it on)
        // and reports the backend's death as a plain exit status: a graceful
        // stop is 0, a backend killed out from under us is uv's nonzero 128+n.
        // So shutdown()'s SIGTERM on a quit or a retry arrives as 0, and the
        // one thing that arrives as null -- a real signal death -- is its
        // SIGKILL escalation, SIGKILL being the signal uv cannot trap. Not
        // airtight (any SIGKILL of uv reads the same), but treating that as a
        // crash would pop the error screen over an escalated teardown.
        if (code === 0 || code === null) return;
        console.error(
          `[backend] exited unexpectedly with code ${code} (${bundles.size} window(s) open)`,
        );
        // Deliberately NOT gated on there being a window. Recording the
        // takeover is what makes the crash recoverable: with none open it is
        // replayed into the next window the user opens, so they get the error
        // screen and its Retry instead of a fresh window loaded at the dead
        // port -- whose own Reload button only re-loads that same dead port.
        showErrorInAllWindows(
          'Minds stopped unexpectedly',
          readLastLogLines(50) || `Process exited with code ${code}`,
        );
      });
    }
  } catch (err) {
    showErrorInAllWindows('Failed to start Minds', err.message);
  }
}

// -- Deeplinks (minds://) --

function handleDeeplink(rawUrl) {
  console.log(`[deeplink] received: ${String(rawUrl).slice(0, 256)}`);
  const mru = getMostRecentWindow();
  // A current error takeover counts as not-ready even with no window to read it
  // off: after a crash backendBaseUrl still names the dead port, and navigating
  // a window there is the failure this app's error screen exists to replace.
  if (
    !canApplyDeeplinks
    || !backendBaseUrl
    || lastErrorTakeover
    || (mru && (mru.isLoadingState || mru.isErrorState))
  ) {
    // Not ready to act on it yet: hold it for flushPendingDeeplink(). With no
    // window there is nothing to focus, so open one on whatever takeover
    // screen the app's state warrants instead of dropping the URL silently.
    pendingDeeplinkUrl = rawUrl;
    if (mru) focusBundle(mru);
    // A cold-start argv deeplink reaches here during module evaluation, before
    // app.whenReady() -- no BrowserWindow can be constructed yet, and none is
    // needed: onReady opens the startup window and flushPendingDeeplink()
    // applies the URL to it.
    else if (app.isReady()) openOrFocusWindow();
    return;
  }
  // A Template link always navigates to the SPA's Create from
  // Template page: it carries both legacy branches (create a new machine,
  // or add the Template to an existing one), so the old in-machine modal
  // variant is gone.
  const targetPath = deeplinkTargetPath(rawUrl);
  if (!mru) {
    // macOS hands a URL to an already-running app via application:openURLs:,
    // which need not fire 'activate', so nothing else will open a window for
    // it. A focus-only link (bare minds://, what the browser sign-in success
    // page's "Open app" uses) opens the home page: opening the app IS the
    // documented contract for it.
    //
    // An explicit deeplink outranks a first-window route still owed (the docs'
    // rule: a deeplink wins over the welcome screen), and settles it -- this
    // window is where the launch landed, so a later Cmd+N must not still pop
    // the restored session open.
    isStartupRoutingPending = false;
    openNewWindow(toAbsoluteUrl(targetPath || '/'));
    return;
  }
  focusBundle(mru);
  if (!targetPath) return;
  navigateBundle(mru, targetPath);
}

function flushPendingDeeplink() {
  canApplyDeeplinks = true;
  if (!pendingDeeplinkUrl) return;
  const url = pendingDeeplinkUrl;
  pendingDeeplinkUrl = null;
  handleDeeplink(url);
}

function handleNotification(event) {
  const agentName = event.agent_name || 'Agent';
  const title = event.title || `Notification from ${agentName}`;
  console.log(`[notification] received: title=${JSON.stringify(title)} agent=${agentName}`);
  if (!Notification.isSupported()) {
    // No JS-level "ask for permission" exists for Electron's native
    // Notification module -- macOS owns that decision entirely (System
    // Settings > Notifications) and never surfaces the verdict back to app
    // code. isSupported() is the one thing we CAN check: false means this
    // platform/session cannot show native notifications at all (e.g. no
    // Notification Center backend available), so show() would silently do
    // nothing. Log it so "notifications aren't working" is at least
    // distinguishable from "the OS declined to display it" (unobservable)
    // versus a real bug upstream of this point.
    console.warn('[notification] Notification.isSupported() is false -- the OS cannot show native notifications here; skipping .show()');
    return;
  }
  const notification = new Notification({
    title,
    body: event.message,
  });
  // 'show' fires once the OS has actually presented the banner -- the one
  // signal that distinguishes "displayed" from "silently declined" (macOS
  // exposes no permission-check API to app code, so this is the closest
  // thing to a confirmation we get).
  notification.on('show', () => {
    console.log(`[notification] shown by the OS: ${JSON.stringify(title)}`);
  });
  // A definitive OS-reported failure (see probeNotificationPermission's own
  // 'failed' handler) -- distinct from getting neither 'show' nor 'failed'
  // at all, which the generic hint after .show() below already covers.
  notification.on('failed', () => {
    console.warn(`[notification] failed to display (OS-reported): ${JSON.stringify(title)}`);
  });
  notification.on('click', () => {
    const url = event.url;
    if (!url) {
      const mru = getMostRecentWindow();
      if (mru) focusBundleFromNotificationClick(mru);
      return;
    }
    const absolute = toAbsoluteUrl(url);
    const agentId = parseWorkspaceId(absolute);
    if (agentId) {
      // The most-recently-focused window already showing this workspace, else
      // navigate the most recent window (never auto-open a new one).
      const showingBundle = mostRecentBundleForWorkspace(agentId);
      const target = showingBundle || getMostRecentWindow();
      if (target) {
        focusBundleFromNotificationClick(target);
        if (showingBundle === null) navigateBundle(target, absolute);
      }
    } else {
      // Notification deep links use the SPA's /workspace/<agent-id>?review=
      // route -- a chrome-page path, not a workspace origin, so
      // parseWorkspaceId above cannot see it. Match the path id against each
      // window's tracked workspace so the window already on that workspace is
      // the one focused -- and still navigated: it needs the ?review= param
      // to open the review popup. Anything else falls back to the MRU window.
      const routeId = parseSpaWorkspaceRouteId(absolute);
      const target = (routeId && mostRecentBundleForWorkspace(routeId)) || getMostRecentWindow();
      if (target) {
        focusBundleFromNotificationClick(target);
        navigateBundle(target, absolute);
      }
    }
  });
  notification.show();
  console.log(`[notification] .show() called for ${JSON.stringify(title)} -- if no banner appeared, check System Settings > Notifications for this app`);
}

// How long to wait for the OS to confirm a probe notification was actually
// displayed before concluding permission is not granted, on top of the
// definitive 'failed' event below. Real headroom, not just enough for the
// typical case: 'show' firing is intermittently slow under an unsigned dev
// build (see probeNotificationPermission's own comment), observed timing
// out under 4s and then firing normally moments later with no permission
// change in between.
const NOTIFICATION_PERMISSION_PROBE_TIMEOUT_MS = 10000;

// Ask the OS for native-notification permission the only way Electron allows
// on macOS: by actually attempting to show one. There is no separate
// "request permission" call -- posting a notification for the first time IS
// the request, and the system's own "Would You Like to Allow Notifications"
// dialog appears before the banner does. Resolves true if the OS confirmed
// it displayed the probe ('show' fired in time), false on a definitive
// 'failed' event or on timing out unseen (denied, or simply not decided fast
// enough -- Electron cannot tell the two apart). The probe self-dismisses as
// soon as it is resolved either way.
//
// On macOS, the underlying UNNotification API requires the app to be
// code-signed for 'show' to fire reliably at all -- an unsigned build (any
// local dev run of this app, including via `pnpm start`) instead gets an
// intermittent mix of a delayed 'show', an explicit 'failed', or neither
// within the timeout, even though the OS's own notification permission is
// genuinely granted and a banner may visibly appear. This is a real Electron
// limitation of unsigned binaries (see electronjs.org/docs/latest/api/notification),
// not a bug in this probe: a packaged, signed build does not have it.
function probeNotificationPermission() {
  if (!Notification.isSupported()) {
    console.warn('[notification] permission probe skipped: Notification.isSupported() is false');
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    const notification = new Notification({
      title: 'Notifications enabled',
      body: 'minds will notify you here when a machine needs your attention.',
      silent: true,
    });
    let settled = false;
    const settle = (granted, reason) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      notification.close();
      console.log(`[notification] permission probe ${reason}`);
      resolve(granted);
    };
    const timer = setTimeout(() => settle(false, 'timed out -- treating as declined'), NOTIFICATION_PERMISSION_PROBE_TIMEOUT_MS);
    notification.on('show', () => settle(true, 'confirmed shown'));
    // An unsigned build (see this function's own comment) reliably gets this
    // instead of 'show' -- resolve it immediately rather than waiting out
    // the full timeout for a verdict the OS already gave.
    notification.on('failed', () => settle(false, 'failed (likely an unsigned build -- see this function\'s comment)'));
    notification.show();
  });
}

ipcMain.handle('probe-notification-permission', () => probeNotificationPermission());

// Open the OS's own notification-settings pane so the reader can flip the
// permission back on themselves after declining it -- no app can force a
// re-prompt once the OS has decided (see probeNotificationPermission).
// Deliberately opens the general pane rather than deep-linking to this app's
// own row: on macOS, every locally-run unpackaged dev build shares one
// "com.github.Electron" identity in the OS's eyes (Electron.app's own
// Info.plist, regardless of which project's checkout is running), and a
// packaged build's real bundle id isn't available to look up here anyway.
function openExternalBestEffort(url) {
  return shell.openExternal(url).then(
    () => true,
    (err) => {
      console.warn('[notification] failed to open OS notification settings:', err && err.message);
      return false;
    },
  );
}

// Linux has no cross-desktop-environment URI for this the way macOS has
// x-apple.systempreferences: -- ms-settings: (the previous fallback here) is
// a Windows-only scheme that does nothing on Linux, one of the two platforms
// this app commits to supporting (see CLAUDE.md). Try each desktop
// environment's own settings command in turn, GNOME first as the most
// common target, then KDE Plasma, resolving on the first one that actually
// launches. Spawned detached/unref'd rather than waited on to exit: these
// are GUI apps the reader may leave open for a while.
const LINUX_NOTIFICATION_SETTINGS_COMMANDS = [
  ['gnome-control-center', ['notifications']],
  ['systemsettings5', ['kcm_notifications']],
  // KDE dropped the "5" suffix from KF6-based tool binaries (see KDE's own
  // T14763), so Plasma 6 ships `systemsettings` rather than `systemsettings5`.
  // Tried after the Plasma 5 name so neither desktop's fallback regresses.
  ['systemsettings', ['kcm_notifications']],
];

function trySpawnDetached(command, args) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (ok) => {
      if (settled) return;
      settled = true;
      resolve(ok);
    };
    const child = spawn(command, args, { detached: true, stdio: 'ignore' });
    child.once('error', () => settle(false));
    child.once('spawn', () => {
      child.unref();
      settle(true);
    });
  });
}

async function openLinuxNotificationSettings() {
  for (const [command, args] of LINUX_NOTIFICATION_SETTINGS_COMMANDS) {
    if (await trySpawnDetached(command, args)) return true;
  }
  console.warn('[notification] no known Linux notification-settings command was found');
  return false;
}

ipcMain.handle('open-notification-settings', () => {
  if (isMac) return openExternalBestEffort('x-apple.systempreferences:com.apple.preference.notifications');
  if (process.platform === 'linux') return openLinuxNotificationSettings();
  return openExternalBestEffort('ms-settings:notifications');
});

// Trust the forward proxy's CA-signed leaf certs for its loopback origins.
// The CA is local to this machine (under the plugin state dir) and only
// minds' own loopback origins use it; every real https origin still gets
// Chromium's default verification (cb(-3)).
function isLoopbackHostname(hostname) {
  return hostname === 'localhost' || hostname.endsWith('.localhost') || hostname === '127.0.0.1';
}

function trustLoopbackCerts() {
  session.defaultSession.setCertificateVerifyProc((request, callback) => {
    if (isLoopbackHostname(request.hostname)) {
      callback(0); // trust
    } else {
      callback(-3); // defer to Chromium's default verification
    }
  });
  console.log('[startup] Trusting forward-proxy loopback certs (HTTP/2 on)');
}

async function handleMngrForwardStarted(event) {
  const port = event.mngr_forward_port;
  const preauth = event.preauth_cookie;
  if (!port || !preauth) {
    console.warn('[startup] mngr_forward_started missing port or preauth_cookie:', event);
    return;
  }
  const url = `https://localhost:${port}`;
  mngrForwardBaseUrl = url;
  trustLoopbackCerts();
  // The workspace iframe is cross-site to the chrome page, so the pre-set
  // cookie must be SameSite=None (Electron: 'no_restriction') + Secure to be
  // sent from inside it -- matching the attributes the plugin itself sets.
  try {
    await session.defaultSession.cookies.set({
      url,
      name: 'mngr_forward_session',
      value: preauth,
      httpOnly: true,
      sameSite: 'no_restriction',
      path: '/',
      secure: true,
    });
    console.log('[startup] mngr_forward_session cookie pre-set on', url);
  } catch (err) {
    console.warn('[startup] Failed to set mngr_forward_session cookie:', err);
  }
}

function handleAuthEvent(event) {
  if (event.event === 'auth_success') {
    // Sign-in happens on the hosted browser page; the SPA's accounts channel
    // frame carries the new identity, but pre-WS surfaces (and any window
    // stuck on a stale state) pick it up from a plain reload.
    for (const b of bundles) {
      if (b.window.isDestroyed() || b.window.webContents.isDestroyed()) continue;
      b.window.webContents.reload();
    }
  } else if (event.event === 'auth_required') {
    const mru = getMostRecentWindow();
    if (!mru) return;
    focusBundle(mru);
    if (backendBaseUrl) {
      // ``?web-login=1`` makes the SPA start the browser sign-in flow on
      // load, with the message rendered in its waiting modal.
      const authUrl = `${backendBaseUrl}/?web-login=1&web-login-message=` +
        encodeURIComponent('You need to sign in to Imbue in order to share');
      navigateBundle(mru, authUrl);
    }
  }
}

// -- IPC handlers --

ipcMain.handle('get-update-state', () => updater.describe());

// Returns what each channel currently serves, for the switch confirmation.
ipcMain.handle('peek-update-channels', () => updater.peekChannels());

ipcMain.handle('set-update-channel', async (_event, channel) => {
  await updater.setChannel(channel);
  return updater.describe();
});

ipcMain.handle('check-for-updates', async () => {
  await updater.check();
  return updater.describe();
});

// The "Restart" control on the update card. Quits, so it returns nothing.
ipcMain.handle('install-update', () => updater.installNow());

ipcMain.on('bring-app-to-front', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.window.isFocused()) return;
  stealFocusAndFocusBundle(bundle);
});

ipcMain.handle('report-error', () => {
  try {
    const eventId = captureManualReport({
      message: lastErrorTakeover ? lastErrorTakeover.message : null,
      details: lastErrorTakeover ? lastErrorTakeover.details : null,
    });
    return { ok: Boolean(eventId), eventId: eventId || null };
  } catch (err) {
    console.error('[report-error] failed to capture manual report:', err && err.message);
    return { ok: false, eventId: null };
  }
});

ipcMain.on('open-workspace-in-new-window', (_event, agentId) => {
  if (typeof agentId !== 'string' || !/^(?:agent|host)-[a-f0-9]{1,64}$/i.test(agentId)) return;
  // "Open in new window" always opens a new window, even when another window
  // already shows the workspace (two windows on one workspace is allowed).
  const url = wrapperUrlForWorkspace(agentId);
  if (url) openNewWindow(url);
});

// Reload after the window showed the crash strip: back to the failed URL if a
// load failure got us here, else the page the window was on.
ipcMain.on('reload-chrome', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || bundle.window.isDestroyed() || bundle.window.webContents.isDestroyed()) return;
  bundle.isChromeCrashed = false;
  const failedUrl = bundle.chromeLoadFailedUrl;
  bundle.chromeLoadFailedUrl = null;
  bundle.chromeLoadRetryCount = 0;
  bundle.chromeLoadRetryPendingUrl = null;
  if (failedUrl) {
    bundle.window.webContents.loadURL(failedUrl).catch(() => {});
    return;
  }
  const target = bundle.currentWorkspaceId
    ? wrapperUrlForWorkspace(bundle.currentWorkspaceId)
    : toAbsoluteUrl(bundle.currentContentUrl || '/');
  if (target) bundle.window.webContents.loadURL(target).catch(() => {});
  else if (backendBaseUrl) bundle.window.webContents.loadURL(backendBaseUrl + '/').catch(() => {});
});

ipcMain.on('retry', async (event) => {
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

ipcMain.handle('show-file-picker', async (event, options) => {
  const bundle = getBundleFromEvent(event);
  const opts = options || {};
  const property = opts.mode === 'directory' ? 'openDirectory' : 'openFile';
  const dialogOptions = { properties: [property] };
  if (typeof opts.defaultPath === 'string' && opts.defaultPath.length > 0) {
    dialogOptions.defaultPath = opts.defaultPath;
  }
  const result = bundle && bundle.window && !bundle.window.isDestroyed()
    ? await dialog.showOpenDialog(bundle.window, dialogOptions)
    : await dialog.showOpenDialog(dialogOptions);
  if (result.canceled || !Array.isArray(result.filePaths) || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
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

let isQuitSequenceRunning = false;
let isHeadlessQuit = false;

async function runQuitSequence() {
  if (isShuttingDown || isQuitSequenceRunning) return;
  isQuitSequenceRunning = true;

  let plan = { proceed: true, stop: false, running: [] };
  try {
    if (!isHeadlessQuit) {
      plan = await promptMindShutdown();
      if (!plan.proceed) {
        isQuitSequenceRunning = false;
        return;
      }
    }
  } catch (err) {
    console.warn('[lifecycle] local-mind shutdown prompt failed, quitting anyway:', err);
    plan = { proceed: true, stop: false, running: [] };
  }

  isShuttingDown = true;
  debouncedSessionSaver.cancel();
  if (!isHeadlessQuit) showQuittingInAllWindows();
  if (bundles.size > 0) saveSessionState();

  if (plan.stop && plan.running.length > 0) {
    let shouldProceed = true;
    try {
      shouldProceed = await stopAllMindsThenDecide(plan.running);
    } catch (err) {
      console.warn('[lifecycle] stopping local minds failed, quitting anyway:', err);
    }
    if (!shouldProceed) {
      isShuttingDown = false;
      restoreFromQuittingInAllWindows();
      isQuitSequenceRunning = false;
      return;
    }
  }

  updateQuittingStatus('Closing…');
  await shutdown();
  if (bundles.size > 0) saveSessionState();
  // The console capture writes through an async stream, so records logged in the
  // last moments before quitting sit in its buffer. Flushing here is what puts
  // the run-up to a shutdown in the file a later bug report reads.
  await closeConsoleCapture();
  app.quit();
}

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    console.log(`[lifecycle] ${signal} received, requesting quit`);
    isHeadlessQuit = true;
    app.quit();
  });
}

app.on('window-all-closed', () => {
  console.log('[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  // On macOS the app stays alive with no windows (the dock icon remains); the
  // user re-opens a window from the dock (see 'activate' below) and quits with
  // Cmd+Q. Other platforms quit when the last window closes.
  if (!shouldQuitOnWindowAllClosed({ isMac, isShuttingDown, isQuitSequenceRunning })) return;
  runQuitSequence();
});

// macOS: activating the app (clicking the dock icon) with no open windows
// re-opens one on whatever the app's state warrants -- the home page, the
// loading screen, or the error takeover. With a window already open the OS
// just brings it forward, so we act only when none remain.
app.on('activate', () => {
  if (!shouldOpenWindowOnActivate({
    isShuttingDown,
    isQuitSequenceRunning,
    hasLiveWindow: getMostRecentWindow() != null,
  })) {
    return;
  }
  openOrFocusWindow();
});

app.on('before-quit', (event) => {
  console.log('[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
  if (isShuttingDown) return;
  event.preventDefault();
  runQuitSequence();
});
