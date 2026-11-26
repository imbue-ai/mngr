// End-to-end test of the client-permission gate, driving a real Electron
// process (Playwright's `_electron`) that runs client-permission-app.js: the
// shipped gate, link router and preload.js, and a workspace page framed on a
// real `chat.agent-<hex>.localhost` origin calling getUserMedia. What it pins
// beyond the unit tests is Electron's side of the contract: that a framed
// page's microphone request reaches the request handler as `media` with an
// audio media type, the frame's URL and the window's webContents, and that the
// prompt and its answer travel over the preload bridge.

const path = require('path');
const os = require('os');
const fs = require('fs');
const { _electron } = require('playwright');
const { test, expect } = require('@playwright/test');
const { waitFor } = require('./wait-for');

const REPO_ROOT = path.resolve(__dirname, '../../../..');
const ELECTRON_BIN = path.join(REPO_ROOT, 'apps/minds/node_modules/.bin/electron');
const APP_JS = path.join(__dirname, 'client-permission-app.js');

const harness = (electronApp) => electronApp.evaluate(() => globalThis.__clientPermissionHarness);

async function setStored(electronApp, stored) {
  await electronApp.evaluate((_electron, value) => {
    globalThis.__clientPermissionHarness.stored = value;
  }, stored);
}

// Runs `expression` in the workspace frame, through Electron's own frame
// handle: Playwright's frame list can miss the out-of-process workspace frame.
async function inWorkspaceFrame(electronApp, expression) {
  return electronApp.evaluate(async ({ BrowserWindow }, code) => {
    const contents = BrowserWindow.getAllWindows()[0].webContents;
    const frame = contents.mainFrame.framesInSubtree.find((candidate) => /\/\/chat\.agent-[0-9a-f]+\.localhost:\d+\/workspace/.test(candidate.url));
    if (frame === undefined) return 'no workspace frame';
    return frame.executeJavaScript(code);
  }, expression);
}

test('a workspace frame gets the microphone by its stored decision, and by the prompt on ask', async () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-client-permission-'));
  const electronApp = await _electron.launch({
    executablePath: ELECTRON_BIN,
    args: [APP_JS, '--no-sandbox', `--user-data-dir=${userDataDir}`],
    timeout: 60_000,
  });
  try {
    const page = await electronApp.firstWindow();
    const tryMicrophone = () => inWorkspaceFrame(electronApp, 'window.tryMicrophone()');
    await waitFor(
      () => inWorkspaceFrame(electronApp, 'typeof window.tryMicrophone'),
      (type) => type === 'function',
      'the workspace frame',
    );
    const { workspaceId, port } = await harness(electronApp);

    await setStored(electronApp, { decision: 'granted', source: 'workspace', workspaceName: 'Research notes' });
    expect(await tryMicrophone()).toBe('granted');
    const first = (await harness(electronApp)).requests.find((entry) => entry.permission === 'media');
    expect(first.mediaTypes).toContain('audio');
    expect(first.requestingUrl).toBe(`http://chat.${workspaceId}.localhost:${port}/workspace`);
    expect((await harness(electronApp)).fetches[0]).toEqual({ workspaceId, permission: 'microphone' });

    await setStored(electronApp, { decision: 'denied', source: 'workspace', workspaceName: 'Research notes' });
    expect(await tryMicrophone()).toBe('NotAllowedError');
    expect((await harness(electronApp)).toasts).toEqual([
      {
        title: 'Microphone blocked for "Research notes"',
        body: 'Blocked on this computer. Change it under Permissions > Client permissions.',
        link: { kind: 'client-permissions', workspaceId, text: 'Permissions > Client permissions' },
      },
    ]);
    expect(await tryMicrophone()).toBe('NotAllowedError');
    expect((await harness(electronApp)).toasts).toHaveLength(1);

    await setStored(electronApp, null);
    expect(await tryMicrophone()).toBe('NotAllowedError');
    expect((await harness(electronApp)).toasts).toHaveLength(1);

    // Ask: the prompt reaches the window's page, which answers "Allow for
    // this session"; the next request is granted without a second prompt.
    await setStored(electronApp, { decision: 'ask', source: 'default', workspaceName: 'Research notes' });
    const pending = tryMicrophone();
    const prompts = await waitFor(() => page.evaluate(() => window.__prompts), (asked) => asked.length === 1, 'the prompt');
    expect(prompts[0]).toMatchObject({ workspaceId, permission: 'microphone', workspaceName: 'Research notes' });
    await page.evaluate(
      (promptId) => window.mindsNative.answerClientPermission({ promptId, decision: 'granted', isRemembered: false }),
      prompts[0].promptId,
    );
    expect(await pending).toBe('granted');
    expect(await tryMicrophone()).toBe('granted');
    expect(await page.evaluate(() => window.__prompts.length)).toBe(1);

    // The chrome's own page is not the workspace: refused without asking.
    const fetchesBefore = (await harness(electronApp)).fetches.length;
    expect(await page.evaluate(() => window.tryMicrophone())).toBe('NotAllowedError');
    expect((await harness(electronApp)).fetches.length).toBe(fetchesBefore);

    expect((await harness(electronApp)).uncaughtErrors).toEqual([]);
  } finally {
    await electronApp.close().catch(() => {});
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
});
