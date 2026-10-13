'use strict';

// Unit tests for the embed contract module (the single sanctioned postMessage
// channel between the minds chrome and workspace content). The module runs in
// browsers; these tests stand in a minimal window double so the structural
// source checks, payload validation, and tolerant-unknown-type policy are
// exercised under plain node.

const { test, beforeEach } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MODULE_URL = pathToFileURL(
  path.join(__dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'static', 'embed_contract.js'),
).href;

function makeWindowDouble() {
  const win = {
    listeners: [],
    posted: [],
    addEventListener(type, fn) {
      if (type === 'message') this.listeners.push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners = this.listeners.filter((l) => l !== fn);
    },
    postMessage(data, targetOrigin) {
      this.posted.push({ data, targetOrigin });
    },
    deliver(event) {
      for (const listener of [...this.listeners]) listener(event);
    },
  };
  return win;
}

let contract;
let win;
let parentWin;

beforeEach(async () => {
  contract = await import(MODULE_URL);
  win = makeWindowDouble();
  parentWin = makeWindowDouble();
  win.parent = parentWin;
  global.window = win;
});

test('workspace endpoint dispatches a valid embedder message from the parent', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.CLOSE_ACTIVE_TAB]: (msg) => seen.push(msg.type) },
  });
  win.deliver({ source: parentWin, origin: 'http://chrome', data: { type: contract.CLOSE_ACTIVE_TAB } });
  assert.deepStrictEqual(seen, [contract.CLOSE_ACTIVE_TAB]);
});

test('workspace endpoint ignores messages from non-parent sources', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.CLOSE_ACTIVE_TAB]: (msg) => seen.push(msg.type) },
  });
  const nestedFrame = makeWindowDouble();
  win.deliver({ source: nestedFrame, origin: 'http://evil', data: { type: contract.CLOSE_ACTIVE_TAB } });
  assert.deepStrictEqual(seen, []);
});

test('workspace endpoint ignores unknown and wrong-direction types without throwing', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.CLOSE_ACTIVE_TAB]: (msg) => seen.push(msg.type) },
  });
  win.deliver({ source: parentWin, origin: 'o', data: { type: 'minds:future-type' } });
  // A workspace->embedder type delivered TO the workspace is wrong-direction.
  win.deliver({ source: parentWin, origin: 'o', data: { type: contract.OPEN_HELP } });
  win.deliver({ source: parentWin, origin: 'o', data: 'not-an-object' });
  win.deliver({ source: parentWin, origin: 'o', data: null });
  assert.deepStrictEqual(seen, []);
});

test('workspace endpoint send posts to the parent with the type merged in', () => {
  const endpoint = contract.createWorkspaceEndpoint({ handlers: {} });
  endpoint.send(contract.OPEN_REQUEST_MODAL, { requestId: 'evt-123' });
  assert.strictEqual(parentWin.posted.length, 1);
  assert.deepStrictEqual(parentWin.posted[0].data, { type: contract.OPEN_REQUEST_MODAL, requestId: 'evt-123' });
  assert.strictEqual(parentWin.posted[0].targetOrigin, '*');
});

test('workspace endpoint validates the permission-resolutions payload', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: {
      [contract.PERMISSION_RESOLUTIONS]: (msg) => seen.push(msg.resolutions.map((r) => [r.requestId, r.resolution])),
    },
  });
  const from = (resolutions) =>
    win.deliver({ source: parentWin, origin: 'o', data: { type: contract.PERMISSION_RESOLUTIONS, resolutions } });
  // Only the two verdicts the contract defines, and only ids of the
  // server-issued shape, reach the card; one bad entry rejects the message.
  from([{ requestId: 'evt-a', resolution: 'maybe' }]);
  from([{ requestId: 'evt-1/../admin', resolution: 'granted' }]);
  from([{ resolution: 'granted' }]);
  from('evt-a');
  assert.deepStrictEqual(seen, []);
  // An empty answer is valid ("all still pending"), and good entries pass whole.
  from([]);
  from([
    { requestId: 'evt-a', resolution: 'granted' },
    { requestId: 'evt-b', resolution: 'denied' },
  ]);
  assert.deepStrictEqual(seen, [
    [],
    [
      ['evt-a', 'granted'],
      ['evt-b', 'denied'],
    ],
  ]);
});

