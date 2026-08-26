// End-to-end test of the MIND-205 right-click menu, driving a real Electron
// process (Playwright's `_electron`) that runs context-menu-app.js and wires the
// shipped registerContextMenuFor. `_electron` is used for its MAIN-process
// access -- the only way to observe a native menu, which Playwright can neither
// screenshot nor click. The hook on `Menu.buildFromTemplate` records the template
// each right-click builds and no-ops the popup, whose native run loop would
// otherwise block. A real right-click plus a Copy->Paste round trip covers both
// the items offered and that the clipboard roles move text. Run under xvfb via
// `just minds-test-electron-menu`.

const path = require('path');
const os = require('os');
const fs = require('fs');
const { _electron } = require('playwright');
const { test, expect } = require('@playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../../../..');
const ELECTRON_BIN = path.join(REPO_ROOT, 'apps/minds/node_modules/.bin/electron');
const APP_JS = path.join(__dirname, 'context-menu-app.js');
const COPY_TEXT = 'MIND205-copy-me';

// Record every menu registerContextMenuFor builds and suppress the native popup
// (whose run loop would block the main process). orig() still runs, so a bad
// template (an invalid role) still throws.
async function installMenuHook(electronApp) {
  await electronApp.evaluate(({ Menu }) => {
    globalThis.__ctxMenus = [];
    globalThis.__popupCount = 0;
    const orig = Menu.buildFromTemplate.bind(Menu);
    Menu.buildFromTemplate = (template) => {
      globalThis.__ctxMenus.push(
        template.map((item) => (item.type === 'separator' ? '---' : `${item.role}:${item.enabled ? 'enabled' : 'disabled'}`)),
      );
      const menu = orig(template);
      menu.popup = () => {
        globalThis.__popupCount += 1;
      };
      return menu;
    };
  });
}

// Wait for the main-process context-menu handler to record the menu: the
// right-click's renderer->main IPC lands after page.mouse.click resolves, so
// reading immediately can miss it.
async function waitForMenu(electronApp, minCount) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const menus = await electronApp.evaluate(() => globalThis.__ctxMenus);
    if (menus.length >= minCount) return menus[menus.length - 1];
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`context-menu not recorded within 5s (wanted >= ${minCount})`);
}

const popupCount = (electronApp) => electronApp.evaluate(() => globalThis.__popupCount || 0);

// Run an editing command through the focused webContents -- exactly what the
// menu's 'copy'/'paste' roles invoke. Playwright cannot click a native menu
// item, so this drives the same mechanism the roles are bound to.
async function webContentsEdit(electronApp, method) {
  await electronApp.evaluate(({ BrowserWindow }, editMethod) => {
    const win = BrowserWindow.getAllWindows().find((candidate) => !candidate.isDestroyed());
    win.webContents[editMethod]();
  }, method);
}

async function rightClick(page, selector) {
  const box = await page.locator(selector).boundingBox();
  if (box === null) throw new Error(`No bounding box for ${selector}`);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, { button: 'right' });
}

test('right-click menu offers Copy then Paste, and drives a Copy->Paste round trip', async () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-ctxmenu-'));
  const electronApp = await _electron.launch({
    executablePath: ELECTRON_BIN,
    args: [APP_JS, '--no-sandbox', `--user-data-dir=${userDataDir}`],
    env: { ...process.env, CTXMENU_COPY_TEXT: COPY_TEXT },
    timeout: 60_000,
  });
  try {
    await installMenuHook(electronApp);
    const page = await electronApp.firstWindow();
    await page.waitForSelector('#src');
    await page.waitForSelector('#dst');

    // Paste enablement here depends on prior clipboard state, so it is not asserted.
    await page.locator('#src').selectText();
    await rightClick(page, '#src');
    const copyMenu = await waitForMenu(electronApp, 1);
    expect(copyMenu).toContain('copy:enabled');
    expect(copyMenu).toContain('cut:enabled');
    expect(copyMenu).toContain('selectAll:enabled');
    // Re-select before copying in case the right-click moved the caret.
    await page.locator('#src').selectText();
    await webContentsEdit(electronApp, 'copy');

    await page.locator('#dst').click();
    await rightClick(page, '#dst');
    const pasteMenu = await waitForMenu(electronApp, 2);
    expect(pasteMenu).toContain('paste:enabled');
    expect(pasteMenu).toContain('cut:disabled');
    expect(pasteMenu).toContain('copy:disabled');
    await webContentsEdit(electronApp, 'paste');

    await expect(page.locator('#dst')).toHaveValue(COPY_TEXT, { timeout: 5_000 });
    expect(await popupCount(electronApp)).toBeGreaterThanOrEqual(2);
  } finally {
    await electronApp.close().catch(() => {});
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
});
