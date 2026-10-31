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
  isCustomSchemeUrl,
  decidePopup,
  decideFrameNavigation,
  externalOpenPrompt,
  createExternalOpener,
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
const ANNOUNCING = { workspaceIds: [AGENT, HOST], opensLinks: true, opensCustomSchemeLinks: false };
const TAKES_SCHEMES = { workspaceIds: [AGENT, HOST], opensLinks: true, opensCustomSchemeLinks: true };
const SILENT = { workspaceIds: [AGENT, HOST], opensLinks: false, opensCustomSchemeLinks: false };
const CUSTOM_SCHEME_URLS = ['paseo://open?workspace=1', 'slack://channel?id=C1', 'vscode://file/tmp/a.txt'];

const workspaceAddress = (id, app = 'web') => `https://${app}.${id}.localhost:8421/notes?x=1`;
const shellAddress = (id) => `https://${id}.localhost:8421/`;
const LOCAL_URL = 'http://localhost:3000/preview';

function decide(url, { referrer = '', mount = ANNOUNCING } = {}) {
  return decidePopup({ url, referrer, appOrigins: APP_ORIGINS, mount });
}

test('isExternalUrl and isCustomSchemeUrl answer every shared externality vector', () => {
  assert.ok(EXTERNALITY_VECTORS.length > 0);
  for (const vector of EXTERNALITY_VECTORS) {
    assert.equal(isExternalUrl(vector.url), vector.isExternal, `${vector.url}: ${vector.note}`);
    assert.equal(isCustomSchemeUrl(vector.url), vector.isCustomScheme, `${vector.url}: ${vector.note}`);
  }
});

test('an external link opens outside the app when no announcing workspace asked for it', () => {
  for (const mount of [null, SILENT]) {
    assert.equal(decide('https://example.com/', { mount, referrer: shellAddress(AGENT) }), OPEN_EXTERNALLY);
    assert.equal(decide('mailto:someone@example.com', { mount }), OPEN_EXTERNALLY);
  }
  // The Imbue Studio page's own, another workspace's, and an unattributable (noreferrer) one: the Imbue Studio
  // page opens noreferrer links of its own, which are not the workspace's to route.
  for (const referrer of [`${BACKEND_ORIGIN}/`, shellAddress(OTHER_AGENT), '']) {
    assert.equal(decide('https://example.com/', { referrer }), OPEN_EXTERNALLY);
    assert.equal(decide('tel:+15551234567', { referrer }), OPEN_EXTERNALLY);
  }
});

test("an external link the announcing workspace's own pages asked for goes to the workspace", () => {
  assert.equal(decide('https://example.com/', { referrer: shellAddress(AGENT) }), FORWARD);
  assert.equal(decide('mailto:someone@example.com', { referrer: workspaceAddress(AGENT, 'chat') }), FORWARD);
  assert.equal(decide('tel:+15551234567', { referrer: workspaceAddress(HOST) }), FORWARD);
  // A localhost-looking userinfo is an external site, and the workspace routes it as one.
  assert.equal(decide('http://localhost@evil.com/', { referrer: shellAddress(AGENT) }), FORWARD);
  // So is one of the workspace's share addresses, which the workspace alone can tell from any other site.
  assert.equal(decide('https://web-ab12cd34.0123456789abcdef0123456789abcdef.us1.example.com/', { referrer: shellAddress(AGENT) }), FORWARD);
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

// Stand-ins for Electron's WebFrameMain: a disposed one throws on `url`.
const liveFrame = (url) => ({ url, isDestroyed: () => false });
const disposedFrame = () => ({
  get url() {
    throw new Error('Render frame was disposed before WebFrameMain could be accessed');
  },
  isDestroyed: () => true,
});

function makeRouter() {
  const opened = [];
  const forwarded = [];
  const router = createLinkRouter({
    openExternal: (url, webContents, workspaceId) => opened.push([url, webContents, workspaceId]),
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
  assert.equal(router.recordMount(mainWindow, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false }), true);
  assert.equal(router.recordMount(popoutWindow, { workspaceId: OTHER_AGENT, opensLinks: false, opensCustomSchemeLinks: false }), true);

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
  assert.deepEqual(opened, [['https://example.com/', mainWindow, null]]);
});

test('the router forgets a mount when the page reports none or goes away', () => {
  const { router, forwarded } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false });
  router.recordMount(wc, null);
  assert.deepEqual(wc.openPopup(workspaceAddress(AGENT)), { action: 'allow' });
  assert.equal(router.workspaceOf(wc), null);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false });
  assert.equal(router.workspaceOf(wc), AGENT);
  router.forgetMount(wc);
  assert.deepEqual(wc.openPopup(workspaceAddress(AGENT)), { action: 'allow' });
  assert.equal(router.workspaceOf(wc), null);
  assert.deepEqual(forwarded, []);
});