test('workspace endpoint validates the focus-chat chat id against the agent-id shape', () => {
  // A chat's id is its first agent's id, so it takes that shape.
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.FOCUS_CHAT]: (msg) => seen.push(msg.chatId) },
  });
  const deliver = (payload) =>
    win.deliver({ source: parentWin, origin: 'http://chrome', data: { type: contract.FOCUS_CHAT, ...payload } });
  deliver({ chatId: 'agent-0a1b2c' });
  deliver({ chatId: '../escape' });
  deliver({ chatId: '' });
  deliver({});
  // The pre-rename field name is not a chat id any more.
  deliver({ agentId: 'agent-0a1b2c' });
  assert.deepStrictEqual(seen, ['agent-0a1b2c']);
});

test('embedder endpoint accepts a workspace readiness announcement', () => {
  const frameWin = makeWindowDouble();
  let readyCount = 0;
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    handlers: {
      [contract.WORKSPACE_READY]: () => {
        readyCount += 1;
      },
    },
  });
  win.deliver({ source: frameWin, origin: 'http://ws', data: { type: contract.WORKSPACE_READY } });
  // Wrong direction: the workspace side never honours its own outbound type.
  contract.createWorkspaceEndpoint({
    handlers: {
      [contract.WORKSPACE_READY]: () => {
        readyCount += 1;
      },
    },
  });
  win.deliver({ source: parentWin, origin: 'http://chrome', data: { type: contract.WORKSPACE_READY } });
  assert.strictEqual(readyCount, 1);
});

test('embedder endpoint requires the frame source and a matching origin', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: (origin) => origin === 'https://host-ab.localhost:8421',
    handlers: { [contract.OPEN_REQUEST_MODAL]: (msg) => seen.push(msg.requestId) },
  });
  const good = { type: contract.OPEN_REQUEST_MODAL, requestId: 'evt-abc' };
  // Wrong source: dropped.
  win.deliver({ source: makeWindowDouble(), origin: 'https://host-ab.localhost:8421', data: good });
  // Wrong origin: dropped.
  win.deliver({ source: frameWin, origin: 'https://evil.example', data: good });
  // Right source + origin: dispatched.
  win.deliver({ source: frameWin, origin: 'https://host-ab.localhost:8421', data: good });
  assert.deepStrictEqual(seen, ['evt-abc']);
});

test('embedder endpoint validates payload shapes before dispatch', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: {
      [contract.OPEN_REQUEST_MODAL]: (msg) => seen.push(['request', msg.requestId]),
      [contract.OPEN_HELP]: (msg) => seen.push(['help', msg.agentId]),
      [contract.OPEN_AI_KEYS_PAGE]: (msg) => seen.push(['keys', msg.hostId]),
      [contract.OPEN_SHARE_SETTINGS]: (msg) => seen.push(['share', msg.serviceName]),
    },
  });
  const from = (data) => win.deliver({ source: frameWin, origin: 'o', data });
  from({ type: contract.OPEN_REQUEST_MODAL, requestId: '../smuggle?x=1' });
  from({ type: contract.OPEN_REQUEST_MODAL });
  from({ type: contract.OPEN_HELP, agentId: 'agent-XYZ!' });
  from({ type: contract.OPEN_AI_KEYS_PAGE, hostId: 'agent-abc123' });
  // serviceName is required: absent, off-shape, and over-length are dropped.
  from({ type: contract.OPEN_SHARE_SETTINGS });
  from({ type: contract.OPEN_SHARE_SETTINGS, serviceName: '' });
  from({ type: contract.OPEN_SHARE_SETTINGS, serviceName: 'web/../admin' });
  from({ type: contract.OPEN_SHARE_SETTINGS, serviceName: 'a'.repeat(65) });
  assert.deepStrictEqual(seen, []);
  from({ type: contract.OPEN_HELP, agentId: 'agent-abc123' });
  from({ type: contract.OPEN_HELP });
  from({ type: contract.OPEN_AI_KEYS_PAGE, hostId: 'host-abc123' });
  from({ type: contract.OPEN_AI_KEYS_PAGE, hostId: '' });
  from({ type: contract.OPEN_SHARE_SETTINGS, serviceName: 'system_interface' });
  assert.deepStrictEqual(seen, [
    ['help', 'agent-abc123'],
    ['help', undefined],
    ['keys', 'host-abc123'],
    ['keys', ''],
    ['share', 'system_interface'],
  ]);
});

