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

const AGENT_ID = 'agent-bb340d1c1d5a4c43b1396f277cfd6d81';

// Minimal DOM + a faithful stub of the Electron preload bridge. chrome.js wires
// these ids/callbacks at init; the test fires the captured callbacks exactly as
// main.js's broadcastChromeEvent / current-workspace-changed IPC do. Captured
// navigateContent calls land in window.__nav.
const HARNESS_HTML = `<!DOCTYPE html><html><body data-mngr-forward-origin="http://localhost:8421">
  <button id="sidebar-toggle"></button><button id="home-btn"></button>
  <button id="back-btn"></button><button id="forward-btn"></button>
  <button id="min-btn"></button><button id="max-btn"></button><button id="close-btn"></button>
  <button id="user-btn"></button><button id="requests-toggle"></button>
  <span id="page-title"></span><span id="requests-badge"></span>
  <iframe id="content-frame"></iframe>
  <div id="sidebar-backdrop"></div><div id="sidebar-workspaces"></div>
  <script>
    window.__nav = [];
    window.__cb = {};
    window.mindsAccent = { get: function (id, cb) { cb('#ffffff'); }, pickForeground: function () { return '0 0 0'; } };
    window.minds = {
      onChromeEvent: function (cb) { window.__cb.chromeEvent = cb; },
      onCurrentWorkspaceChanged: function (cb) { window.__cb.currentWorkspace = cb; },
      onContentTitleChange: function (cb) { window.__cb.contentTitle = cb; },
      onContentURLChange: function (cb) { window.__cb.contentUrl = cb; },
      onAccentChanged: function (cb) { window.__cb.accent = cb; },
      onModalStateChanged: function (cb) { window.__cb.modal = cb; },
      navigateContent: function (url) { window.__nav.push(url); },
      toggleSidebar: function () {}, toggleInbox: function () {},
      minimize: function () {}, maximize: function () {}, close: function () {},
      contentGoBack: function () {}, contentGoForward: function () {},
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
  await page.addScriptTag({ path: CHROME_JS_PATH });
  // Sanity: chrome.js ran to completion and registered the bridge callbacks.
  await expect
    .poll(() => page.evaluate(() => typeof window.__cb.chromeEvent === 'function' && typeof window.__cb.currentWorkspace === 'function'))
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
      window.__cb.chromeEvent({ type: 'requests', count: 0, request_ids: [] });
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
      window.__cb.chromeEvent({ type: 'requests', count: 0, request_ids: [] });
      // THE FIX: the prime now replays the non-healthy status (mirrors
      // primeViewWithCachedChromeState's system_interface_status replay loop).
      window.__cb.chromeEvent({ type: 'system_interface_status', agent_id: agentId, status: 'stuck' });
      window.__cb.currentWorkspace(agentId);
      return window.__nav.slice();
    }, AGENT_ID);
    expect(nav).toEqual([EXPECTED_RECOVERY_URL]);
  });
});
