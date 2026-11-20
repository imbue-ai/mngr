'use strict';

// The key probe: a bare Electron window that logs every keystroke the main
// process sees (`before-input-event`) and every keydown its page sees, so the
// per-platform chord lists of workspace_profile_contract.json (which keys the
// OS keeps from every app, which ones the host acts on) can be pinned from
// what actually arrives rather than from memory. Opened in a plain browser,
// key-probe.html logs only what reaches a page, which pins the browser list.
//
//   apps/minds/node_modules/.bin/electron apps/minds/electron/dev/key-probe.js
//   apps/minds/node_modules/.bin/electron apps/minds/electron/dev/key-probe.js --intercept
//
// With --intercept, main calls preventDefault on every keydown it sees, which
// answers whether that stops the OS's own action (Alt+F4 closing the window on
// Windows) without the key reaching the page; without it, every key goes on to
// the page. No application menu is installed, so no accelerator takes a key
// before the probe does.

const path = require('node:path');
const { app, BrowserWindow, Menu } = require('electron');

const isIntercepting = process.argv.includes('--intercept');

function describeInput(input) {
  return {
    key: input.key,
    code: input.code,
    control: input.control,
    alt: input.alt,
    shift: input.shift,
    meta: input.meta,
  };
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  const window = new BrowserWindow({ width: 900, height: 700, title: 'Key probe' });
  const wc = window.webContents;
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    if (isIntercepting) event.preventDefault();
    const entry = { ...describeInput(input), intercepted: isIntercepting };
    wc.executeJavaScript(`window.__keyProbe.recordMain(${JSON.stringify(entry)})`).catch(() => {});
  });
  window.loadFile(path.join(__dirname, 'key-probe.html'), {
    query: { platform: process.platform, intercept: isIntercepting ? '1' : '0' },
  });
});

app.on('window-all-closed', () => app.quit());