test('embedder endpoint send targets the current frame window and no-ops without one', () => {
  let frameWin = null;
  const endpoint = contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    handlers: {},
  });
  endpoint.send(contract.CLOSE_ACTIVE_TAB);
  frameWin = makeWindowDouble();
  endpoint.send(contract.CLOSE_ACTIVE_TAB);
  assert.strictEqual(frameWin.posted.length, 1);
  assert.deepStrictEqual(frameWin.posted[0].data, { type: contract.CLOSE_ACTIVE_TAB });
});

test('dispose unregisters the listener', () => {
  const seen = [];
  const endpoint = contract.createWorkspaceEndpoint({
    handlers: { [contract.CLOSE_ACTIVE_TAB]: () => seen.push(1) },
  });
  endpoint.dispose();
  win.deliver({ source: parentWin, origin: 'o', data: { type: contract.CLOSE_ACTIVE_TAB } });
  assert.deepStrictEqual(seen, []);
});

test('embedder endpoint validates the pull-out message payloads', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: {
      [contract.POP_OUT_WINDOW]: (msg) => seen.push(['out', msg.windowId]),
      [contract.WINDOW_DRAG_STARTED]: (msg) => seen.push(['drag', msg.windowId, msg.grabX]),
      [contract.WINDOW_DRAG_ENDED]: (msg) => seen.push(['ended', msg.windowId, msg.isDetached, msg.isCancelled]),
      [contract.DETACHED_WINDOWS]: (msg) => seen.push(['set', msg.windows.length]),
    },
  });
  const deliver = (data) => win.deliver({ source: frameWin, origin: 'https://agent-1.localhost', data });
  const open = { type: contract.POP_OUT_WINDOW, windowId: 'win-0123456789abcdef', title: 'Notes', width: 640, height: 480 };
  deliver(open);
  // Off-shape ids, a non-positive size, and an over-long title are dropped.
  deliver({ ...open, windowId: 'win-../x' });
  deliver({ ...open, width: 0 });
  deliver({ ...open, title: 'x'.repeat(contract.MAX_WINDOW_TITLE_LENGTH + 1) });
  const drag = { ...open, type: contract.WINDOW_DRAG_STARTED, grabX: 12, grabY: 8 };
  deliver(drag);
  deliver({ ...drag, grabX: Number.NaN });
  deliver({ ...drag, height: -1 });
  deliver({ type: contract.WINDOW_DRAG_ENDED, windowId: 'win-abc', isDetached: true });
  deliver({ type: contract.WINDOW_DRAG_ENDED, windowId: 'win-abc', isDetached: false, isCancelled: true });
  deliver({ type: contract.WINDOW_DRAG_ENDED, windowId: 'win-abc', isDetached: 'yes' });
  deliver({ type: contract.WINDOW_DRAG_ENDED, windowId: 'win-abc', isDetached: false, isCancelled: 'no' });
  deliver({ type: contract.WINDOW_DRAG_ENDED, windowId: 'agent-abc', isDetached: false });
  deliver({ type: contract.DETACHED_WINDOWS, windows: [{ windowId: 'win-abc', title: 'A' }] });
  deliver({ type: contract.DETACHED_WINDOWS, windows: [] });
  deliver({ type: contract.DETACHED_WINDOWS, windows: [{ windowId: 'nope', title: 'A' }] });
  deliver({ type: contract.DETACHED_WINDOWS, windows: 'not-a-list' });
  deliver({
    type: contract.DETACHED_WINDOWS,
    windows: Array.from({ length: contract.MAX_DETACHED_WINDOW_ENTRIES + 1 }, () => ({ windowId: 'win-a', title: '' })),
  });
  assert.deepStrictEqual(seen, [
    ['out', 'win-0123456789abcdef'],
    ['drag', 'win-0123456789abcdef', 12],
    ['ended', 'win-abc', true, undefined],
    ['ended', 'win-abc', false, true],
    ['set', 1],
    ['set', 0],
  ]);
});

