const { BaseWindow, WebContentsView, Menu, Notification, clipboard, dialog, ipcMain, net, shell, app, session, screen, nativeImage, powerMonitor } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { initElectronLogging } = require('./logger');
const { initSentry, captureManualReport, isLogInclusionEnabled } = require('./sentry');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');
const { decideStartupRoute } = require('./startup-routing');
const { computeBundleViewBounds } = require('./view-layout');
const { deeplinkTargetPath, extractDeeplinkUrlFromArgv } = require('./deeplink');
// URL classification for the two content surfaces lives in ./surface-routing so
// it can be unit-tested under plain node (main.js can't be required outside
// Electron). navigateBundle uses selectSurfaceForUrl / SURFACE_CONTENT to send
// agent URLs to the content view and local pages to the chrome view.
const {
  parseWorkspaceId,
  parseAccentSourceAgentId,
  selectSurfaceForUrl,
  isSwappableLocalPath,
  SURFACE_CONTENT,
} = require('./surface-routing');
const { shouldWriteSessionState, createDebouncedSaver } = require('./session-persistence');

// Tee console output into ~/.minds/logs/electron.log and record uncaught
// main-process failures BEFORE anything else runs, so startup output (including
// Sentry init and any early crash) is durably captured on disk.
initElectronLogging();

// Initialize Sentry as early as possible so errors thrown during main-process
// startup (window creation, env setup, backend spawn) are captured. The SDK is
// always initialized but only sends when the user has enabled error reporting
// (the report_unexpected_errors setting, read live per event) -- see
// electron/sentry.js. ``rendererNameForWebContents`` labels renderer-death events
// by which of the window's three views died (it's a hoisted declaration, so
// referencing it here before its definition is fine; it's only invoked at crash
// time, long after ``bundles`` is populated).
initSentry({ getRendererName: rendererNameForWebContents });

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

// Surface the git SHA the build was cut from in the standard macOS About
// panel, appended to ToDesktop's buildId so you can map a shipped binary
// back to a commit. Gated on app.isPackaged because dev runs do not
// regenerate build-info.json and would otherwise show a stale SHA.
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
// Gap on the left, right, and bottom of the contentView. Matches
// browser mode's iframe layout (``left-[4px]`` with ``width:
// calc(100% - 8px)`` in Chrome.jinja) so Electron and browser modes
// render the same shape. The top of the contentView is flush with the
// titlebar's bottom edge; the rounded top corners are revealed by the
// chromeView painting accent in the cutouts.
const CONTENT_INSET = 4;
// Corner radius applied to the contentView. Native ``setBorderRadius``
// rounds all four corners with the same radius; the cutouts at every
// corner reveal the chromeView underneath (which paints the accent
// color everywhere it isn't covered by the contentView), so the visible
// frame around the contentView is uniformly accent-colored regardless
// of OS or workspace content background.
//
// For the inner content corners to look concentric with the OS's outer
// window rounding (where one exists), the inner radius should be
// ``outer_radius - inset``. Calibrated by eye against macOS Tahoe's
// outer rounding (the Liquid Glass redesign bumped this up significantly
// from Big Sur-era ~11px) -- 12px with our 4px inset reads as a parallel
// offset of the window's outer curve. Windows/Linux frameless windows
// are square (no OS rounding to match) so 12px is a free design choice
// there -- if we ever wire DWM ``DWMWCP_ROUND`` on Win11 the outer would
// be ~8px and a smaller inner would be more concentric.
const CONTENT_CORNER_RADIUS = 12;
const CONTENT_PARTITION = 'persist:workspace-content';

// Local crash page shown in the content view when its renderer process dies.
const CRASHED_PAGE_FILE = path.join(__dirname, 'crashed.html');

// Local crash page shown in the chrome (titlebar) view when its renderer process
// dies. Unlike the content crash page it is a compact strip: only the top ~38px
// of the chrome view is visible (the content view overlays the rest), so the page
// anchors its message + Reload button to that titlebar-height band.
const CHROME_CRASHED_PAGE_FILE = path.join(__dirname, 'chrome-crashed.html');

// Coalesce rapid SSE-triggered list refreshes when the inbox modal is open. A
// burst of requests events (count 1 -> 2 -> 3 within a few ms) would otherwise
// re-post the chrome-event multiple times in flight; the inbox shell would
// queue several /inbox/list fetches and waste backend HTTP load by
// (open windows) x (events).
const INBOX_LIST_REFRESH_DEBOUNCE_MS = 50;

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
// Latest per-agent system-interface health (``healthy`` / ``stuck`` /
// ``restarting`` / ``restart_failed``) as pushed by the chrome SSE's
// ``system_interface_status`` events. Read by the landing-page Stop handler so
// it can leave a window that is mid-restart alone (the user is intentionally
// restarting it there) rather than yanking it out from under them.
const systemInterfaceStatusByAgent = new Map();
let isShuttingDown = false;
let initialBundle = null; // the first window created at startup
let hasCompletedInitialStart = false;
// A minds:// URL that arrived before the app could act on it (backend not up,
// startup navigation still in flight, or an error takeover showing).
// Last-writer-wins; applied by ``flushPendingDeeplink`` once startup
// navigation has settled.
let pendingDeeplinkUrl = null;
let canApplyDeeplinks = false;

// Central cache of the latest SSE state from /_chrome/events so newly-loaded
// chrome and modal webContents (which may host the sidebar, inbox, or any
// future overlay page) can be primed without opening their own SSE connection.
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
//
// ``parseWorkspaceId`` (is this URL a workspace?), ``parseAccentSourceAgentId``
// (which workspace's accent tints the titlebar?), and ``selectSurfaceForUrl``
// (content vs chrome surface) live in ./surface-routing so the classification
// is unit-testable under plain node (main.js can't be required outside
// Electron). They are required at the top of this file.

function toAbsoluteUrl(url) {
  if (!url) return url;
  if (url.startsWith('/') && backendBaseUrl) return backendBaseUrl + url;
  return url;
}

// Whether ``url`` is our local content-view crash page (crashed.html). The
// navigation handlers use this to skip treating the crash page as real workspace
// content (which would clobber the pre-crash URL / workspace id we need to reload).
function isCrashPageUrl(url) {
  if (!url || !url.startsWith('file://')) return false;
  try {
    return decodeURIComponent(new URL(url).pathname).endsWith('/crashed.html');
  } catch {
    return false;
  }
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

// The window whose CURRENT SCREEN is scoped to ``agentId`` -- the workspace
// itself or its settings / sharing / destroying / recovery screens (tracked by
// ``currentAccentAgentId``, which follows parseAccentSourceAgentId of the
// current screen). Wider than findBundleForWorkspace: a window sitting on
// workspace X's SETTINGS owns X's scope even though it displays no workspace.
// One window owns a workspace's whole scope: opening the workspace while its
// settings are open elsewhere (or vice versa) routes to that window instead of
// splitting the scope across two windows.
function findBundleForWorkspaceScope(agentId) {
  if (!agentId) return null;
  for (const b of bundles) {
    if (!b.window.isDestroyed() && b.currentAccentAgentId === agentId) return b;
  }
  return null;
}

function getBundleFromEvent(event) {
  if (!event || !event.sender) return null;
  const senderId = event.sender.id;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    const views = [b.chromeView, b.contentView, b.modalView];
    for (const v of views) {
      if (!v) continue;
      if (v.webContents.isDestroyed()) continue;
      if (v.webContents.id === senderId) return b;
    }
  }
  return null;
}

// Map a webContents to the name of the bundle view it backs
// (``chrome`` / ``content`` / ``modal``), or undefined if it isn't one of ours.
// Passed to Sentry.init as ``getRendererName`` so the childProcess integration's
// renderer-death events are labeled by which view died. A renderer that is gone
// is not yet destroyed, so its stable ``id`` is still readable here.
function rendererNameForWebContents(contents) {
  if (!contents) return undefined;
  const id = contents.id;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    if (b.chromeView && !b.chromeView.webContents.isDestroyed() && b.chromeView.webContents.id === id) return 'chrome';
    if (b.contentView && !b.contentView.webContents.isDestroyed() && b.contentView.webContents.id === id) return 'content';
    if (b.modalView && !b.modalView.webContents.isDestroyed() && b.modalView.webContents.id === id) return 'modal';
  }
  return undefined;
}

// After the machine wakes from sleep (or the screen unlocks), a WebContentsView's
// renderer can survive but stop painting: Electron leaves its GPU/compositor
// surface detached, so the view keeps showing its pinned-white background until
// something forces a repaint (historically only a manual window move, focus
// change, or Home-and-back navigation would). This is distinct from a renderer
// *death* -- that fires `render-process-gone`, which wireContentViewEvents already
// recovers with the crash page. Here the renderer is alive, no event fires, and
// nothing recovers it (Sentry event fc1dc9eb: "came back from sleep and was on a
// blank white screen"). `webContents.invalidate()` schedules a full repaint -- the
// programmatic equivalent of that window-move nudge -- reattaching the surface
// without a reload, so no scroll, websocket, or terminal state is lost. It is
// non-destructive and idempotent, so firing it across every live view on each
// wake (macOS emits resume AND unlock-screen for a single wake) is safe.
function repaintAllBundleViewsAfterWake(trigger) {
  let repainted = 0;
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    for (const view of [b.chromeView, b.contentView, b.modalView]) {
      if (!view || view.webContents.isDestroyed()) continue;
      view.webContents.invalidate();
      repainted += 1;
    }
  }
  // Log unconditionally: wake events are rare, and this trail is what makes the
  // otherwise-invisible blank-after-sleep failure diagnosable in electron.log.
  console.log(`[wake-repaint] ${trigger}: forced repaint of ${repainted} view(s)`);
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

// Whether ``bundle`` is the only still-open window. The `close` event fires
// before `closed` (which removes the bundle from the set), so the closing
// bundle is still counted here; "last" therefore means exactly one live window.
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
  bundle.window.setTitle(computeTitleFor(bundle));
}

function updateAllOsTitles() {
  for (const b of bundles) updateOsTitle(b);
}

// Tear down every live window currently open to ``agentId``. If those windows
// are the only ones left, navigate them to the home page instead of closing
// them, so we never close the last window (which would commit an app
// shutdown). Shared by the workspace-destroyed handler and the landing-page
// Stop handler. (At most one window exists per workspace, so this affects at
// most one window in practice.)
function detachWindowsForWorkspace(agentId) {
  if (!agentId) return;
  const affected = [];
  for (const b of bundles) {
    if (!b.window.isDestroyed() && b.currentWorkspaceId === agentId) {
      affected.push(b);
    }
  }
  if (affected.length === 0) return;
  const liveBundleCount = [...bundles].filter((b) => !b.window.isDestroyed()).length;
  for (const b of affected) {
    if (liveBundleCount - affected.length >= 1) {
      b.window.close();
    } else if (backendBaseUrl) {
      // The window's workspace is gone: route it back to Home. Home is a local
      // page, so navigateBundle renders it in the chrome view and hides the
      // (agent) content view, clears currentWorkspaceId + notifies the chrome
      // renderer -- releasing its recovery-redirect lock -- and drops the
      // titlebar accent to the neutral chrome. Blank the parked page too: the
      // workspace was DESTROYED, so the keep-alive residency must not offer a
      // dead page for a later reveal.
      navigateBundle(b, backendBaseUrl + '/');
      if (b.contentView && !b.contentView.webContents.isDestroyed()) {
        try {
          b.contentView.webContents.loadURL('about:blank').catch(() => {});
        } catch { /* noop */ }
      }
    }
  }
}

// -- Layout --

