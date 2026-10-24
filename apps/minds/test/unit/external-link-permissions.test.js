// Unit tests for the stored "Always allow" choices of the external-link prompt.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createExternalLinkPermissions } = require('../../electron/external-link-permissions');

const PERMISSIONS_FILENAME = 'external-link-permissions.json';

const AGENT = 'agent-0a1b2c3d4e5f';
const OTHER_AGENT = 'agent-77aa88bb99cc';

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'minds-external-link-permissions-'));
}

test('an allowed scheme stays allowed for its workspace across restarts, and for no other', () => {
  const dataDir = tempDataDir();
  const warnings = [];
  const first = createExternalLinkPermissions(dataDir, (message) => warnings.push(message));
  assert.equal(first.isAllowed(AGENT, 'paseo:'), false);
  first.allow(AGENT, 'Paseo:');
  first.allow(AGENT, 'mailto:');

  const second = createExternalLinkPermissions(dataDir, (message) => warnings.push(message));
  assert.equal(second.isAllowed(AGENT, 'paseo:'), true);
  assert.equal(second.isAllowed(AGENT, 'PASEO:'), true);
  assert.equal(second.isAllowed(AGENT, 'mailto:'), true);
  assert.equal(second.isAllowed(AGENT, 'tel:'), false);
  assert.equal(second.isAllowed(OTHER_AGENT, 'paseo:'), false);
  assert.deepEqual(warnings, []);
});

test('an unreadable file allows nothing and says why; off-shape entries are skipped', () => {
  const unreadable = tempDataDir();
  fs.writeFileSync(path.join(unreadable, PERMISSIONS_FILENAME), '{not json');
  const warnings = [];
  const permissions = createExternalLinkPermissions(unreadable, (message) => warnings.push(message));
  assert.equal(permissions.isAllowed(AGENT, 'paseo:'), false);
  assert.equal(warnings.length, 1);
  // The next choice replaces the unreadable file.
  permissions.allow(AGENT, 'paseo:');
  assert.equal(createExternalLinkPermissions(unreadable, assert.fail).isAllowed(AGENT, 'paseo:'), true);

  const offShape = tempDataDir();
  fs.writeFileSync(
    path.join(offShape, PERMISSIONS_FILENAME),
    JSON.stringify({ allowed: { [AGENT]: ['paseo:', 7, 'not a scheme'], [OTHER_AGENT]: 'slack:' } }),
  );
  const read = createExternalLinkPermissions(offShape, assert.fail);
  assert.equal(read.isAllowed(AGENT, 'paseo:'), true);
  assert.equal(read.isAllowed(AGENT, 'not a scheme'), false);
  assert.equal(read.isAllowed(OTHER_AGENT, 'slack:'), false);
  assert.throws(() => read.allow(AGENT, 'not a scheme'));
});

test('a choice that cannot be stored still holds for this run, and says why', () => {
  // A data directory that is a file can be neither read nor written.
  const notADirectory = path.join(tempDataDir(), 'not-a-directory');
  fs.writeFileSync(notADirectory, '');
  const warnings = [];
  const permissions = createExternalLinkPermissions(notADirectory, (message) => warnings.push(message));
  permissions.allow(AGENT, 'paseo:');
  assert.equal(permissions.isAllowed(AGENT, 'paseo:'), true);
  assert.equal(warnings.length, 2);
  assert.match(warnings[1], /could not store/);
});