test('workspace endpoint validates the capabilities and reattach payloads', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: {
      [contract.EMBEDDER_CAPABILITIES]: (msg) => seen.push(['caps', msg.canPopOut, msg.opensExternalLinks]),
      [contract.REATTACH_WINDOW]: (msg) => seen.push(['reattach', msg.windowId, msg.frame === undefined]),
      [contract.TEAR_OUT]: (msg) => seen.push(['tear', msg.windowId, msg.phase]),
    },
  });
  const deliver = (data) => win.deliver({ source: parentWin, origin: 'http://chrome', data });
  deliver({ type: contract.TEAR_OUT, windowId: 'win-abc', phase: 'out' });
  deliver({ type: contract.TEAR_OUT, windowId: 'win-abc', phase: 'gone' });
  deliver({ type: contract.TEAR_OUT, windowId: 'agent-abc', phase: 'in' });
  deliver({ type: contract.EMBEDDER_CAPABILITIES, canPopOut: true });
  deliver({ type: contract.EMBEDDER_CAPABILITIES, canPopOut: true, opensExternalLinks: true });
  deliver({ type: contract.EMBEDDER_CAPABILITIES, canPopOut: true, opensExternalLinks: 'yes' });
  deliver({ type: contract.EMBEDDER_CAPABILITIES, canPopOut: 'yes' });
  deliver({ type: contract.REATTACH_WINDOW, windowId: 'win-abc' });
  deliver({ type: contract.REATTACH_WINDOW, windowId: 'win-abc', frame: { x: 0.1, y: 0.2, width: 0.5, height: 0.5 } });
  deliver({ type: contract.REATTACH_WINDOW, windowId: 'win-abc', frame: { x: 0.1, y: 0.2, width: 0.5 } });
  deliver({ type: contract.REATTACH_WINDOW, windowId: 'win-abc', frame: 'here' });
  deliver({ type: contract.REATTACH_WINDOW, windowId: 'agent-abc' });
  assert.deepStrictEqual(seen, [
    ['tear', 'win-abc', 'out'],
    ['caps', true, undefined],
    ['caps', true, true],
    ['reattach', 'win-abc', true],
    ['reattach', 'win-abc', false],
  ]);
});

test('embedder endpoint validates a provider sign-in request before dispatch', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: {
      [contract.PROVIDER_SIGN_IN]: (msg) => seen.push([msg.url, msg.flowId]),
    },
  });
  const from = (data) => win.deliver({ source: frameWin, origin: 'o', data });
  const url = 'https://claude.ai/oauth/authorize?state=s-1';
  from({ type: contract.PROVIDER_SIGN_IN, url, flowId: '../../etc' });
  from({ type: contract.PROVIDER_SIGN_IN, url: 'http://claude.ai/oauth/authorize', flowId: 'flow-1' });
  from({ type: contract.PROVIDER_SIGN_IN, url: 'https://claude.ai/' + 'a'.repeat(8192), flowId: 'flow-1' });
  from({ type: contract.PROVIDER_SIGN_IN, url });
  assert.deepStrictEqual(seen, []);
  from({ type: contract.PROVIDER_SIGN_IN, url, flowId: '5e0a9f6c1b2d4e8fa7c3b19d0e6f2a41' });
  assert.deepStrictEqual(seen, [[url, '5e0a9f6c1b2d4e8fa7c3b19d0e6f2a41']]);
});

test('embedder endpoint takes a provider sign-in end only with a well-formed flow id', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: {
      [contract.PROVIDER_SIGN_IN_END]: (msg) => seen.push(msg.flowId),
    },
  });
  const from = (data) => win.deliver({ source: frameWin, origin: 'o', data });
  from({ type: contract.PROVIDER_SIGN_IN_END });
  from({ type: contract.PROVIDER_SIGN_IN_END, flowId: '../../etc' });
  from({ type: contract.PROVIDER_SIGN_IN_END, flowId: '9b1f04d2c6e84a7d' });
  assert.deepStrictEqual(seen, ['9b1f04d2c6e84a7d']);
});

test('workspace endpoint takes a provider sign-in ack only with a boolean relay', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.PROVIDER_SIGN_IN_ACK]: (msg) => seen.push(msg.relay) },
  });
  const fromParent = (data) => win.deliver({ source: parentWin, origin: 'o', data });
  fromParent({ type: contract.PROVIDER_SIGN_IN_ACK });
  fromParent({ type: contract.PROVIDER_SIGN_IN_ACK, relay: 'true' });
  fromParent({ type: contract.PROVIDER_SIGN_IN_ACK, relay: true });
  fromParent({ type: contract.PROVIDER_SIGN_IN_ACK, relay: false });
  assert.deepStrictEqual(seen, [true, false]);
});

