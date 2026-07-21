// Renderer-contract regression test for the workspace-recovery auto-redirect.
//
// Drives the REAL chrome.js (apps/.../desktop_client/static/chrome.js) in a
// plain browser page -- no Electron app, no Docker, no backend -- feeding it
// events through the exact ``window.minds`` bridge surface that Electron's
// main.js drives. This locks in the contract the recovery fix depends on:
//
//   chrome.js redirects the content view to the recovery page IFF it is
//   holding a ``stuck`` system-interface status for the currently-displayed
//   workspace.
//
// The bug (stuck workspace stranded on the plugin's "Loading workspace"
// loader, never reaching the recovery page) was that a reloaded chrome
// renderer lost its one-shot ``system_interface_status`` and main.js's
// ``primeViewWithCachedChromeState`` never replayed it. The fix makes the
// prime replay non-healthy statuses; scenario 3 below mirrors exactly what the
// fixed prime now sends. See main.js primeViewWithCachedChromeState and
// app.py's periodic re-assert backstop.

const path = require('path');
const { test, expect } = require('@playwright/test');

const CHROME_JS_PATH = path.join(
  __dirname,
  '..',
  '..',
  'imbue',
  'minds',
  'desktop_client',
  'static',
  'chrome.js',
);
// The compiled mithril bundle chrome.js expects (window.MindsUI: titlebar +
// menu components) -- loaded before chrome.js, as in the production shell.
const CHROME_BUNDLE_PATH = path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'static', 'dist', 'chrome.bundle.js');

const AGENT_ID = 'agent-bb340d1c1d5a4c43b1396f277cfd6d81';

// Minimal DOM + a faithful stub of the Electron preload bridge. chrome.js wires
// these ids/callbacks at init; the test fires the captured callbacks exactly as
// main.js's broadcastChromeEvent / current-workspace-changed IPC do. Captured
// navigateContent calls land in window.__nav.
// The titlebar interior is the mithril TitleBar component (mounted into the
// #minds-titlebar bar by chrome.js), so the harness DOM carries only the bar
// container + the shell elements chrome.js touches. Every bridge callback
// registry is an ARRAY fired in order -- the component and chrome.js both
// subscribe, exactly like multiple ipcRenderer.on listeners in production.
const HARNESS_HTML = `<!DOCTYPE html><html><body data-mngr-forward-origin="http://localhost:8421">
  <div id="minds-titlebar"></div>
  <iframe id="content-frame"></iframe>
  <div id="sidebar-backdrop"><div id="sidebar-menu"></div></div>
  <script>
    window.__nav = [];
    window.__help = [];
    var registries = { chromeEvent: [], currentWorkspace: [], contentUrl: [], accent: [], modal: [] };
    window.__cb = {
      chromeEvent: function (data) { registries.chromeEvent.forEach(function (cb) { cb(data); }); },
      currentWorkspace: function (agentId, ready) { registries.currentWorkspace.forEach(function (cb) { cb(agentId, ready); }); },
      contentUrl: function (url) { registries.contentUrl.forEach(function (cb) { cb(url); }); },
      accent: function (agentId) { registries.accent.forEach(function (cb) { cb(agentId); }); },
    };
    window.__registered = registries;
    window.mindsAccent = { get: function (id, cb) { cb('#ffffff'); }, pickForeground: function () { return '0 0 0'; } };
    window.minds = {
      onChromeEvent: function (cb) { registries.chromeEvent.push(cb); },
      onCurrentWorkspaceChanged: function (cb) { registries.currentWorkspace.push(cb); },
      onContentURLChange: function (cb) { registries.contentUrl.push(cb); },
      onAccentChanged: function (cb) { registries.accent.push(cb); },
      onModalStateChanged: function (cb) { registries.modal.push(cb); },
      navigateContent: function (url) { window.__nav.push(url); },
      toggleHelp: function (agentId, assistAvailable) { window.__help.push({ agentId: agentId, assistAvailable: assistAvailable }); },
      toggleSidebar: function () {},
      minimize: function () {}, maximize: function () {}, close: function () {},
      contentGoBack: function () {},
    };
  </script>
</body></html>`;

const EXPECTED_RECOVERY_URL =
  '/agents/' + AGENT_ID + '/recovery?return_to=' +
  encodeURIComponent('http://localhost:8421/goto/' + AGENT_ID + '/');

// Load the harness DOM + bridge stub, then inject the real chrome.js. Each call
// is a fresh page, which is exactly what a reloaded chrome renderer looks like:
// empty in-memory health state, listeners freshly registered.
async function loadChrome(page) {
  await page.setContent(HARNESS_HTML);
  await page.addScriptTag({ path: CHROME_BUNDLE_PATH });
  await page.addScriptTag({ path: CHROME_JS_PATH });
  // Sanity: chrome.js + the titlebar mount ran to completion and registered
  // the bridge callbacks (chrome.js's recovery handler + the component's).
  await expect
    .poll(() => page.evaluate(() => window.__registered.chromeEvent.length > 0 && window.__registered.currentWorkspace.length > 0))
    .toBe(true);
}

