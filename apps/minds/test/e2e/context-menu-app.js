// Minimal Electron entry for the context-menu e2e (context-menu.spec.js): it
// wires the shipped registerContextMenuFor onto a window, as electron/main.js's
// createBundle does. This exercises the real wiring + real Menu + real
// clipboard + a real cross-origin frame without booting the minds backend -- a
// dev-mode boot needs the snapshot-sandbox setup the Python CDP driver
// (e2e_workspace_runner.py) relies on, and the menu is per-window and
// page-independent, so fixture pages hit the identical code path.
//
// The top page holds two editable fields and a cross-origin frame (a second
// loopback port) whose page keeps rows for the menu the way the template's
// element menu does where the desktop app draws it (a submenu of its own, and
// the reference row): it lets the right-click through and answers the menu's
// reads through the page bridge, and a picked row puts its text on the
// clipboard once the page has focus.
const http = require('http');
const { app, BrowserWindow, Menu, clipboard, nativeImage } = require('electron');
const { registerContextMenuFor } = require('../../electron/context-menu');
const { global: PAGE_MENU_GLOBAL } = require('../../electron/page-menu-bridge.json');

const COPY_TEXT = process.env.CTXMENU_COPY_TEXT || 'MIND205-copy-me';
const REFERENCE_TEXT = process.env.CTXMENU_REFERENCE_TEXT || 'REF-ctxmenu-e2e';

const FRAME_HTML = `<!doctype html><html><body style="font:16px system-ui;padding:12px">
<p id="ref-target">Right-click this paragraph</p>
<p id="picked">nothing picked</p>
<script>
  let rows = null;
  let pageRows = null;
  document.addEventListener('contextmenu', () => {
    rows = [
      { kind: 'action', key: 'copy-reference', label: 'Copy reference', isDisabled: false, tooltip: null, symbol: 'doc.on.doc' },
    ];
    pageRows = [
      {
        kind: 'submenu',
        label: 'Status',
        tooltip: null,
        symbol: 'flag',
        items: [
          { kind: 'check', key: 'status-open', label: 'Open', isChecked: true, tooltip: null, symbol: null },
          { kind: 'action', key: 'status-close', label: 'Close', isDisabled: false, tooltip: null, symbol: null },
        ],
      },
    ];
  });
  const whenFocused = () => document.hasFocus() ? Promise.resolve() : new Promise((resolve) => {
    const timer = setTimeout(resolve, 500);
    window.addEventListener('focus', () => { clearTimeout(timer); resolve(); }, { once: true });
    window.focus();
  });
  window[${JSON.stringify(PAGE_MENU_GLOBAL)}] = {
    sections: () => (rows === null ? null : { page: pageRows, link: [], reference: rows }),
    select: async (key) => {
      await whenFocused();
      await navigator.clipboard.writeText(${JSON.stringify(REFERENCE_TEXT)});
      document.getElementById('picked').textContent = 'picked ' + key;
    },
  };
</script>
</body></html>`;

function serve(html) {
  return new Promise((resolve) => {
    const server = http.createServer((_request, response) => {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(html);
    });
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  const framePort = await serve(FRAME_HTML);
  const topPort = await serve(
    '<!doctype html><html><body style="font:16px system-ui;padding:20px">' +
      `<label>src <input id="src" value="${COPY_TEXT}" style="width:320px"></label>` +
      '<label>dst <input id="dst" value="" style="width:320px"></label>' +
      `<iframe id="page" allow="clipboard-write" src="http://127.0.0.1:${framePort}/" width="560" height="160"></iframe>` +
      '</body></html>',
  );
  const win = new BrowserWindow({ width: 680, height: 420, show: true });
  registerContextMenuFor(win, win.webContents, {
    Menu,
    clipboard,
    createMenuSymbol: (name) => nativeImage.createMenuSymbol(name),
    platform: process.platform,
    showsInspectElement: true,
  });
  await win.loadURL(`http://127.0.0.1:${topPort}/`);
});
