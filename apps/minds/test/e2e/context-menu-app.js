// Minimal Electron entry for the context-menu e2e (context-menu.spec.js): it
// wires the shipped registerContextMenuFor onto a window of two editable fields,
// as electron/main.js's createBundle does. This exercises the real wiring + real
// Menu + real clipboard without booting the minds backend -- a dev-mode boot
// needs the snapshot-sandbox setup the Python CDP driver (e2e_workspace_runner.py)
// relies on, and the menu is per-window and page-independent, so a fixture page
// hits the identical code path.
const { app, BrowserWindow, Menu } = require('electron');
const { registerContextMenuFor } = require('../../electron/context-menu');

const COPY_TEXT = process.env.CTXMENU_COPY_TEXT || 'MIND205-copy-me';
const FIXTURE_HTML =
  '<!doctype html><html><body style="font:16px system-ui;padding:20px">' +
  `<label>src <input id="src" value="${COPY_TEXT}" style="width:320px"></label>` +
  '<label>dst <input id="dst" value="" style="width:320px"></label>' +
  '</body></html>';

Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  const win = new BrowserWindow({ width: 640, height: 320, show: true });
  registerContextMenuFor(win, win.webContents, Menu);
  await win.loadURL('data:text/html,' + encodeURIComponent(FIXTURE_HTML));
});
