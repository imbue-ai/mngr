// Unit tests for the persist/restore URL canonicalization.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The persist side is where a window's live content URL is frozen into
// window-state.json. Getting it wrong strands an ephemeral mngr_forward port in
// the saved state, which is dead after the next launch -- so this is split into
// the pure ``./workspace-urls`` helpers (out of main.js, which can't be required
// outside Electron) and pinned here.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  parseWorkspaceId,
  parseRecoveryAgentId,
  toPersistedContentUrl,
} = require('../../electron/workspace-urls');

const AGENT = 'agent-85b7e498ad094bd5820ba803c04e947e';

// The real shape that caused a blank page: a recovery URL whose return_to embeds
// an absolute mngr_forward URL on the PRIOR run's ephemeral port (49564).
const STALE_RECOVERY_URL =
  `http://localhost:50529/agents/${AGENT}/recovery` +
  `?return_to=${encodeURIComponent(`https://localhost:49564/goto/${AGENT}/`)}`;

test('parseWorkspaceId reads the agent from a live subdomain URL', () => {
  assert.equal(parseWorkspaceId(`https://${AGENT}.localhost:50545/dockview`), AGENT);
});

test('parseWorkspaceId reads the agent from a /goto/ auth-bridge URL', () => {
  assert.equal(parseWorkspaceId(`https://localhost:50545/goto/${AGENT}/`), AGENT);
});

test('parseWorkspaceId does not match a recovery URL or general backend screens', () => {
  assert.equal(parseWorkspaceId(STALE_RECOVERY_URL), null);
  assert.equal(parseWorkspaceId('http://localhost:50529/'), null);
  assert.equal(parseWorkspaceId('not a url'), null);
});

test('parseRecoveryAgentId reads the agent only from a recovery URL', () => {
  assert.equal(parseRecoveryAgentId(STALE_RECOVERY_URL), AGENT);
  assert.equal(parseRecoveryAgentId(`https://localhost:50545/goto/${AGENT}/`), null);
  assert.equal(parseRecoveryAgentId('http://localhost:50529/'), null);
});

test('toPersistedContentUrl canonicalizes a live workspace URL to the port-independent /goto/ path', () => {
  assert.equal(toPersistedContentUrl(`https://${AGENT}.localhost:50545/dockview`), `/goto/${AGENT}/`);
  assert.equal(toPersistedContentUrl(`https://localhost:50545/goto/${AGENT}/`), `/goto/${AGENT}/`);
});

test('toPersistedContentUrl canonicalizes a recovery URL to /goto/, dropping the stale port (the fix)', () => {
  const persisted = toPersistedContentUrl(STALE_RECOVERY_URL);
  assert.equal(persisted, `/goto/${AGENT}/`);
  // The dead ephemeral port must not survive into the persisted state.
  assert.ok(!persisted.includes('49564'), 'persisted URL must not carry the stale port');
  assert.ok(!persisted.includes('return_to'), 'persisted URL must not carry the return_to');
});

test('toPersistedContentUrl round-trips non-workspace backend screens as relative paths', () => {
  assert.equal(toPersistedContentUrl('http://localhost:50529/'), '/');
  assert.equal(
    toPersistedContentUrl(`http://localhost:50529/workspace/${AGENT}/settings`),
    `/workspace/${AGENT}/settings`,
  );
});

test('toPersistedContentUrl returns null for an empty/absent url', () => {
  assert.equal(toPersistedContentUrl(null), null);
  assert.equal(toPersistedContentUrl(''), null);
});