function updateBundleBounds(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const { width, height } = bundle.window.getContentBounds();

  // The per-view bounds math -- the takeover collapse, the error-state modal
  // overlay (so the "Report a bug" /help modal is visible over the error
  // screen), and the normal inset/accent layout -- lives in
  // computeBundleViewBounds so it can be unit-tested under plain node. Apply
  // each result only to the views that currently exist: in the takeover states
  // the content/modal views are usually already torn down, and during a quit a
  // present-but-hidden modal is sized to 0x0 (it stays setVisible(false)
  // regardless). The chrome view fills the window in every regime; in the
  // normal layout it paints the workspace accent in the inset frame + rounded
  // corner cutouts the content view leaves, and the modal overlays the whole
  // window (its own transparent dim backdrop shows the content behind it).
  const bounds = computeBundleViewBounds({
    isErrorState: bundle.isErrorState,
    isLoadingState: bundle.isLoadingState,
    isQuittingState: bundle.isQuittingState,
    modalVisible: bundle.modalVisible,
    width,
    height,
    titlebarHeight: TITLEBAR_HEIGHT,
    contentInset: CONTENT_INSET,
  });
  // A takeover collapses the overlay view out from under any showing tooltip, so
  // drop the flag that says the tooltip owns the view's bounds. Otherwise the
  // modal-bounds gate below would keep skipping the full-window restore after
  // recovery and leave the overlay surface stuck at 0x0.
  if (bundle.isErrorState || bundle.isLoadingState || bundle.isQuittingState) {
    bundle.tooltipVisible = false;
  }
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.setBounds(bounds.chrome);
  }
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.setBounds(bounds.content);
  }
  if (bundle.modalView && !bundle.modalView.webContents.isDestroyed()) {
    // While a tooltip is showing (and no modal is open), the overlay manager
    // owns the view's bounds (it's shrunk to the tooltip's rect); leave them so
    // a resize doesn't snap it back to full-window. Tooltips dismiss on resize,
    // which restores the full-window bounds. Otherwise apply the computed bounds
    // (full window normally / when a modal is open, collapsed during a takeover).
    if (!(bundle.tooltipVisible && !bundle.modalVisible)) {
      bundle.modalView.setBounds(bounds.modal);
    }
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
    // Match the app's light surface so any gap between view paints (e.g. a
    // chrome-view navigation between local pages) exposes white, not the
    // platform default (black). The chrome view pins the same color below.
    backgroundColor: '#ffffff',
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
  // The chrome view now NAVIGATES between local pages (Home, Create, settings,
  // and the /_chrome agent-wrapper). During each navigation Chromium paints the
  // view's background color between the old page's teardown and the new page's
  // first paint; the default is uninitialized (renders black over the window
  // backing), which flashed the whole window black on every local navigation.
  // Pin it to the app's light surface, mirroring the content view below.
  chromeView.setBackgroundColor('#ffffff');
  const contentView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'content-relay-preload.js'),
      partition: CONTENT_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  // Round all four corners of the contentView so it floats inside a
  // rounded inset frame painted by the full-window chromeView underneath.
  // See ``updateBundleBounds`` for how the chromeView covers the entire
  // window and the contentView is inset on three sides. Note from
  // Electron docs: ``setBorderRadius`` rounds the RENDERED region, but
  // the view's bounds rect still captures clicks -- so a click in a
  // corner cutout hits this contentView, which is fine (the cutouts are
  // at the corners of the workspace content, not over chrome affordances).
  contentView.setBorderRadius(CONTENT_CORNER_RADIUS);
  // Pin the contentView's between-load background to white so navigation
  // between workspace pages never reveals the chromeView's accent color
  // through the gap. (When the previous page tears down, Electron paints
  // the view's background color for a frame or two before the next page's
  // first paint lands; the default background is transparent, which would
  // briefly show the accent through.)
  contentView.setBackgroundColor('#ffffff');
  // Commit a real (blank) document immediately. A WebContentsView that has
  // NEVER navigated exposes a CDP page target with no committed document, and
  // Playwright's connect_over_cdp hangs forever initializing that target --
  // which deterministically broke the e2e harness's attach once keep-alive
  // residency stopped blanking this view on local-page navigations (before
  // that, the first local nav's about:blank load masked this). A pending
  // real navigation (workspace restore) simply supersedes this load.
  contentView.webContents.loadURL('about:blank').catch(() => {});
  win.contentView.addChildView(chromeView);
  win.contentView.addChildView(contentView);
  // The content view hosts agent content ONLY, and is shown only while the
  // current screen is an agent path (navigateBundle toggles it). It is created
  // hidden so a window that opens on a trusted local page (Home, Create, ...)
  // shows just the chrome view's own titlebar + page, with no empty agent card.
  contentView.setVisible(false);

  // Auto-open DevTools on both views when MINDS_OPEN_DEVTOOLS=1 is set.
  // The built-in cmd+opt+I shortcut crashes on BaseWindow + WebContentsViews
  // (Electron's menu handler assumes BrowserWindow), so this env var is
  // the dev-time escape hatch.
  if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
    chromeView.webContents.once('did-finish-load', () => {
      if (!chromeView.webContents.isDestroyed()) {
        chromeView.webContents.openDevTools({ mode: 'detach' });
      }
    });
    contentView.webContents.once('did-finish-load', () => {
      if (!contentView.webContents.isDestroyed()) {
        contentView.webContents.openDevTools({ mode: 'detach' });
      }
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
  // Reflow the child views on resize, and persist the new geometry (debounced)
  // so a non-graceful quit still restores the last-known bounds. Moves don't
  // affect layout, but they do change the saved x/y, so they persist too.
  win.on('resize', () => {
    updateBundleBounds(bundle);
    scheduleSessionSave();
  });
  win.on('move', () => scheduleSessionSave());

  // Run cleanup on `close` (before views are detached) rather than `closed`
  // so we can still reach the child webContents. BaseWindow does not guarantee
  // destruction of child WebContentsView render processes on its own; leaking
  // them across create/close cycles eventually starves new ones of resources.
  win.on('close', (event) => {
    // Closing the LAST window quits the app (the backend shuts down with it),
    // so route that close through the quit sequence -- which shows the
    // local-mind shutdown prompt BEFORE the window disappears. If the user
    // proceeds, the quit sequence sets isShuttingDown and re-closes everything
    // (this handler then falls through to teardown); if they cancel, the window
    // stays open. Non-last windows, and closes once a quit is already underway,
    // fall straight through to normal teardown.
    if (!isShuttingDown && !isQuitSequenceRunning && getBackendProcess() && isLastLiveWindow(bundle)) {
      event.preventDefault();
      runQuitSequence();
      return;
    }
    // Snapshot session state on every manual window close: by the time
    // `before-quit` fires on the `window-all-closed` path, every bundle has
    // already been removed from `bundles` by its `closed` handler, so saving
    // there would clobber the file with `[]`. Skip when we're tearing down as
    // part of a `cmd+Q` / crash quit -- `before-quit` already saved the full
    // set and we must not overwrite it with a progressively shrinking snapshot
    // as the teardown closes each window.
    if (!isShuttingDown) saveSessionState();
    if (bundle.inboxListReloadTimer) {
      clearTimeout(bundle.inboxListReloadTimer);
      bundle.inboxListReloadTimer = null;
    }
    const views = [bundle.chromeView, bundle.contentView, bundle.modalView];
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
  // showInactiveOnFirstShow lets callers (e.g. the startup multi-window restore
  // loop) surface the window without stealing focus from another bundle.
  const surface = () => {
    if (win.isDestroyed() || win.isVisible()) return;
    if (bundle.showInactiveOnFirstShow) win.showInactive();
    else win.show();
  };
  chromeView.webContents.once('did-finish-load', surface);
  win.once('ready-to-show', surface);
  setTimeout(surface, 3000);
}

function createBundle() {
  const win = new BaseWindow(buildBundleWindowOptions());
  const { chromeView, contentView } = createBundleWebContentsViews(win);

  const bundle = {
    window: win,
    chromeView,
    contentView,
    modalView: null,
    modalVisible: false,
    modalUrl: null,
    // The latest 'show-modal' overlay command, replayed on the overlay host's
    // did-finish-load if it was issued before the host page finished loading.
    pendingOverlayCommand: null,
    // True while a hover tooltip is showing (overlay view shrunk to the tooltip
    // rect). Gates updateBundleBounds so a resize doesn't clobber that rect with
    // the full-window modal bounds.
    tooltipVisible: false,
    inboxListReloadTimer: null,
    // Which surface the LATEST navigateBundle call targeted ('chrome' |
    // 'content'). The surfaces' did-navigate handlers only apply their state
    // bookkeeping when their surface is the intended one, so a slow load from
    // a SUPERSEDED navigation that commits late (e.g. a settings page landing
    // after the user already flipped back to the workspace) cannot clobber
    // the newer navigation's workspace identity / URL / accent, and a stale
    // workspace commit cannot re-show the content view over a local page.
    // Windows start on local pages (shell.html -> Home/welcome).
    intendedSurface: 'chrome',
    // Snapshot of contentWorkspaceReady taken when the workspace was parked
    // behind a local page, restored on reveal (see navigateBundle).
    parkedWorkspaceReady: false,
    // In-flight in-place swap dispatched to the chrome shell, with its
    // lost-swap fallback timer (see loadLocalIntoChrome / onChromeNavigate).
    pendingSwapUrl: null,
    pendingSwapTimer: null,
    // Pathname of the chrome target loadLocalIntoChrome is currently driving
    // the view toward; nulled when a navigation we did not drive starts.
    // Lets ensureBundleChromeWrapper skip re-issuing a wrapper load that is
    // already in flight (see there).
    currentChromeTargetPathname: null,
    // True once the chrome view's current document has registered its
    // swap-local-page listener (the renderer's 'shell-ready' handshake).
    // Cleared on every full chrome-view load start; required by
    // chromeViewHasShell so a swap is never dispatched into a document that
    // cannot hear it (which would burn the watchdog grace period).
    chromeShellReady: false,
    // Diagnostics + failsafe for the overlay modal (see openModal): when the
    // 'show-modal' command was issued, and the timer that force-closes a modal
    // whose iframe never reports loaded (the full-window overlay view eats
    // every click while invisible -- a stall must not freeze the app).
    modalOpenedAt: null,
    modalStallTimer: null,
    // True between a fresh openModal and the hosted page's overlay-modal-loaded:
    // the overlay view stays HIDDEN (not capturing input) until the modal has
    // actually painted (see openModal's deferred show).
    modalAwaitingLoad: false,
    currentContentUrl: null,
    currentWorkspaceId: null,
    // Whether the content view is currently displaying a REACHABLE workspace
    // rather than the "Loading workspace" proxy loader. The mngr_forward proxy
    // serves that loader (HTTP 503) at the workspace's own URL while the backend
    // is still unreachable, so ``currentWorkspaceId`` alone can't tell a loaded
    // workspace from one that's merely loading (or stopped). Tracked from the
    // content view's ``did-navigate`` HTTP status and forwarded to the chrome so
    // the get-help modal only offers "have an agent help" when the workspace can
    // actually host a chat. False until a non-loader navigation confirms it.
    contentWorkspaceReady: false,
    // ``currentAccentAgentId`` is the accent source of THIS window's current
    // screen -- the workspace id whose color tints the titlebar -- kept as a
    // tiny piece of per-window state only so the chrome renderer (a separate
    // view) can be (re-)primed with it. It tracks ``parseAccentSourceAgentId``
    // of the current content URL: the workspace id on a workspace-scoped screen
    // (the workspace itself plus its settings / sharing / destroying / recovery
    // screens), and null on every general screen (Home, Create, accounts, ...),
    // where the bar paints the neutral chrome. Distinct from
    // ``currentWorkspaceId``, which is narrower (only the workspace itself, for
    // uniqueness / recovery-redirect) -- so settings / sharing keep the accent
    // even though they don't count as "displaying" the workspace. NOT persisted:
    // a restored window re-derives it from its saved content URL.
    currentAccentAgentId: null,
    preErrorUrl: null,
    // Content-view renderer-crash recovery. ``isContentCrashed`` is true while
    // the local crash page (crashed.html) is showing in the content view;
    // ``crashedFromUrl`` is the workspace URL that was displayed when the
    // renderer died, re-loaded when the user clicks Reload on the crash page.
    isContentCrashed: false,
    crashedFromUrl: null,
    // Chrome (titlebar) view renderer-crash recovery. True while the compact
    // chrome-crashed.html strip is showing in the chrome view; cleared when the
    // user clicks Reload (reloadCrashedChromeView) and the chrome reloads /_chrome.
    isChromeCrashed: false,
    isErrorState: false,
    isLoadingState: true,
    isQuittingState: false,
    showInactiveOnFirstShow: false,
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
    primeViewWithCachedChromeState(bundle, chromeView.webContents);
  });

  // Every full MAIN-FRAME load replaces the document, so the previous
  // document's 'shell-ready' handshake no longer holds; the incoming page's
  // chrome.js re-sends it once its swap listener is registered. Subframe and
  // same-document activity must NOT clear it -- the persistent shell never
  // re-sends the handshake, so a spurious clear (e.g. an iframe inside a
  // swapped-in page loading) would permanently demote hub navigation to full
  // loads. (Positional args kept as a fallback for the structured event.)
  chromeView.webContents.on('did-start-navigation', (event, navUrl, isInPlace, isMainFrame) => {
    const mainFrame = event && typeof event.isMainFrame === 'boolean' ? event.isMainFrame : isMainFrame;
    const sameDocument = event && typeof event.isSameDocument === 'boolean' ? event.isSameDocument : isInPlace;
    if (mainFrame && !sameDocument) {
      bundle.chromeShellReady = false;
      // Keep the driven-target record honest: a main-frame navigation that
      // does not match what loadLocalIntoChrome last drove (a redirect, a
      // renderer-initiated nav) invalidates it.
      const startedUrl = event && typeof event.url === 'string' ? event.url : navUrl;
      try {
        if (new URL(startedUrl).pathname !== bundle.currentChromeTargetPathname) {
          bundle.currentChromeTargetPathname = null;
        }
      } catch {
        bundle.currentChromeTargetPathname = null;
      }
    }
  });

  // A trusted local page renders in the chrome view itself, so the chrome view's
  // OWN navigation is what drives the titlebar accent + the restored-URL
  // bookkeeping (mirroring onContentNavigate, which now handles only agent
  // content). The /_chrome agent-wrapper is skipped: on an agent path the accent
  // + current-workspace come from the content view's did-navigate, and a wrapper
  // nav must not clobber them (the accent race). A local page never displays a
  // workspace, so it clears currentWorkspaceId (releasing the recovery-redirect
  // lock) while still tinting for the wider workspace-scoped screens (its own
  // breadcrumb, via chrome.js, reads its location directly).
  const onChromeNavigate = (url, inPage) => {
    if (bundle.isErrorState) return;
    let parsed = null;
    try { parsed = new URL(url); } catch { return; }
    // Ignore the loading/quitting/error takeover pages (shell.html, a file://
    // URL): they are not a trusted local page and must not overwrite the
    // window's current URL / accent / current-workspace. Recording them would
    // clobber preErrorUrl -- e.g. the quitting screen loads while isErrorState
    // is false -- leaving nothing to restore the window to on a quit backout.
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return;
    console.log(`[nav] chrome view committed (${inPage ? 'in-place' : 'full load'}) ${url}`);
    // Confirm a dispatched in-place swap (its pushState lands here as
    // did-navigate-in-page) so the lost-swap fallback timer stands down.
    if (bundle.pendingSwapUrl) {
      try {
        const pending = new URL(bundle.pendingSwapUrl);
        if (parsed.pathname === pending.pathname && parsed.search === pending.search) {
          bundle.pendingSwapUrl = null;
          if (bundle.pendingSwapTimer) {
            clearTimeout(bundle.pendingSwapTimer);
            bundle.pendingSwapTimer = null;
          }
          // A confirmed swap is live proof the shell document and its swap
          // listener are healthy; re-arm readiness in case anything cleared it.
          bundle.chromeShellReady = true;
        }
      } catch { /* noop */ }
    }
    if (parsed.pathname === '/_chrome') return;
    // A commit from a SUPERSEDED navigation (the user flipped surfaces before
    // this page finished loading) must not clobber the newer navigation's
    // workspace identity / URL / accent bookkeeping.
    if (bundle.intendedSurface !== 'chrome') {
      console.log('[nav] stale chrome commit ignored (content surface is current)');
      return;
    }
    bundle.currentContentUrl = url;
    bundle.preErrorUrl = url;
    if (bundle.currentWorkspaceId !== null) {
      bundle.currentWorkspaceId = null;
      // No workspace is displayed on a local page; keep readiness in lockstep so
      // the chrome (which now receives it) never sees a stale "reachable".
      bundle.contentWorkspaceReady = false;
      sendCurrentWorkspaceToBundleViews(bundle);
    }
    updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(url));
    updateOsTitle(bundle);
  };
  chromeView.webContents.on('did-navigate', (_e, url) => onChromeNavigate(url, false));
  chromeView.webContents.on('did-navigate-in-page', (_e, url) => onChromeNavigate(url, true));

  // Defense in depth: the chrome view hosts ONLY trusted local pages served from
  // the backend origin. It must never navigate to untrusted agent content -- an
  // ``agent-<id>.localhost`` subdomain or the ``/goto/<id>/`` auth bridge -- which
  // belongs on the content view (caged relay preload, workspace-content session).
  // The trusted flow always hands agent URLs to navigateBundle via the
  // navigate-content bridge (sidebar rows, Landing rows, the create-complete
  // redirect), so a chrome-view attempt to reach one is a bug or a compromised
  // trusted page; block it. Mirrors the content view's will-navigate guard, which
  // blocks the opposite direction (trusted pages off the untrusted surface).
  chromeView.webContents.on('will-navigate', (event, url) => {
    if (selectSurfaceForUrl(url) === SURFACE_CONTENT) {
      event.preventDefault();
      console.warn('[chrome-guard] Blocked an agent-content navigation in the chrome view:', url);
    }
  });

  // Server redirects bypass will-navigate: a trusted local page loading in the
  // chrome view can 302 to agent content -- e.g. the recovery route redirects
  // to its return_to workspace URL the moment the health tracker reports
  // HEALTHY (a restored recovery window hits this immediately at startup).
  // Intercept the redirect and route the target through navigateBundle so the
  // workspace lands on the caged content surface. Following it here would load
  // untrusted agent content into the trusted chrome view -- and fail anyway,
  // since only the workspace-content session trusts the forward proxy's
  // self-signed loopback cert.
  chromeView.webContents.on('will-redirect', (event, url) => {
    if (selectSurfaceForUrl(url) === SURFACE_CONTENT) {
      event.preventDefault();
      console.log(`[nav] chrome-view redirect re-routed to content surface: ${url}`);
      navigateBundle(bundle, url);
    }
  });

  // When the chrome (titlebar) view's renderer dies -- it runs in a separate
  // process from the content view and can be reaped independently over a long
  // sleep -- Electron leaves a blank, dead titlebar with no recovery affordance.
  // Show a compact local crash strip with a Reload button instead. Only the top
  // ~38px of the chrome view is visible (the content view overlays the rest), so
  // chrome-crashed.html anchors its UI to that band. Never navigate synchronously
  // inside this handler (electron#19887); defer to a later tick. Mirrors the
  // content view's render-process-gone handling (wireContentViewEvents).
  chromeView.webContents.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason;
    const exitCode = details && typeof details.exitCode === 'number' ? details.exitCode : null;
    // A clean exit is intentional teardown (window close), not a crash.
    if (reason === 'clean-exit') return;
    if (isShuttingDown || bundle.window.isDestroyed()) return;
    // The full-app error takeover owns the chrome view (it loads shell.html there)
    // and drives its own retry; don't fight it with a titlebar crash strip.
    if (bundle.isErrorState) return;
    if (bundle.chromeView !== chromeView || chromeView.webContents.isDestroyed()) return;
    console.error(`[chrome-crash] chrome (titlebar) view renderer gone (reason=${reason}, exitCode=${exitCode})`);
    bundle.isChromeCrashed = true;
    setImmediate(() => {
      if (bundle.window.isDestroyed()) return;
      if (bundle.chromeView !== chromeView || chromeView.webContents.isDestroyed()) return;
      // A real reload (or another crash) since then superseded this.
      if (!bundle.isChromeCrashed) return;
      chromeView.webContents.loadFile(CHROME_CRASHED_PAGE_FILE).catch((err) => {
        console.error('[chrome-crash] failed to load crash page:', err && err.message);
      });
    });
  });

  wireContentViewEvents(bundle, contentView);
  registerShortcutsFor(bundle, chromeView.webContents);
  registerShortcutsFor(bundle, contentView.webContents);
  // The overlay view is created on top of chrome + content and loads the warm
  // host page (when the backend is up). For the initial window the backend is
  // not ready yet, so loadOverlayHost is a no-op here and the host is loaded
  // once the backend comes up (see the startup-ready transition).
  createBundleOverlayView(bundle);
  wireBundleShowLogic(bundle);

  return bundle;
}

