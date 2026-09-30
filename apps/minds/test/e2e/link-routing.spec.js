// End-to-end test of popup routing, driving a real Electron process
// (Playwright's `_electron`) that runs link-routing-app.js: the shipped
// createLinkRouter and preload.js, the real embed contract on both sides, and a
// workspace page framed on a real `agent-<hex>.localhost` origin. `_electron`
// is used for its main-process access: counting windows, and standing in for
// shell.openExternal so no real browser opens.

const path = require('path');
const os = require('os');
const fs = require('fs');
const { _electron } = require('playwright');
const { test, expect } = require('@playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../../../..');
const ELECTRON_BIN = path.join(REPO_ROOT, 'apps/minds/node_modules/.bin/electron');
const APP_JS = path.join(__dirname, 'link-routing-app.js');

async function harness(electronApp) {
  return electronApp.evaluate(() => globalThis.__linkRoutingHarness);
}

async function waitFor(read, isDone, what, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await read();
    if (isDone(last)) return last;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${what} not seen within ${timeoutMs}ms; last: ${JSON.stringify(last)}`);
}

const windowCount = (electronApp) =>
  electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);

// The page whose document is the stand-in Imbue Studio page for one workspace.
async function chromePageFor(electronApp, opensLinks) {
  const suffix = `/?opensLinks=${opensLinks ? 1 : 0}`;
  const page = await waitFor(
    async () => {
      for (const candidate of electronApp.windows()) {
        const url = await candidate.evaluate(() => location.href).catch(() => '');
        if (url.endsWith(suffix)) return candidate;
      }
      return null;
    },
    (found) => found !== null,
    `the window at ${suffix}`,
  );
  return page;
}

function workspaceFrameOf(page) {
  return page.frames().find((frame) => /\/\/agent-[0-9a-f]+\.localhost:\d+\/workspace/.test(frame.url()));
}

test('popups from an announcing workspace go back into it; external links reach the browser; a silent workspace keeps its windows', async () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-link-routing-'));
  const electronApp = await _electron.launch({
    executablePath: ELECTRON_BIN,
    args: [APP_JS, '--no-sandbox', `--user-data-dir=${userDataDir}`],
    timeout: 60_000,
  });
  try {
    await electronApp.evaluate(({ shell }) => {
      globalThis.__openedExternally = [];
      shell.openExternal = async (url) => {
        globalThis.__openedExternally.push(url);
      };
    });
    const announcing = await chromePageFor(electronApp, true);
    const silent = await chromePageFor(electronApp, false);
    const { port, workspaceId } = await harness(electronApp);
    await waitFor(
      async () => (await harness(electronApp)).mountReports.map((entry) => entry.report),
      (reports) =>
        reports.some((report) => report && report.opensLinks === true) &&
        reports.some((report) => report && report.opensLinks === false),
      'both windows reporting their mounted workspace',
    );
    expect(await windowCount(electronApp)).toBe(2);

    const frame = workspaceFrameOf(announcing);
    const openedInWorkspace = () => frame.evaluate(() => window.__openedLinks);

    // An app address of the workspace, a local URL with the workspace's page
    // as referrer, and one with no referrer all arrive as minds:open-link.
    const appAddress = `http://web.${workspaceId}.localhost:${port}/notes`;
    const localUrl = `http://127.0.0.1:${port}/local`;
    const noReferrerUrl = `http://127.0.0.1:${port}/local-noreferrer`;
    await frame.click('#app-link');
    await frame.click('#local-link');
    await frame.click('#noreferrer-link');
    await waitFor(openedInWorkspace, (links) => links.length === 3, 'the three links in the workspace');
    expect(await openedInWorkspace()).toEqual([appAddress, localUrl, noReferrerUrl]);

    // An external link reaches the default browser.
    await frame.click('#external-link');
    await waitFor(
      () => electronApp.evaluate(() => globalThis.__openedExternally),
      (urls) => urls.length === 1,
      'the external link handed to the browser',
    );
    expect(await electronApp.evaluate(() => globalThis.__openedExternally)).toEqual([
      'https://example.com/from-workspace',
    ]);
    // Every popup above was denied: the links have landed, so any window they
    // opened would exist by now.
    expect(await windowCount(electronApp)).toBe(2);

    // A local popup the Imbue Studio page itself asked for keeps its window:
    // its referrer is the page's own origin, not the workspace's.
    const chromePopup = electronApp.waitForEvent('window');
    await announcing.click('#chrome-local-link');
    await expect.poll(async () => (await chromePopup).url()).toBe(`http://127.0.0.1:${port}/local-from-chrome`);
    (await chromePopup).close();

    // A workspace that never announced opensLinks still gets a window.
    const silentPopup = electronApp.waitForEvent('window');
    await workspaceFrameOf(silent).click('#app-link');
    await expect.poll(async () => (await silentPopup).url()).toBe(appAddress);
    expect(await workspaceFrameOf(silent).evaluate(() => window.__openedLinks)).toEqual([]);
    expect(await openedInWorkspace()).toHaveLength(3);
  } finally {
    await electronApp.close().catch(() => {});
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
});