test('the router ignores an off-shape mount report and keeps the last good one', () => {
  const { router } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false });
  for (const report of [
    undefined,
    'agent-0a1b',
    { workspaceId: '../escape', opensLinks: true },
    { workspaceId: 'agent-xyz', opensLinks: true },
    { workspaceId: AGENT, opensLinks: 'yes', opensCustomSchemeLinks: false },
    { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: 'yes' },
    { workspaceId: AGENT, opensLinks: true },
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
    wc.listeners['will-frame-navigate']({ url, isMainFrame: false, frame: liveFrame(shellAddress(AGENT)), preventDefault: () => prevented.push(url) });
  navigate('https://example.com/');
  navigate(workspaceAddress(AGENT));
  navigate(LOCAL_URL);
  assert.deepEqual(prevented, ['https://example.com/']);
  assert.deepEqual(opened, [['https://example.com/', wc, null]]);
});

test("an announcing workspace's in-place navigation to an external site goes to the workspace", () => {
  const { router, opened, forwarded } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  assert.equal(router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false }), true);
  const prevented = [];
  const navigate = (url, frameUrl, isMainFrame = false) =>
    wc.listeners['will-frame-navigate']({ url, isMainFrame, frame: liveFrame(frameUrl), preventDefault: () => prevented.push(url) });
  navigate('mailto:someone@example.com', workspaceAddress(AGENT, 'notes'));
  navigate('https://example.com/a', 'https://example.org/embed');
  navigate('https://example.com/b', `${BACKEND_ORIGIN}/`, true);
  assert.deepEqual(prevented, ['mailto:someone@example.com', 'https://example.com/a', 'https://example.com/b']);
  assert.deepEqual(forwarded, [[wc, 'mailto:someone@example.com']]);
  // The window's own page is not the workspace's.
  assert.deepEqual(opened, [['https://example.com/a', wc, AGENT], ['https://example.com/b', wc, null]]);
});

test('a navigation of a disposed frame is decided as one from an unknown page of the workspace', () => {
  const { router, opened, forwarded } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false });
  const prevented = [];
  const navigate = (url) =>
    wc.listeners['will-frame-navigate']({ url, isMainFrame: false, frame: disposedFrame(), preventDefault: () => prevented.push(url) });
  navigate('paseo://open?workspace=1');
  navigate('https://example.com/');
  assert.deepEqual(prevented, ['https://example.com/']);
  assert.deepEqual(opened, [['https://example.com/', wc, AGENT]]);
  assert.deepEqual(forwarded, []);
});

test("a launch of another app Chromium asks permission for goes through the opener, the workspace's unless the window's own page asked", () => {
  const { router, opened } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: false });
  // A frame of the workspace (a navigation or a server redirect), and the window's own page.
  router.openExternalRequested(wc, { externalURL: 'slack://channel?id=C1', isMainFrame: false, requestingUrl: 'about:blank' });
  router.openExternalRequested(wc, { externalURL: 'mailto:someone@example.com', isMainFrame: true, requestingUrl: `${BACKEND_ORIGIN}/` });
  assert.deepEqual(opened, [
    ['slack://channel?id=C1', wc, AGENT],
    ['mailto:someone@example.com', wc, null],
  ]);
});