function wireContentViewEvents(bundle, contentView) {
  // Forward content view nav events to the bundle's chrome view and update state.
  // Called from both createBundle and prepareAllWindowsForRetry (which rebuilds
  // the contentView that showErrorInAllWindows tore down).
  const onContentNavigate = (url, httpResponseCode) => {
    // Ignore the about:blank the content view is parked on while hidden (see
    // hideBundleContentView): it is not real content and must not overwrite the
    // window's current URL / accent / current-workspace.
    if (!url || url === 'about:blank') return;
    // Loading the local crash page is not a real content navigation: skip all
    // state updates so ``currentContentUrl`` / ``currentWorkspaceId`` keep
    // pointing at the pre-crash workspace (which Reload re-loads).
    if (isCrashPageUrl(url)) return;
    // A commit from a SUPERSEDED navigation must not re-show the content view
    // over a local page or clobber the newer navigation's bookkeeping (e.g. a
    // slow workspace load landing after the user already went Home).
    if (bundle.intendedSurface !== 'content') {
      console.log('[nav] stale content commit ignored (chrome surface is current)');
      return;
    }
    // A commit for a DIFFERENT workspace than this window's claim, landing
    // while the view is still HIDDEN (mid-load), is a stale cross-host commit:
    // the hopped-away-from workspace's in-flight navigation finishing after
    // the claim moved on. Ignore it -- running the bookkeeping below would
    // clobber the claim and reveal the wrong workspace under the new
    // titlebar. The target's own load (already issued) replaces this document
    // when it commits. A VISIBLE cross-host commit is deliberately left
    // alone: content history can legitimately traverse workspaces
    // (contentGoBack), and a visible commit is the user doing exactly that.
    const committedAgentId = parseWorkspaceId(url);
    if (
      committedAgentId
      && bundle.currentWorkspaceId
      && committedAgentId !== bundle.currentWorkspaceId
      && bundle.contentView
      && typeof bundle.contentView.getVisible === 'function'
      && !bundle.contentView.getVisible()
    ) {
      console.log(`[nav] stale cross-host content commit ignored (committed ${committedAgentId}, claim ${bundle.currentWorkspaceId})`);
      return;
    }
    // A genuine navigation clears any pending crash state (e.g. the user hit
    // Home from the crash page, or Reload succeeded).
    bundle.isContentCrashed = false;
    bundle.crashedFromUrl = null;
    console.log(`[nav] content view committed ${url}`);
    // A real navigation committed on the agent surface: make sure it is
    // visible. navigateBundle deliberately leaves the view hidden while a
    // workspace loads over the parked about:blank (the /_chrome wrapper is the
    // loading surface); this is the moment real content exists to reveal.
    showBundleContentView(bundle);
    if (!bundle.isErrorState) {
      bundle.currentContentUrl = url;
      bundle.preErrorUrl = url;
      // The persisted url is derived from these fields, so re-persist the
      // session (debounced) whenever a navigation changes them.
      scheduleSessionSave();
    }
    const newAgentId = parseWorkspaceId(url);
    // Recompute workspace reachability from the HTTP status. The mngr_forward
    // proxy serves its "Loading workspace" loader as HTTP 503 at the workspace's
    // own URL while the backend is unreachable, so a non-success status on a
    // workspace URL means "still loading / not reachable" (a loaded workspace
    // answers 200). Only a full navigation carries a status; ``did-navigate-in-page``
    // (anchors, pushState) passes undefined, and since it doesn't reload the
    // document we keep the prior readiness in that case.
    let newContentReady = bundle.contentWorkspaceReady;
    if (httpResponseCode !== undefined) {
      newContentReady = !!newAgentId && httpResponseCode < 400;
    }
    if (bundle.currentWorkspaceId !== newAgentId || bundle.contentWorkspaceReady !== newContentReady) {
      bundle.currentWorkspaceId = newAgentId;
      bundle.contentWorkspaceReady = newContentReady;
      sendCurrentWorkspaceToBundleViews(bundle);
    }
    // Tint the titlebar with the workspace's accent on any
    // workspace-scoped URL, not just the workspace itself: navigating
    // to ``/workspace/<id>/settings`` or ``/sharing/<id>/<svc>`` is
    // conceptually still "I'm working on workspace <id>", so the bar
    // should adopt that accent. Distinct from the narrower
    // ``parseWorkspaceId`` above which drives workspace uniqueness.
    //
    // A null result (any non-workspace minds screen -- Home, Create,
    // accounts, ...) clears the accent back to the neutral chrome. We
    // intentionally pass it through unconditionally rather than gating
    // on truthiness: the accent tracks the *current* screen, not the last
    // workspace opened in this window.
    updateOsTitle(bundle);
    // The content view hosts agent content only, so its navigations always own
    // the titlebar's breadcrumb + accent (there is no local page competing on
    // this surface). Push the workspace-scoped accent and the content URL to the
    // chrome view.
    updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(url));
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-url-changed', url);
    }
    // The workspace switcher (hosted in an overlay iframe) refreshes its
    // "Manage account(s)" / "Log in" label on every content URL change so
    // a sign-in / sign-out performed in the workspace iframe propagates
    // to the menu the next time the user opens it. Sent per-frame so it
    // reaches the iframe.
    sendToOverlayFrames(bundle, 'content-url-changed', url);
  };

  contentView.webContents.on('did-navigate', (_e, url, httpResponseCode) => onContentNavigate(url, httpResponseCode));
  contentView.webContents.on('did-navigate-in-page', (_e, url) => onContentNavigate(url));

  // A workspace load that fails outright (connection refused, bad TLS cert --
  // e.g. a stale port squatted by another checkout's forward) never fires
  // did-navigate: the hidden content view stays parked on about:blank and the
  // window would sit on the empty /_chrome wrapper forever, with the stale
  // ``currentWorkspaceId`` claim swallowing every retry. Route the window Home
  // instead, releasing the claim. errorCode -3 (ERR_ABORTED) is a superseded
  // load (another loadURL landed first), not a failure -- ignore it, along
  // with subframe failures. Note the reachable-but-down case is NOT this path:
  // mngr_forward serves its "Loading workspace" loader as a successful HTTP
  // response, which commits normally and shows the loader.
  contentView.webContents.on('did-fail-load', (_e, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) return;
    if (bundle.isErrorState || bundle.window.isDestroyed()) return;
    const failedAgentId = parseWorkspaceId(validatedURL);
    if (!failedAgentId || failedAgentId !== bundle.currentWorkspaceId) return;
    // Route to the workspace's recovery page rather than Home: it keeps the
    // user's context (they asked for THIS workspace), shows diagnostics, and
    // its health poll 302s straight back into the workspace -- through the
    // redirect guard, onto the content surface, at the canonical main-built
    // URL -- the moment it is reachable. So a failed link (wrong scheme /
    // stale port) self-heals into the workspace instead of dumping the user
    // on the workspace list. Home stays the fallback for the startup window
    // where no forward origin is known yet.
    const workspaceUrl = workspaceUrlForAgent(failedAgentId);
    const target = workspaceUrl
      ? toAbsoluteUrl(
        '/agents/' + encodeURIComponent(failedAgentId)
        + '/recovery?return_to=' + encodeURIComponent(workspaceUrl),
      )
      : (backendBaseUrl ? backendBaseUrl + '/' : null);
    console.warn(
      `[content] Workspace load failed (${errorCode} ${errorDescription}) for ${validatedURL}; routing to ${target}`,
    );
    if (target) navigateBundle(bundle, target);
  });

  // When the content view's renderer process dies (e.g. killed by the OS over a
  // long sleep, or an OOM), Electron leaves the view painting its pinned-white
  // background with no way to recover except manual Home-and-back navigation.
  // Show a local crash page instead, with a Reload button. We never navigate
  // synchronously inside this handler -- loadURL/loadFile here can crash the
  // whole app (electron#19887) -- so the navigation is deferred to a later tick.
  contentView.webContents.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason;
    const exitCode = details && typeof details.exitCode === 'number' ? details.exitCode : null;
    // A clean exit is an intentional teardown (window close, error-takeover
    // view destroy, navigation swap), not a crash -- ignore it.
    if (reason === 'clean-exit') return;
    if (isShuttingDown || bundle.window.isDestroyed()) return;
    // The full-app error takeover owns the screen and tears the content view
    // down itself; don't fight it with a content-only crash page.
    if (bundle.isErrorState) return;
    if (bundle.contentView !== contentView || contentView.webContents.isDestroyed()) return;
    console.error(
      `[content-crash] workspace content view renderer gone ` +
        `(reason=${reason}, exitCode=${exitCode}, url=${bundle.currentContentUrl || 'unknown'})`
    );
    bundle.isContentCrashed = true;
    bundle.crashedFromUrl = bundle.currentContentUrl;
    const crashedAgentId = bundle.currentWorkspaceId || '';
    setImmediate(() => {
      if (bundle.window.isDestroyed()) return;
      if (bundle.contentView !== contentView || contentView.webContents.isDestroyed()) return;
      // A real navigation (or another crash) since the crash superseded this.
      if (!bundle.isContentCrashed) return;
      contentView.webContents
        .loadFile(CRASHED_PAGE_FILE, {
          query: {
            reason: reason || 'unknown',
            exitCode: exitCode === null ? '' : String(exitCode),
            workspace: crashedAgentId,
          },
        })
        .catch((err) => {
          console.error('[content-crash] failed to load crash page:', err && err.message);
        });
    });
  });

  contentView.webContents.on('will-navigate', (event, url) => {
    const targetAgentId = parseWorkspaceId(url);
    if (targetAgentId) {
      // Agent URL: enforce workspace uniqueness at the Electron level so it
      // applies to EVERY in-page path that can drive the content view to a
      // workspace (landing-page row clicks, in-page anchors, pushState, ...),
      // not just sidebar-driven navigate-content IPC. Focus the existing window
      // rather than duplicating the workspace.
      const existing = findBundleForWorkspace(targetAgentId);
      if (existing && existing !== bundle) {
        event.preventDefault();
        focusBundle(existing);
      }
      return;
    }
    // Non-agent URL. Defense in depth: the content view hosts ONLY foreign agent
    // content (the ``agent-<id>.localhost`` subdomain / the ``/goto/`` auth
    // bridge). It must never navigate to a trusted minds page served from the
    // bare backend origin -- those render on the chrome surface with the full
    // preload bridge. Block any in-page attempt to load one here (e.g. a
    // compromised agent page trying to surface a trusted screen inside the
    // untrusted surface). External origins and the /goto plugin origin (a
    // different port) are left alone; they are handled elsewhere. about:blank
    // (the parked state) has no backend origin, so it is not blocked.
    if (isBackendOriginUrl(url)) {
      event.preventDefault();
      console.warn('[content-guard] Blocked a trusted backend-origin navigation in the content view:', url);
    }
  });

  // Symmetric redirect guard: will-navigate above only sees renderer-initiated
  // navigations, so a server redirect could still land a trusted backend-origin
  // page on the untrusted content surface. Route it to the chrome surface
  // instead. (The /goto auth bridge's own redirect targets the
  // agent-<id>.localhost subdomain, not the backend origin, so it passes
  // through untouched.)
  contentView.webContents.on('will-redirect', (event, url) => {
    if (isBackendOriginUrl(url)) {
      event.preventDefault();
      console.log(`[nav] content-view redirect re-routed to chrome surface: ${url}`);
      navigateBundle(bundle, url);
    }
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
    // Cmd+W (macOS) / Ctrl+W (Windows, Linux) closes the active dockview tab
    // INSIDE the displayed workspace, not the window (the app menu deliberately
    // carries no close-window accelerator; Cmd/Ctrl+Q quits). Intercept it
    // here -- before-input-event sees the keystroke even when focus is inside
    // one of the workspace's embedded service iframes (terminal / browser
    // panels), where a top-document keydown listener in the page would not --
    // and forward it into the page via the content relay's outbound channel;
    // the system interface closes its active panel on the resulting
    // ``minds:close-active-tab`` message. preventDefault so the page's own
    // fallback keydown binding can't double-fire. This eats Ctrl+W from
    // terminals (where it natively means delete-word) -- the same trade every
    // major browser makes, so it matches user expectations.
    const closeTabCombo = isMac
      ? input.meta && !input.shift && !input.alt && !input.control
      : input.control && !input.shift && !input.alt && !input.meta;
    if (
      closeTabCombo && key === 'w'
      && bundle.contentView && !bundle.contentView.webContents.isDestroyed()
      && wc === bundle.contentView.webContents
      && bundle.currentWorkspaceId
    ) {
      event.preventDefault();
      wc.send('close-active-tab');
      return;
    }
    // When the app menu is installed, it owns cmd+Q / cmd+N; handling them here
    // too would double-fire (e.g. two new windows per cmd+N).
    if (appMenuInstalled) return;
    // Ctrl/Cmd+W never closes the WINDOW on any platform: on a workspace it
    // closes the active tab (handled above); elsewhere it falls through to the
    // page. Close Window remains a menu item with no accelerator.
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

// -- Content / chrome surface navigation (per-bundle) --
//
// The content view hosts untrusted foreign AGENT content only; every trusted
// local/native page (Home, Create, Settings, and the workspace-scoped settings /
// sharing / destroying / recovery screens) renders on the chrome view, which
// draws the titlebar + the page body itself. ``navigateBundle`` routes each
// navigation to the right surface (``selectSurfaceForUrl``) and toggles the
// content view's visibility -- so no fourth WebContentsView is needed and the
// app's trust boundary matches its view boundary.

// Whether a URL is served from the trusted minds backend's bare origin
// (``http://localhost:<backendPort>``) -- i.e. a trusted local/native page, as
// opposed to an agent subdomain (``agent-<id>.localhost``), the ``/goto`` plugin
// origin (a different port), or an external site. Used by the content view's
// navigation guard to keep trusted pages off the untrusted content surface.
function isBackendOriginUrl(url) {
  if (!backendBaseUrl || !url) return false;
  try {
    return new URL(url).origin === new URL(backendBaseUrl).origin;
  } catch {
    return false;
  }
}

// Show the content view (the agent surface) and re-apply its inset bounds.
function showBundleContentView(bundle) {
  if (!bundle || !bundle.contentView || bundle.contentView.webContents.isDestroyed()) return;
  bundle.contentView.setVisible(true);
  updateBundleBounds(bundle);
}

// Whether the bundle's content view is ACTUALLY displaying ``agentId`` --
// visible with a committed navigation for that workspace -- as opposed to
// merely claiming it via the optimistically-stamped ``currentWorkspaceId``
// (which a failed or still-loading navigation leaves dangling). navigateBundle
// uses this so re-opening a workspace whose load failed retries instead of
// no-oping on the stale claim.
function isBundleDisplayingWorkspace(bundle, agentId) {
  if (!contentViewHoldsWorkspace(bundle, agentId)) return false;
  const view = bundle.contentView;
  return typeof view.getVisible !== 'function' || view.getVisible();
}

// Hide the content view when leaving agent content. The workspace page is
// deliberately kept RESIDENT (hidden, not unloaded): flipping between a
// workspace and its settings tab -- or Home -- and back must be instant, and
// unloading here forced a full /goto -> subdomain -> dockview re-boot on every
// return (seconds of blank card, lost in-page layout). The cost is one live
// workspace renderer behind a local page, the same footprint as displaying it.
function hideBundleContentView(bundle) {
  if (!bundle || !bundle.contentView || bundle.contentView.webContents.isDestroyed()) return;
  bundle.contentView.setVisible(false);
}

// Whether the bundle's content view currently HOLDS ``agentId``'s page
// (committed on its subdomain / auth bridge), visible or hidden. The hidden
// case is the parked-workspace state navigateBundle reveals without a reload.
function contentViewHoldsWorkspace(bundle, agentId) {
  if (!bundle || !agentId) return false;
  const view = bundle.contentView;
  if (!view || view.webContents.isDestroyed()) return false;
  return parseWorkspaceId(view.webContents.getURL()) === agentId;
}

// Whether the chrome view currently hosts a live backend-served shell document
// (able to receive swap-local-page IPC). Requires the renderer's 'shell-ready'
// handshake -- sent by chrome.js right after it registers the swap listener --
// so a swap is never dispatched into a document that cannot hear it. A full
// load in flight means no reliable listener, so callers fall back to a real
// navigation.
function chromeViewHasShell(bundle) {
  if (!bundle || !bundle.chromeView || bundle.chromeView.webContents.isDestroyed() || !backendBaseUrl) return false;
  if (!bundle.chromeShellReady) return false;
  const wc = bundle.chromeView.webContents;
  if (wc.isLoading()) return false;
  try {
    return new URL(wc.getURL()).origin === new URL(backendBaseUrl).origin;
  } catch {
    return false;
  }
}

// Drive the chrome view to a local URL: swap the page in place inside the
// persistent shell when possible (hub pages -- instant, titlebar untouched),
// else a full load. A dispatched swap is watched: if no commit for it arrives
// within the grace period (shell mid-teardown so the IPC had no listener, or
// the renderer died between check and dispatch), fall back to a full load so
// a navigation is never silently lost.
function loadLocalIntoChrome(bundle, absolute) {
  if (!bundle.chromeView || bundle.chromeView.webContents.isDestroyed()) return;
  const wc = bundle.chromeView.webContents;
  let pathname = null;
  try { pathname = new URL(absolute).pathname; } catch { pathname = null; }
  // The chrome target WE are driving the view toward (swap or full load).
  // ensureBundleChromeWrapper consults it to tell "a superseded page is still
  // loading" apart from "the wrapper itself is still loading" (see there).
  bundle.currentChromeTargetPathname = pathname;
  // Mirror the renderer's canSwapTo: the CURRENT page must also be a hub page
  // (an excluded page always leaves via a full navigation for its document
  // lifecycle), so we don't dispatch a swap the renderer will refuse and then
  // burn the watchdog grace period on its fallback.
  let currentPathname = null;
  try { currentPathname = new URL(wc.getURL()).pathname; } catch { currentPathname = null; }
  if (
    pathname && isSwappableLocalPath(pathname)
    && currentPathname && isSwappableLocalPath(currentPathname)
    && chromeViewHasShell(bundle)
  ) {
    wc.send('swap-local-page', absolute);
    bundle.pendingSwapUrl = absolute;
    // Short grace period covering IPC delivery only: the shell acks receipt
    // (swap-received) and the timer is re-armed with a generous bound there,
    // so a slow-but-alive swap fetch is never demoted to a full load.
    armSwapFallbackTimer(bundle, absolute, 1500);
  } else {
    // Load failures surface via onChromeNavigate / the error takeover; the
    // rejected promise alone would just print an unhandled-rejection warning.
    wc.loadURL(absolute).catch(() => {});
  }
}

// (Re-)arm the lost-swap fallback: if the dispatched swap for ``absolute`` has
// not been confirmed (onChromeNavigate clears pendingSwapUrl on its pushState
// commit) within ``delayMs``, full-load the target so a navigation is never
// silently lost. Armed at dispatch with a short IPC-delivery grace period and
// re-armed with a generous bound once the shell acks receipt (swap-received) --
// after the ack, the only losses left are the renderer dying mid-swap (this
// timer) or the fetch failing (the shell full-navigates itself).
function armSwapFallbackTimer(bundle, absolute, delayMs) {
  if (bundle.pendingSwapTimer) clearTimeout(bundle.pendingSwapTimer);
  bundle.pendingSwapTimer = setTimeout(() => {
    if (
      bundle.pendingSwapUrl === absolute
      && !bundle.window.isDestroyed()
      && bundle.chromeView
      && !bundle.chromeView.webContents.isDestroyed()
    ) {
      console.warn(`[nav] swap not confirmed in time; full-loading ${absolute}`);
      bundle.chromeView.webContents.loadURL(absolute).catch(() => {});
    }
  }, delayMs);
}

// The most recent SSE-carried accent color for ``agentId``, or null. Used to
// seed the /_chrome wrapper's titlebar color server-side so its first paint is
// already workspace-tinted (no neutral-then-accent pop-in while the wrapper's
// chrome.js waits for the SSE color cache).
function accentColorForAgent(agentId) {
  if (!agentId || !latestChromeState.workspaces) return null;
  for (const w of latestChromeState.workspaces) {
    if (w && w.id === agentId && typeof w.accent === 'string' && /^#[0-9a-f]{6}$/i.test(w.accent)) {
      return w.accent;
    }
  }
  return null;
}

// Ensure the chrome view is on the agent-wrapper (/_chrome) -- the titlebar with
// an empty content region that the content view floats over on an agent path.
// Only (re)load it when it isn't already there, so an agent navigation doesn't
// reload the wrapper (a spurious /_chrome chrome did-navigate would race the
// content view's accent -- see onChromeNavigate). The wrapper URL carries the
// current accent as a query param purely for the server-side first paint; the
// pathname-only comparison means accent changes never force a reload (chrome.js
// repaints those live).
function ensureBundleChromeWrapper(bundle) {
  if (!bundle || !bundle.chromeView || bundle.chromeView.webContents.isDestroyed() || !backendBaseUrl) return;
  let pathname = null;
  try { pathname = new URL(bundle.chromeView.webContents.getURL()).pathname; } catch { pathname = null; }
  // Reload when the committed page isn't the wrapper -- OR when a load is
  // still in flight for some OTHER page: a superseded local page (e.g. a
  // settings click the user flipped away from before it landed) could
  // otherwise finish loading underneath the workspace and leave the chrome
  // view off the wrapper; re-issuing the wrapper load cancels it. When the
  // in-flight load is ALREADY the wrapper (tracked via
  // ``currentChromeTargetPathname`` -- getURL()'s committed/pending reporting
  // is not reliable mid-flight), there is nothing to cancel: re-issuing would
  // just restart the identical load and double the handoff's paint (observed
  // as a second flash on the create -> workspace handoff, where a repeat
  // navigateBundle lands during wrapper load #1).
  const inFlightIsWrapper = bundle.currentChromeTargetPathname === '/_chrome';
  if (pathname !== '/_chrome' || (bundle.chromeView.webContents.isLoading() && !inFlightIsWrapper)) {
    const accent = accentColorForAgent(bundle.currentAccentAgentId);
    // Seed the server-rendered titlebar: the accent AND the workspace crumb
    // (name + tabs) for the workspace being displayed, so a fresh wrapper
    // load first-paints with the full context instead of a bare "Minds"
    // until the content commits. chrome.js owns every later update.
    const params = new URLSearchParams();
    if (accent) params.set('accent', accent);
    if (bundle.currentWorkspaceId) params.set('agent', bundle.currentWorkspaceId);
    const query = params.toString();
    const url = backendBaseUrl + '/_chrome' + (query ? '?' + query : '');
    // Swapped in place when the persistent shell is live (instant, titlebar
    // untouched); full load otherwise (startup, crash recovery).
    loadLocalIntoChrome(bundle, url);
  }
}

// Single choke point for driving what a window displays. Untrusted agent content
// (agent-<id>.localhost / /goto/<id>/) goes to the content view (shown, floating
// over the /_chrome wrapper); every trusted local page goes to the chrome view,
// which renders the titlebar + the page body itself (content view hidden). The
// caller passes an absolute url or a backend-relative path. Enforces
// one-window-per-workspace for agent URLs (focus the existing owner rather than
// duplicating). State (currentWorkspaceId for uniqueness / recovery-redirect, the
// accent source, and currentContentUrl/preErrorUrl for session restore) is
// stamped optimistically here and authoritatively re-applied by the destination
// surface's did-navigate handler (onContentNavigate / onChromeNavigate). Stamping
// currentWorkspaceId synchronously also lets a concurrent findBundleForWorkspace
// see this bundle occupying the workspace before its content view fires
// did-navigate (avoids a duplicate window). Closes any open overlay modal,
// matching the old navigate-content behavior.
function navigateBundle(bundle, url) {
  if (!bundle || bundle.window.isDestroyed() || !url) return;
  const absolute = toAbsoluteUrl(url);
  // One line per routed navigation: which surface the URL resolved to. Cheap
  // (navigations are user-driven) and the primary breadcrumb when debugging
  // "clicked X, nothing happened" reports -- silence here means the click
  // never reached main.
  console.log(`[nav] ${selectSurfaceForUrl(absolute)} <- ${absolute}`);
  if (selectSurfaceForUrl(absolute) === SURFACE_CONTENT) {
    const targetAgentId = parseWorkspaceId(absolute);
    const existing = findBundleForWorkspace(targetAgentId) || findBundleForWorkspaceScope(targetAgentId);
    if (existing && existing !== bundle) {
      // Another window owns this workspace's SCOPE (the workspace itself, or
      // its settings / sharing / recovery screens): focus that window and, if
      // it is not already displaying the workspace (e.g. it sits on the
      // settings screen), open the workspace THERE. Enforces
      // one-window-per-workspace-scope for EVERY caller (sidebar/landing
      // rows, notification + deep-link opens, ...).
      focusBundle(existing);
      closeModal(bundle);
      if (!isBundleDisplayingWorkspace(existing, targetAgentId)) {
        navigateBundle(existing, absolute);
      }
      return;
    }
    if (existing === bundle && isBundleDisplayingWorkspace(bundle, targetAgentId)) {
      // Already displaying it -- nothing to do. Note the claim alone is NOT
      // enough: ``currentWorkspaceId`` is stamped optimistically below before
      // the load commits, so a failed/stalled load leaves the claim without the
      // content. Re-checking the actual displayed state here means a re-click
      // retries the load instead of being silently swallowed.
      closeModal(bundle);
      return;
    }
    bundle.intendedSurface = 'content';
    if (contentViewHoldsWorkspace(bundle, targetAgentId)) {
      // The parked resident workspace: the content view still holds this
      // workspace's live page from before the user flipped to a local screen
      // (settings, Home). Reveal it -- no reload, no /goto round-trip, the
      // in-page layout intact -- and re-claim the workspace identity.
      console.log(`[nav] revealing parked workspace ${targetAgentId}`);
      bundle.currentWorkspaceId = targetAgentId;
      bundle.contentWorkspaceReady = bundle.parkedWorkspaceReady;
      bundle.currentContentUrl = bundle.contentView.webContents.getURL();
      bundle.preErrorUrl = bundle.currentContentUrl;
      // The persisted url derives from these fields: re-persist (debounced) at
      // claim time so a quit during the load restores the intended workspace.
      scheduleSessionSave();
      updateOsTitle(bundle);
      sendCurrentWorkspaceToBundleViews(bundle);
      updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(bundle.currentContentUrl));
      // Stamp the titlebar breadcrumb NOW rather than on commit: for
      // navigations initiated outside the chrome renderer (sidebar rows,
      // landing rows) the shell otherwise shows the previous context for the
      // whole load. Idempotent with the commit-time push in onContentNavigate.
      if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
        bundle.chromeView.webContents.send('content-url-changed', bundle.currentContentUrl);
      }
      ensureBundleChromeWrapper(bundle);
      // getURL() also reports a PENDING navigation (this path deliberately
      // doubles as the dedupe for a repeated open of a workspace whose first
      // load is still in flight -- e.g. the recovery page's two poll loops both
      // firing the healthy return). Only show immediately when the held page is
      // actually committed; an in-flight load is revealed by onContentNavigate
      // when it commits, keeping the loading surface (the wrapper) visible
      // instead of a blank card.
      if (!bundle.contentView.webContents.isLoading()) {
        showBundleContentView(bundle);
      }
      closeModal(bundle);
      return;
    }
    bundle.currentWorkspaceId = targetAgentId;
    // We're only starting the navigation; the workspace isn't confirmed
    // reachable until ``did-navigate`` lands a non-loader status, so clear
    // readiness now to avoid a stale "reachable" carrying over from the
    // previously-displayed workspace during the load.
    // ``sendCurrentWorkspaceToBundleViews`` below forwards the readiness to the
    // chrome, so it must be cleared first.
    bundle.contentWorkspaceReady = false;
    bundle.currentContentUrl = absolute;
    bundle.preErrorUrl = absolute;
    // Claim-time (debounced) session persist, so a second open arriving during
    // the load window restores correctly and a quit mid-load lands back here.
    scheduleSessionSave();
    updateOsTitle(bundle);
    sendCurrentWorkspaceToBundleViews(bundle);
    updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(absolute));
    // Optimistic breadcrumb stamp at claim time (see the reveal path above).
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-url-changed', absolute);
    }
    ensureBundleChromeWrapper(bundle);
    // Deliberately do NOT show the content view yet: it may be parked on
    // about:blank (hidden after a local page), and showing it now would cover
    // the window with a blank card for the whole load -- or forever, if the
    // load fails. ``onContentNavigate`` shows it when a real navigation
    // commits; until then the /_chrome wrapper (titlebar + white content
    // mirror, accent-tinted) stays visible as the loading surface. Failures
    // are handled by the did-fail-load fallback, so the rejection is
    // swallowed.
    //
    // Clean a mismatched resident first: if the view still holds a DIFFERENT
    // workspace's page (a parked resident, or the workspace being hopped away
    // from), hide it so the wrapper -- already tinted and crumbed for the
    // TARGET -- is the loading surface. Without this a workspace->workspace
    // hop kept the OLD workspace on screen under the NEW workspace's titlebar
    // until the target committed: out of sync for the whole load, and
    // indefinitely if the load stalled. (A same-workspace reload stays
    // visible through the load, matching pre-swap behavior; the stale
    // document itself is replaced when the target's load commits.)
    if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
      const heldAgentId = parseWorkspaceId(bundle.contentView.webContents.getURL());
      if (heldAgentId && heldAgentId !== targetAgentId) {
        console.log(`[nav] hiding mismatched content view (held ${heldAgentId}, loading ${targetAgentId})`);
        hideBundleContentView(bundle);
      }
      bundle.contentView.webContents.loadURL(absolute).catch(() => {});
    }
    // Keep-alive residency means another window may still HOLD this workspace
    // hidden (parked, claim released when it flipped to a local page). Now that
    // THIS window is loading the live copy, blank those stale residents so two
    // live views of one workspace never coexist.
    for (const b of bundles) {
      if (b === bundle || b.window.isDestroyed()) continue;
      if (b.currentWorkspaceId !== targetAgentId && contentViewHoldsWorkspace(b, targetAgentId)) {
        try {
          b.contentView.webContents.loadURL('about:blank').catch(() => {});
        } catch { /* noop */ }
      }
    }
    closeModal(bundle);
    return;
  }
  // A workspace-scoped local page (settings / sharing / destroying /
  // recovery) belongs to the window that owns that workspace's scope: if
  // another window is displaying the workspace (or already sits on one of its
  // scoped screens), route this navigation there instead of splitting the
  // scope across two windows (settings here, workspace there).
  const localScopeAgentId = parseAccentSourceAgentId(absolute);
  if (localScopeAgentId) {
    const owner = findBundleForWorkspace(localScopeAgentId) || findBundleForWorkspaceScope(localScopeAgentId);
    if (owner && owner !== bundle) {
      focusBundle(owner);
      closeModal(bundle);
      navigateBundle(owner, absolute);
      return;
    }
  }
  // Trusted local page: the chrome view IS the content. A local page never
  // "displays" a workspace, so clear currentWorkspaceId (releasing the
  // recovery-redirect lock); the accent still tracks the wider workspace-scoped
  // screens via parseAccentSourceAgentId. The content view is hidden here, so no
  // reachable workspace is displayed -- clear readiness too (kept in lockstep
  // with currentWorkspaceId, which it can only be true with), remembering it so
  // revealing the parked workspace can restore it without a fresh probe.
  bundle.intendedSurface = 'chrome';
  bundle.currentContentUrl = absolute;
  bundle.preErrorUrl = absolute;
  // The persisted url derives from currentContentUrl; keep it fresh on local
  // navigations too (debounced).
  scheduleSessionSave();
  if (bundle.currentWorkspaceId !== null) {
    bundle.parkedWorkspaceReady = bundle.contentWorkspaceReady;
    bundle.currentWorkspaceId = null;
    bundle.contentWorkspaceReady = false;
    sendCurrentWorkspaceToBundleViews(bundle);
  }
  updateOsTitle(bundle);
  updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(absolute));
  hideBundleContentView(bundle);
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    // Hub pages swap in place inside the persistent shell (instant, titlebar
    // untouched); everything else full-loads. A local page that
    // server-redirects to agent content is re-routed by the chrome view's
    // will-redirect guard.
    loadLocalIntoChrome(bundle, absolute);
  }
  closeModal(bundle);
}

