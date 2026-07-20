// Unit tests for the desktop client's content-surface URL classification.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The classification
// is deliberately split out of main.js (which can't be required outside
// Electron) so the "which WebContentsView renders this URL" decision -- the
// crux of the content-in-chrome surface swap -- is verifiable without launching
// Electron. The e2e Playwright suite covers the actual view show/hide.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  parseWorkspaceId,
  parseAccentSourceAgentId,
  selectSurfaceForUrl,
  isSwappableLocalPath,
  SURFACE_CONTENT,
  SURFACE_CHROME,
} = require('../../electron/surface-routing');

const BASE = 'http://localhost:8080';
const AGENT = 'agent-0a1b2c3d4e5f';

test('parseWorkspaceId matches the agent subdomain and the /goto bridge only', () => {
  assert.equal(parseWorkspaceId(`http://${AGENT}.localhost:8080/anything`), AGENT);
  assert.equal(parseWorkspaceId(`${BASE}/goto/${AGENT}/`), AGENT);
  assert.equal(parseWorkspaceId(`${BASE}/goto/${AGENT}`), AGENT);
  // Local / general routes are NOT workspaces.
  assert.equal(parseWorkspaceId(`${BASE}/`), null);
  assert.equal(parseWorkspaceId(`${BASE}/create`), null);
  assert.equal(parseWorkspaceId(`${BASE}/settings`), null);
  // Workspace-SCOPED local screens are not the workspace itself (they render on
  // the chrome surface); parseWorkspaceId must not claim them.
  assert.equal(parseWorkspaceId(`${BASE}/workspace/${AGENT}/settings`), null);
  assert.equal(parseWorkspaceId(`${BASE}/sharing/${AGENT}/code`), null);
  assert.equal(parseWorkspaceId(`${BASE}/agents/${AGENT}/recovery`), null);
  // Junk / relative.
  assert.equal(parseWorkspaceId(''), null);
  assert.equal(parseWorkspaceId('/create'), null);
});

test('parseAccentSourceAgentId is wider: it tints for workspace-scoped local screens too', () => {
  assert.equal(parseAccentSourceAgentId(`http://${AGENT}.localhost:8080/x`), AGENT);
  assert.equal(parseAccentSourceAgentId(`${BASE}/goto/${AGENT}/`), AGENT);
  assert.equal(parseAccentSourceAgentId(`${BASE}/workspace/${AGENT}/settings`), AGENT);
  assert.equal(parseAccentSourceAgentId(`${BASE}/sharing/${AGENT}/code`), AGENT);
  assert.equal(parseAccentSourceAgentId(`${BASE}/destroying/${AGENT}`), AGENT);
  assert.equal(parseAccentSourceAgentId(`${BASE}/agents/${AGENT}/recovery`), AGENT);
  // General screens resolve to null so the titlebar drops back to neutral chrome.
  assert.equal(parseAccentSourceAgentId(`${BASE}/`), null);
  assert.equal(parseAccentSourceAgentId(`${BASE}/create`), null);
  assert.equal(parseAccentSourceAgentId(`${BASE}/accounts`), null);
});

test('selectSurfaceForUrl: agent content -> content view; every trusted local page -> chrome view', () => {
  // Agent content (the ONLY thing on the content surface after the swap).
  assert.equal(selectSurfaceForUrl(`http://${AGENT}.localhost:8080/`), SURFACE_CONTENT);
  assert.equal(selectSurfaceForUrl(`${BASE}/goto/${AGENT}/`), SURFACE_CONTENT);
  // Trusted local pages -- including the workspace-scoped ones, which tint the
  // titlebar but are NOT agent content -- render on the chrome surface.
  for (const path of [
    '/',
    '/create',
    '/settings',
    '/accounts',
    '/welcome',
    `/creating/${AGENT}`,
    `/workspace/${AGENT}/settings`,
    `/sharing/${AGENT}/code`,
    `/destroying/${AGENT}`,
    `/agents/${AGENT}/recovery`,
    '/auth/login',
  ]) {
    assert.equal(selectSurfaceForUrl(BASE + path), SURFACE_CHROME, `${path} should be chrome`);
  }
});

test('isSwappableLocalPath: hub pages (including recovery) swap in place; lifecycle pages need full navigations', () => {
  for (const path of [
    '/',
    '/create',
    '/settings',
    '/accounts',
    '/_chrome',
    `/workspace/${AGENT}/settings`,
    // Recovery is swappable: its poll loops are minds:page-teardown-guarded, so
    // the constant hub <-> recovery hops of a flapping workspace keep the
    // titlebar intact instead of blinking on every full load.
    `/agents/${AGENT}/recovery`,
  ]) {
    assert.equal(isSwappableLocalPath(path), true, `${path} should be swappable`);
  }
  for (const path of [
    '/welcome',
    `/creating/${AGENT}`,
    `/destroying/${AGENT}`,
    '/auth/login',
    '/help',
    `/sharing/${AGENT}/code`,
  ]) {
    assert.equal(isSwappableLocalPath(path), false, `${path} should require a full navigation`);
  }
});

test('a workspace-scoped local screen routes to chrome but still tints (settings in one window, workspace in another)', () => {
  const settings = `${BASE}/workspace/${AGENT}/settings`;
  // Routes to the chrome surface (it is a trusted local page)...
  assert.equal(selectSurfaceForUrl(settings), SURFACE_CHROME);
  // ...but is NOT the displayed workspace (drives no workspace-uniqueness)...
  assert.equal(parseWorkspaceId(settings), null);
  // ...yet still paints the workspace accent.
  assert.equal(parseAccentSourceAgentId(settings), AGENT);
});
