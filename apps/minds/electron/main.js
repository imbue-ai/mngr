const { BaseWindow, WebContentsView, Menu, Notification, clipboard, dialog, ipcMain, net, shell, app, session, screen } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { initSentry, captureManualReport, isLogInclusionEnabled } = require('./sentry');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');
const { decideStartupRoute } = require('./startup-routing');

// Initialize Sentry as early as possible so errors thrown during main-process
// startup (window creation, env setup, backend spawn) are captured. The SDK is
// always initialized but only sends when the user has enabled error reporting
// (the report_unexpected_errors setting, read live per event) -- see
// electron/sentry.js.
initSentry();

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

// Wider than ``parseWorkspaceId`` -- also recognises the workspace-scoped
// minds-backend routes (``/workspace/<id>/settings``, ``/workspace/<id>/
// associate``, ``/sharing/<id>/<service>``, ``/destroying/<id>``,
// ``/agents/<id>/recovery``). Used ONLY to decide which workspace's
// accent should tint the titlebar; deliberately not fed into
// ``bundle.currentWorkspaceId`` / ``findBundleForWorkspace`` because
// those drive workspace uniqueness, and we want the user to be able to
// open ``/workspace/X/settings`` in one window while another window
// holds the actual workspace X.
//
// Returns null for every non-workspace minds screen (Home, Create,
// accounts, inbox, auth, ...). That null is load-bearing: the
// navigation handlers feed it straight into
// ``updateBundleAccentAgentId``, so leaving a workspace-scoped
// screen clears the accent back to the neutral chrome rather than
// stranding the previous workspace's color on a general screen.
function parseAccentSourceAgentId(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const hostMatch = parsed.hostname.match(/^(agent-[a-f0-9]+)\.localhost$/i);
    if (hostMatch) return hostMatch[1];
    const pathMatch =
      parsed.pathname.match(/^\/(?:goto|workspace|sharing)\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      parsed.pathname.match(/^\/destroying\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      parsed.pathname.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
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
    const views = [b.chromeView, b.contentView, b.modalView];
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
  const title = computeTitleFor(bundle);
  bundle.window.setTitle(title);
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    bundle.chromeView.webContents.send('window-title-changed', title);
  }
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
    } else {
      b.currentWorkspaceId = null;
      if (b.contentView && !b.contentView.webContents.isDestroyed() && backendBaseUrl) {
        b.contentView.webContents.loadURL(backendBaseUrl + '/');
      }
      updateOsTitle(b);
      // Notify the chrome renderer that this window is no longer displaying a
      // workspace, so its recovery-redirect lock (`currentTitleAgentId`)
      // resets. The did-navigate handler that fires after the loadURL above
      // would NOT send this IPC: its diff-guard (`bundle.currentWorkspaceId !==
      // newAgentId`) sees null !== null and skips. The titlebar accent is
      // handled separately -- the workspace-destroyed handler clears it
      // explicitly and the loadURL('/') above re-derives it via its own
      // navigation -- so the bar falls back to the neutral chrome regardless.
      sendCurrentWorkspaceToBundleViews(b);
    }
  }
}

// -- Layout --