// -- Sidebar helpers (per-bundle) --
//
// The sidebar is just the workspace menu hosted on the shared warm overlay
// surface (``modalView``), loaded with /_chrome/sidebar -- it shares the same
// transparent background + Escape handling + ``modal-state-changed``
// titlebar-drag suppression as the inbox. There is no separate sidebar
// WebContentsView.

function sidebarUrlFor(anchor) {
  if (!backendBaseUrl) return null;
  const base = backendBaseUrl + '/_chrome/sidebar';
  // ``anchor`` is { trigger: {x, y, width, height}, offset: {x, y} } -- the
  // trigger button's viewport-relative rect plus a caller-chosen offset.
  // The chrome view (where the trigger lives) and the modal view share
  // window coordinate space, so the rect translates directly into the
  // sidebar page's coordinate system; Sidebar.jinja anchors the menu
  // at trigger.bottom-left + offset via server-rendered inline style.
  // When no anchor is provided, the server falls back to defaults.
  if (!anchor || !anchor.trigger || !anchor.offset) return base;
  const params = new URLSearchParams();
  params.set('trigger_x', Math.round(anchor.trigger.x).toString());
  params.set('trigger_y', Math.round(anchor.trigger.y).toString());
  params.set('trigger_w', Math.round(anchor.trigger.width).toString());
  params.set('trigger_h', Math.round(anchor.trigger.height).toString());
  params.set('offset_x', Math.round(anchor.offset.x).toString());
  params.set('offset_y', Math.round(anchor.offset.y).toString());
  return base + '?' + params.toString();
}

function isSidebarModalOpen(bundle) {
  if (!bundle || !bundle.modalVisible || !bundle.modalUrl) return false;
  try {
    return new URL(bundle.modalUrl).pathname === '/_chrome/sidebar';
  } catch {
    return false;
  }
}

function openSidebar(bundle, anchor) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = sidebarUrlFor(anchor);
  if (!url) return;
  openModal(bundle, url);
}

function toggleSidebar(bundle, anchor) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (isSidebarModalOpen(bundle)) closeModal(bundle);
  else openSidebar(bundle, anchor);
}

// -- Overlay surface (per-bundle) --
//
// The overlay surface is a single always-warm WebContentsView (``modalView``)
// that main loads ONCE with /_chrome/overlay at window creation and keeps
// mounted for the window's life. Every overlay -- the workspace menu, inbox,
// help, and sign-in modals -- is hosted there as a mount-on-demand iframe
// (created when opened, destroyed when closed) driven over IPC, so the surface
// stays warm without loading a fresh page into this view on every open. The
// hosted pages are first-party and same-origin, so ``nodeIntegrationInSubFrames`` runs
// the preload in each iframe and exposes the same ``window.minds`` bridge their
// existing code already calls; navigation is locked to the backend origin (see
// createBundleOverlayView) so no foreign page can ever inherit that bridge.
//
// Electron 40 has no per-view click-through, so while a modal is open the view
// is shown full-window and captures pointer events exactly as the old modal
// overlay did. The view is hidden (and so captures nothing) whenever no overlay
// is open. The dynamic-bounds path that tooltips use lands separately.

function overlayIdForUrl(url) {
  let pathname;
  try {
    pathname = new URL(url).pathname;
  } catch {
    return null;
  }
  if (pathname === '/_chrome/sidebar') return 'sidebar';
  if (pathname === '/inbox') return 'inbox';
  if (pathname === '/help') return 'help';
  if (pathname === '/auth/signin-modal') return 'signin';
  if (pathname === '/settings/modal') return 'settings';
  if (pathname === '/settings/ai-keys') return 'ai-keys';
  if (pathname === '/accounts/modal') return 'accounts';
  if (/^\/sharing\/agent-[a-f0-9]+\/[^/]+\/modal$/i.test(pathname)) return 'sharing';
  return null;
}

// Push an IPC payload to EVERY frame of the overlay view (the host page plus
// each hosted modal iframe). ``webContents.send`` reaches only the top frame, so
// per-frame ``frame.send`` is required to deliver chrome-events / current-
// workspace / priming into the iframes where the sidebar and inbox actually run.
function sendToOverlayFrames(bundle, channel, payload) {
  const view = bundle && bundle.modalView;
  if (!view || view.webContents.isDestroyed()) return;
  let frames;
  try {
    frames = view.webContents.mainFrame.framesInSubtree;
  } catch {
    return;
  }
  for (const frame of frames) {
    try {
      frame.send(channel, payload);
    } catch { /* noop */ }
  }
}

function loadOverlayHost(bundle) {
  if (!bundle || !bundle.modalView || !backendBaseUrl) return;
  if (bundle.modalView.webContents.isDestroyed()) return;
  bundle.modalView.webContents.loadURL(backendBaseUrl + '/_chrome/overlay').catch(() => {});
}

// Tell the overlay host which overlay to show/hide. The command is sent
// immediately AND stashed so the host's did-finish-load can replay the latest
// one: Electron drops IPC with no listener, so a command issued before the host
// page finished loading would otherwise be lost (the same replay-on-load pattern
// primeViewWithCachedChromeState uses for the chrome view).
function sendOverlayCommand(bundle, cmd) {
  if (!bundle || !bundle.modalView || bundle.modalView.webContents.isDestroyed()) return;
  bundle.pendingOverlayCommand = cmd.type === 'hide-all' ? null : cmd;
  try {
    bundle.modalView.webContents.send('overlay-command', cmd);
  } catch { /* noop */ }
}

// Replay the cached chrome state into the overlay's iframes. The hosted sidebar
// renders its workspace list from the SSE-driven ``workspaces`` event and the
// inbox keys off ``requests``; an iframe that just (re)loaded must be handed the
// current state immediately rather than waiting for the next SSE push. Mirrors
// primeViewWithCachedChromeState, but fans out per-frame (see sendToOverlayFrames).
function primeOverlayFrames(bundle) {
  if (latestChromeState.workspaces !== null) {
    sendToOverlayFrames(bundle, 'chrome-event', { type: 'workspaces', workspaces: latestChromeState.workspaces });
  }
  if (latestChromeState.authStatus) {
    sendToOverlayFrames(bundle, 'chrome-event', latestChromeState.authStatus);
  }
  sendToOverlayFrames(bundle, 'chrome-event', {
    type: 'requests',
    count: latestChromeState.requestCount,
    request_ids: latestChromeState.requestIds,
  });
  for (const [agentId, status] of systemInterfaceStatusByAgent) {
    if (!status || status === 'healthy') continue;
    sendToOverlayFrames(bundle, 'chrome-event', { type: 'system_interface_status', agent_id: agentId, status });
  }
  sendToOverlayFrames(bundle, 'current-workspace-changed', bundle.currentWorkspaceId);
  // The workspace switcher highlights the workspace whose SCOPE is active --
  // including its settings / sharing screens, where ``currentWorkspaceId`` is
  // null -- so it keys the current row off the accent-source agent id.
  sendToOverlayFrames(bundle, 'accent-changed', bundle.currentAccentAgentId || null);
}

// Create the per-bundle overlay view and load the warm host page. Called once
// from createBundle; the view persists (hidden) until an overlay is opened.
function createBundleOverlayView(bundle) {
  const modal = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // The hosted modals run as same-origin iframes inside the host page; this
      // runs the preload in each so their existing window.minds.*() calls and
      // onChromeEvent subscriptions work unchanged. Safe because the only frames
      // here are first-party pages from our own backend, and the navigation
      // guards below keep it that way.
      nodeIntegrationInSubFrames: true,
    },
  });
  // Transparent so each hosted overlay's own dim backdrop reveals the workspace
  // behind it instead of an opaque rectangle.
  modal.setBackgroundColor('#00000000');
  // Commit a real (blank) document immediately: the warm host page cannot load
  // until the backend is up (loadOverlayHost no-ops without backendBaseUrl),
  // and a never-navigated WebContents exposes a CDP page target that hangs
  // Playwright's connect_over_cdp initialization forever -- the e2e harness
  // attaches during startup, before the backend is ready. loadOverlayHost's
  // later real load simply supersedes this. (Same fix as the content view.)
  modal.webContents.loadURL('about:blank').catch(() => {});
  // Hidden until an overlay is opened: a full-window view would otherwise
  // capture every pointer event over the window (Electron has no per-view
  // click-through), so it must not be visible while idle.
  modal.setVisible(false);
  bundle.modalView = modal;
  bundle.window.contentView.addChildView(modal);
  registerShortcutsFor(bundle, modal.webContents);
  // Escape closes the open overlay even if a hosted page's own key handling
  // fails -- the same main-process backstop the modal overlay had before.
  modal.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'Escape') {
      event.preventDefault();
      closeModal(bundle);
    }
  });
  // Lock the overlay view to the backend origin. ``nodeIntegrationInSubFrames``
  // hands the window.minds bridge to every frame here, so a foreign page must
  // never be allowed to load in this view or any of its iframes.
  const isAllowedOverlayUrl = (url) =>
    url === 'about:blank' || (backendBaseUrl && url.startsWith(backendBaseUrl));
  modal.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedOverlayUrl(url)) event.preventDefault();
  });
  modal.webContents.on('will-frame-navigate', (event) => {
    if (!isAllowedOverlayUrl(event.url)) event.preventDefault();
  });
  modal.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  // Replay the latest show command once the host page is ready, in case an
  // overlay was opened before the host finished loading (Electron drops
  // listener-less IPC). Hosted iframes are primed when they signal
  // overlay-modal-loaded, not here.
  modal.webContents.on('did-finish-load', () => {
    if (modal.webContents.isDestroyed()) return;
    if (modal.webContents.getURL() === 'about:blank') return;
    if (bundle.pendingOverlayCommand) {
      try {
        modal.webContents.send('overlay-command', bundle.pendingOverlayCommand);
      } catch { /* noop */ }
    }
  });
  // When the overlay host's renderer dies (it runs in a separate process and can
  // be reaped over a long sleep like the other views), there is nothing visible to
  // recover -- the overlay is hidden whenever no modal is open. So we just reset any
  // open-modal state and silently reload the warm host, so the next sidebar / inbox /
  // help open lands on a fresh page instead of a dead one. Defer the reload a tick
  // (never navigate synchronously in this handler -- electron#19887).
  modal.webContents.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason;
    if (reason === 'clean-exit') return;
    if (isShuttingDown || bundle.window.isDestroyed()) return;
    if (bundle.modalView !== modal || modal.webContents.isDestroyed()) return;
    const exitCode = details && typeof details.exitCode === 'number' ? details.exitCode : null;
    console.error(`[overlay-crash] overlay host view renderer gone (reason=${reason}, exitCode=${exitCode})`);
    if (bundle.modalVisible) closeModal(bundle);
    setImmediate(() => {
      if (bundle.window.isDestroyed()) return;
      if (bundle.modalView !== modal || modal.webContents.isDestroyed()) return;
      loadOverlayHost(bundle);
    });
  });
  // Auto-open DevTools for dev-time inspection, matching the content view; gated
  // on the same env var so a single switch covers all surfaces.
  if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
    modal.webContents.once('did-finish-load', () => {
      if (!modal.webContents.isDestroyed()) {
        modal.webContents.openDevTools({ mode: 'detach' });
      }
    });
  }
  loadOverlayHost(bundle);
  // Give the view its full-window bounds now (it stays hidden) so the overlay
  // host has a real viewport to measure tooltips against before the first show.
  updateBundleBounds(bundle);
}

function openModal(bundle, url) {
  if (!bundle || bundle.window.isDestroyed() || !url || !bundle.modalView) return;
  const id = overlayIdForUrl(url);
  if (!id) return;
  if (bundle.modalView.webContents.isDestroyed()) return;
  // Raise the warm overlay view to the top of z-order.
  bundle.window.contentView.removeChildView(bundle.modalView);
  bundle.window.contentView.addChildView(bundle.modalView);
  const freshOpen = !bundle.modalVisible;
  bundle.modalVisible = true;
  bundle.modalUrl = url;
  if (freshOpen) {
    // Do NOT show the view yet: shown, it is a full-window sheet that captures
    // every pointer event (Electron has no per-view click-through) while
    // rendering nothing until the hosted iframe paints -- a slow modal load
    // (observed at ~2s for the workspace menu) froze the entire app. The view
    // becomes visible when the hosted page reports overlay-modal-loaded; until
    // then the window underneath stays fully interactive. The titlebar
    // drag-region drop below is deferred with it.
    bundle.modalAwaitingLoad = true;
  } else {
    // Modal-to-modal switch: the view is already visible and the overlay
    // host's front/back iframe buffer swaps flash-free; keep it shown.
    bundle.modalView.setVisible(true);
  }
  // Notify the chrome view that the modal is open so it can drop the
  // ``-webkit-app-region: drag`` on #minds-titlebar. macOS unions drag
  // regions across all visible views in a window, so the chrome
  // titlebar's drag rule otherwise wins over the modal's no-drag in
  // the y=0..TITLEBAR strip and intercepts clicks (e.g. the inbox X).
  // Deferred to overlay-modal-loaded on a fresh open (see above): while the
  // sheet is hidden the titlebar must keep its normal drag behavior.
  if (!freshOpen && bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    try {
      bundle.chromeView.webContents.send('modal-state-changed', { open: true });
    } catch { /* noop */ }
  }
  sendOverlayCommand(bundle, { type: 'show-modal', id, url });
  updateBundleBounds(bundle);
  // Diagnostics + stall failsafe. From this moment the overlay view is
  // full-window and captures every pointer event while showing NOTHING until
  // the hosted iframe paints -- if that load stalls, the app reads as frozen.
  // overlay-modal-loaded cancels the timer; if it never arrives, force-close
  // so input is never eaten indefinitely.
  console.log(`[modal] open ${id} (${url})`);
  bundle.modalOpenedAt = Date.now();
  if (bundle.modalStallTimer) clearTimeout(bundle.modalStallTimer);
  bundle.modalStallTimer = setTimeout(() => {
    bundle.modalStallTimer = null;
    if (bundle.window.isDestroyed() || !bundle.modalVisible || bundle.modalUrl !== url) return;
    console.warn(`[modal] STALL: ${id} never reported loaded after 10s; closing overlay to unblock input`);
    closeModal(bundle);
  }, 10000);
}

function closeModal(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.modalView || !bundle.modalVisible) return;
  console.log('[modal] close');
  if (bundle.modalStallTimer) {
    clearTimeout(bundle.modalStallTimer);
    bundle.modalStallTimer = null;
  }
  bundle.modalOpenedAt = null;
  bundle.modalAwaitingLoad = false;
  bundle.modalView.setVisible(false);
  bundle.modalVisible = false;
  bundle.modalUrl = null;
  // Restore the chrome titlebar's drag region.
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    try {
      bundle.chromeView.webContents.send('modal-state-changed', { open: false });
    } catch { /* noop */ }
  }
  // Tell the warm overlay host to hide its overlays. The host page itself stays
  // loaded (warm) so the next open is instant; only the hosted iframes hide.
  sendOverlayCommand(bundle, { type: 'hide-all' });
  if (bundle.inboxListReloadTimer) {
    clearTimeout(bundle.inboxListReloadTimer);
    bundle.inboxListReloadTimer = null;
  }
}

function inboxUrlFor(query) {
  if (!backendBaseUrl) return null;
  return backendBaseUrl + '/inbox' + (query || '');
}

