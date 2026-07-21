// Contract test for chrome.js's local-page swap engine (the persistent chrome
// shell): navigating between hub pages swaps #local-page-root +
// #local-page-scripts in place -- the titlebar element must NOT be rebuilt --
// executes the incoming page's scripts, pushState's the URL, and dispatches
// ``minds:page-teardown`` to the outgoing page. Non-hub targets and non-hub
// CURRENT pages fall back to full navigation.
//
// Like recovery-redirect.spec.js this drives the REAL chrome.js against a
// minimal harness DOM, with pages served from a routed origin so URL parsing
// and fetch work. No Electron, no backend.
const { test, expect } = require('@playwright/test');
const path = require('path');

const CHROME_JS = path.resolve(__dirname, '../../imbue/minds/desktop_client/static/chrome.js');
// The compiled mithril bundle chrome.js expects (window.MindsUI) -- loaded
// before chrome.js exactly like the production shell's classic script tag.
const CHROME_BUNDLE = path.resolve(__dirname, '../../imbue/minds/desktop_client/static/dist/chrome.bundle.js');
const ORIGIN = 'http://minds-swap-harness.localhost:7777';

// The shared shell chrome (titlebar + switcher + shell-script slots), matching
// ChromeShell.jinja's structure. ``marker`` stamps the titlebar node so the
// test can prove it survived a swap; ``label``/``script`` fill the page body.
function shellPage({ title, label, script = '', bodyClass = 'page-surface' }) {
  return `<!DOCTYPE html><html><head><title>${title}</title></head>
<body class="${bodyClass}" data-mngr-forward-origin="https://localhost:8421" data-is-mac="true">
  <div id="minds-titlebar">
    <button id="back-btn" hidden></button>
    <button id="home-btn"></button>
    <div id="ws-crumb" hidden>
      <button id="workspace-switcher-btn"><span id="workspace-switcher-name"></span></button>
      <button id="ws-tab-workspace"></button><button id="ws-tab-settings"></button>
    </div>
    <div id="page-crumb" hidden><span id="page-crumb-name"></span></div>
    <button id="requests-toggle"><span id="requests-badge" hidden></span></button>
    <button id="help-toggle"></button>
    <button id="min-btn"></button><button id="max-btn"></button><button id="close-btn"></button>
  </div>
  <div id="sidebar-backdrop" class="hidden"><div id="sidebar-menu"><div id="sidebar-workspaces"></div><button id="sidebar-new-workspace"></button></div></div>
  <div id="local-page-root" style="display: contents"><main data-page="${label}">${label}</main></div>
  <div id="local-page-scripts" style="display: none">${script}</div>
</body></html>`;
}

async function bootHarness(page, { startPath = '/' } = {}) {
  await page.route(`${ORIGIN}/`, (r) => r.fulfill({
    contentType: 'text/html',
    body: shellPage({ title: 'Workspaces', label: 'home' }),
  }));
  await page.route(`${ORIGIN}/create`, (r) => r.fulfill({
    contentType: 'text/html',
    body: shellPage({
      title: 'Create a workspace',
      label: 'create',
      bodyClass: 'create-surface',
      script: '<script>window.__createScriptRan = (window.__createScriptRan || 0) + 1;</script>',
    }),
  }));
  await page.route(`${ORIGIN}/creating/creation-abc`, (r) => r.fulfill({
    contentType: 'text/html',
    body: shellPage({ title: 'Creating', label: 'creating' }),
  }));
  await page.goto(`${ORIGIN}${startPath}`);
  // Browser mode (no window.minds): chrome.js's local-page branch drives
  // swaps. The bundle loads first, as in the production shell.
  await page.addScriptTag({ path: CHROME_BUNDLE });
  await page.addScriptTag({ path: CHROME_JS });
  // Stamp the titlebar node so a rebuild (full navigation) would erase it.
  await page.evaluate(() => { document.getElementById('minds-titlebar').dataset.stamp = 'persistent'; });
}

test.describe('local page swap engine (chrome.js contract)', () => {
  test('hub-to-hub navigation swaps in place: content, scripts, URL, title -- titlebar untouched', async ({ page }) => {
    await bootHarness(page);
    await page.evaluate(() => {
      window.__tornDown = 0;
      window.addEventListener('minds:page-teardown', () => { window.__tornDown += 1; });
      document.getElementById('sidebar-new-workspace').click();
    });
    await expect(page.locator('[data-page="create"]')).toBeVisible();
    const state = await page.evaluate(() => ({
      stamp: document.getElementById('minds-titlebar').dataset.stamp,
      path: window.location.pathname,
      title: document.title,
      bodyClass: document.body.className,
      scriptRan: window.__createScriptRan,
      tornDown: window.__tornDown,
      crumb: document.getElementById('page-crumb-name').textContent,
    }));
    expect(state.stamp).toBe('persistent'); // titlebar NOT rebuilt
    expect(state.path).toBe('/create');
    expect(state.title).toBe('Create a workspace');
    expect(state.bodyClass).toBe('create-surface');
    expect(state.scriptRan).toBe(1); // incoming page's script executed once
    expect(state.tornDown).toBe(1); // outgoing page told to tear down
    expect(state.crumb).toBe('New workspace'); // titlebar context re-derived
  });

  test('back over a swap restores the previous hub page via popstate', async ({ page }) => {
    await bootHarness(page);
    await page.evaluate(() => document.getElementById('sidebar-new-workspace').click());
    await expect(page.locator('[data-page="create"]')).toBeVisible();
    await page.goBack();
    await expect(page.locator('[data-page="home"]')).toBeVisible();
    expect(await page.evaluate(() => document.getElementById('minds-titlebar').dataset.stamp)).toBe('persistent');
    expect(await page.evaluate(() => window.location.pathname)).toBe('/');
  });

  test('a non-hub CURRENT page never swaps out (full navigation tears it down)', async ({ page }) => {
    await bootHarness(page, { startPath: '/creating/creation-abc' });
    // From the excluded page, a hub navigation must NOT be a swap: chrome.js's
    // canSwapTo requires the current path to be swappable too, so the switcher
    // entry does a full navigation that replaces the document -- proven by the
    // titlebar stamp being gone on the destination.
    await page.evaluate(() => { document.getElementById('sidebar-new-workspace').click(); });
    await expect(page.locator('[data-page="create"]')).toBeVisible({ timeout: 5000 });
    expect(await page.evaluate(() => document.getElementById('minds-titlebar').dataset.stamp)).toBeUndefined();
  });
});
