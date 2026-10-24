// Minimal Electron entry for the link-routing e2e (link-routing.spec.js). It
// wires the shipped createLinkRouter, createExternalOpener and preload.js the
// way electron/main.js does, onto windows whose page stands in for the Imbue
// Studio page: it frames a workspace page on a real `agent-<hex>.localhost`
// origin and relays over the real embed contract, reporting the mounted
// workspace to main as WorkspaceFrame.ts does. Booting the real app needs its
// backend and a running workspace whose template announces opensLinks; this
// exercises the same main process, preload, and contract code against a local
// server instead.
//
// Three windows: one whose workspace announces opensLinks, one whose
// workspace does not (an older template), and one whose workspace announces
// opensCustomSchemeLinks too.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, Menu, ipcMain, session, shell } = require('electron');
const { createExternalOpener, createLinkRouter } = require('../../electron/link-routing');
const { createExternalLinkPermissions } = require('../../electron/external-link-permissions');

const WORKSPACE_ID = 'agent-0123456789abcdef';
const EMBED_CONTRACT_PATH = path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'static', 'embed_contract.js',
);
const PRELOAD_PATH = path.join(__dirname, '..', '..', 'electron', 'preload.js');
const FRAME_SANDBOX =
  'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals';

// What the spec reads back (and sets: the answer to an ask-first prompt,
// Always allow included) through electronApplication.evaluate.
globalThis.__linkRoutingHarness = {
  port: null,
  workspaceId: WORKSPACE_ID,
  mountReports: [],
  prompts: [],
  promptAnswer: { accepted: false, remember: false },
  frameNavigations: [],
  uncaughtErrors: [],
};

// The packaged app shows an uncaught main-process exception as an error dialog.
process.on('uncaughtException', (error) => {
  globalThis.__linkRoutingHarness.uncaughtErrors.push(String(error && error.message));
});

function chromePage(port, query) {
  const frameUrl = `http://${WORKSPACE_ID}.localhost:${port}/workspace?${query}`;
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
          opensCustomSchemeLinks: message.opensCustomSchemeLinks === true,
        });
        endpoint.send(contract.EMBEDDER_CAPABILITIES, {
          canPopOut: false,
          opensExternalLinks: true,
          opensCustomSchemeLinks: true,
        });
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

function workspacePage(port, opensLinks, opensCustomSchemeLinks) {
  const readyPayload = JSON.stringify({ ...(opensLinks && { opensLinks }), ...(opensCustomSchemeLinks && { opensCustomSchemeLinks }) });
  return `<!doctype html><html><body>
<a id="app-link" href="http://web.${WORKSPACE_ID}.localhost:${port}/notes" target="_blank">app</a>
<a id="local-link" href="http://127.0.0.1:${port}/local" target="_blank">local</a>
<a id="noreferrer-link" href="http://127.0.0.1:${port}/local-noreferrer" target="_blank" rel="noopener noreferrer">local, no referrer</a>
<a id="external-link" href="https://example.com/from-workspace" target="_blank">external</a>
<a id="mailto-link" href="mailto:someone@example.com" target="_blank">mailto</a>
<a id="scheme-link" href="imbue-e2e-unhandled://from-workspace" target="_blank">custom scheme</a>
<button id="scheme-launcher" type="button">launch a custom scheme</button>
<button id="scheme-redirector" type="button" data-to="imbue-e2e-unhandled://redirected">launch a custom scheme through a redirect</button>
<button id="mailto-redirector" type="button" data-to="mailto:redirected@example.com">open a mailto link through a redirect</button>
<script type="module">
  // As a workspace app hands a custom-scheme URL to the OS: one hidden iframe, reused for every launch.
  document.getElementById('scheme-launcher').addEventListener('click', () => {
    let launcher = document.getElementById('scheme-launcher-frame');
    if (!launcher) {
      launcher = document.createElement('iframe');
      launcher.id = 'scheme-launcher-frame';
      launcher.hidden = true;
      document.body.appendChild(launcher);
    }
    launcher.src = 'imbue-e2e-unhandled://open';
  });
  // A local page answering with a redirect to a link for another app, which no will-frame-navigate reports:
  // only the openExternal permission request sees it.
  for (const redirectorButton of document.querySelectorAll('[data-to]')) {
    redirectorButton.addEventListener('click', () => {
      const redirector = document.createElement('iframe');
      redirector.hidden = true;
      redirector.src = '/redirect?to=' + encodeURIComponent(redirectorButton.dataset.to);
      document.body.appendChild(redirector);
    });
  }
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
  const opensCustomSchemeLinks = url.searchParams.get('schemes') === '1';
  if (url.pathname === '/embed_contract.js') return send('text/javascript', fs.readFileSync(EMBED_CONTRACT_PATH));
  if (url.hostname === 'localhost' && url.pathname === '/') return send('text/html', chromePage(port, url.search.slice(1)));
  if (url.pathname === '/workspace') return send('text/html', workspacePage(port, opensLinks, opensCustomSchemeLinks));
  if (url.pathname === '/redirect') {
    res.writeHead(302, { Location: url.searchParams.get('to') });
    return res.end();
  }
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
      globalThis.__linkRoutingHarness.prompts.push({ detail: prompt.detail, canRemember: prompt.rememberLabel !== null });
      return globalThis.__linkRoutingHarness.promptAnswer;
    },
    appNameFor: (url) => app.getApplicationNameForProtocol(url),
    permissions: createExternalLinkPermissions(app.getPath('userData'), (message) => console.warn(message)),
    open: (url) => {
      shell.openExternal(url).catch(() => {});
    },
  });
  const linkRouter = createLinkRouter({
    openExternal: (url, wc, workspaceId) => void openExternal(url, wc, workspaceId),
    forward: (wc, url) => wc.send('open-link', url),
    appOrigins: () => [chromeOrigin],
    workspaceAliases: (workspaceId) => [workspaceId],
  });
  app.on('web-contents-created', (_event, contents) => {
    // Ahead of the router's listener, so a navigation is seen even when that listener throws.
    contents.on('will-frame-navigate', (details) => globalThis.__linkRoutingHarness.frameNavigations.push(details.url));
    linkRouter.install(contents);
  });
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    if (permission !== 'openExternal') {
      callback(true);
      return;
    }
    callback(false);
    linkRouter.openExternalRequested(wc, details);
  });
  ipcMain.on('workspace-link-handling', (event, report) => {
    globalThis.__linkRoutingHarness.mountReports.push({ webContentsId: event.sender.id, report });
    linkRouter.recordMount(event.sender, report);
  });
  ipcMain.on('open-external-link', (event, url) => void openExternal(url, event.sender, linkRouter.workspaceOf(event.sender)));

  for (const query of ['opensLinks=1', 'opensLinks=0', 'opensLinks=1&schemes=1']) {
    const win = new BrowserWindow({
      width: 700,
      height: 400,
      show: true,
      webPreferences: { preload: PRELOAD_PATH, contextIsolation: true, nodeIntegration: false },
    });
    await win.loadURL(`${chromeOrigin}/?${query}`);
  }
});
