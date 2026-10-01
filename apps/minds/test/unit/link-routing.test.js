// Unit tests for where a popup a window's page asks for goes: the default
// browser, back into the mounted workspace (minds:open-link), or a window of
// its own.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  POPUP_ACTIONS,
  isExternalUrl,
  decidePopup,
  createLinkRouter,
} = require('../../electron/link-routing');
const EXTERNALITY_VECTORS = require('../../electron/link-externality-vectors.json');

const { OPEN_EXTERNALLY, FORWARD, ALLOW } = POPUP_ACTIONS;

const BACKEND_ORIGIN = 'http://localhost:8420';
const FORWARDER_ORIGIN = 'https://localhost:8421';
const APP_ORIGINS = [BACKEND_ORIGIN, FORWARDER_ORIGIN];
const AGENT = 'agent-0a1b2c3d4e5f';
const HOST = 'host-9f8e7d6c5b4a';
const OTHER_AGENT = 'agent-77aa88bb99cc';
const ANNOUNCING = { workspaceIds: [AGENT, HOST], opensLinks: true };
const SILENT = { workspaceIds: [AGENT, HOST], opensLinks: false };

const workspaceAddress = (id, app = 'web') => `https://${app}.${id}.localhost:8421/notes?x=1`;
const shellAddress = (id) => `https://${id}.localhost:8421/`;
const LOCAL_URL = 'http://localhost:3000/preview';

function decide(url, { referrer = '', mount = ANNOUNCING } = {}) {
  return decidePopup({ url, referrer, appOrigins: APP_ORIGINS, mount });
}

test('isExternalUrl answers every shared externality vector', () => {
  assert.ok(EXTERNALITY_VECTORS.length > 0);
  for (const vector of EXTERNALITY_VECTORS) {
    assert.equal(isExternalUrl(vector.url), vector.isExternal, `${vector.url}: ${vector.note}`);
  }
});

test('an external link opens in the default browser whatever is mounted', () => {
  for (const mount of [null, SILENT, ANNOUNCING]) {
    assert.equal(decide('https://example.com/', { mount }), OPEN_EXTERNALLY);
    assert.equal(decide('mailto:someone@example.com', { mount }), OPEN_EXTERNALLY);
    // A localhost-looking userinfo is still an external site, even from the workspace.
    assert.equal(decide('http://localhost@evil.com/', { mount, referrer: shellAddress(AGENT) }), OPEN_EXTERNALLY);
  }
});

test("the app's own backend and forwarder pages keep their window, even with a workspace announcing links", () => {
  // Checked before the no-referrer rule: the Imbue Studio page opens popups of
  // its own onto these origins, and a noreferrer one would otherwise be sent
  // into the workspace.
  assert.equal(decide(`${BACKEND_ORIGIN}/settings`), ALLOW);
  assert.equal(decide(`${FORWARDER_ORIGIN}/goto/${AGENT}/`), ALLOW);
  assert.equal(decide(`${FORWARDER_ORIGIN}/goto/${AGENT}/`, { referrer: shellAddress(AGENT) }), ALLOW);
});

test('a window with no workspace mounted keeps opening windows for local links', () => {
  assert.equal(decide(workspaceAddress(AGENT), { mount: null }), ALLOW);
  assert.equal(decide(LOCAL_URL, { mount: null }), ALLOW);
  assert.equal(decide(LOCAL_URL, { mount: null, referrer: `${BACKEND_ORIGIN}/` }), ALLOW);
});

test('a workspace that never announced opensLinks keeps opening windows for local links', () => {
  assert.equal(decide(workspaceAddress(AGENT), { mount: SILENT }), ALLOW);
  assert.equal(decide(workspaceAddress(OTHER_AGENT), { mount: SILENT }), ALLOW);
  assert.equal(decide(LOCAL_URL, { mount: SILENT, referrer: shellAddress(AGENT) }), ALLOW);
  assert.equal(decide(LOCAL_URL, { mount: SILENT }), ALLOW);
});

test("a workspace address goes to the announcing workspace, its own or another workspace's", () => {
  assert.equal(decide(workspaceAddress(AGENT)), FORWARD);
  assert.equal(decide(shellAddress(AGENT)), FORWARD);
  assert.equal(decide(workspaceAddress(HOST, 'terminal')), FORWARD);
  // Another workspace's address is forwarded too: the workspace refuses it
  // with a notice rather than a window opening onto it.
  assert.equal(decide(workspaceAddress(OTHER_AGENT)), FORWARD);
  // Whoever asked: the Imbue Studio page never opens a workspace address itself.
  assert.equal(decide(workspaceAddress(AGENT), { referrer: `${BACKEND_ORIGIN}/workspace/${AGENT}` }), FORWARD);
});

test("a local link goes to the announcing workspace when its own pages asked for it", () => {
  assert.equal(decide(LOCAL_URL, { referrer: shellAddress(AGENT) }), FORWARD);
  assert.equal(decide(LOCAL_URL, { referrer: workspaceAddress(AGENT, 'chat') }), FORWARD);
  // The legacy host-keyed alias of the mounted workspace is the same workspace.
  assert.equal(decide('http://127.0.0.1:8080/', { referrer: shellAddress(HOST) }), FORWARD);
  assert.equal(decide('http://[::1]:9000/', { referrer: workspaceAddress(AGENT.toUpperCase()) }), FORWARD);
});