test.describe('workspace recovery auto-redirect (chrome.js contract)', () => {
  test('redirects to the recovery page when a stuck status arrives for the displayed workspace', async ({ page }) => {
    await loadChrome(page);
    const nav = await page.evaluate((agentId) => {
      window.__cb.currentWorkspace(agentId);
      window.__cb.chromeEvent({ type: 'system_interface_status', agent_id: agentId, status: 'stuck' });
      return window.__nav.slice();
    }, AGENT_ID);
    expect(nav).toEqual([EXPECTED_RECOVERY_URL]);
  });

  test('does NOT redirect when a reloaded renderer is only re-primed without the status (the bug)', async ({ page }) => {
    await loadChrome(page);
    const nav = await page.evaluate((agentId) => {
      // Replay exactly what the PRE-FIX primeViewWithCachedChromeState sent on a
      // chrome reload: workspaces, auth, requests -- but NOT the stuck status.
      window.__cb.chromeEvent({ type: 'workspaces', workspaces: [{ id: agentId, name: 'queuetest', account: '' }] });
      window.__cb.chromeEvent({ type: 'auth_status', signedIn: true });
      window.__cb.chromeEvent({ type: 'requests', count: 0, request_ids: [], cards: [] });
      // The content view is parked on the stuck workspace's loader.
      window.__cb.currentWorkspace(agentId);
      // The tracker is still STUCK server-side, but its one-shot transition
      // already fired before the reload -- so no fresh status event arrives.
      return window.__nav.slice();
    }, AGENT_ID);
    expect(nav).toEqual([]);
  });

  test('redirects after a reload when the prime replays the stuck status (the fix)', async ({ page }) => {
    await loadChrome(page);
    const nav = await page.evaluate((agentId) => {
      window.__cb.chromeEvent({ type: 'workspaces', workspaces: [{ id: agentId, name: 'queuetest', account: '' }] });
      window.__cb.chromeEvent({ type: 'auth_status', signedIn: true });
      window.__cb.chromeEvent({ type: 'requests', count: 0, request_ids: [], cards: [] });
      // THE FIX: the prime now replays the non-healthy status (mirrors
      // primeViewWithCachedChromeState's system_interface_status replay loop).
      window.__cb.chromeEvent({ type: 'system_interface_status', agent_id: agentId, status: 'stuck' });
      window.__cb.currentWorkspace(agentId);
      return window.__nav.slice();
    }, AGENT_ID);
    expect(nav).toEqual([EXPECTED_RECOVERY_URL]);
  });
});

// Contract for the get-help modal's "have an agent help" gating. main.js pushes
// the displayed workspace id AND a ``contentReady`` flag (from the content view's
// HTTP status) over ``current-workspace-changed``; the titlebar's help-toggle
// offers agent-help (``assist=1``) only when there's a workspace AND it is not
// stuck/restarting AND its content is actually reachable -- not the "Loading
// workspace" proxy loader that mngr_forward serves (503) at the workspace URL
// while the backend is unreachable. The loader state is invisible to the
// health-tracker ``system_interface_status`` signal during startup, so the
// ``contentReady`` gate is what keeps the option disabled there.
test.describe('get-help agent-assist gating (chrome.js contract)', () => {
  // Replay a list of bridge events (as main.js would), then click the titlebar
  // help button and return the captured ``toggleHelp(agentId, assistAvailable)``
  // args. Each step is ``{ ready }`` (a current-workspace-changed push) or
  // ``{ status }`` (a system_interface_status chrome-event).
  async function clickHelpArgs(page, steps) {
    return page.evaluate((args) => {
      args.steps.forEach((step) => {
        if ('ready' in step) window.__cb.currentWorkspace(args.agentId, step.ready);
        if ('status' in step) {
          window.__cb.chromeEvent({ type: 'system_interface_status', agent_id: args.agentId, status: step.status });
        }
      });
      document.getElementById('help-toggle').click();
      return window.__help.slice();
    }, { agentId: AGENT_ID, steps });
  }

  test('offers agent-help on a reachable, healthy workspace', async ({ page }) => {
    await loadChrome(page);
    const calls = await clickHelpArgs(page, [{ ready: true }]);
    expect(calls).toEqual([{ agentId: AGENT_ID, assistAvailable: true }]);
  });

  test('disables agent-help while the workspace shows the loading proxy loader', async ({ page }) => {
    await loadChrome(page);
    // ready=false is exactly what main.js sends when the content view is parked
    // on the mngr_forward "Loading workspace" 503 loader.
    const calls = await clickHelpArgs(page, [{ ready: false }]);
    // The workspace id is still forwarded (so a bug report stays scoped), but
    // agent-help is off.
    expect(calls).toEqual([{ agentId: AGENT_ID, assistAvailable: false }]);
  });

  test('disables agent-help on a stuck workspace even when content was reachable', async ({ page }) => {
    await loadChrome(page);
    const calls = await clickHelpArgs(page, [{ ready: true }, { status: 'stuck' }]);
    expect(calls).toEqual([{ agentId: AGENT_ID, assistAvailable: false }]);
  });

  test('re-enables agent-help once the workspace becomes reachable', async ({ page }) => {
    await loadChrome(page);
    // Loader first (not ready), then the real workspace lands (ready).
    const calls = await clickHelpArgs(page, [{ ready: false }, { ready: true }]);
    expect(calls).toEqual([{ agentId: AGENT_ID, assistAvailable: true }]);
  });
});