// Local path a successful sign-in should land on. Must start with a single
// '/' (never '//') and stay within a conservative charset; the server
// re-validates with safe_local_redirect_path. Anything else falls back to the
// server default (the create screen).
const SIGNIN_RETURN_TO_PATTERN = /^\/(?!\/)[A-Za-z0-9\-._~/?=&%]*$/;

function signinModalUrlFor(returnTo, mode) {
  if (!backendBaseUrl) return null;
  const params = new URLSearchParams();
  if (typeof returnTo === 'string' && returnTo && SIGNIN_RETURN_TO_PATTERN.test(returnTo)) {
    params.set('return_to', returnTo);
  }
  // Only the literal 'signin' switches the leading tab (for "Log In"
  // callers); the server keeps the sign-up default otherwise.
  if (mode === 'signin') params.set('mode', 'signin');
  const query = params.toString();
  return backendBaseUrl + '/auth/signin-modal' + (query ? '?' + query : '');
}

function openSigninModal(bundle, returnTo, mode) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = signinModalUrlFor(returnTo, mode);
  if (!url) return;
  openModal(bundle, url);
}

function openMindsSettingsModal(bundle) {
  if (!bundle || bundle.window.isDestroyed() || !backendBaseUrl) return;
  openModal(bundle, backendBaseUrl + '/settings/modal');
}

function openAccountsModal(bundle) {
  if (!bundle || bundle.window.isDestroyed() || !backendBaseUrl) return;
  openModal(bundle, backendBaseUrl + '/accounts/modal');
}

// Open the sharing editor as a centered modal in the shared overlay. Both ids
// are validated to conservative server-issued shapes (mirroring the content
// relay's allowlist; never trust the renderer) before being packed into the
// /sharing/<agent>/<service>/modal URL.
function openSharingModal(bundle, agentId, serviceName) {
  if (!bundle || bundle.window.isDestroyed() || !backendBaseUrl) return;
  if (typeof agentId !== 'string' || !/^agent-[a-f0-9]{1,64}$/i.test(agentId)) return;
  if (typeof serviceName !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(serviceName)) return;
  openModal(bundle, backendBaseUrl + '/sharing/' + agentId + '/' + serviceName + '/modal');
}

function isInboxModalOpen(bundle) {
  if (!bundle || !bundle.modalVisible) return false;
  if (!bundle.modalUrl) return false;
  // Compare path only -- the query (?selected=) may differ between auto-open
  // and selection-driven loads.
  try {
    const u = new URL(bundle.modalUrl);
    return u.pathname === '/inbox';
  } catch {
    return false;
  }
}

function openInbox(bundle, query) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = inboxUrlFor(query || '');
  if (!url) return;
  openModal(bundle, url);
}

function toggleInbox(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (isInboxModalOpen(bundle)) closeModal(bundle);
  // ``keep_open=1`` marks this as an intentional open of the whole inbox
  // (the Requests button), so resolving a request advances to the next
  // pending one rather than dismissing the window. Notification-click and
  // auto-open paths omit it, so they close after Approve/Deny.
  else openInbox(bundle, '?keep_open=1');
}

// -- Get-help modal (per-bundle) --
//
// The help modal shares the same modalView overlay as the inbox and sidebar (see
// openModal): it just loads the backend's /help page. ``agentId`` (the
// currently-displayed workspace, or falsy on a general screen) is forwarded as a
// ?workspace= query so the help page can scope its bug report to that workspace.

function helpUrlFor(agentId, description, assistAvailable) {
  if (!backendBaseUrl) return null;
  const params = new URLSearchParams();
  if (agentId) params.set('workspace', agentId);
  // ``assist=1`` enables the "have an agent help" option; the titlebar sets it only when the
  // displayed workspace is healthy (chrome.js), so it stays off for the recovery / agent-escalation
  // open-help paths that don't pass it. The workspace id is still sent for report scoping.
  if (assistAvailable) params.set('assist', '1');
  // A description is only ever passed by the open_help (agent-escalation) flow; the
  // titlebar button opens /help with none. So a present description marks this as an
  // agent-submitted report, which the /help page frames differently (agent wording,
  // no mode choice).
  if (description) {
    params.set('description', description);
    params.set('agent_report', '1');
  }
  const query = params.toString();
  return backendBaseUrl + '/help' + (query ? '?' + query : '');
}

function isHelpModalOpen(bundle) {
  if (!bundle || !bundle.modalVisible || !bundle.modalUrl) return false;
  // Compare path only; the ?workspace= query may differ between screens.
  try {
    return new URL(bundle.modalUrl).pathname === '/help';
  } catch {
    return false;
  }
}

function openHelp(bundle, agentId, description, assistAvailable) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = helpUrlFor(agentId, description, assistAvailable);
  if (!url) return;
  openModal(bundle, url);
}

function toggleHelp(bundle, agentId, assistAvailable) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (isHelpModalOpen(bundle)) closeModal(bundle);
  else openHelp(bundle, agentId, undefined, assistAvailable);
}

// Coalesce rapid SSE-triggered chrome-event posts so the inbox shell
// doesn't queue several /inbox/list fetches when a burst of requests
// events arrives in quick succession.
function scheduleInboxListRefresh(bundle, evt) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!isInboxModalOpen(bundle)) return;
  if (bundle.inboxListReloadTimer) {
    clearTimeout(bundle.inboxListReloadTimer);
  }
  bundle.inboxListReloadTimer = setTimeout(() => {
    bundle.inboxListReloadTimer = null;
    if (!bundle || bundle.window.isDestroyed()) return;
    if (!bundle.modalView || !bundle.modalVisible) return;
    if (bundle.modalView.webContents.isDestroyed()) return;
    // Per-frame so the event reaches the inbox iframe, not just the host frame.
    sendToOverlayFrames(bundle, 'chrome-event', evt);
  }, INBOX_LIST_REFRESH_DEBOUNCE_MS);
}

function sendCurrentWorkspaceToBundleViews(bundle) {
  if (!bundle) return;
  // The titlebar (chrome view) and any open modal (sidebar, inbox, ...) both
  // key UI off the current workspace -- the titlebar's recovery-page
  // auto-redirect lock, and the sidebar modal's selected-row highlight + bonus
  // icons. The titlebar ACCENT is NOT driven from here: it rides its own
  // ``accent-changed`` channel (``updateBundleAccentAgentId``), set by the
  // navigation handlers off ``parseAccentSourceAgentId(url)`` and re-primed
  // when the chrome view (re)loads.
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.webContents.send('current-workspace-changed', bundle.currentWorkspaceId, bundle.contentWorkspaceReady);
  }
  // The sidebar/inbox run in overlay iframes, so fan out per-frame.
  sendToOverlayFrames(bundle, 'current-workspace-changed', bundle.currentWorkspaceId);
}

// -- Window opening / focusing --

// Re-load the workspace URL that was showing when the content view's renderer
// crashed (spawning a fresh renderer). Falls back to Home if the pre-crash URL
// is unknown. Driven by the crash page's Reload button via the reload-crashed-view
// IPC relay. Stateless: if the reload crashes again, the crash page reappears.
function reloadCrashedContentView(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.contentView || bundle.contentView.webContents.isDestroyed()) return;
  // The content view only ever crashes while displaying agent content, so the
  // crashed-from URL is a workspace URL. Reload it straight into the (visible)
  // content view rather than through navigateBundle: this window still owns the
  // workspace (currentWorkspaceId is set), so navigateBundle's uniqueness guard
  // would focus-and-return instead of reloading.
  const target = bundle.crashedFromUrl
    || (bundle.currentWorkspaceId ? bundle.currentContentUrl : null)
    || (backendBaseUrl ? backendBaseUrl + '/' : null);
  if (!target) return;
  // Clear crash state before loading so the ensuing did-navigate is processed
  // as a normal content navigation.
  bundle.isContentCrashed = false;
  bundle.crashedFromUrl = null;
  bundle.contentWorkspaceReady = false;
  showBundleContentView(bundle);
  bundle.contentView.webContents.loadURL(target);
}

// Reload the chrome (titlebar) view after its renderer crashed, spawning a fresh
// renderer. Driven by chrome-crashed.html's Reload button via the reload-chrome
// IPC. Reloads /_chrome (or shell.html before the backend is up, matching the
// normal chrome-load choice); the chrome view's did-finish-load re-primes it from
// the cached chrome state, so the bar returns fully populated. Stateless: if the
// reload crashes again, the crash strip reappears.
function reloadCrashedChromeView(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (!bundle.chromeView || bundle.chromeView.webContents.isDestroyed()) return;
  bundle.isChromeCrashed = false;
  if (backendBaseUrl) {
    bundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
  } else {
    bundle.chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
  }
}

function openOrFocusWorkspace(agentId, url) {
  // Scope-wide uniqueness: a window sitting on this workspace's settings /
  // sharing / recovery screen owns the workspace too -- open it THERE rather
  // than spawning a second window for the same workspace's scope.
  const existing = findBundleForWorkspace(agentId) || findBundleForWorkspaceScope(agentId);
  if (existing) {
    focusBundle(existing);
    if (!isBundleDisplayingWorkspace(existing, agentId)) {
      navigateBundle(existing, toAbsoluteUrl(url || workspaceUrlForAgent(agentId)));
    }
    return existing;
  }
  const absolute = toAbsoluteUrl(url || workspaceUrlForAgent(agentId));
  return openNewWindow(absolute);
}

function openNewWindow(url, { showInactive = false } = {}) {
  const bundle = createBundle();
  if (showInactive) bundle.showInactiveOnFirstShow = true;
  bundle.isLoadingState = false;
  updateBundleBounds(bundle);
  // navigateBundle picks the surface: an agent url loads the /_chrome wrapper
  // into the chrome view + the workspace into the (shown) content view; a local
  // url loads the page straight into the chrome view (content view stays hidden).
  navigateBundle(bundle, url);
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

// The most recent error takeover's message/details, captured so the shell's
// "Report a bug" button can file a one-shot Sentry report of the on-screen error
// when the backend is down (and thus the normal /help flow is unreachable).
let lastErrorTakeover = null;

// ``canUseBackendReport`` is true only when the Python backend is still up (e.g. the
// discovery-pipeline-stall takeover): there the shell's report button opens the full
// /help modal. For a crashed/never-started backend it is false, and the report button
// falls back to a one-shot main-process Sentry report (see the report-error IPC handler).
function showErrorInAllWindows(message, details, actionLabel, canUseBackendReport = false) {
  lastErrorTakeover = { message, details };
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isErrorState = true;

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
            bundle.chromeView.webContents.send('error-details', { message, details, actionLabel, canUseBackendReport });
          }
        });
      } else {
        bundle.chromeView.webContents.send('error-details', { message, details, actionLabel, canUseBackendReport });
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
      // Match the rounding + background applied in
      // createBundleWebContentsViews so the post-retry contentView keeps
      // the same tucked-under appearance and doesn't flash the accent
      // through during in-workspace navigation.
      contentView.setBorderRadius(CONTENT_CORNER_RADIUS);
      contentView.setBackgroundColor('#ffffff');
      bundle.contentView = contentView;
      bundle.window.contentView.addChildView(contentView);
      // Rebuilt hidden, like the initial content view: reloadAllWindowsAfterRetry
      // routes the pre-error URL through navigateBundle, which shows it only if
      // it is agent content.
      contentView.setVisible(false);
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
    // Route the pre-error URL back to its surface: navigateBundle loads the
    // /_chrome wrapper + shows the content view for an agent URL, or renders a
    // local page straight in the chrome view (content view stays hidden).
    const target = bundle.preErrorUrl || (backendBaseUrl ? backendBaseUrl + '/' : null);
    if (target) navigateBundle(bundle, target);
  }
}

// -- Quitting takeover --
//
// Once a quit has committed (isShuttingDown is set) every open window flips to
// a full-window "quitting" screen. It reuses shell.html -- the same animated
// wordmark as the startup loading screen -- loaded with a `#quitting` hash so
// the page reveals a status line. updateBundleBounds collapses every other view
// so the chrome view alone fills the window, and stop/teardown progress is
// pushed to it through the existing `status-update` IPC channel. Iterating an
// empty `bundles` set makes this a natural no-op for a headless signal quit.

// The most recent status pushed to the quitting page. The `#quitting` hash
// seeds the page with this anyway, but an update can race ahead of the page
// load (the first `Stopping N minds…` is sent right after `loadFile`, before
// the renderer registers its listener), so each quitting screen re-pushes it
// once it finishes loading -- otherwise that first update would be dropped and
// the page would sit on `Quitting…` for the whole stop.
let latestQuittingStatus = 'Quitting…';

function showQuittingInAllWindows() {
  latestQuittingStatus = 'Quitting…';
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isQuittingState = true;
    // Hide the auxiliary views (without tearing them down or clearing their
    // visible flags) so the full-window chrome view is the only thing on
    // screen, and restoreFromQuittingInAllWindows can bring back exactly what
    // was open if the user backs out of the quit.
    for (const view of [bundle.modalView]) {
      if (view && !view.webContents.isDestroyed()) view.setVisible(false);
    }
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      const chromeContents = bundle.chromeView.webContents;
      chromeContents.loadFile(path.join(__dirname, 'shell.html'), { hash: 'quitting' });
      chromeContents.once('did-finish-load', () => {
        if (!chromeContents.isDestroyed()) chromeContents.send('status-update', latestQuittingStatus);
      });
    }
    updateBundleBounds(bundle);
  }
}

// Broadcast a status line to every window currently showing the quitting page.
function updateQuittingStatus(message) {
  latestQuittingStatus = message;
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed() || !bundle.isQuittingState) continue;
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('status-update', message);
    }
  }
}

