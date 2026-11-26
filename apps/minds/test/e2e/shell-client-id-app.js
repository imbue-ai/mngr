// Minimal Electron entry for the shell client id e2e (shell-client-id.spec.js),
// run twice on one profile.
//
// `--phase=seed` stands in for the releases without main's shell client id
// store (0.10.7 and earlier): a window in the default session (minds 0.10.5)
// and one in the device partition (0.10.6 and 0.10.7), neither with the
// preload, each frame the same shell origin, which records a different
// `si-client-id` in each session. It flushes and quits.
//
// `--phase=run` stands in for a release with the store: it runs the shipped one-time
// import over that profile, wires the `shell-client-id` ask the way
// electron/main.js does, and opens two windows with the shipped preload in two
// account partitions, each framing the shell (which frames a page of its own)
// and a workspace app.
//
// Every page is answered by each session's protocol handler, so no server or
// certificate is involved; the page on `http://localhost/` stands in for the
// Imbue Studio page, and a shell page records the id it booted with the way
// workspace_ui's ClientIdentity.ts reads and mints it.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { app, BrowserWindow, Menu, ipcMain, session } = require('electron');
const { askingShellHostname, createShellClientIds, shellHostnameOf } = require('../../electron/shell-client-ids');
const { runLegacyShellClientIdImport } = require('../../electron/legacy-shell-client-id-import');
const { readLegacyShellClientIds } = require('../../electron/legacy-shell-client-ids');

const phase = (process.argv.find((argument) => argument.startsWith('--phase=')) || '').slice('--phase='.length);

const PRELOAD_PATH = path.join(__dirname, '..', '..', 'electron', 'preload.js');
// The run phase's forward fell back off its default port, as when another
// forward holds it on the first launch after an update.
const FORWARD_PORT = phase === 'seed' ? 8421 : 51234;
const AGENT_ID = 'agent-0123456789abcdef0123456789abcdef';
const HOST_ID = 'host-fedcba9876543210fedcba9876543210';
const SHELL_LABEL = 'system-interface-abcd1234';
const SHELL_URL = `https://${SHELL_LABEL}.${AGENT_ID}.localhost:${FORWARD_PORT}/`;
const APP_URL = `https://chat-wxyz5678.${AGENT_ID}.localhost:${FORWARD_PORT}/`;
const NESTED_URL = 'https://nested.example.com/';
const STUDIO_PAGE_URL = 'http://localhost/';
const SERVICE_MAP = JSON.stringify({
  format_version: 2,
  services_by_instance: {},
  label_to_name_by_instance: { [`${AGENT_ID}@${HOST_ID}`]: { [SHELL_LABEL]: 'system_interface', 'chat-wxyz5678': 'chat' } },
});

globalThis.__shellClientIdHarness = {
  phase,
  shellUrl: SHELL_URL,
  appUrl: APP_URL,
  nestedUrl: NESTED_URL,
  isSeeded: false,
  imported: null,
  isReady: false,
  uncaughtErrors: [],
};
const harness = globalThis.__shellClientIdHarness;
process.on('uncaughtException', (error) => harness.uncaughtErrors.push(String(error && error.message)));

const STUDIO_PAGE = `<!doctype html><html><body>
<script>window.__hasBridge = typeof window.mindsNative !== 'undefined';</script>
<iframe src="${SHELL_URL}"></iframe>
<iframe src="${APP_URL}"></iframe>
</body></html>`;
const SHELL_PAGE = `<!doctype html><html><body>
<script>
  window.__hasBridge = typeof window.mindsNative !== 'undefined';
  window.__bootId = localStorage.getItem('si-client-id');
  if (!window.__bootId) {
    window.__bootId = 'minted-by-shell-' + crypto.randomUUID();
    localStorage.setItem('si-client-id', window.__bootId);
  }
</script>
<iframe src="${NESTED_URL}"></iframe>
</body></html>`;
const OTHER_PAGE = `<!doctype html><html><body>
<script>window.__hasBridge = typeof window.mindsNative !== 'undefined';</script>
</body></html>`;

function answerEverything(ses) {
  const html = (body) => new Response(body, { headers: { 'content-type': 'text/html' } });
  ses.protocol.handle('http', () => html(STUDIO_PAGE));
  ses.protocol.handle('https', (request) => html(new URL(request.url).hostname.startsWith(SHELL_LABEL) ? SHELL_PAGE : OTHER_PAGE));
}

function openWindow(ses, { withPreload }) {
  const webPreferences = { session: ses, contextIsolation: true, nodeIntegration: false };
  if (withPreload) Object.assign(webPreferences, { preload: PRELOAD_PATH, nodeIntegrationInSubFrames: true });
  const win = new BrowserWindow({ width: 500, height: 300, show: false, webPreferences });
  return win.loadURL(STUDIO_PAGE_URL).then(() => win);
}

function shellFrameOf(win) {
  return win.webContents.mainFrame.frames.find((frame) => shellHostnameOf(frame.url) !== null);
}

async function seed() {
  const legacy = session.defaultSession;
  const devicePartition = session.fromPartition('persist:account-local');
  answerEverything(legacy);
  answerEverything(devicePartition);
  const legacyWindow = await openWindow(legacy, { withPreload: false });
  await shellFrameOf(legacyWindow).executeJavaScript("localStorage.setItem('si-client-id', '6a8bc152-a4af-4c08-a240-7d80ba93757e')");
  const partitionWindow = await openWindow(devicePartition, { withPreload: false });
  await shellFrameOf(partitionWindow).executeJavaScript("localStorage.setItem('si-client-id', '3e0a6136-c0dd-4bc9-a247-38995b8ece3b')");
  legacy.flushStorageData();
  devicePartition.flushStorageData();
  harness.isSeeded = true;
}

async function run() {
  const stateDir = app.getPath('userData');
  const store = createShellClientIds(stateDir, { mintClientId: () => crypto.randomUUID(), warn: (message) => console.warn(message) });
  const serviceMapPath = path.join(stateDir, 'service_map.json');
  fs.writeFileSync(serviceMapPath, SERVICE_MAP);
  const result = await runLegacyShellClientIdImport({
    store,
    stateDir,
    serviceMapPath,
    forwardPort: FORWARD_PORT,
    readLegacyShellClientIds: (origins) => readLegacyShellClientIds({ stateDir, origins, timeoutMs: 15000 }),
  });
  harness.imported = Object.fromEntries(result.importedIdsByHostname);

  // As main.js's shellClientIdForSender, with the Studio page's origin as the trusted page.
  ipcMain.on('shell-client-id', (event, reportedId) => {
    let clientId = null;
    try {
      const isStudioPage = (page) => new URL(page.url).origin === new URL(STUDIO_PAGE_URL).origin;
      const hostname = askingShellHostname(event.senderFrame, event.sender.mainFrame, isStudioPage);
      clientId = hostname === null ? null : store.clientIdFor(hostname, reportedId);
    } finally {
      event.returnValue = clientId;
    }
  });

  for (const partition of ['persist:account-local', 'persist:account-0b8d1c5e']) {
    const ses = session.fromPartition(partition);
    answerEverything(ses);
    await openWindow(ses, { withPreload: true });
  }
  harness.isReady = true;
}

Menu.setApplicationMenu(null);
// As main.js: the import's hidden window is the only one while it reads, and
// Electron quits when the last window closes unless something listens.
app.on('window-all-closed', () => {});

app.whenReady().then(async () => {
  try {
    if (phase === 'seed') await seed();
    else await run();
  } catch (error) {
    harness.uncaughtErrors.push(String(error && error.stack));
  }
});
