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

test('workspace endpoint validates the permission-resolution payload', () => {
  const seen = [];
  contract.createWorkspaceEndpoint({
    handlers: {
      [contract.PERMISSION_REQUEST_RESOLVED]: (msg) => seen.push([msg.requestId, msg.resolution]),
    },
  });
  const from = (data) => win.deliver({ source: parentWin, origin: 'o', data });
  // Only the two verdicts the contract defines, and only ids of the
  // server-issued shape, reach the card.
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, requestId: 'evt-a', resolution: 'maybe' });
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, requestId: 'evt-a' });
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, requestId: 'evt-1/../admin', resolution: 'granted' });
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, resolution: 'granted' });
  assert.deepStrictEqual(seen, []);
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, requestId: 'evt-a', resolution: 'granted' });
  from({ type: contract.PERMISSION_REQUEST_RESOLVED, requestId: 'evt-b', resolution: 'denied' });
  assert.deepStrictEqual(seen, [
    ['evt-a', 'granted'],
    ['evt-b', 'denied'],
  ]);
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