// Reverse showQuittingInAllWindows. Used when the user backs out of an already
// committed quit via the "could not be stopped" dialog's "Cancel quit": every
// window returns to its normal layout with whatever views were open before the
// flip (their pages were only hidden, never torn down).
function restoreFromQuittingInAllWindows() {
  for (const bundle of bundles) {
    if (bundle.window.isDestroyed()) continue;
    bundle.isQuittingState = false;
    // Route the window back to the page it was on before the quitting takeover,
    // via navigateBundle so it lands on the right surface: an agent URL reloads
    // the /_chrome wrapper + re-shows the content view, while a trusted local
    // page (Home, Create, settings, recovery, ...) renders straight in the
    // chrome view. Hardcoding /_chrome here would leave a local-page window
    // showing the empty agent wrapper over a hidden content view (a blank body).
    const target = bundle.preErrorUrl || bundle.currentContentUrl
      || (backendBaseUrl ? backendBaseUrl + '/' : null);
    if (target) navigateBundle(bundle, target);
    if (bundle.modalView && bundle.modalVisible && !bundle.modalView.webContents.isDestroyed()) {
      bundle.modalView.setVisible(true);
    }
    updateBundleBounds(bundle);
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
// ...] }`` in ``window-state.json``. Older variants -- a bare array of
// window entries, or entries carrying a now-ignored ``lastWorkspaceAgentId``
// field -- are accepted by ``loadSessionState`` so existing installs migrate
// transparently on first read. The titlebar accent is NOT persisted: a
// restored window re-derives it from its own saved ``url`` (the content URL
// it reopens to), so there is nothing per-window to remember.
//
// A workspace window's live content URL is on the agent subdomain
// (``agent-<id>.localhost:<mngr_forward_port>/...``), whose host AND port both
// change between runs, so it can't be replayed verbatim. ``toPersistedContentUrl``
// canonicalises such windows to the port-independent ``/goto/<agent>/``
// auth-bridge path; ``toRestoredContentUrl`` rebuilds the live workspace URL
// from it on launch. The mind itself persists its panel layout server-side and
// restores it on a fresh load of its root, so reopening the root is enough to
// land the user back where they were. Non-workspace screens (Home, Create,
// ``/workspace/<id>/settings``, ...) live on the minds backend and round-trip
// as plain backend-relative paths.

function loadSessionState() {
  try {
    const p = getSessionStatePath();
    if (!fs.existsSync(p)) return { windows: [] };
    const raw = fs.readFileSync(p, 'utf-8');
    const parsed = JSON.parse(raw);
    // Legacy shape: a bare array of window entries (pre-titlebar-accent).
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
// relative path loses the agent identity (the subdomain host) and would
// reopen against the minds backend (the landing page) on restore. Persist the
// port-independent ``/goto/<agent>/`` auth-bridge path instead -- it carries
// the agent id, is recognised by ``parseWorkspaceId`` (so dead-workspace
// filtering works), and ``toRestoredContentUrl`` rebuilds the live origin from
// it. Everything else round-trips as a minds-backend-relative path.
function toPersistedContentUrl(url) {
  const agentId = parseWorkspaceId(url);
  if (agentId) return `/goto/${encodeURIComponent(agentId)}/`;
  return toRelativeBackendUrl(url);
}

// Inverse of ``toPersistedContentUrl``: turn a persisted entry's ``url`` back
// into a loadable absolute URL. Workspace entries (``/goto/<agent>/``) are
// rebuilt through ``workspaceUrlForAgent`` so the bridge targets the CURRENT
// run's mngr_forward origin; other entries resolve against the minds backend.
// Persisted urls are backend-relative, so resolve to absolute BEFORE parsing
// the workspace id -- ``parseWorkspaceId`` runs ``new URL(url)``, which throws
// (yielding null) on a bare relative path.
function toRestoredContentUrl(entry) {
  const absolute = toAbsoluteUrl(entry.url);
  const agentId = parseWorkspaceId(absolute);
  if (agentId) {
    const workspaceUrl = workspaceUrlForAgent(agentId);
    if (workspaceUrl) return workspaceUrl;
  }
  return absolute;
}

function saveSessionState() {
  try {
    const windows = [];
    // Iterate in MRU order so entry 0 is the most-recently-focused window.
    // The startup path applies entry 0's bounds to the loading window before
    // the loading screen renders, so MRU ordering means the loading window
    // appears at the user's last-active window's position.
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
    // Empty-clobber guard: never let an empty snapshot overwrite a non-empty
    // on-disk file. Saves now run continuously (debounced on move/resize/nav),
    // so a save can land while windows are being torn down by a non-graceful
    // quit (ToDesktop "Install and Restart", crash, force-quit) -- the live
    // window set momentarily reads empty, and writing it would drop the user on
    // the create screen next launch. shouldWriteSessionState only permits an
    // empty write when the persisted file is already empty. See
    // session-persistence.js for the full reasoning (the normal last-window
    // close saves through the quit sequence while the window is still alive, so
    // it produces a non-empty snapshot and is unaffected).
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

// Continuously persist window-state.json (debounced) so the saved set reflects
// the live windows regardless of how the app exits. The trailing-throttle
// coalesces bursts (a window drag fires move/resize every frame) into at most
// one write per interval, keeping disk churn negligible. Callers schedule via
// scheduleSessionSave(), which is suppressed once a quit has committed
// (isShuttingDown) -- the quit sequence performs its own authoritative save and
// we must not race a debounced write against the teardown.
const SESSION_SAVE_DEBOUNCE_MS = 1000;
const debouncedSessionSaver = createDebouncedSaver({ save: saveSessionState, delayMs: SESSION_SAVE_DEBOUNCE_MS });

function scheduleSessionSave() {
  if (isShuttingDown) return;
  debouncedSessionSaver.schedule();
}

// Update a single bundle's ``currentAccentAgentId`` (the accent source of its
// current screen) and push it to that bundle's chrome view over the
// ``accent-changed`` channel. No-op when the value isn't actually changing, so
// the per-navigation calls (Electron emits one per content navigation) don't
// thrash the IPC. Pass ``null`` to clear to the neutral chrome (general screen,
// workspace deleted, user signed out). Not persisted -- the saved content URL
// is the source of truth across restarts, so a restored window re-derives it.
function updateBundleAccentAgentId(bundle, agentId) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const normalized = typeof agentId === 'string' && agentId ? agentId : null;
  if (normalized === bundle.currentAccentAgentId) return;
  bundle.currentAccentAgentId = normalized;
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.webContents.send('accent-changed', normalized);
  }
  // Also reach the open workspace switcher (an overlay iframe) so its current-row
  // highlight follows the active workspace scope, e.g. onto its settings screen.
  sendToOverlayFrames(bundle, 'accent-changed', normalized);
}

function filterRestorableUrls(state, knownAgentIdsSet) {
  const results = [];
  for (const entry of state) {
    // Persisted urls are backend-relative; resolve to absolute so
    // ``parseWorkspaceId`` (``new URL``) can read the workspace id rather than
    // throwing on the bare path.
    const agentId = parseWorkspaceId(toAbsoluteUrl(entry.url));
    // A workspace window can only be restored if its agent is known to exist
    // (live workspaces UNION the persisted last-good topology). Drop workspace
    // entries whose agent isn't in that set -- and, when the set is null
    // (NOTHING is known: empty live AND empty last-good), drop every workspace
    // entry, because restoring a workspace URL against an unknown topology can
    // only render the workspace-recovery "unresponsive" page. Non-workspace
    // screens (home, settings, ...) carry no agent id and always pass through.
    if (agentId && (!knownAgentIdsSet || !knownAgentIdsSet.has(agentId))) {
      continue; // workspace no longer exists (or none are known), skip silently
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
// Every chrome and (formerly) sidebar view used to open its own
// EventSource to /_chrome/events. Chromium caps same-host HTTP/1.1
// connections at 6, so with a couple of workspace windows + sidebars,
// subsequent requests (/_chrome/sidebar, /inbox/list, home navigation)
// would queue behind the SSE streams -- load-finish latencies could
// creep from 50ms to 8+ seconds. Running one SSE connection in the main
// process and broadcasting events via IPC avoids the exhaustion entirely.

// Whether we have already taken over every window with the discovery-pipeline
// "blocked" error screen. Guards against re-driving the takeover when the
// backend re-emits the terminal `discovery_health` state (e.g. on an SSE
// reconnect). Reset by the `retry` handler so a re-block after a restart
// surfaces again.
let discoveryBlockedShown = false;

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
      detachWindowsForWorkspace(oldId);
      // Clear the accent in any window whose accent source was the destroyed
      // workspace, so its titlebar falls back to the neutral chrome instead of
      // pointing at a workspace that no longer exists. ``detachWindowsForWorkspace``
      // already navigates a window that was *displaying* the workspace (which
      // re-derives the accent from the new URL); this also catches a window
      // parked on the destroyed workspace's settings / sharing screen, which
      // doesn't auto-navigate.
      for (const b of bundles) {
        if (b.window.isDestroyed()) continue;
        if (b.currentAccentAgentId === oldId) {
          updateBundleAccentAgentId(b, null);
        }
      }
    }

    updateAllOsTitles();
  } else if (evt.type === 'system_interface_status') {
    // Remember each mind's latest non-healthy health so (a) the Stop handler
    // can leave a window that is actively restarting alone (see
    // confirm-stop-mind) and (b) primeViewWithCachedChromeState can replay it
    // to a freshly (re)loaded view. A ``healthy`` (or empty) status clears the
    // entry: it mirrors the server snapshot (which omits healthy agents), keeps
    // the map scoped to agents that still need attention, and matches chrome.js
    // dropping the agent from its own map on ``healthy``.
    if (evt.agent_id) {
      const status = evt.status ? String(evt.status) : '';
      if (!status || status === 'healthy') {
        systemInterfaceStatusByAgent.delete(String(evt.agent_id));
      } else {
        systemInterfaceStatusByAgent.set(String(evt.agent_id), status);
      }
    }
  } else if (evt.type === 'auth_required') {
    // Clear every window's accent on the authenticated -> unauthenticated
    // boundary (account sign-out or session expiration). Without this each
    // window's bar would stay tinted with its last workspace's color on the
    // sign-in page (the content view redirects there, but a settings / sharing
    // screen won't have re-derived a neutral accent on its own). The SSE
    // endpoint emits ``auth_required`` and closes whenever the request is
    // unauthenticated, so a mid-session sign-out manifests here as: stream that
    // was delivering ``workspaces`` -> stream closes -> reconnect after 1.5s ->
    // ``auth_required`` payload. ``latestChromeState.workspaces`` is only ever
    // set from the ``workspaces`` branch above, so its non-null state is a
    // stable "we have been authenticated this session" flag -- gating on it
    // leaves freshly-hydrated ``bundle.currentAccentAgentId`` values alone
    // during the cold-start unauthenticated path.
    if (latestChromeState.workspaces !== null) {
      for (const b of bundles) {
        if (b.window.isDestroyed()) continue;
        updateBundleAccentAgentId(b, null);
      }
    }
  } else if (evt.type === 'auth_status') {
    latestChromeState.authStatus = evt;
  } else if (evt.type === 'requests') {
    const prevIds = latestChromeState.requestIds || [];
    const newIds = Array.isArray(evt.request_ids) ? evt.request_ids.map(String) : [];
    const newCount = evt.count || 0;
    // Backend defaults auto_open to true; treat a missing field the same way.
    const autoOpen = evt.auto_open !== false;
    // Diff the pending *set* (ordered ids), not the count, so a swap at
    // constant size still refreshes the inbox list. Auto-open keys off a
    // genuinely new id appearing (not a count increase, which is blind to
    // replacements), so approving/denying never reopens an inbox the user
    // closed.
    const prevSet = new Set(prevIds);
    const hasNewRequest = newIds.some((id) => !prevSet.has(id));
    const idsChanged = newIds.length !== prevIds.length || hasNewRequest;
    latestChromeState.requestIds = newIds;
    latestChromeState.requestCount = newCount;
    const shouldAutoOpen = autoOpen && hasNewRequest;
    // When the inbox modal is already open in a bundle, forward the
    // chrome-event to its shell JS (debounced) so the master list
    // re-fetches its fragment; otherwise, on a genuinely new id, open it.
    //
    // The auto-open is gated on ``!b.modalVisible`` (not just
    // ``!isInboxModalOpen``) because the sidebar now shares ``modalView``:
    // auto-opening the inbox while the sidebar (or any other modal) is open
    // would ``loadURL`` the inbox over it, silently yanking the user's open
    // menu out from under them. When a modal is already up we leave it
    // alone; the titlebar requests badge still updates live (via the
    // broadcastChromeEvent below), and the next genuinely-new request (or
    // the user closing the modal and clicking the bell) surfaces the inbox.
    if (idsChanged || shouldAutoOpen) {
      for (const b of bundles) {
        if (shouldAutoOpen && !b.modalVisible) {
          openInbox(b, '');
        } else if (isInboxModalOpen(b)) {
          scheduleInboxListRefresh(b, evt);
        }
      }
    }
  } else if (evt.type === 'open_help') {
    // An in-workspace ``/assist`` agent asked the app to open the report-a-bug
    // modal pre-filled with its diagnosis (the /api/v1 report route). Surface it
    // in the window currently showing that workspace; if no window is showing it,
    // fall back to the most-recent window so the report isn't silently lost. Leave
    // an already-open modal alone (matching the requests auto-open), so we never
    // yank a menu the user has up.
    const description = typeof evt.description === 'string' ? evt.description : '';
    const wsId = evt.workspace_agent_id ? String(evt.workspace_agent_id) : '';
    const target = (wsId && findBundleForWorkspace(wsId)) || getMostRecentWindow();
    if (target && !target.modalVisible) {
      openHelp(target, wsId, description);
    }
  } else if (evt.type === 'discovery_health') {
    // App-global discovery-pipeline health. Only the terminal `blocked` state is
    // ever sent (the reconnecting tier heals silently in the background, retrying
    // forever). `blocked` means the consumer subprocess died -- it is also the
    // HTTP traffic proxy, so agent forwarding is down / the app is unusable. Take
    // over every window with the error screen; its Restart button runs the
    // existing retry path (shut down + restart the backend, respawning the
    // consumer). Surface the tail of the log as details so "Show details" isn't
    // an empty box (matches the other takeover sites).
    if (evt.state === 'blocked' && !discoveryBlockedShown) {
      discoveryBlockedShown = true;
      showErrorInAllWindows(
        "Minds has disconnected from your workspaces and can't automatically reconnect. Restart the app to recover. Your data has not been lost.",
        readLastLogLines(50),
        'Restart Minds',
        // The Python backend is still up in this takeover (only the discovery pipeline
        // stalled), so the shell's report button can use the full /help modal.
        true,
      );
    }
  }
  broadcastChromeEvent(evt);
}

function broadcastChromeEvent(evt) {
  for (const b of bundles) {
    if (b.window.isDestroyed()) continue;
    // Push to the chrome titlebar and to the overlay's iframes (sidebar, inbox).
    // The inbox shell uses these events too (e.g. ``requests`` count); the
    // sidebar uses ``workspaces`` / ``auth_status`` to render its list. The
    // overlay's hosted pages live in iframes, so fan out per-frame.
    if (b.chromeView && !b.chromeView.webContents.isDestroyed()) {
      try {
        b.chromeView.webContents.send('chrome-event', evt);
      } catch { /* noop */ }
    }
    sendToOverlayFrames(b, 'chrome-event', evt);
  }
}

function primeViewWithCachedChromeState(bundle, wc) {
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
  // Replay the latest non-healthy system-interface status for each agent so a
  // freshly (re)loaded chrome/sidebar view re-learns which workspaces are
  // stuck / restarting. Unlike the events above, per-agent health is NOT held
  // in ``latestChromeState`` -- it lives in ``systemInterfaceStatusByAgent``,
  // populated one-shot from the SSE's ``system_interface_status`` events.
  // Without this replay, a renderer that reloads after an agent went STUCK
  // never re-learns it: the backend only (re)emits ``system_interface_status``
  // on a state transition or a brand-new SSE connection, and the main process
  // holds a single long-lived SSE -- so the in-memory status here is the only
  // surviving copy. Missing the replay leaves the stuck workspace's content
  // view parked on the plugin's "Loading workspace" loader forever, because
  // chrome.js's auto-redirect to the recovery page only fires when it holds a
  // ``stuck`` status for the displayed agent. HEALTHY (and the empty
  // placeholder) is skipped to mirror the server's connect-time snapshot,
  // which omits healthy agents.
  for (const [agentId, status] of systemInterfaceStatusByAgent) {
    if (!status || status === 'healthy') continue;
    wc.send('chrome-event', { type: 'system_interface_status', agent_id: agentId, status });
  }
  // Re-send modal state to the chrome titlebar in case the modal opened
  // before chrome.js registered its onModalStateChanged listener (e.g. the
  // requests panel auto-opens at startup faster than chrome.js loads).
  // Electron IPC drops events with no listener, so without this replay the
  // initial open is missed and the titlebar drag region wins over the
  // modal's no-drag in the y=0..TITLEBAR strip. The modal's hosted pages
  // (sidebar, inbox) don't listen for this event, so we scope the send to
  // the chrome view.
  if (bundle && bundle.chromeView && wc === bundle.chromeView.webContents) {
    wc.send('modal-state-changed', { open: !!bundle.modalVisible });
    // Paint the titlebar accent on (re)load. The per-navigation
    // ``accent-changed`` send can land before this view's chrome.js has
    // registered its listener (Electron drops listener-less IPC), so a fresh
    // or rebuilt chrome view would otherwise come up with no accent. Replaying
    // the current value here -- after did-finish-load, when the listener is
    // ready -- is what makes a cold start / crash-recovery rebuild paint the
    // right accent (or the neutral chrome, when it's null) without the
    // renderer remembering anything.
    wc.send('accent-changed', bundle.currentAccentAgentId);
    // Replay the displayed page's URL for the same reason: on the agent-wrapper
    // (/_chrome) the titlebar's breadcrumb / icon-tabs / contextual back arrow
    // are a pure function of the content view's URL, and the per-navigation
    // ``content-url-changed`` push may have fired before this chrome view
    // registered its listener. (A local page in the chrome view ignores this --
    // it derives its own breadcrumb from its own location -- so the replay is a
    // harmless no-op there.)
    const displayedUrl = bundle.currentContentUrl;
    if (displayedUrl) {
      wc.send('content-url-changed', displayedUrl);
    }
  }
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
  // `_started` tracks whether this loop is currently running so callers can
  // tell whether it needs to be (re)started -- it is cleared on exit below.
  runChromeSSELoop._started = true;
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
  // The loop has exited (isShuttingDown went true). Clear the flag so that if
  // the user backs out of an already-committed quit, ensureChromeSSELoopRunning
  // can restart it rather than leaving the titlebar without a live SSE feed.
  runChromeSSELoop._started = false;
}

// Guarantee the chrome-events SSE consumer is running. Starts it if it has
// never run or has exited (e.g. it terminated when a quit committed and the
// user then backed out); otherwise nudges the existing connection to reconnect.
// Idempotent and safe to call whenever the app returns to its normal state.
function ensureChromeSSELoopRunning() {
  if (!runChromeSSELoop._started) {
    runChromeSSELoop();
  } else {
    kickChromeSSEReconnect();
  }
}

// POST the v1 restart endpoint with a ``scope`` ('services' to restart just the
// system-services agent, 'host' to restart the whole host) and resolve once the
// server has acknowledged the 202 dispatch (or the request errors / times out).
// The route returns 202 immediately (with an ``{operation_id, kind}`` handle we
// don't need here) and drives recovery asynchronously; the 202 also means the
// health tracker is already RESTARTING, so callers navigate to the recovery
// page afterward, which polls health, shows restart progress, and returns to
// the workspace once healthy.
//
// Always resolves (never rejects) so callers can chain navigation
// regardless of network outcome.
const RESTART_REQUEST_TIMEOUT_MS = 10000;
function postRestart(agentId, scope) {
  return new Promise((resolve) => {
    if (!agentId || !backendBaseUrl) {
      resolve();
      return;
    }
    let req;
    try {
      req = net.request({
        url: `${backendBaseUrl}/api/v1/workspaces/${encodeURIComponent(agentId)}/restart`,
        method: 'POST',
        useSessionCookies: true,
      });
      req.setHeader('Content-Type', 'application/json');
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
    req.write(JSON.stringify({ scope }));
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

// ---------- Mind shutdown on quit + landing Stop button ----------

// Timeout for the instant, in-memory liveness lookup (GET
// /api/v1/desktop/running-workspaces).
const MIND_HTTP_TIMEOUT_MS = 10000;
// Timeout for the synchronous command endpoints (single/bulk host stop, state
// container stop). These block until the underlying ``mngr``/docker command
// finishes; the server's own per-command ceiling is ~120s, so allow margin
// above it here so the server-side failure surfaces rather than a client abort.
const MIND_COMMAND_TIMEOUT_MS = 150000;

// GET /api/v1/desktop/running-workspaces -> { ok, running }. ``running`` is an
// array of {id, name}; ``ok`` is false when the check itself failed (network,
// parse, no backend) so the caller can distinguish "nothing running" from
// "couldn't tell" instead of silently treating a failed check as an empty list.
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

// POST /api/v1/workspaces/<id>/stop (synchronous). Resolves true when the server
// reports the stop succeeded (<400), false otherwise. Used by the single-row
// landing Stop relay. (The v1 route blocks until the host transition resolves,
// same as the legacy /api/agents/<id>/stop-host it replaced; cookie auth is
// accepted via useSessionCookies.)
function postMindStop(agentId) {
  return new Promise((resolve) => {
    if (!agentId || !backendBaseUrl) {
      console.warn('[mind-shutdown] missing agent id or backend URL; cannot stop mind');
      resolve(false);
      return;
    }
    let req;
    try {
      req = net.request({
        url: `${backendBaseUrl}/api/v1/workspaces/${encodeURIComponent(agentId)}/stop`,
        method: 'POST',
        useSessionCookies: true,
      });
    } catch (e) {
      console.warn('[mind-shutdown] failed to construct stop request:', e);
      resolve(false);
      return;
    }
    let settled = false;
    let isOk = false;
    const settle = () => { if (!settled) { settled = true; resolve(isOk); } };
    const timer = setTimeout(() => {
      console.warn(`[mind-shutdown] stop request for ${agentId} timed out after ${MIND_COMMAND_TIMEOUT_MS}ms`);
      try { req.abort(); } catch { /* noop */ }
      settle();
    }, MIND_COMMAND_TIMEOUT_MS);
    req.on('response', (response) => {
      isOk = response.statusCode < 400;
      if (!isOk) console.warn(`[mind-shutdown] stop for ${agentId} returned HTTP ${response.statusCode}`);
      response.on('data', () => {});
      response.on('end', () => { clearTimeout(timer); settle(); });
      response.on('error', (err) => { console.warn(`[mind-shutdown] stop response error for ${agentId}:`, err); clearTimeout(timer); settle(); });
    });
    req.on('error', (err) => { console.warn(`[mind-shutdown] stop request failed for ${agentId}:`, err); clearTimeout(timer); settle(); });
    req.end();
  });
}

// POST /api/v1/desktop/stop-hosts?agent_id=...&agent_id=... (synchronous).
// Issues ONE ``mngr stop <ids...> --stop-host`` server-side (mngr stops the
// hosts concurrently). Resolves { ok, stillRunning }: ``stillRunning`` is the subset
// of requested minds the server still sees running after the attempt; ``ok`` is
// false when the request itself failed (so the caller treats it as "couldn't
// stop" rather than "all stopped").
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

// POST /api/v1/desktop/state-container/stop -- stops this env's mngr docker
// "state container" (provider bookkeeping) so nothing minds-related is left
// running after a full shutdown. Best-effort: resolves regardless of outcome, but logs.
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

// Stop every running mind via one synchronous bulk-stop call (the server runs a
// single ``mngr stop --stop-host`` over all of them), then decide what to do
// about any that did not stop. Progress is shown in-page on the quitting
// takeover screen (which the quit sequence has already flipped to). Returns true
// to proceed with the quit, false to cancel it. On a failure to stop everything,
// offers Retry / Quit anyway / Cancel.
async function stopAllMindsThenDecide(running) {
  let remaining = running;
  while (true) {
    updateQuittingStatus(remaining.length === 1 ? 'Stopping 1 mind…' : `Stopping ${remaining.length} minds…`);
    const { ok, stillRunning } = await postStopMinds(remaining.map((mind) => mind.id));
    // ``ok`` && empty stillRunning = the server confirms everything is down. A
    // request-level failure (ok=false) is treated as "could not confirm", so we
    // fall through to the recovery dialog rather than quit assuming success.
    if (ok && stillRunning.length === 0) {
      // Every mind is down; also stop the mngr docker state container so no
      // minds-related container is left running. Best-effort -- it preserves its
      // volume and restarts on next use.
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

// On quit, ask whether to shut down any still-running minds. This is the FIRST
// native prompt and runs BEFORE any window is flipped to the quitting page, so
// cancelling here leaves the app fully intact with no visual change.
// Returns a plan: `{ proceed, stop, running }`.
//   proceed=false           -> user cancelled; stay open.
//   proceed=true, stop=false -> quit now (no minds, or "Leave running").
//   proceed=true, stop=true  -> quit and stop `running` after the flip.
async function promptMindShutdown() {
  if (!getBackendProcess() || !backendBaseUrl) return { proceed: true, stop: false, running: [] };
  const { ok, running } = await getRunningMinds();
  if (!ok) {
    // The liveness check itself failed -- don't silently quit leaving minds
    // running. Surface the uncertainty and let the user decide explicitly.
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
  if (response === 0) return { proceed: false, stop: false, running: [] };
  if (response === 1) return { proceed: true, stop: false, running: [] };
  return { proceed: true, stop: true, running };
}

function fetchInitialChromeState(timeoutMs = 25000) {
  // Drives one round-trip to /_chrome/events (SSE) to learn auth status and the
  // workspace list, resolving on the first ``workspaces`` snapshot. Returns:
  //   { authenticated: true, workspaces: [...], hasAccounts, restorableWorkspaceIds }
  //                                                 on the first workspaces snapshot
  //   { authenticated: false }                      when the backend says auth_required
  //   null                                          on timeout (no snapshot) / network error
  //
  // We resolve on the first snapshot even though discovery may still be mid-sweep
  // (providers enumerate at different speeds): window restore filters against
  // ``restorableWorkspaceIds`` -- the live workspaces UNION the persisted last-good
  // topology -- so a workspace that hasn't been re-discovered yet is still kept,
  // and a window is never dropped just because the snapshot is partial.
  //
  // The timeout only fires when NO snapshot arrives at all. It must comfortably
  // exceed the connect-time snapshot's slowest blocking step: the backend computes
  // ``has_accounts`` (a cold ``mngr imbue_cloud auth list`` subprocess, ~5s on
  // first call) before emitting the first ``workspaces`` event. A timeout shorter
  // than that returns ``null``, which the startup path treats as unauthenticated
  // and routes to /welcome -- bouncing an already-signed-in user.
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
              finish({
                authenticated: true,
                workspaces: parsed.workspaces,
                hasAccounts: !!parsed.has_accounts,
                restorableWorkspaceIds: Array.isArray(parsed.restorable_workspace_ids)
                  ? parsed.restorable_workspace_ids.map(String)
                  : [],
              });
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

// -- Deeplinks (minds://) protocol registration + single instance lock --

// Register as the OS handler for minds:// URLs. In dev (`electron .`) the
// registration must point the OS at the electron binary plus this checkout's
// app path; that works on Windows/Linux but is a no-op on macOS, where
// LaunchServices only honors schemes declared in a bundle's Info.plist
// (packaged builds get that via ``appProtocolScheme`` in todesktop.js).
if (process.defaultApp) {
  if (process.argv.length >= 2) {
    app.setAsDefaultProtocolClient('minds', process.execPath, [path.resolve(process.argv[1])]);
  }
} else {
  app.setAsDefaultProtocolClient('minds');
}

// macOS delivery. Registered at module scope -- before the 'ready' event --
// so a cold-start launch URL is caught too.
app.on('open-url', (event, url) => {
  event.preventDefault();
  handleDeeplink(url);
});

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    // Windows/Linux deliver deeplink re-invocations through the second
    // instance's argv.
    const url = extractDeeplinkUrlFromArgv(argv || []);
    if (url) {
      handleDeeplink(url);
      return;
    }
    const mru = getMostRecentWindow();
    if (mru) focusBundle(mru);
  });
  // Windows/Linux cold start passes the URL in this process's argv. Harmless
  // on macOS (open-url is used there) -- and it doubles as the dev-mode test
  // path on any platform: ``electron . 'minds://...'``.
  const coldStartDeeplinkUrl = extractDeeplinkUrlFromArgv(process.argv);
  if (coldStartDeeplinkUrl) handleDeeplink(coldStartDeeplinkUrl);
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
  // the app ever creates (all three bundle views -- chrome, content, modal --
  // plus any popup windows), rather than wiring each view individually.
  // Registered before the first bundle is created so it covers the initial
  // chrome/content views too.
  app.on('web-contents-created', (_event, contents) => {
    applyExternalLinkHandling(contents);
  });
  installApplicationMenu();
  installDockMenu();
  installDevDockIcon();
  // Repaint views that survive sleep but stop painting (see
  // repaintAllBundleViewsAfterWake). powerMonitor is only usable after the app is
  // ready, which onReady guarantees. Both events can fire for one wake on macOS;
  // the repaint is idempotent, so the redundant call is harmless.
  powerMonitor.on('resume', () => repaintAllBundleViewsAfterWake('resume'));
  powerMonitor.on('unlock-screen', () => repaintAllBundleViewsAfterWake('unlock-screen'));
  setupContentPartitionCookieSync();
  await syncContentCookiesToDefaultSession();

  initialBundle = createBundle();
  // Apply saved bounds before the loading screen renders so the window doesn't
  // jump from default-centered to its restored position once content loads.
  // ``loadSessionState`` returns ``{ windows: [...] }`` (see comment above
  // the function) -- entry 0 is the MRU window, which is the bundle the
  // loading screen will surface as.
  const initialSavedState = loadSessionState();
  if (initialSavedState.windows.length > 0) {
    restoreWindowBounds(initialBundle, initialSavedState.windows[0]);
  }
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
          // Deliberately NO Cmd+W accelerator: the app must not claim it, so
          // the keystroke passes through to the focused web contents -- inside
          // a workspace the dockview UI closes its active tab with it. Closing
          // the window stays available from this menu item and Cmd+Q quits.
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
        {
          label: 'Toggle Outer Developer Tools',
          // Opens DevTools (in detached windows so the small surfaces
          // aren't constrained to an embedded panel) for all of the
          // bundle's WebContentsViews -- the chrome view (titlebar +
          // sidebar shell), the workspace content view, and the modal
          // view if it exists. Equivalent to setting
          // MINDS_OPEN_DEVTOOLS=1 at startup but on-demand. No
          // accelerator -- it's a dev-time affordance.
          //
          // ``toggleDevTools()`` ignores its options argument, so to
          // get the detached window we explicitly open / close.
          click: () => {
            const bundle = getMostRecentWindow();
            if (!bundle || bundle.window.isDestroyed()) return;
            for (const view of [bundle.chromeView, bundle.contentView, bundle.modalView]) {
              if (!view) continue;
              if (view.webContents.isDestroyed()) continue;
              if (view.webContents.isDevToolsOpened()) {
                view.webContents.closeDevTools();
              } else {
                view.webContents.openDevTools({ mode: 'detach' });
              }
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

// In dev (unpackaged) runs the macOS dock shows the generic Electron icon,
// which is indistinguishable from any other Electron app and from a packaged
// Minds. Override it with the "dev"-labeled variant so a running dev build is
// obvious in the dock. Packaged builds get their icon from the bundled .icns
// (see todesktop.js) and must not be overridden here. BrowserWindow's `icon`
// option is ignored for the macOS dock, so app.dock.setIcon is the only path.
function installDevDockIcon() {
  if (app.isPackaged || !isMac || !app.dock) return;
  const devIcon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon-dev.png'));
  if (!devIcon.isEmpty()) app.dock.setIcon(devIcon);
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

    // Kick off the shared chrome-events SSE consumer (idempotent: starts it if
    // it isn't already running, otherwise forces a reconnect after a backend
    // restart).
    ensureChromeSSELoopRunning();

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

      // Filter restored windows against the backend's *restorable* workspace ids
      // -- live workspaces UNION the persisted last-good topology -- rather than
      // the live list alone. The last-good entries keep a window whose workspace
      // exists but a slow provider hasn't re-listed yet this session (e.g. local
      // docker lagging the cloud provider on cold start); absence from an
      // incomplete live snapshot is not evidence a workspace was destroyed (the
      // live discovery flow navigates genuinely-destroyed workspaces away later).
      // When nothing is known yet (empty live AND empty last-good -- first launch,
      // or a wiped topology), pass ``null``. ``filterRestorableUrls`` then keeps
      // non-workspace windows (home, settings, ...) but drops workspace windows,
      // since a workspace URL restored against an unknown topology can only
      // render the "unresponsive" recovery page.
      const restorableWorkspaceIds = (authenticated && chromeState.restorableWorkspaceIds) || [];
      const knownAgentIdsSet = restorableWorkspaceIds.length > 0
        ? new Set(restorableWorkspaceIds.map(String))
        : null;
      const restorable = authenticated
        ? filterRestorableUrls(savedState.windows, knownAgentIdsSet)
        : [];
      // (A workspace that no longer exists needs no special accent handling
      // here: ``filterRestorableUrls`` drops windows whose saved URL points at a
      // workspace absent from the known set, and the accent is re-derived from
      // whatever URL each restored window actually reopens to.)

      initialBundle.isLoadingState = false;
      updateBundleBounds(initialBundle);
      // The chrome view is deliberately NOT pointed at /_chrome here: every
      // startup route below ends in navigateBundle, which loads the landing
      // local page straight into the chrome view (or the /_chrome wrapper for
      // an agent restore, via ensureBundleChromeWrapper). Preloading /_chrome
      // first would just double-navigate the chrome view and flash.
      // The initial window's overlay view was created before the backend was
      // up, so load its warm host page now that ``backendBaseUrl`` is known.
      loadOverlayHost(initialBundle);

      // Decide the cold-start landing screen. The precedence (welcome > create
      // > restore) lives in the pure ``decideStartupRoute`` helper so it can be
      // unit-tested (startup-routing.test.js). Key subtlety: a "functionally
      // empty" app -- signed out of every account AND no workspaces -- routes
      // to /welcome even when stale window-state lingers, so a leftover home
      // (`/`) window can't silently suppress onboarding for a signed-out user.
      const startupRoute = decideStartupRoute({
        authenticated,
        hasAccounts: !!(chromeState && chromeState.hasAccounts),
        workspaceCount: workspaceList.length,
        restorableCount: restorable.length,
      });
      // Headline diagnostic for "why did I land on create/welcome instead of my
      // restored workspace": logs every input to the routing decision and the
      // chosen route, so a bad restore can be traced from ~/.minds/logs.
      console.log(
        `[startup] route=${startupRoute} authenticated=${authenticated} hasAccounts=${!!(chromeState && chromeState.hasAccounts)} workspaceCount=${workspaceList.length} restorableCount=${restorable.length}`,
      );

      const loadInitialContent = (relativePath) => {
        // /welcome and / are local pages -> navigateBundle renders them in the
        // chrome view (the /_chrome preloaded at createBundle is replaced), and
        // the content view stays hidden.
        navigateBundle(initialBundle, backendBaseUrl + relativePath);
      };

      if (startupRoute === 'welcome') {
        // Either unauthenticated (one-time code somehow not consumed -- handled
        // gracefully) or functionally empty (signed out + no workspaces).
        loadInitialContent('/welcome');
      } else if (startupRoute === 'create') {
        // Has accounts (or workspaces) but nothing to restore -- land on home.
        loadInitialContent('/');
      } else {
        // Restore saved windows with their positions and sizes. Each window's
        // titlebar accent is re-derived from its restored content URL by
        // ``navigateBundle`` (which ``openNewWindow`` calls too), and each URL is
        // routed to its surface (agent -> content view, local -> chrome view), so
        // no separately-persisted accent is needed.
        const [first, ...rest] = restorable;
        restoreWindowBounds(initialBundle, first);
        navigateBundle(initialBundle, toRestoredContentUrl(first));
        // Open the lesser-MRU windows without stealing focus, so the
        // MRU-zero window (already focused as initialBundle) stays focused
        // after restore completes.
        const restoredBundles = [];
        for (const entry of rest) {
          const bundle = openNewWindow(toRestoredContentUrl(entry), { showInactive: true });
          restoreWindowBounds(bundle, entry);
          restoredBundles.push(bundle);
        }
        // createBundle unshifts each new bundle to the front of mruWindows,
        // which reverses the saved order. Re-write the MRU list so
        // initialBundle stays MRU-zero and the restored windows follow in
        // their saved (i.e. previously most-recent-first) order, so the next
        // saveSessionState preserves recency across restarts.
        mruWindows.length = 0;
        mruWindows.push(initialBundle, ...restoredBundles);
        // showInactive() blocks keyboard focus but not z-order: on macOS the
        // restored windows still surface in front of initialBundle. Re-raise
        // initialBundle as each restored window appears so it stays on top.
        const raiseInitial = () => {
          if (initialBundle && !initialBundle.window.isDestroyed()) initialBundle.window.focus();
        };
        for (const bundle of restoredBundles) {
          if (bundle.window.isVisible()) raiseInitial();
          else bundle.window.once('show', raiseInitial);
        }
      }
    } else {
      // Retry path: re-load every existing window
      reloadAllWindowsAfterRetry();
    }

    // Startup navigation has settled (first start: the chosen route is
    // loading; retry: windows reloaded) -- deeplinks can now navigate
    // directly, and any URL queued while starting is applied. On the restore
    // route this deliberately navigates the focused window to the deeplink
    // target: an explicit link click wins over a restored page.
    flushPendingDeeplink();

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

// -- Deeplinks (minds://) --

// Single router for minds:// URLs regardless of how the OS delivered them
// (macOS ``open-url``, win/linux second-instance argv, or cold-start argv).
// Mirrors the notification-click flow below: focus the most recent window,
// then navigate it when the URL names a known action. The navigated path
// comes exclusively from ``deeplinkTargetPath``'s fixed allowlist -- raw
// deeplink text is never handed to navigation -- and ``navigateBundle``
// routes it to the right surface like any other in-app navigation.
function handleDeeplink(rawUrl) {
  console.log(`[deeplink] received: ${String(rawUrl).slice(0, 256)}`);
  const mru = getMostRecentWindow();
  if (!canApplyDeeplinks || !backendBaseUrl || (mru && (mru.isLoadingState || mru.isErrorState))) {
    // Startup, retry, or an error takeover in progress: queue and let
    // ``flushPendingDeeplink`` apply it after a successful start.
    pendingDeeplinkUrl = rawUrl;
    if (mru) focusBundle(mru);
    return;
  }
  if (!mru) return; // only reachable mid-quit; nothing to focus or navigate
  focusBundle(mru);
  const targetPath = deeplinkTargetPath(rawUrl);
  if (!targetPath) return; // bare/unknown/malformed minds:// -> focus only
  navigateBundle(mru, targetPath);
}

// Apply a deeplink queued during startup. Called once startup navigation has
// settled (first start: the chosen route is loading, including the first-run
// welcome route -- an explicit deeplink wins over the startup screen;
// retry: windows reloaded).
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
      if (mru) {
        focusBundle(mru);
        navigateBundle(mru, absolute);
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
// Loopback hosts the `mngr forward` proxy serves under its self-signed cert:
// the bare `localhost` origin, the `agent-<id>.localhost` workspace subdomains
// (`*.localhost`), and the `127.0.0.1` IP host. Every other host must fall
// through to Chromium's normal verification.
function isLoopbackHostname(hostname) {
  return hostname === 'localhost' || hostname.endsWith('.localhost') || hostname === '127.0.0.1';
}

// Trust the proxy's ephemeral self-signed cert for its loopback origins, in the
// workspace-content partition only. The proxy regenerates the cert every
// startup and only minds' own loopback origins use it, so there is no OS trust
// store or CA to install; every real https origin still gets Chromium's default
// verification (cb(-3)). Registered only when the proxy actually serves TLS.
function trustLoopbackCertsForWorkspaceContent() {
  const contentSession = session.fromPartition(CONTENT_PARTITION);
  contentSession.setCertificateVerifyProc((request, callback) => {
    if (isLoopbackHostname(request.hostname)) {
      callback(0); // trust
    } else {
      callback(-3); // defer to Chromium's default verification
    }
  });
  console.log('[startup] Trusting self-signed loopback certs for workspace content (HTTP/2 on)');
}

async function handleMngrForwardStarted(event) {
  const port = event.mngr_forward_port;
  const preauth = event.preauth_cookie;
  if (!port || !preauth) {
    console.warn('[startup] mngr_forward_started missing port or preauth_cookie:', event);
    return;
  }
  // The proxy serves TLS + HTTP/2, so the origin is https and the session
  // cookie must be Secure (a Secure cookie is only sent over https).
  const url = `https://localhost:${port}`;
  // Cache the plugin origin so workspaceUrlForAgent() can build /goto/ URLs
  // against the correct port (the plugin, not minds).
  mngrForwardBaseUrl = url;
  trustLoopbackCertsForWorkspaceContent();
  const baseSpec = {
    url,
    name: 'mngr_forward_session',
    value: preauth,
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    secure: true,
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
    if (backendBaseUrl) {
      const authUrl = `${backendBaseUrl}/auth/login?message=` +
        encodeURIComponent('You need to sign in to Imbue in order to share');
      navigateBundle(mru, authUrl);
    }
  }
}

// -- IPC handlers --

ipcMain.on('go-home', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !backendBaseUrl) return;
  // Home is a local page: navigateBundle renders it in the chrome view and hides
  // the agent content view.
  navigateBundle(bundle, backendBaseUrl + '/');
});

// OAuth sign-in finished in the external browser (which stole OS focus); the
// login page asks us to bring the Minds app to the front. Only do so if the
// window isn't already focused, so we never yank the user out of the app when
// they stayed put. Fires from the login page in either the content view (via
// the content-relay `minds:bring-app-to-front` message) or the sign-in modal
// overlay (via window.minds.bringAppToFront()); getBundleFromEvent resolves
// both.
ipcMain.on('bring-app-to-front', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || bundle.window.isDestroyed()) return;
  if (bundle.window.isFocused()) return;
  // On macOS, window.focus() alone can't pull a backgrounded app in front of
  // the frontmost app (the OAuth browser) -- the OS blocks focus-stealing.
  // app.focus({steal:true}) activates Minds over the browser; focusBundle then
  // raises and focuses the specific window within the app.
  if (isMac) app.focus({ steal: true });
  focusBundle(bundle);
});

ipcMain.on('navigate-content', (event, url) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle) return;
  navigateBundle(bundle, url);
});

// The contextual back arrow acts on whichever surface is currently showing: the
// content view while it displays agent content (currentWorkspaceId set), else
// the chrome view (which navigates full-page among trusted local pages). Back is
// a safety-net affordance, not load-bearing -- real navigation is always explicit.
function surfaceViewForHistory(bundle) {
  return bundle && bundle.currentWorkspaceId ? bundle.contentView : (bundle ? bundle.chromeView : null);
}

ipcMain.on('content-go-back', (event) => {
  const view = surfaceViewForHistory(getBundleFromEvent(event));
  if (view && !view.webContents.isDestroyed()) {
    view.webContents.goBack();
  }
});

ipcMain.on('toggle-sidebar', (event, anchor) => {
  toggleSidebar(getBundleFromEvent(event), anchor);
});

ipcMain.on('toggle-inbox', (event) => {
  toggleInbox(getBundleFromEvent(event));
});

ipcMain.on('toggle-help', (event, agentId, assistAvailable) => {
  toggleHelp(getBundleFromEvent(event), agentId, assistAvailable);
});

// One-shot bug report from the full-app error takeover (shell.html), used when the
// Python backend is down and its /help flow is unreachable. Reports the on-screen
// error via the always-initialized main-process Sentry -- with host basics, plus
// recent log files when the user opted in (the persistent include-logs setting, or
// the takeover's per-report ``includeLogs`` checkbox) -- since the backend's richer
// collector is gone. Returns the event id so the shell can show it. ``invoke`` (not
// ``send``) so the renderer gets the id back.
ipcMain.handle('report-error', (_event, opts) => {
  try {
    const eventId = captureManualReport({
      message: lastErrorTakeover ? lastErrorTakeover.message : null,
      details: lastErrorTakeover ? lastErrorTakeover.details : null,
      includeLogs: Boolean(opts && opts.includeLogs),
    });
    return { ok: Boolean(eventId), eventId: eventId || null };
  } catch (err) {
    console.error('[report-error] failed to capture manual report:', err && err.message);
    return { ok: false, eventId: null };
  }
});

// Lets the error takeover (shell.html) learn the persistent ``include_error_logs``
// setting so it only offers its per-report "Include recent logs" checkbox when the
// setting is off (when on, logs are always attached and the checkbox is redundant).
ipcMain.handle('get-log-inclusion-setting', () => {
  try {
    return isLogInclusionEnabled();
  } catch (err) {
    console.error('[get-log-inclusion-setting] failed to read setting:', err && err.message);
    return false;
  }
});

ipcMain.on('open-workspace-in-new-window', (event, agentId) => {
  if (!agentId) return;
  openOrFocusWorkspace(agentId, workspaceUrlForAgent(agentId));
  // The sidebar is the sender for both the hover-icon click and the native
  // context-menu "Open in new window" item; close it now that the action is done.
  const bundle = getBundleFromEvent(event);
  if (bundle) closeModal(bundle);
});

ipcMain.on('navigate-to-request', (event, _agentId, eventId) => {
  if (!eventId) return;
  // Open the inbox modal pre-selected on the target request. Keeps the user's
  // workspace exactly as they left it -- closing the inbox returns them to
  // their work with no context lost, and no window switching. The
  // sender-supplied agent id is deliberately ignored: the inbox is global.
  const sender = getBundleFromEvent(event);
  if (sender) openInbox(sender, '?selected=' + encodeURIComponent(eventId));
});

// Open the inbox modal pre-selected on a request on behalf of the (otherwise
// unprivileged) workspace content view. Only content-relay-preload.js can
// emit this channel -- the page itself never sees ipcRenderer -- and it does
// so only for an allowlisted `minds:open-request-modal` postMessage. We
// re-validate the id here (never trust the renderer) before building the
// `/inbox?selected=<id>` URL.
ipcMain.on('open-request-modal', (event, requestId) => {
  if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
  const sender = getBundleFromEvent(event);
  if (sender) openInbox(sender, '?selected=' + encodeURIComponent(requestId));
});

// Open the get-help / report-a-bug modal on behalf of the (otherwise
// unprivileged) content view -- used by error pages like the workspace-recovery
// page. Only content-relay-preload.js can emit this channel, and only for an
// allowlisted `minds:open-help` postMessage. The agent id is re-validated here
// (never trust the renderer) before being packed into the help URL.
ipcMain.on('open-help', (event, agentId) => {
  const scopedAgentId = typeof agentId === 'string' && /^agent-[a-f0-9]{1,64}$/i.test(agentId) ? agentId : '';
  openHelp(getBundleFromEvent(event), scopedAgentId);
});

// Reload the crashed content view on behalf of the crash page (crashed.html).
// Only content-relay-preload.js can emit this channel, and only for an
// allowlisted `minds:reload-crashed-view` postMessage. No payload -- the target
// URL is the shell's own record of the pre-crash workspace, never the renderer's.
ipcMain.on('reload-crashed-view', (event) => {
  reloadCrashedContentView(getBundleFromEvent(event));
});

// Open the workspace AI-key mint page (/settings/ai-keys) on behalf of the
// (otherwise unprivileged) content view -- used by the workspace's Claude
// sign-in modal ("Sign in with Imbue"). Only content-relay-preload.js can emit
// this channel, and only for an allowlisted `minds:open-ai-keys-page`
// postMessage. The mint page lives on the minds backend, whose origin (a
// random per-run port) the workspace page cannot know, so it opens in the
// sender's own window as an overlay modal. The host id is re-validated here
// (never trust the renderer) before being packed into the URL; an empty id
// still opens the page, which renders its own explanation.
ipcMain.on('open-ai-keys', (event, hostId) => {
  const workspaceHostId = typeof hostId === 'string' && /^host-[a-f0-9]{1,64}$/i.test(hostId) ? hostId : '';
  const bundle = getBundleFromEvent(event);
  if (!bundle || !backendBaseUrl) return;
  const query = workspaceHostId ? '?workspace=' + encodeURIComponent(workspaceHostId) : '';
  openModal(bundle, backendBaseUrl + '/settings/ai-keys' + query);
});

// Reload the crashed chrome (titlebar) view on behalf of chrome-crashed.html's
// Reload button. The chrome view runs the trusted first-party preload bridge, so
// this is a direct channel (no content-relay indirection). No payload -- the
// reload target is the shell's own /_chrome, never anything from the renderer.
ipcMain.on('reload-chrome', (event) => {
  reloadCrashedChromeView(getBundleFromEvent(event));
});

// Open the sign-in modal in the shared overlay. Senders are all trusted pages
// calling window.minds: local pages on the chrome surface (the create screen's
// signed-out "Create" press, the home screen's "Log in" launcher, the welcome
// splash) and overlay-hosted pages (the accounts modal's "Add account").
// ``returnTo`` is where a successful sign-in lands; it is validated here (never
// trust the renderer) and again by the server route.
ipcMain.on('open-signin-modal', (event, returnTo, mode) => {
  const sender = getBundleFromEvent(event);
  if (sender) {
    openSigninModal(sender, typeof returnTo === 'string' ? returnTo : '', mode === 'signin' ? 'signin' : '');
  }
});

// Open the centered Minds Settings / Manage Accounts modals. Senders are all
// trusted pages calling window.minds: local pages on the chrome surface (the
// home-screen launchers) and overlay-hosted pages (the workspace switcher's
// account entry). No payload; the URLs are fixed server routes.
ipcMain.on('open-minds-settings', (event) => {
  const sender = getBundleFromEvent(event);
  if (sender) openMindsSettingsModal(sender);
});

ipcMain.on('open-accounts', (event) => {
  const sender = getBundleFromEvent(event);
  if (sender) openAccountsModal(sender);
});

// Open the sharing-editor modal. The workspace-settings page -- a trusted local
// page on the chrome surface -- calls this via window.minds; openSharingModal
// re-validates both ids (never trust the renderer).
ipcMain.on('open-sharing-modal', (event, agentId, serviceName) => {
  const sender = getBundleFromEvent(event);
  if (sender) openSharingModal(sender, agentId, serviceName);
});

ipcMain.on('close-modal', (event) => {
  closeModal(getBundleFromEvent(event));
});

// The overlay host fires this once a hosted modal iframe (workspace menu /
// inbox / ...) has loaded and registered its window.minds listeners. We replay
// the cached chrome state into the overlay's frames so the just-loaded iframe
// paints its workspace list / request count immediately instead of waiting for
// the next SSE push. (Pre-migration, this priming happened on the modal view's
// own did-finish-load; now each hosted iframe signals when it's ready.)
ipcMain.on('overlay-modal-loaded', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle) return;
  // Modal diagnostics + stall failsafe: the iframe reported loaded, so the
  // overlay is painting -- cancel the force-close timer and record how long
  // the open-to-painted window was (the interval during which the invisible
  // full-window overlay view was eating every click).
  if (bundle.modalStallTimer) {
    clearTimeout(bundle.modalStallTimer);
    bundle.modalStallTimer = null;
  }
  if (bundle.modalOpenedAt !== null) {
    console.log(`[modal] loaded after ${Date.now() - bundle.modalOpenedAt}ms`);
    bundle.modalOpenedAt = null;
  }
  // Deferred show (see openModal): the hosted page has painted, so the
  // full-window overlay view can start capturing input without the app ever
  // presenting an invisible click-eating sheet. The titlebar drag-region drop
  // deferred with it lands now too.
  if (bundle.modalVisible && bundle.modalAwaitingLoad) {
    bundle.modalAwaitingLoad = false;
    if (bundle.modalView && !bundle.modalView.webContents.isDestroyed()) {
      bundle.modalView.setVisible(true);
    }
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      try {
        bundle.chromeView.webContents.send('modal-state-changed', { open: true });
      } catch { /* noop */ }
    }
  }
  primeOverlayFrames(bundle);
});

// The chrome view's document signals that its swap-local-page listener is
// registered (see chromeViewHasShell). Only honored from the chrome view: the
// same preload runs in the overlay host and its hosted iframes, which must not
// vouch for the chrome document.
ipcMain.on('shell-ready', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.chromeView || bundle.chromeView.webContents.isDestroyed()) return;
  if (event.sender !== bundle.chromeView.webContents) return;
  bundle.chromeShellReady = true;
});

// The shell acked receipt of a dispatched swap (sent before its fetch starts):
// re-arm the lost-swap fallback with a generous bound so a slow-but-alive swap
// fetch on a busy backend is not demoted to a full load by the short
// IPC-delivery grace period. Only honored from the chrome view for the swap
// currently pending.
ipcMain.on('swap-received', (event, url) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.chromeView || bundle.chromeView.webContents.isDestroyed()) return;
  if (event.sender !== bundle.chromeView.webContents) return;
  if (bundle.pendingSwapUrl !== url) return;
  armSwapFallbackTimer(bundle, url, 15000);
});

// Custom tooltip: a trigger (a titlebar button in the chrome view, or an element
// in a hosted modal page like the help dialog) sends its rect + label; forward
// it to the overlay host to render. When NO modal is open the host measures the
// bubble and reports a small rect so the overlay view shrinks to it (the rest of
// the window stays interactive). When a modal IS open the overlay view is already
// full-window and capturing, so ``inModal`` tells the host to render the bubble
// in-page above the modal iframe (above everything via z-index) without a bounds
// change. Titlebar tooltips can't fire while a modal is open (the modal covers
// the titlebar), so this only enables modal-internal tooltips.
ipcMain.on('show-tooltip', (event, payload) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.modalView || bundle.modalView.webContents.isDestroyed()) return;
  if (!payload || typeof payload !== 'object' || !payload.rect) return;
  // Pass the real window size: the overlay host can't trust its own
  // window.innerWidth for measuring/positioning, because between tooltips the
  // view is hidden and a hidden WebContentsView doesn't update innerWidth when
  // main resizes it -- so it can be stale (a previous tooltip's small rect).
  const cb = bundle.window.getContentBounds();
  try {
    bundle.modalView.webContents.send('overlay-command', {
      type: 'show-tooltip',
      rect: payload.rect,
      text: typeof payload.text === 'string' ? payload.text : '',
      shortcut: typeof payload.shortcut === 'string' ? payload.shortcut : '',
      html: typeof payload.html === 'string' ? payload.html : null,
      windowWidth: cb.width,
      windowHeight: cb.height,
      inModal: !!bundle.modalVisible,
    });
  } catch { /* noop */ }
});