test('a local link with no referrer (a noreferrer link) goes to the announcing workspace', () => {
  assert.equal(decide(LOCAL_URL, { referrer: '' }), FORWARD);
  assert.equal(decide('http://app.localhost:5173/', { referrer: '' }), FORWARD);
});

test('a local link some other page asked for keeps its window', () => {
  // The Imbue Studio page itself.
  assert.equal(decide(LOCAL_URL, { referrer: `${BACKEND_ORIGIN}/` }), ALLOW);
  // Another workspace's page (not the one this window shows).
  assert.equal(decide(LOCAL_URL, { referrer: shellAddress(OTHER_AGENT) }), ALLOW);
  // A third-party page framed inside the workspace, or a plain local server.
  assert.equal(decide(LOCAL_URL, { referrer: 'https://example.com/embed' }), ALLOW);
  assert.equal(decide(LOCAL_URL, { referrer: 'http://localhost:4000/' }), ALLOW);
});

test('a non-http link that is not external keeps its window', () => {
  // Nothing the workspace could route: a scratch window a page writes into, a blob.
  assert.equal(decide('about:blank', { referrer: shellAddress(AGENT) }), ALLOW);
  assert.equal(decide('about:blank'), ALLOW);
  assert.equal(decide(`blob:${shellAddress(AGENT)}0f1e2d3c`), ALLOW);
});

// A stand-in for an Electron webContents: the window-open handler and the
// frame-navigation listener the router installs.
function makeWebContents() {
  return {
    windowOpenHandler: null,
    listeners: {},
    setWindowOpenHandler(handler) {
      this.windowOpenHandler = handler;
    },
    on(event, listener) {
      this.listeners[event] = listener;
    },
    openPopup(url, referrerUrl = '') {
      return this.windowOpenHandler({ url, referrer: { url: referrerUrl, policy: 'default' } });
    },
  };
}

function makeRouter() {
  const opened = [];
  const forwarded = [];
  const router = createLinkRouter({
    openExternal: (url, webContents) => opened.push([url, webContents]),
    forward: (webContents, url) => forwarded.push([webContents, url]),
    appOrigins: () => APP_ORIGINS,
    workspaceAliases: (id) => (id === AGENT ? [AGENT, HOST] : [id]),
  });
  return { router, opened, forwarded };
}

test("the router denies a forwarded popup and hands its URL to the page of the window it came from", () => {
  const { router, opened, forwarded } = makeRouter();
  const mainWindow = makeWebContents();
  const popoutWindow = makeWebContents();
  router.install(mainWindow);
  router.install(popoutWindow);
  assert.equal(router.recordMount(mainWindow, { workspaceId: AGENT, opensLinks: true }), true);
  assert.equal(router.recordMount(popoutWindow, { workspaceId: OTHER_AGENT, opensLinks: false }), true);

  assert.deepEqual(mainWindow.openPopup(workspaceAddress(AGENT)), { action: 'deny' });
  // The host-keyed alias of the mounted workspace, through the aliases main knows.
  assert.deepEqual(mainWindow.openPopup(LOCAL_URL, shellAddress(HOST)), { action: 'deny' });
  // Each window answers for its own mount: this one's workspace never announced.
  assert.deepEqual(popoutWindow.openPopup(workspaceAddress(OTHER_AGENT)), { action: 'allow' });
  assert.deepEqual(mainWindow.openPopup('https://example.com/'), { action: 'deny' });

  assert.deepEqual(forwarded, [
    [mainWindow, workspaceAddress(AGENT)],
    [mainWindow, LOCAL_URL],
  ]);
  assert.deepEqual(opened, [['https://example.com/', mainWindow]]);
});

test('the router forgets a mount when the page reports none or goes away', () => {
  const { router, forwarded } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true });
  router.recordMount(wc, null);
  assert.deepEqual(wc.openPopup(workspaceAddress(AGENT)), { action: 'allow' });
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true });
  router.forgetMount(wc);
  assert.deepEqual(wc.openPopup(workspaceAddress(AGENT)), { action: 'allow' });
  assert.deepEqual(forwarded, []);
});

test('the router ignores an off-shape mount report and keeps the last good one', () => {
  const { router } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true });
  for (const report of [
    undefined,
    'agent-0a1b',
    { workspaceId: '../escape', opensLinks: true },
    { workspaceId: 'agent-xyz', opensLinks: true },
    { workspaceId: AGENT, opensLinks: 'yes' },
    { workspaceId: AGENT },
  ]) {
    assert.equal(router.recordMount(wc, report), false, JSON.stringify(report));
  }
  assert.deepEqual(wc.openPopup(workspaceAddress(AGENT)), { action: 'deny' });
});

test('an in-place navigation to an external site opens in the browser instead', () => {
  const { router, opened } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  const prevented = [];
  const navigate = (url) =>
    wc.listeners['will-frame-navigate']({ url, preventDefault: () => prevented.push(url) });
  navigate('https://example.com/');
  navigate(workspaceAddress(AGENT));
  navigate(LOCAL_URL);
  assert.deepEqual(prevented, ['https://example.com/']);
  assert.deepEqual(opened, [['https://example.com/', wc]]);
});