test('a frame navigation is forwarded only for an external URL in a frame of the announcing workspace', () => {
  const navigation = (url, frameUrl, { isMainFrame = false, mount = ANNOUNCING } = {}) =>
    decideFrameNavigation({ url, frameUrl, isMainFrame, mount });
  assert.equal(navigation('https://example.com/', workspaceAddress(AGENT)), FORWARD);
  assert.equal(navigation('https://example.com/', workspaceAddress(HOST, 'terminal')), FORWARD);
  assert.equal(navigation('https://example.com/', workspaceAddress(OTHER_AGENT)), OPEN_EXTERNALLY);
  assert.equal(navigation('https://example.com/', workspaceAddress(AGENT), { mount: SILENT }), OPEN_EXTERNALLY);
  assert.equal(navigation('https://example.com/', workspaceAddress(AGENT), { mount: null }), OPEN_EXTERNALLY);
  assert.equal(navigation('https://example.com/', workspaceAddress(AGENT), { isMainFrame: true }), OPEN_EXTERNALLY);
  assert.equal(navigation('https://example.com/', ''), OPEN_EXTERNALLY);
  assert.equal(navigation(LOCAL_URL, workspaceAddress(AGENT)), ALLOW);
  assert.equal(navigation(workspaceAddress(AGENT, 'files'), workspaceAddress(AGENT)), ALLOW);
});

test('a custom-scheme popup opens outside the app instead of a window when no workspace that takes them is mounted', () => {
  for (const url of CUSTOM_SCHEME_URLS) {
    for (const mount of [ANNOUNCING, SILENT, null]) {
      assert.equal(decide(url, { mount, referrer: shellAddress(AGENT) }), OPEN_EXTERNALLY, url);
      assert.equal(decide(url, { mount, referrer: `${BACKEND_ORIGIN}/` }), OPEN_EXTERNALLY, url);
    }
  }
  // Schemes the app loads itself are not another app's.
  for (const url of ['data:text/plain,hi', 'javascript:void(0)', 'ws://localhost:8000/socket']) {
    assert.equal(decide(url, { referrer: shellAddress(AGENT) }), ALLOW, url);
  }
});

test('a custom-scheme popup goes to a workspace that takes them when its own pages, or a noreferrer link, asked for it', () => {
  const mount = TAKES_SCHEMES;
  for (const url of CUSTOM_SCHEME_URLS) {
    assert.equal(decide(url, { mount, referrer: shellAddress(AGENT) }), FORWARD, url);
    assert.equal(decide(url, { mount, referrer: workspaceAddress(HOST, 'chat') }), FORWARD, url);
    // The Imbue Studio page opens no custom-scheme link itself, so a noreferrer one is the workspace's.
    assert.equal(decide(url, { mount, referrer: '' }), FORWARD, url);
    assert.equal(decide(url, { mount, referrer: `${BACKEND_ORIGIN}/` }), OPEN_EXTERNALLY, url);
    assert.equal(decide(url, { mount, referrer: shellAddress(OTHER_AGENT) }), OPEN_EXTERNALLY, url);
  }
  // Any other external link with no referrer (a mailto: link) is still the Imbue Studio page's own.
  assert.equal(decide('mailto:someone@example.com', { mount, referrer: '' }), OPEN_EXTERNALLY);
});

test("a custom-scheme navigation of any frame but the window's own page goes to a workspace that takes them", () => {
  const navigation = (url, { frameUrl = 'about:blank', isMainFrame = false, mount = TAKES_SCHEMES } = {}) =>
    decideFrameNavigation({ url, frameUrl, isMainFrame, mount });
  for (const url of CUSTOM_SCHEME_URLS) {
    // A workspace app's hidden launcher frame, a page of the workspace, and a frame it embeds.
    assert.equal(navigation(url), FORWARD, url);
    assert.equal(navigation(url, { frameUrl: workspaceAddress(AGENT) }), FORWARD, url);
    assert.equal(navigation(url, { frameUrl: 'https://example.org/embed' }), FORWARD, url);
    // Otherwise Chromium hands it to its app, asking through openExternalRequested.
    assert.equal(navigation(url, { isMainFrame: true }), ALLOW, url);
    assert.equal(navigation(url, { mount: ANNOUNCING }), ALLOW, url);
    assert.equal(navigation(url, { mount: null }), ALLOW, url);
  }
});

