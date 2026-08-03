// Unit tests for the desktop shell's workspace-URL classification.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The classification
// is deliberately split out of main.js (which can't be required outside
// Electron) so the "is this URL a workspace" decision -- which drives session
// persistence, window titles, and the top-level workspace-navigation guard --
// is verifiable without launching Electron.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parseWorkspaceId } = require('../../electron/surface-routing');

const BASE = 'http://localhost:8080';
const AGENT = 'agent-0a1b2c3d4e5f';
const HOST = 'host-0a1b2c3d4e5f';

test('parseWorkspaceId matches the workspace origins and the /goto bridge only', () => {
  assert.equal(parseWorkspaceId(`http://${HOST}.localhost:8080/anything`), HOST);
  // Service origins (and deeper sub-origins) belong to the same workspace.
  assert.equal(parseWorkspaceId(`https://terminal.${HOST}.localhost:8080/`), HOST);
  assert.equal(parseWorkspaceId(`https://deep.svc.${HOST}.localhost:8080/x`), HOST);
  assert.equal(parseWorkspaceId(`${BASE}/goto/${HOST}/`), HOST);
  assert.equal(parseWorkspaceId(`${BASE}/goto/${HOST}`), HOST);
  // Local / general routes are NOT workspaces.
  assert.equal(parseWorkspaceId(`${BASE}/`), null);
  assert.equal(parseWorkspaceId(`${BASE}/create`), null);
  assert.equal(parseWorkspaceId(`${BASE}/settings`), null);
  // Workspace-SCOPED local screens are not the workspace itself (they render
  // in the chrome shell); parseWorkspaceId must not claim them.
  assert.equal(parseWorkspaceId(`${BASE}/workspace/${AGENT}/settings`), null);
  assert.equal(parseWorkspaceId(`${BASE}/sharing/${AGENT}`), null);
  assert.equal(parseWorkspaceId(`${BASE}/agents/${AGENT}/recovery`), null);
  // A workspace-origin lookalike on a foreign domain is not a workspace.
  assert.equal(parseWorkspaceId(`https://${HOST}.evil.com/`), null);
  // Junk / relative.
  assert.equal(parseWorkspaceId(''), null);
  assert.equal(parseWorkspaceId('/create'), null);
});
