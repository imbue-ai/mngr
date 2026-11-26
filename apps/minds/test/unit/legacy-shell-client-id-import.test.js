'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createShellClientIds } = require('../../electron/shell-client-ids');
const { runLegacyShellClientIdImport } = require('../../electron/legacy-shell-client-id-import');

function tempStateDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'legacy-shell-client-ids-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function openStore(stateDir) {
  return createShellClientIds(stateDir, { mintClientId: () => 'minted', warn: () => {} });
}

async function attempt(stateDir, { forwardPort, serviceMapText }) {
  const serviceMapPath = path.join(stateDir, 'service_map.json');
  if (serviceMapText !== undefined) fs.writeFileSync(serviceMapPath, serviceMapText);
  return runLegacyShellClientIdImport({ store: openStore(stateDir), stateDir, serviceMapPath, forwardPort, readLegacyShellClientIds: async () => new Map() });
}

for (const [cause, failure] of [
  ['the forward has not reported its port', { forwardPort: null }],
  ['the service map is in an unknown format', { forwardPort: 8421, serviceMapText: '{"format_version": 99}' }],
]) {
  test(`an import that fails because ${cause} is not attempted again on the next launch`, async (t) => {
    const stateDir = tempStateDir(t);
    await assert.rejects(attempt(stateDir, failure));
    assert.equal(openStore(stateDir).isLegacyImportDone(), true);
    assert.equal(await attempt(stateDir, { forwardPort: 8421 }), null);
  });
}