test('embedder endpoint reads opensLinks on a readiness announcement only as a boolean or absent', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: { [contract.WORKSPACE_READY]: (msg) => seen.push(msg.opensLinks) },
  });
  const announce = (payload) =>
    win.deliver({ source: frameWin, origin: 'https://agent-1.localhost', data: { type: contract.WORKSPACE_READY, ...payload } });
  announce({});
  announce({ opensLinks: true });
  announce({ opensLinks: false });
  announce({ opensLinks: 'yes' });
  announce({ opensLinks: 1 });
  announce({ opensLinks: null });
  assert.deepStrictEqual(seen, [undefined, true, false]);
});

test('workspace endpoint accepts an open-link only for an absolute http(s), mailto or tel URL within the length bound', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.OPEN_LINK]: (msg) => seen.push(msg.url) },
  });
  const deliver = (payload) =>
    win.deliver({ source: parentWin, origin: 'http://chrome', data: { type: contract.OPEN_LINK, ...payload } });
  const atBound = 'http://localhost:8000/' + 'a'.repeat(contract.MAX_OPEN_LINK_URL_LENGTH - 'http://localhost:8000/'.length);
  deliver({ url: 'http://localhost:8000/docs?q=1#top' });
  deliver({ url: 'https://web.agent-0a1b.localhost:8421/page' });
  deliver({ url: atBound });
  deliver({ url: atBound + 'a' });
  deliver({});
  deliver({ url: 42 });
  deliver({ url: '' });
  deliver({ url: '/relative/path' });
  deliver({ url: 'localhost:8000/no-scheme' });
  deliver({ url: 'javascript:alert(1)' });
  deliver({ url: 'file:///etc/passwd' });
  deliver({ url: 'mailto:someone@example.com' });
  deliver({ url: 'tel:+15551234567' });
  deliver({ url: 'data:text/html,<p>hi</p>' });
  deliver({ url: 'http://exa mple.com/' });
  assert.deepStrictEqual(seen, [
    'http://localhost:8000/docs?q=1#top',
    'https://web.agent-0a1b.localhost:8421/page',
    atBound,
    'mailto:someone@example.com',
    'tel:+15551234567',
  ]);
});

test('embedder endpoint accepts an open-external only for an absolute http(s), mailto or tel URL within the length bound', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: { [contract.OPEN_EXTERNAL]: (msg) => seen.push(msg.url) },
  });
  const deliver = (payload) =>
    win.deliver({ source: frameWin, origin: 'https://agent-1.localhost', data: { type: contract.OPEN_EXTERNAL, ...payload } });
  const atBound = 'https://example.com/' + 'a'.repeat(contract.MAX_OPEN_LINK_URL_LENGTH - 'https://example.com/'.length);
  deliver({ url: 'https://example.com/a' });
  deliver({ url: 'mailto:someone@example.com' });
  deliver({ url: 'tel:+15551234567' });
  deliver({ url: atBound });
  deliver({ url: atBound + 'a' });
  deliver({});
  deliver({ url: 'javascript:alert(1)' });
  deliver({ url: 'file:///etc/passwd' });
  deliver({ url: 'smb://server/share' });
  assert.deepStrictEqual(seen, ['https://example.com/a', 'mailto:someone@example.com', 'tel:+15551234567', atBound]);
});

test('workspace endpoint never honours an open-external, which only travels to the embedder', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: { [contract.OPEN_EXTERNAL]: (msg) => seen.push(msg.url) },
  });
  win.deliver({ source: parentWin, origin: 'http://chrome', data: { type: contract.OPEN_EXTERNAL, url: 'https://example.com/' } });
  assert.deepStrictEqual(seen, []);
});

test('embedder endpoint never honours an open-link, which only travels to the workspace', () => {
  const frameWin = makeWindowDouble();
  const seen = [];
  contract.createEmbedderEndpoint({
    getFrameWindow: () => frameWin,
    isExpectedOrigin: () => true,
    handlers: { [contract.OPEN_LINK]: (msg) => seen.push(msg.url) },
  });
  win.deliver({ source: frameWin, origin: 'https://agent-1.localhost', data: { type: contract.OPEN_LINK, url: 'http://localhost/' } });
  assert.deepStrictEqual(seen, []);
});