ipcMain.on('hide-tooltip', (event) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.modalView || bundle.modalView.webContents.isDestroyed()) return;
  try {
    bundle.modalView.webContents.send('overlay-command', { type: 'hide-tooltip' });
  } catch { /* noop */ }
});

// The overlay host reports the overlay view's required bounds (it is the only
// authority on its size, since Electron 40 has no per-view click-through). A
// tooltip reports a small rect so the rest of the window stays interactive;
// 'hidden' restores the full-window (hidden) bounds so the next tooltip can be
// measured. Modals are full-window and own their own visibility, so tooltip
// bounds are ignored while a modal is open.
ipcMain.on('overlay-set-bounds', (event, spec) => {
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.modalView || bundle.modalView.webContents.isDestroyed()) return;
  if (!spec || typeof spec !== 'object') return;
  if (bundle.modalVisible) return;
  if (spec.mode === 'rect' && spec.rect) {
    const r = spec.rect;
    bundle.tooltipVisible = true;
    // Raise above the content view (it may have been re-added on a crash
    // rebuild) and size to the tooltip's rect.
    bundle.window.contentView.removeChildView(bundle.modalView);
    bundle.window.contentView.addChildView(bundle.modalView);
    bundle.modalView.setBounds({
      x: Math.round(r.x),
      y: Math.round(r.y),
      width: Math.max(1, Math.round(r.width)),
      height: Math.max(1, Math.round(r.height)),
    });
    bundle.modalView.setVisible(true);
  } else {
    bundle.tooltipVisible = false;
    bundle.modalView.setVisible(false);
    // Restore the full-window (hidden) bounds for the next measurement.
    updateBundleBounds(bundle);
  }
});

