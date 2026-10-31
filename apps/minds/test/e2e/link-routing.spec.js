// End-to-end test of link routing (popups, in-place frame navigations and
// server redirects), driving a real Electron process (Playwright's
// `_electron`) that runs link-routing-app.js: the shipped createLinkRouter and
// preload.js, the real embed contract on both sides, and a workspace page
// framed on a real `agent-<hex>.localhost` origin. `_electron` is used for its
// main-process access: counting windows, and standing in for
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
async function chromePageFor(electronApp, query) {
  const suffix = `/?${query}`;
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

// Playwright attaches the out-of-process workspace frame asynchronously, so it
// can trail the frame's own mount report.
async function workspaceFrameOf(page) {
  return waitFor(
    async () => page.frames().find((frame) => /\/\/agent-[0-9a-f]+\.localhost:\d+\/workspace/.test(frame.url())),
    (found) => found !== undefined,
    'the workspace frame',
  );
}

async function withHarness(run) {
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
    await run(electronApp);
    expect((await harness(electronApp)).uncaughtErrors).toEqual([]);
  } finally {
    await electronApp.close().catch(() => {});
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
}

test('popups from an announcing workspace go back into it, external ones back out to the browser; a silent workspace keeps its windows', async () => {
  await withHarness(async (electronApp) => {
    const announcing = await chromePageFor(electronApp, 'opensLinks=1');
    const silent = await chromePageFor(electronApp, 'opensLinks=0');
    const { port, workspaceId } = await harness(electronApp);
    await waitFor(
      async () => (await harness(electronApp)).mountReports.map((entry) => entry.report),
      (reports) =>
        reports.some((report) => report && report.opensLinks === true) &&
        reports.some((report) => report && report.opensLinks === false),
      'both windows reporting their mounted workspace',
    );
    expect(await windowCount(electronApp)).toBe(3);

    const frame = await workspaceFrameOf(announcing);
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

    // An external link goes to the workspace too, which hands it back out
    // (minds:open-external) when no app of its own takes it, and reaches the
    // default browser.
    const openedExternally = () => electronApp.evaluate(() => globalThis.__openedExternally);
    await frame.click('#external-link');
    await waitFor(openedExternally, (urls) => urls.length === 1, 'the external link handed to the browser');
    expect(await openedExternally()).toEqual(['https://example.com/from-workspace']);
    expect((await openedInWorkspace()).slice(3)).toEqual(['https://example.com/from-workspace']);

    // A mailto link asks first: declined, nothing opens; accepted, it does.
    const prompts = async () => (await harness(electronApp)).prompts;
    await frame.click('#mailto-link');
    await waitFor(prompts, (asked) => asked.length === 1, 'the mailto prompt');
    expect(await prompts()).toEqual([{ detail: 'someone@example.com', canRemember: true }]);
    await electronApp.evaluate(() => {
      globalThis.__linkRoutingHarness.promptAnswer = { accepted: true, remember: false };
    });
    await frame.click('#mailto-link');
    await waitFor(openedExternally, (urls) => urls.length === 2, 'the accepted mailto link');
    expect(await openedExternally()).toEqual(['https://example.com/from-workspace', 'mailto:someone@example.com']);
    // Every popup above was denied: the links have landed, so any window they
    // opened would exist by now.
    expect(await windowCount(electronApp)).toBe(3);

    // A local popup the Imbue Studio page itself asked for keeps its window:
    // its referrer is the page's own origin, not the workspace's.
    const chromePopup = electronApp.waitForEvent('window');
    await announcing.click('#chrome-local-link');
    await expect.poll(async () => (await chromePopup).url()).toBe(`http://127.0.0.1:${port}/local-from-chrome`);
    await (await chromePopup).close();

    // A workspace that never announced opensLinks still gets a window, and
    // its external links go straight to the browser.
    const silentPopup = electronApp.waitForEvent('window');
    const silentFrame = await workspaceFrameOf(silent);
    await silentFrame.click('#app-link');
    await expect.poll(async () => (await silentPopup).url()).toBe(appAddress);
    await silentFrame.click('#external-link');
    await waitFor(openedExternally, (urls) => urls.length === 3, "the silent workspace's external link");
    expect(await silentFrame.evaluate(() => window.__openedLinks)).toEqual([]);
    expect(await openedInWorkspace()).toHaveLength(6);
  });
});

test('a custom-scheme link a workspace page launches asks first, until the user always allows it for that workspace', async () => {
  await withHarness(async (electronApp) => {
    const frame = await workspaceFrameOf(await chromePageFor(electronApp, 'opensLinks=1'));
    const launchUrl = 'imbue-e2e-unhandled://open';
    const prompts = async () => (await harness(electronApp)).prompts;
    const openedExternally = () => electronApp.evaluate(() => globalThis.__openedExternally);
    // As a workspace app launches one, through one hidden iframe it reuses:
    // each launch also checks the reused frame raises nothing in main.
    const launch = async (launches) => {
      await frame.click('#scheme-launcher');
      await waitFor(
        () => harness(electronApp),
        (state) => state.frameNavigations.filter((url) => url === launchUrl).length === launches,
        `launch ${launches} reaching will-frame-navigate`,
      );
      expect((await harness(electronApp)).uncaughtErrors, `after launch ${launches}`).toEqual([]);
    };

    await launch(1);
    await waitFor(prompts, (asked) => asked.length === 1, 'the first prompt');
    expect(await prompts()).toEqual([{ detail: launchUrl, canRemember: true }]);

    await electronApp.evaluate(() => {
      globalThis.__linkRoutingHarness.promptAnswer = { accepted: true, remember: true };
    });
    await launch(2);
    await waitFor(openedExternally, (urls) => urls.length === 1, 'the accepted launch');
    expect(await prompts()).toHaveLength(2);

    await electronApp.evaluate(() => {
      globalThis.__linkRoutingHarness.promptAnswer = { accepted: false, remember: false };
    });
    await launch(3);
    await waitFor(openedExternally, (urls) => urls.length === 2, 'the always-allowed launch');
    expect(await openedExternally()).toEqual([launchUrl, launchUrl]);
    expect(await prompts()).toHaveLength(2);
  });
});

test('a custom-scheme or mailto link a workspace page reaches through a server redirect asks first too', async () => {
  await withHarness(async (electronApp) => {
    const frame = await workspaceFrameOf(await chromePageFor(electronApp, 'opensLinks=1'));
    const prompts = async () => (await harness(electronApp)).prompts;
    await frame.click('#scheme-redirector');
    await waitFor(prompts, (asked) => asked.length === 1, 'the redirected custom-scheme link prompt');
    await frame.click('#mailto-redirector');
    await waitFor(prompts, (asked) => asked.length === 2, 'the redirected mailto link prompt');
    expect(await prompts()).toEqual([
      { detail: 'imbue-e2e-unhandled://redirected', canRemember: true },
      { detail: 'redirected@example.com', canRemember: true },
    ]);
  });
});

test('a custom-scheme link goes into a workspace that takes them, and asks first once the workspace hands it back out', async () => {
  await withHarness(async (electronApp) => {
    const page = await chromePageFor(electronApp, 'opensLinks=1&schemes=1');
    const frame = await workspaceFrameOf(page);
    await waitFor(
      async () => (await harness(electronApp)).mountReports.map((entry) => entry.report),
      (reports) => reports.some((report) => report && report.opensCustomSchemeLinks === true),
      'the workspace reporting it takes custom-scheme links',
    );
    const openedInWorkspace = () => frame.evaluate(() => window.__openedLinks);
    const prompts = async () => (await harness(electronApp)).prompts;
    // A target=_blank link, a launch through a hidden frame, and a server redirect each arrive as
    // minds:open-link; the workspace stand-in takes none itself, so each comes back out and asks.
    const cases = [
      ['#scheme-link', 'imbue-e2e-unhandled://from-workspace'],
      ['#scheme-launcher', 'imbue-e2e-unhandled://open'],
      ['#scheme-redirector', 'imbue-e2e-unhandled://redirected'],
    ];
    for (const [index, [selector, url]] of cases.entries()) {
      await frame.click(selector);
      await waitFor(openedInWorkspace, (links) => links.length === index + 1, `${url} in the workspace`);
      await waitFor(prompts, (asked) => asked.length === index + 1, `the prompt for ${url}`);
    }
    expect(await openedInWorkspace()).toEqual(cases.map(([, url]) => url));
    expect(await prompts()).toEqual(cases.map(([, url]) => ({ detail: url, canRemember: true })));
    expect(await electronApp.evaluate(() => globalThis.__openedExternally)).toEqual([]);
    expect(await windowCount(electronApp)).toBe(3);
  });
});