function updateBundleBounds(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const { width, height } = bundle.window.getContentBounds();

  if (bundle.isErrorState || bundle.isLoadingState || bundle.isQuittingState) {
    // The chrome view takes over the whole window; every other view collapses
    // to zero so the takeover screen (shell.html) is the only thing visible.
    // For loading/error the auxiliary views are already absent, so the loop is
    // a no-op there; the quitting flip leaves them present-but-hidden, so this
    // guarantees none of them peek out from behind the full-window chrome view.
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.setBounds({ x: 0, y: 0, width, height });
    }
    for (const view of [bundle.contentView, bundle.modalView]) {
      if (view && !view.webContents.isDestroyed()) {
        view.setBounds({ x: 0, y: 0, width: 0, height: 0 });
      }
    }
    return;
  }

  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    // The chromeView covers the entire window. Its body background is
    // ``var(--titlebar-bg)``, so wherever the contentView doesn't paint
    // (the ``CONTENT_INSET``-wide frame around three sides and the
    // rounded-corner cutouts at all four corners of the contentView),
    // the chromeView fills in with the current workspace's accent
    // color. This mirrors browser mode where the iframe sits inside a
    // matching body inset that paints the same accent color.
    bundle.chromeView.setBounds({ x: 0, y: 0, width, height });
  }
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    // Inset the contentView by ``CONTENT_INSET`` on left, right, and
    // bottom (top is flush with the titlebar's bottom edge). That gap
    // plus ``setBorderRadius(CONTENT_CORNER_RADIUS)`` (applied in
    // ``createBundleWebContentsViews``) together create the "tucks
    // under a rounded inset frame" look without needing per-corner
    // control: all four corners of the contentView are visibly rounded
    // and the cutouts reveal accent color (the chromeView below),
    // independent of whatever background the workspace content paints.
    bundle.contentView.setBounds({
      x: CONTENT_INSET,
      y: TITLEBAR_HEIGHT,
      width: width - CONTENT_INSET * 2,
      height: height - TITLEBAR_HEIGHT - CONTENT_INSET,
    });
  }
  // The modal overlays the entire window (including the title bar) so
  // the inbox drawer reads as a top-level panel rather than something
  // nested under the chrome. On macOS the OS-level traffic-light
  // buttons stay visible (they're floating overlays the system draws);
  // the in-content window controls used on Windows/Linux are hidden
  // while the modal is open and reappear on close. The view is
  // transparent, so the dialog's own dim backdrop shows the workspace
  // behind it.
  if (bundle.modalView && !bundle.modalView.webContents.isDestroyed()) {
    bundle.modalView.setBounds({
      x: 0,
      y: 0,
      width,
      height,
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
  win.contentView.addChildView(chromeView);
  win.contentView.addChildView(contentView);

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
  win.on('resize', () => updateBundleBounds(bundle));

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
    inboxListReloadTimer: null,
    currentContentUrl: null,
    currentWorkspaceId: null,
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
    updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(url));
    updateOsTitle(bundle);
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
      bundle.chromeView.webContents.send('content-url-changed', url);
    }
    // The sidebar (now hosted in the shared modalView) refreshes its
    // "Manage account(s)" / "Log in" label on every content URL change so
    // a sign-in / sign-out performed in the workspace iframe propagates
    // to the menu the next time the user opens it. Inbox doesn't subscribe
    // to this channel so the send is a no-op when the modal is showing it.
    if (bundle.modalView && !bundle.modalView.webContents.isDestroyed()) {
      bundle.modalView.webContents.send('content-url-changed', url);
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

// -- Sidebar helpers (per-bundle) --
//
// The sidebar is just the modal overlay loaded with /_chrome/sidebar -- it
// shares ``modalView``, the same lazy-creation + transparent background +
// Escape handling + ``modal-state-changed`` titlebar-drag suppression as
// the inbox. There is no separate sidebar WebContentsView.

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

// -- Modal overlay (per-bundle) --
//
// The modal is a full-content-area overlay that hosts the inbox modal and
// any one-off dialog pages. It does not replace the user's workspace in
// the content view -- the workspace stays visible behind the dialog's dim
// backdrop. Created lazily and reused via setVisible(true/false). It uses
// the default session (so it carries the auth cookie, like the chrome
// view) plus the preload bridge, so the page inside can call
// `window.minds.closeModal()`.

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
    // Each new URL load (sidebar, inbox, ...) gets the cached chrome state
    // and the current workspace id pushed before it can fall behind. The
    // inbox renders its initial list server-side, but the sidebar reuses
    // the SSE-driven ``workspaces`` event for first paint, so without this
    // prime an existing workspace list would only appear after the next SSE
    // push arrives.
    modal.webContents.on('did-finish-load', () => {
      if (modal.webContents.isDestroyed()) return;
      if (modal.webContents.getURL() === 'about:blank') return;
      sendCurrentWorkspaceToBundleViews(bundle);
      primeViewWithCachedChromeState(bundle, modal.webContents);
    });
    // Auto-open DevTools for dev-time inspection. Matches the
    // contentView behavior in createBundleWebContentsViews; gated on
    // the same env var so a single switch covers both surfaces.
    if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
      modal.webContents.once('did-finish-load', () => {
        if (!modal.webContents.isDestroyed()) {
          modal.webContents.openDevTools({ mode: 'detach' });
        }
      });
    }
  } else {
    // Re-add to the parent to raise to the top of z-order, then make visible.
    bundle.window.contentView.removeChildView(bundle.modalView);
    bundle.window.contentView.addChildView(bundle.modalView);
    bundle.modalView.setVisible(true);
  }
  bundle.modalVisible = true;
  bundle.modalUrl = url;
  // Notify the chrome view that the modal is open so it can drop the
  // ``-webkit-app-region: drag`` on #minds-titlebar. macOS unions drag
  // regions across all visible views in a window, so the chrome
  // titlebar's drag rule otherwise wins over the modal's no-drag in
  // the y=0..TITLEBAR strip and intercepts clicks (e.g. the inbox X).
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    try {
      bundle.chromeView.webContents.send('modal-state-changed', { open: true });
    } catch { /* noop */ }
  }
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
  bundle.modalUrl = null;
  // Restore the chrome titlebar's drag region.
  if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed()) {
    try {
      bundle.chromeView.webContents.send('modal-state-changed', { open: false });
    } catch { /* noop */ }
  }
  // Drop the page so its websockets/timers stop and a stale dialog isn't
  // briefly visible the next time the modal opens.
  if (!bundle.modalView.webContents.isDestroyed()) {
    bundle.modalView.webContents.loadURL('about:blank').catch(() => {});
  }
  if (bundle.inboxListReloadTimer) {
    clearTimeout(bundle.inboxListReloadTimer);
    bundle.inboxListReloadTimer = null;
  }
}

