// Minimal Electron entry for the popup-routing e2e (link-routing.spec.js). It
// wires the shipped createLinkRouter, createExternalOpener and preload.js the
// way electron/main.js does, onto windows whose page stands in for the Imbue
// Studio page: it frames a workspace page on a real `agent-<hex>.localhost`
// origin and relays over the real embed contract, reporting the mounted
// workspace to main as WorkspaceFrame.ts does. Booting the real app needs its backend and a running
// workspace whose template announces opensLinks; this exercises the same main
// process, preload, and contract code against a local server instead.
//
// Two windows: one whose workspace announces opensLinks, one whose workspace
// does not (an older template).
const http = require('http');
const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron');
const { createExternalOpener, createLinkRouter } = require('../../electron/link-routing');

const WORKSPACE_ID = 'agent-0123456789abcdef';
const EMBED_CONTRACT_PATH = path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'static', 'embed_contract.js',
);
const PRELOAD_PATH = path.join(__dirname, '..', '..', 'electron', 'preload.js');
const FRAME_SANDBOX =
  'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals';

// What the spec reads back (and sets: the answer to a mailto/tel prompt)
// through electronApplication.evaluate.
globalThis.__linkRoutingHarness = {
  port: null,
  workspaceId: WORKSPACE_ID,
  mountReports: [],
  prompts: [],
  isPromptAccepted: false,
};

function chromePage(port, opensLinks) {
  const frameUrl = `http://${WORKSPACE_ID}.localhost:${port}/workspace?opensLinks=${opensLinks ? 1 : 0}`;
  return `<!doctype html><html><body>
<a id="chrome-local-link" href="http://127.0.0.1:${port}/local-from-chrome" target="_blank">chrome local link</a>
<iframe id="content-frame" sandbox="${FRAME_SANDBOX}" style="width:600px;height:300px"></iframe>
<script type="module">
  import * as contract from '/embed_contract.js';
  const frame = document.getElementById('content-frame');
  const family = /^(?:[a-z0-9_-]+\\.)*(?:host|agent)-[a-f0-9]+\\.localhost$/i;
  const endpoint = contract.createEmbedderEndpoint({
    getFrameWindow: () => frame.contentWindow,
    isExpectedOrigin: (origin) => family.test(new URL(origin).hostname),
    handlers: {
      [contract.WORKSPACE_READY]: (message) => {
        window.mindsNative.reportWorkspaceLinkHandling({
          workspaceId: '${WORKSPACE_ID}',
          opensLinks: message.opensLinks === true,
        });
        endpoint.send(contract.EMBEDDER_CAPABILITIES, { canPopOut: false, opensExternalLinks: true });
      },
      [contract.OPEN_EXTERNAL]: (message) => window.mindsNative.openExternalLink(message.url),
    },
  });
  window.mindsNative.onOpenLink((url) => endpoint.send(contract.OPEN_LINK, { url }));
  // Navigated only once the endpoint listens, as WorkspaceFrame arms its frame:
  // a readiness announcement sent before then is lost.
  frame.src = '${frameUrl}';
</script>
</body></html>`;
}

function workspacePage(port, opensLinks) {
  const readyPayload = opensLinks ? '{ opensLinks: true }' : '{}';
  return `<!doctype html><html><body>
<a id="app-link" href="http://web.${WORKSPACE_ID}.localhost:${port}/notes" target="_blank">app</a>
<a id="local-link" href="http://127.0.0.1:${port}/local" target="_blank">local</a>
<a id="noreferrer-link" href="http://127.0.0.1:${port}/local-noreferrer" target="_blank" rel="noopener noreferrer">local, no referrer</a>
<a id="external-link" href="https://example.com/from-workspace" target="_blank">external</a>
<a id="mailto-link" href="mailto:someone@example.com" target="_blank">mailto</a>
<script type="module">
  import * as contract from '/embed_contract.js';
  window.__openedLinks = [];
  // As the workspace shell does for an external link no app of it takes: back out to Imbue Studio.
  const endpoint = contract.createWorkspaceEndpoint({
    handlers: {
      [contract.OPEN_LINK]: (message) => {
        window.__openedLinks.push(message.url);
        const host = new URL(message.url).hostname;
        if (host !== '127.0.0.1' && host !== 'localhost' && !host.endsWith('.localhost')) {
          endpoint.send(contract.OPEN_EXTERNAL, { url: message.url });
        }
      },
    },
  });
  endpoint.send(contract.WORKSPACE_READY, ${readyPayload});
</script>
</body></html>`;
}

function serve(port, req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const send = (type, body) => {
    res.writeHead(200, { 'Content-Type': type });
    res.end(body);
  };
  const opensLinks = url.searchParams.get('opensLinks') === '1';
  if (url.pathname === '/embed_contract.js') return send('text/javascript', fs.readFileSync(EMBED_CONTRACT_PATH));
  if (url.hostname === 'localhost' && url.pathname === '/') return send('text/html', chromePage(port, opensLinks));
  if (url.pathname === '/workspace') return send('text/html', workspacePage(port, opensLinks));
  return send('text/html', `<!doctype html><title>${url.hostname}${url.pathname}</title><p>${url.href}</p>`);
}

Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  const server = http.createServer((req, res) => serve(server.address().port, req, res));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  globalThis.__linkRoutingHarness.port = port;
  const chromeOrigin = `http://localhost:${port}`;

  const openExternal = createExternalOpener({
    confirm: async (prompt) => {
      globalThis.__linkRoutingHarness.prompts.push(prompt.detail);
      return globalThis.__linkRoutingHarness.isPromptAccepted;
    },
    open: (url) => {
      shell.openExternal(url).catch(() => {});
    },
  });
  const linkRouter = createLinkRouter({
    openExternal: (url, wc) => void openExternal(url, wc),
    forward: (wc, url) => wc.send('open-link', url),
    appOrigins: () => [chromeOrigin],
    workspaceAliases: (workspaceId) => [workspaceId],
  });
  app.on('web-contents-created', (_event, contents) => linkRouter.install(contents));
  ipcMain.on('workspace-link-handling', (event, report) => {
    globalThis.__linkRoutingHarness.mountReports.push({ webContentsId: event.sender.id, report });
    linkRouter.recordMount(event.sender, report);
  });
  ipcMain.on('open-external-link', (event, url) => void openExternal(url, event.sender));

  for (const opensLinks of [true, false]) {
    const win = new BrowserWindow({
      width: 700,
      height: 400,
      show: true,
      webPreferences: { preload: PRELOAD_PATH, contextIsolation: true, nodeIntegration: false },
    });
    await win.loadURL(`${chromeOrigin}/?opensLinks=${opensLinks ? 1 : 0}`);
  }
});