test('the router hands a custom-scheme launch to a workspace that takes them, from a navigation or a server redirect', () => {
  const { router, opened, forwarded } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  assert.equal(router.recordMount(wc, { workspaceId: AGENT, opensLinks: true, opensCustomSchemeLinks: true }), true);
  const prevented = [];
  wc.listeners['will-frame-navigate']({
    url: 'slack://channel?id=C1',
    isMainFrame: false,
    frame: liveFrame('about:blank'),
    preventDefault: () => prevented.push('slack://channel?id=C1'),
  });
  router.openExternalRequested(wc, { externalURL: 'vscode://file/a', isMainFrame: false, requestingUrl: 'about:blank' });
  // A redirected mailto link, and a launch from the window's own page, still ask.
  router.openExternalRequested(wc, { externalURL: 'mailto:someone@example.com', isMainFrame: false, requestingUrl: 'about:blank' });
  router.openExternalRequested(wc, { externalURL: 'paseo://open', isMainFrame: true, requestingUrl: `${BACKEND_ORIGIN}/` });
  assert.deepEqual(prevented, ['slack://channel?id=C1']);
  assert.deepEqual(forwarded, [
    [wc, 'slack://channel?id=C1'],
    [wc, 'vscode://file/a'],
  ]);
  assert.deepEqual(opened, [
    ['mailto:someone@example.com', wc, AGENT],
    ['paseo://open', wc, null],
  ]);
});

test("an external popup is the workspace's to always allow only when its own pages asked for it", () => {
  const { router, opened } = makeRouter();
  const wc = makeWebContents();
  router.install(wc);
  router.recordMount(wc, { workspaceId: AGENT, opensLinks: false, opensCustomSchemeLinks: false });
  wc.openPopup('paseo://open', shellAddress(AGENT));
  wc.openPopup('slack://open', `${BACKEND_ORIGIN}/`);
  wc.openPopup('mailto:someone@example.com', workspaceAddress(HOST, 'chat'));
  // The Imbue Studio page opens no custom-scheme link, but noreferrer mailto links of its own.
  wc.openPopup('vscode://file/a', '');
  wc.openPopup('mailto:support@example.com', '');
  assert.deepEqual(opened, [
    ['paseo://open', wc, AGENT],
    ['slack://open', wc, null],
    ['mailto:someone@example.com', wc, AGENT],
    ['vscode://file/a', wc, AGENT],
    ['mailto:support@example.com', wc, null],
  ]);
});

test('a mailto, tel or custom-scheme link asks before it opens, naming the address, number or app; a web link does not ask', () => {
  const mailto = externalOpenPrompt('mailto:someone@example.com?subject=Hi%20there', '');
  assert.equal(mailto.detail, 'someone@example.com');
  assert.equal(externalOpenPrompt('tel:+1%20555%20123%204567', '').detail, '+1 555 123 4567');
  assert.equal(externalOpenPrompt('mailto:?to=someone@example.com', '').detail, 'mailto:?to=someone@example.com');
  assert.equal(externalOpenPrompt('paseo://open?workspace=1', 'Paseo').message, 'Open this link in Paseo?');
  assert.equal(externalOpenPrompt('paseo://open?workspace=1', 'Paseo').detail, 'paseo://open?workspace=1');
  assert.equal(externalOpenPrompt('paseo://open?workspace=1', '').message, 'Open this link in another app?');
  assert.notEqual(externalOpenPrompt('paseo://open', 'Paseo').rememberLabel, mailto.rememberLabel);
  assert.equal(externalOpenPrompt('https://example.com/', ''), null);
});