// Settings-page color picker: optimistic chrome-titlebar paint for the
// bundle the picker is in, so the user sees the new color immediately
// without waiting for the PATCH -> mngr label subprocess -> SSE
// round-trip. The actual persistence still goes through the
// PATCH /api/v1/workspaces/<id> endpoint (color field); this just
// shortcuts the local-window UI feedback. The workspace-settings page -- a
// trusted local page on the chrome surface -- calls this via window.minds; we
// re-validate the agent id + accent shape here defensively and only forward to
// the *sending bundle's* chrome view so a stray sender can't paint another
// window's titlebar.
ipcMain.on('preview-workspace-accent', (event, agentId, accent) => {
  if (typeof agentId !== 'string' || !/^agent-[a-f0-9]{1,64}$/i.test(agentId)) return;
  if (typeof accent !== 'string' || !/^#[0-9a-f]{6}$/.test(accent)) return;
  const bundle = getBundleFromEvent(event);
  if (!bundle || !bundle.chromeView || bundle.chromeView.webContents.isDestroyed()) return;
  bundle.chromeView.webContents.send('chrome-event', {
    type: 'workspace_accent_preview',
    agent_id: agentId,
    accent,
  });
});

// Native file/directory picker for the file-sharing permission dialog.
// The inbox modal calls this (via the preload bridge) so the user can
// pick the path to share. ``options.mode`` is 'file' or 'directory':
// the dialog exposes separate "Choose file" / "Choose folder" buttons
// rather than a single combined picker because a dialog can't be both a
// file and a directory selector on Linux/Windows (Electron would fall
// back to a directory selector, so picking a file there returns its
// parent directory). Returns the chosen absolute path, or null when the
// user cancelled.
ipcMain.handle('show-file-picker', async (event, options) => {
  const bundle = getBundleFromEvent(event);
  const opts = options || {};
  const property = opts.mode === 'directory' ? 'openDirectory' : 'openFile';
  const dialogOptions = { properties: [property] };
  if (typeof opts.defaultPath === 'string' && opts.defaultPath.length > 0) {
    dialogOptions.defaultPath = opts.defaultPath;
  }
  // Anchor the dialog to the requesting window when we can resolve it so
  // it behaves as a sheet/modal; fall back to an unparented dialog
  // otherwise.
  const result = bundle && bundle.window && !bundle.window.isDestroyed()
    ? await dialog.showOpenDialog(bundle.window, dialogOptions)
    : await dialog.showOpenDialog(dialogOptions);
  if (result.canceled || !Array.isArray(result.filePaths) || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
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
        closeModal(bundle);
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
      closeModal(bundle);
      await postRestart(agentId, 'services');
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
      closeModal(bundle);
      await postRestart(agentId, 'host');
      goToRecoveryView();
    },
  });
  const menu = Menu.buildFromTemplate(template);
  // The sidebar runs inside modalView, which covers the full window content
  // area (x: 0, y: 0). e.clientX / e.clientY from sidebar.js's contextmenu
  // handler are therefore already in window-content coordinates, which is
  // what menu.popup({ window, x, y }) expects -- no offset needed.
  const px = Math.round(x || 0);
  const py = Math.round(y || 0);
  menu.popup({ window: bundle.window, x: px, y: py });
});

ipcMain.on('retry', async (event) => {
  // User clicked retry from one window's error screen. Shut down the old
  // backend (if any), put all windows back in loading state, then restart.
  const senderBundle = getBundleFromEvent(event);
  if (senderBundle) focusBundle(senderBundle);
  // Allow a fresh discovery-pipeline "blocked" takeover to surface again if the
  // restarted backend ends up stalled too.
  discoveryBlockedShown = false;
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

// Landing-page Stop button: show a native confirmation, then issue the host
// stop ourselves. The SSE drives the row from running -> stopped once it lands.
// The Landing page -- a trusted local page on the chrome surface -- calls this
// via window.minds; we re-validate the id here against the conservative agent-id
// shape (never trust the renderer).
ipcMain.on('confirm-stop-mind', async (event, agentId, name) => {
  if (typeof agentId !== 'string' || !/^agent-[a-f0-9]{1,64}$/i.test(agentId)) return;
  const bundle = getBundleFromEvent(event) || getMostRecentWindow();
  const parentWindow = bundle && !bundle.window.isDestroyed() ? bundle.window : null;
  const options = {
    type: 'warning',
    buttons: ['Cancel', 'Stop mind'],
    defaultId: 0,
    cancelId: 0,
    message: `Stop "${name || agentId}"?`,
    detail: 'Its agents will stop and its services become inaccessible. '
      + 'Your data is preserved and you can start it again.',
  };
  const { response } = parentWindow
    ? await dialog.showMessageBox(parentWindow, options)
    : await dialog.showMessageBox(options);
  if (response !== 1) return;
  const ok = await postMindStop(agentId);
  if (!ok) {
    console.warn(`[mind-shutdown] single-row stop for ${agentId} did not succeed`);
    return;
  }
  // The stop succeeded, so the mind's container is down. Any other window still
  // open to this mind would observe the now-unreachable system interface, get
  // redirected to the recovery page, and auto-restart the host -- silently
  // undoing the stop. Close that window now. Skip it if the mind is mid-restart
  // (the user is intentionally restarting it in that window).
  if (systemInterfaceStatusByAgent.get(agentId) === 'restarting') return;
  detachWindowsForWorkspace(agentId);
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

// Guards for the quit sequence. ``isQuitSequenceRunning`` prevents the prompt
// from firing twice (before-quit + window-all-closed can both arrive).
// ``isHeadlessQuit`` is set by signal handlers so a programmatic shutdown
// (e.g. ``just minds-stop``) never shows an interactive dialog.
let isQuitSequenceRunning = false;
let isHeadlessQuit = false;

// Single async chokepoint for quitting. The order is deliberate:
//   1. Ask (first native prompt) whether to shut down running local minds.
//      Cancelling here leaves the app fully intact -- no window has changed yet.
//   2. Once the user has committed, flip every window to the quitting page so
//      the teardown delay shows a clear "quitting" state instead of frozen UI.
//   3. If they chose "Shut down all", stop the local minds with progress rendered
//      on that page. Backing out there (its native "Cancel quit") restores the
//      windows and leaves the app running.
//   4. Tear the backend down and quit.
async function runQuitSequence() {
  if (isShuttingDown || isQuitSequenceRunning) return;
  isQuitSequenceRunning = true;

  let plan = { proceed: true, stop: false, running: [] };
  try {
    if (!isHeadlessQuit) {
      plan = await promptMindShutdown();
      if (!plan.proceed) {
        // User cancelled before committing -- stay open, no visual change.
        isQuitSequenceRunning = false;
        return;
      }
    }
  } catch (err) {
    // A failure deciding the prompt must not strand the user unable to quit;
    // fall through to a normal shutdown.
    console.warn('[lifecycle] local-mind shutdown prompt failed, quitting anyway:', err);
    plan = { proceed: true, stop: false, running: [] };
  }

  // The quit is now committed. Flip every open window to the quitting page
  // (a no-op for a headless quit, which has no interactive UI to update), then
  // snapshot session state before teardown (the per-window `close` handler
  // skips saving once isShuttingDown is set).
  isShuttingDown = true;
  // Drop any pending debounced save: the windows are still alive here, so this
  // save is the authoritative pre-teardown snapshot. A later debounced write
  // would only race the teardown (the empty-clobber guard would reject it, but
  // cancelling is cleaner).
  debouncedSessionSaver.cancel();
  if (!isHeadlessQuit) showQuittingInAllWindows();
  if (bundles.size > 0) saveSessionState();

  if (plan.stop && plan.running.length > 0) {
    let shouldProceed = true;
    try {
      shouldProceed = await stopAllMindsThenDecide(plan.running);
    } catch (err) {
      // A failure in the stop loop must not strand the user; quit anyway.
      console.warn('[lifecycle] stopping local minds failed, quitting anyway:', err);
    }
    if (!shouldProceed) {
      // User backed out via "Cancel quit" after committing -- return the app to
      // its normal, running state. Clear isShuttingDown FIRST so the restarted
      // chrome-events SSE loop (which guards on `while (!isShuttingDown)`) does
      // not immediately exit again; the loop can die during the stop window if
      // its connection happened to drop while isShuttingDown was set.
      isShuttingDown = false;
      restoreFromQuittingInAllWindows();
      ensureChromeSSELoopRunning();
      isQuitSequenceRunning = false;
      return;
    }
  }

  updateQuittingStatus('Closing…');
  await shutdown();
  app.quit();
}

// Route POSIX SIGTERM / SIGINT through the quit sequence so they trigger the
// same `backend.shutdown()` chain that window-close uses (SIGTERMing the python
// backend and waiting for its graceful exit). Without these handlers
// Node's default for these signals is to exit immediately, which orphans the
// python backend and the `mngr forward` / `observe` subprocesses. The `just
// minds-stop` recipe sends SIGTERM here; we mark it headless so it shuts down
// without showing an interactive local-mind prompt.
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    console.log(`[lifecycle] ${signal} received, requesting quit`);
    isHeadlessQuit = true;
    app.quit();
  });
}

app.on('window-all-closed', () => {
  console.log('[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  if (isShuttingDown || isQuitSequenceRunning) return;
  runQuitSequence();
});

app.on('before-quit', (event) => {
  console.log('[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
  // Once the quit sequence has committed (isShuttingDown), let the final
  // app.quit() proceed untouched. Otherwise intercept: defer the actual quit
  // until the local-mind prompt + teardown finish (or the user cancels).
  if (isShuttingDown) return;
  event.preventDefault();
  runQuitSequence();
});
