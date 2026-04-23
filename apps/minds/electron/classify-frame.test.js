'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { classifyFrame } = require('./classify-frame');

test('localhost frame routes LOCAL tagged by view', () => {
  const result = classifyFrame('http://localhost:8420/_chrome', 'chrome');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/local/chrome');
  assert.equal(result.mindId, null);
  assert.equal(result.serviceName, null);
});

test('127.0.0.1 frame routes LOCAL (fallback for pre-subdomain startup)', () => {
  const result = classifyFrame('http://127.0.0.1:8420/', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/local/content');
});

test('workspace top-level frame routes LOCAL tagged by mind', () => {
  const result = classifyFrame('http://agent-abcdef.localhost:8420/', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/workspace/agent-abcdef');
  assert.equal(result.mindId, 'agent-abcdef');
  assert.equal(result.serviceName, null);
});

test('workspace subpath without /service/ is still workspace-top', () => {
  const result = classifyFrame('http://agent-abcdef.localhost:8420/api/agents', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/workspace/agent-abcdef');
});

test('service iframe routes MIND tagged by service+mind', () => {
  const result = classifyFrame(
    'http://agent-abcdef.localhost:8420/service/web/index.html',
    'content',
  );
  assert.equal(result.destination, 'mind');
  assert.equal(result.source, 'electron/renderer/service/web/agent-abcdef');
  assert.equal(result.mindId, 'agent-abcdef');
  assert.equal(result.serviceName, 'web');
});

test('service iframe with nested path preserves service name only', () => {
  const result = classifyFrame(
    'http://agent-abcdef.localhost:8420/service/terminal/ws?arg=agent',
    'content',
  );
  assert.equal(result.destination, 'mind');
  assert.equal(result.serviceName, 'terminal');
});

test('unrecognised origin defaults to LOCAL unclassified', () => {
  const result = classifyFrame('https://example.com/anything', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/unclassified');
  assert.equal(result.mindId, null);
  assert.equal(result.serviceName, null);
});

test('malformed URL defaults to LOCAL unclassified rather than throwing', () => {
  const result = classifyFrame('not a url', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/unclassified');
});

test('uppercase agent-id in subdomain is normalized to lowercase', () => {
  const result = classifyFrame('http://AGENT-ABCDEF.localhost:8420/service/web/', 'content');
  assert.equal(result.destination, 'mind');
  assert.equal(result.mindId, 'agent-abcdef');
  assert.equal(result.serviceName, 'web');
});

test('agent-id must match the agent- prefix pattern', () => {
  // A subdomain that looks like a workspace but isn't should fall through to
  // unclassified, not accidentally tag itself with a bogus mindId.
  const result = classifyFrame('http://notagent.localhost:8420/service/web/', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/unclassified');
});

test('service path with trailing-only slash matches (no extra segment)', () => {
  const result = classifyFrame('http://agent-aaa.localhost:8420/service/web/', 'content');
  assert.equal(result.destination, 'mind');
  assert.equal(result.serviceName, 'web');
});

test('bare /service without trailing slash falls through to workspace-top', () => {
  // We anchor on /service/<name>/, so a bare /service or /service/name (no
  // trailing slash) is not treated as an iframe route.
  const result = classifyFrame('http://agent-aaa.localhost:8420/service', 'content');
  assert.equal(result.destination, 'local');
  assert.equal(result.source, 'electron/renderer/workspace/agent-aaa');
});