function makeOpener({ answer = { accepted: false, remember: false }, allowed = [] } = {}) {
  const opened = [];
  const asked = [];
  const stored = new Set(allowed);
  const state = { answer };
  const openExternal = createExternalOpener({
    confirm: async (prompt, webContents) => {
      asked.push([prompt.detail, prompt.rememberLabel !== null, webContents]);
      return state.answer;
    },
    open: (url) => opened.push(url),
    appNameFor: (url) => (url.startsWith('paseo:') ? 'Paseo' : ''),
    permissions: {
      isAllowed: (workspaceId, scheme) => stored.has(`${workspaceId} ${scheme}`),
      allow: (workspaceId, scheme) => stored.add(`${workspaceId} ${scheme}`),
    },
  });
  return { openExternal, opened, asked, stored, state };
}

test('the external opener opens a web link at once and any other only once the user agrees', async () => {
  const { openExternal, opened, asked, state } = makeOpener();
  await openExternal('https://example.com/', 'wc', AGENT);
  await openExternal('mailto:someone@example.com', 'wc', null);
  await openExternal('paseo://open', 'wc', AGENT);
  state.answer = { accepted: true, remember: false };
  await openExternal('tel:+15551234567', 'wc', null);
  await openExternal('paseo://open', 'wc', AGENT);
  // Always-allow is offered only for a link a workspace asked for.
  assert.deepEqual(asked, [
    ['someone@example.com', false, 'wc'],
    ['paseo://open', true, 'wc'],
    ['+15551234567', false, 'wc'],
    ['paseo://open', true, 'wc'],
  ]);
  assert.deepEqual(opened, ['https://example.com/', 'tel:+15551234567', 'paseo://open']);
});

test("an always-allowed scheme opens without asking, for that workspace only", async () => {
  const { openExternal, opened, asked, stored, state } = makeOpener({ answer: { accepted: true, remember: true } });
  await openExternal('paseo://open?a', 'wc', AGENT);
  assert.deepEqual([...stored], [`${AGENT} paseo:`]);
  await openExternal('paseo://open?b', 'wc', AGENT);
  await openExternal('mailto:someone@example.com', 'wc', AGENT);
  state.answer = { accepted: false, remember: true };
  await openExternal('paseo://open?c', 'wc', OTHER_AGENT);
  await openExternal('paseo://open?d', 'wc', null);
  assert.deepEqual(
    asked.map(([detail]) => detail),
    ['paseo://open?a', 'someone@example.com', 'paseo://open?c', 'paseo://open?d'],
  );
  assert.deepEqual(opened, ['paseo://open?a', 'paseo://open?b', 'mailto:someone@example.com']);
  // Remembering from no workspace stores nothing; a declined prompt stores nothing either.
  state.answer = { accepted: true, remember: true };
  await openExternal('slack://open', 'wc', null);
  assert.deepEqual([...stored], [`${AGENT} paseo:`, `${AGENT} mailto:`]);
});

test('a window gets one question per scheme at a time; launches while it is open are dropped', async () => {
  const opened = [];
  const asked = [];
  const pendingAnswers = [];
  const openExternal = createExternalOpener({
    confirm: (prompt, webContents) => {
      asked.push([prompt.detail, webContents]);
      return new Promise((resolve) => pendingAnswers.push(resolve));
    },
    open: (url) => opened.push(url),
    appNameFor: () => 'Paseo',
    permissions: { isAllowed: () => false, allow: () => {} },
  });
  const first = openExternal('paseo://open?a', 'wc', AGENT);
  const dropped = openExternal('paseo://open?b', 'wc', AGENT);
  const otherScheme = openExternal('mailto:someone@example.com', 'wc', AGENT);
  const otherWindow = openExternal('paseo://open?c', 'other-wc', AGENT);
  await dropped;
  assert.deepEqual(asked, [['paseo://open?a', 'wc'], ['someone@example.com', 'wc'], ['paseo://open?c', 'other-wc']]);
  for (const answer of pendingAnswers.splice(0)) answer({ accepted: true, remember: false });
  await Promise.all([first, otherScheme, otherWindow]);
  // Once answered, the next launch asks again.
  const next = openExternal('paseo://open?d', 'wc', AGENT);
  for (const answer of pendingAnswers.splice(0)) answer({ accepted: true, remember: false });
  await next;
  assert.deepEqual(opened, ['paseo://open?a', 'mailto:someone@example.com', 'paseo://open?c', 'paseo://open?d']);
});