function inboxUrlFor(query) {
  if (!backendBaseUrl) return null;
  return backendBaseUrl + '/inbox' + (query || '');
}

function signinModalUrlFor() {
  if (!backendBaseUrl) return null;
  return backendBaseUrl + '/auth/signin-modal';
}

function openSigninModal(bundle) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = signinModalUrlFor();
  if (!url) return;
  openModal(bundle, url);
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
  else openInbox(bundle, '');
}

// -- Get-help modal (per-bundle) --
//
// The help modal shares the same modalView overlay as the inbox and sidebar (see
// openModal): it just loads the backend's /help page. ``agentId`` (the
// currently-displayed workspace, or falsy on a general screen) is forwarded as a
// ?workspace= query so the help page can scope its bug report to that workspace.

function helpUrlFor(agentId) {
  if (!backendBaseUrl) return null;
  const query = agentId ? '?workspace=' + encodeURIComponent(agentId) : '';
  return backendBaseUrl + '/help' + query;
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

function openHelp(bundle, agentId) {
  if (!bundle || bundle.window.isDestroyed()) return;
  const url = helpUrlFor(agentId);
  if (!url) return;
  openModal(bundle, url);
}

function toggleHelp(bundle, agentId) {
  if (!bundle || bundle.window.isDestroyed()) return;
  if (isHelpModalOpen(bundle)) closeModal(bundle);
  else openHelp(bundle, agentId);
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
    try {
      bundle.modalView.webContents.send('chrome-event', evt);
    } catch { /* noop */ }
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
  for (const view of [bundle.chromeView, bundle.modalView]) {
    if (!view || view.webContents.isDestroyed()) continue;
    view.webContents.send('current-workspace-changed', bundle.currentWorkspaceId);
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
  // Stamp the accent source from the URL (the wider
  // ``parseAccentSourceAgentId`` set), so workspace-scoped settings / sharing
  // routes -- and restored windows -- paint the bar before the first
  // did-navigate lands (no neutral flash). A null result (a blank Home window,
  // say) clears the accent to the neutral chrome; passed through
  // unconditionally, matching ``onContentNavigate``.
  updateBundleAccentAgentId(bundle, parseAccentSourceAgentId(url));
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

function openNewWindow(url, { showInactive = false } = {}) {
  const bundle = createBundle();
  if (showInactive) bundle.showInactiveOnFirstShow = true;
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
    if (bundle.chromeView && !bundle.chromeView.webContents.isDestroyed() && backendBaseUrl) {
      bundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
    }
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
      const relative = toRelativeBackendUrl(url);
      if (!relative) continue;
      const bounds = b.window.getBounds();
      const display = screen.getDisplayMatching(bounds);
      windows.push({
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
    fs.writeFileSync(p, JSON.stringify({ windows }, null, 2));
  } catch (err) {
    console.log('[session] Failed to save state:', err.message);
  }
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
  } else if (evt.type === 'discovery_health') {
    // App-global discovery-pipeline health. Only the terminal `blocked` state is
    // ever sent (the reconnecting tier heals silently in the background). The
    // watchdog has exhausted its self-healing -- or the consumer died -- so agent
    // forwarding is down / the app is unusable. Take over every window with the
    // error screen; its Retry button runs the existing restart path (shut down +
    // restart the backend, respawning the discovery producer + consumer).
    if (evt.state === 'blocked' && !discoveryBlockedShown) {
      discoveryBlockedShown = true;
      showErrorInAllWindows(
        "Minds has disconnected from your workspaces and can't automatically reconnect. Restart the app to recover. Your data has not been lost.",
        null,
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
    // Push to the chrome titlebar and to any open modal (sidebar, inbox).
    // The inbox shell uses these events too (e.g. ``requests`` count); the
    // sidebar uses ``workspaces`` / ``auth_status`` to render its list.
    for (const view of [b.chromeView, b.modalView]) {
      if (!view) continue;
      if (view.webContents.isDestroyed()) continue;
      try {
        view.webContents.send('chrome-event', evt);
      } catch { /* noop */ }
    }
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

// ---------- Mind shutdown on quit + landing Stop button ----------

// Timeout for the instant, in-memory liveness lookup (GET /api/minds/running).
const MIND_HTTP_TIMEOUT_MS = 10000;
// Timeout for the synchronous command endpoints (single/bulk host stop, state
// container stop). These block until the underlying ``mngr``/docker command
// finishes; the server's own per-command ceiling is ~120s, so allow margin
// above it here so the server-side failure surfaces rather than a client abort.
const MIND_COMMAND_TIMEOUT_MS = 150000;

// GET /api/minds/running -> { ok, running }. ``running`` is an array of
// {id, name}; ``ok`` is false when the check itself failed (network, parse, no
// backend) so the caller can distinguish "nothing running" from "couldn't tell"
// instead of silently treating a failed check as an empty list.
function getRunningMinds() {
  return new Promise((resolve) => {
    if (!backendBaseUrl) {
      console.warn('[mind-shutdown] no backend URL; cannot list running minds');
      resolve({ ok: false, running: [] });
      return;
    }
    let req;
    try {
      req = net.request({ url: backendBaseUrl + '/api/minds/running', method: 'GET', useSessionCookies: true });
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

// POST /api/agents/<id>/stop-host (synchronous). Resolves true when the server
// reports the stop succeeded (<400), false otherwise. Used by the single-row
// landing Stop relay.
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
        url: `${backendBaseUrl}/api/agents/${encodeURIComponent(agentId)}/stop-host`,
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

// POST /api/minds/stop-hosts?agent_id=...&agent_id=... (synchronous). Issues ONE
// ``mngr stop <ids...> --stop-host`` server-side (mngr stops the hosts
// concurrently). Resolves { ok, stillRunning }: ``stillRunning`` is the subset
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
      req = net.request({ url: `${backendBaseUrl}/api/minds/stop-hosts?${query}`, method: 'POST', useSessionCookies: true });
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

// POST /api/minds/stop-state-container -- stops this env's mngr docker "state
// container" (provider bookkeeping) so nothing minds-related is left running
// after a full shutdown. Best-effort: resolves regardless of outcome, but logs.
function postStopStateContainer() {
  return new Promise((resolve) => {
    if (!backendBaseUrl) {
      console.warn('[mind-shutdown] no backend URL; cannot stop state container');
      resolve();
      return;
    }
    let req;
    try {
      req = net.request({ url: backendBaseUrl + '/api/minds/stop-state-container', method: 'POST', useSessionCookies: true });
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

function fetchInitialChromeState(timeoutMs = 10000) {
  // Drives one round-trip to /_chrome/events (SSE) to learn both auth status
  // and the current workspace list. Returns:
  //   { authenticated: true, workspaces: [...] }  on authenticated success
  //   { authenticated: false }                     when the backend says auth_required
  //   null                                          on timeout / network error
  //
  // The timeout must comfortably exceed the connect-time snapshot's slowest
  // blocking step: the backend computes ``has_accounts`` (a cold ``mngr
  // imbue_cloud auth list`` subprocess, ~5s on first call) before emitting the
  // first ``workspaces`` event. A timeout shorter than that returned ``null``,
  // which the startup path treats as unauthenticated and routes to /welcome --
  // bouncing an already-signed-in user to the onboarding page.
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
  // the app ever creates (all three bundle views -- chrome, content, modal --
  // plus any popup windows), rather than wiring each view individually.
  // Registered before the first bundle is created so it covers the initial
  // chrome/content views too.
  app.on('web-contents-created', (_event, contents) => {
    applyExternalLinkHandling(contents);
  });
  installApplicationMenu();
  installDockMenu();
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

      const knownAgentIdsSet = authenticated
        ? new Set(workspaceList.map((w) => w.id))
        : null;
      const restorable = authenticated
        ? filterRestorableUrls(savedState.windows, knownAgentIdsSet)
        : [];
      // (A workspace that no longer exists needs no special accent handling
      // here: ``filterRestorableUrls`` already drops windows whose saved URL
      // points at a destroyed workspace, and the accent is re-derived from
      // whatever URL each restored window actually reopens to.)

      initialBundle.isLoadingState = false;
      updateBundleBounds(initialBundle);
      if (initialBundle.chromeView && !initialBundle.chromeView.webContents.isDestroyed()) {
        initialBundle.chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
      }

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

      const loadInitialContent = (relativePath) => {
        if (initialBundle.contentView && !initialBundle.contentView.webContents.isDestroyed()) {
          initialBundle.contentView.webContents.loadURL(backendBaseUrl + relativePath);
        }
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
        // ``loadUrlIntoBundleContentView`` (which ``openNewWindow`` calls too),
        // so no separately-persisted accent is needed.
        const [first, ...rest] = restorable;
        restoreWindowBounds(initialBundle, first);
        loadUrlIntoBundleContentView(initialBundle, toAbsoluteUrl(first.url));
        // Open the lesser-MRU windows without stealing focus, so the
        // MRU-zero window (already focused as initialBundle) stays focused
        // after restore completes.
        const restoredBundles = [];
        for (const entry of rest) {
          const bundle = openNewWindow(toAbsoluteUrl(entry.url), { showInactive: true });
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
      closeModal(bundle);
      return;
    }
  }

  // Nobody is on this workspace (or it's a non-workspace URL): navigate sender
  if (bundle.contentView && !bundle.contentView.webContents.isDestroyed()) {
    bundle.contentView.webContents.loadURL(absolute);
  }
  closeModal(bundle);
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

ipcMain.on('toggle-sidebar', (event, anchor) => {
  toggleSidebar(getBundleFromEvent(event), anchor);
});

ipcMain.on('toggle-inbox', (event) => {
  toggleInbox(getBundleFromEvent(event));
});

ipcMain.on('toggle-help', (event, agentId) => {
  toggleHelp(getBundleFromEvent(event), agentId);
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
  // their work with no context lost, and no window switching.
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

// Open the sign-in modal in the shared overlay on behalf of the (otherwise
// unprivileged) workspace content view -- the create screen posts an
// allowlisted `minds:open-signin-modal` when a signed-out user presses
// "Create" with the Imbue Cloud preset selected. No payload to validate; the
// URL is a fixed server route.
ipcMain.on('open-signin-modal', (event) => {
  const sender = getBundleFromEvent(event);
  if (sender) openSigninModal(sender);
});

ipcMain.on('close-modal', (event) => {
  closeModal(getBundleFromEvent(event));
});

// Settings-page color picker: optimistic chrome-titlebar paint for the
// bundle the picker is in, so the user sees the new color immediately
// without waiting for the POST -> mngr label subprocess -> SSE
// round-trip. The actual persistence still goes through the
// /api/workspaces/<id>/color POST endpoint; this just shortcuts the
// local-window UI feedback. Only content-relay-preload.js can emit
// this channel, and it validates the agent id + accent shape there;
// we re-validate here defensively and only forward to the *sending
// bundle's* chrome view so a stray sender can't paint another
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
      closeModal(bundle);
      await postRestart(agentId, 'restart-host');
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
// Only content-relay-preload.js can emit this channel (for an allowlisted
// `minds:confirm-stop-mind` postMessage); we re-validate the id here against the
// same conservative agent-id shape (never trust the renderer).
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
