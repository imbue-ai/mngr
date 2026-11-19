// Minimal Electron entry for the client-permission e2e (client-permission.spec.js).
// It wires the shipped createClientPermissionGate, createLinkRouter and
// preload.js the way electron/main.js does, onto one window whose page stands
// in for the Imbue Studio page: it frames a workspace page on a real
// `chat.agent-<hex>.localhost` origin, reports the mounted workspace to main as
// WorkspaceFrame.ts does, and records the prompts main asks it to draw. The
// backend's decision route is stood in for by `stored`, which the spec sets;
// capture runs on Chromium's fake audio device, so no microphone is touched.
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const { app, BrowserWindow, Menu, ipcMain, session } = require('electron');
const { createLinkRouter } = require('../../electron/link-routing');
const { decidePermissionRequest, originOf } = require('../../electron/immersive-policy');
const { clientPermissionNameOf, createClientPermissionGate } = require('../../electron/client-permission-policy');

const WORKSPACE_ID = 'agent-0123456789abcdef';
const PRELOAD_PATH = path.join(__dirname, '..', '..', 'electron', 'preload.js');

app.commandLine.appendSwitch('use-fake-device-for-media-stream');

// What the spec reads back, and sets (`stored`: the decision route's answer,
// null for a backend that cannot answer), through electronApplication.evaluate.
globalThis.__clientPermissionHarness = {
  port: null,
  workspaceId: WORKSPACE_ID,
  stored: { decision: 'ask', source: 'default', workspaceName: 'Research notes' },
  fetches: [],
  requests: [],
  toasts: [],
  uncaughtErrors: [],
};
const harness = globalThis.__clientPermissionHarness;

process.on('uncaughtException', (error) => {
  harness.uncaughtErrors.push(String(error && error.message));
});

function chromePage(port) {
  return `<!doctype html><html><body>
<iframe id="content-frame" allow="microphone *" style="width:600px;height:300px"></iframe>
<script>
  window.__prompts = [];
  window.mindsNative.onClientPermissionPrompt((prompt) => window.__prompts.push(prompt));
  window.tryMicrophone = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const track of stream.getTracks()) track.stop();
      return 'granted';
    } catch (error) {
      return error.name;
    }
  };
  window.mindsNative.reportWorkspaceLinkHandling({
    workspaceId: '${WORKSPACE_ID}',
    opensLinks: false,
    opensCustomSchemeLinks: false,
  });
  document.getElementById('content-frame').src = 'http://chat.${WORKSPACE_ID}.localhost:${port}/workspace';
</script>
</body></html>`;
}

const WORKSPACE_PAGE = `<!doctype html><html><body>
<script>
  window.tryMicrophone = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const track of stream.getTracks()) track.stop();
      return 'granted';
    } catch (error) {
      return error.name;
    }
  };
</script>
</body></html>`;

function serve(port, req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  res.writeHead(200, { 'Content-Type': 'text/html' });
  if (url.hostname === 'localhost' && url.pathname === '/') return res.end(chromePage(port));
  if (url.pathname === '/workspace') return res.end(WORKSPACE_PAGE);
  return res.end(`<!doctype html><p>${url.href}</p>`);
}

Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  const server = http.createServer((req, res) => serve(server.address().port, req, res));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  harness.port = port;
  const chromeOrigin = `http://localhost:${port}`;

  const linkRouter = createLinkRouter({
    openExternal: () => {},
    forward: () => {},
    appOrigins: () => [chromeOrigin],
    workspaceAliases: (workspaceId) => [workspaceId],
  });
  const gate = createClientPermissionGate({
    mountOf: (wc) => linkRouter.mountOf(wc),
    mountShowing: (workspaceId) => linkRouter.mountShowing(workspaceId),
    fetchDecision: async (workspaceId, permission) => {
      harness.fetches.push({ workspaceId, permission });
      return harness.stored;
    },
    sendPrompt: (wc, prompt) => {
      if (wc.isDestroyed()) return false;
      wc.send('client-permission-prompt', prompt);
      return true;
    },
    showToast: (_wc, toast) => harness.toasts.push(toast),
    newPromptId: () => crypto.randomUUID(),
    warn: (message) => console.warn(message),
    cacheTtlMs: 0,
    promptTimeoutMs: 60_000,
  });

  app.on('web-contents-created', (_event, contents) => {
    linkRouter.install(contents);
    contents.on('destroyed', () => gate.forgetWebContents(contents));
  });
  // As installPermissionHandlersOn in main.js, on the one session this window uses.
  const decide = (contents, permission) =>
    decidePermissionRequest({
      permission,
      topLevelOrigin: originOf(contents && !contents.isDestroyed() ? contents.getURL() : null),
      chromeOrigin,
    });
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    harness.requests.push({ permission, mediaTypes: details.mediaTypes, requestingUrl: details.requestingUrl });
    const isAllowed = decide(contents, permission);
    const clientPermission = isAllowed ? clientPermissionNameOf(permission, details) : null;
    if (clientPermission === null) callback(isAllowed);
    else gate.request(contents, clientPermission, details && details.requestingUrl, callback);
  });
  session.defaultSession.setPermissionCheckHandler((contents, permission, _requestingOrigin, details) => {
    if (!decide(contents, permission)) return false;
    const clientPermission = clientPermissionNameOf(permission, details);
    if (clientPermission === null) return true;
    return gate.check(contents || null, clientPermission, details && details.requestingUrl);
  });
  ipcMain.on('workspace-link-handling', (event, report) => {
    if (linkRouter.recordMount(event.sender, report)) gate.mountChanged(event.sender);
  });
  ipcMain.on('client-permission-answer', (event, answer) => gate.answer(answer, event.sender));

  const win = new BrowserWindow({
    width: 700,
    height: 400,
    show: true,
    webPreferences: { preload: PRELOAD_PATH, contextIsolation: true, nodeIntegration: false },
  });
  await win.loadURL(`${chromeOrigin}/`);
});
