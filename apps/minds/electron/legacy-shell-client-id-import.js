// When the one-time import of the default session's shell client ids runs,
// and what a failed attempt leaves behind. The read itself, which needs
// Electron, is legacy-shell-client-ids.js; this half is deliberately free of
// any `electron` import so it is unit-testable under plain node.
//
// CLEANUP: remove with legacy-shell-client-ids.js (see its header).

const fs = require('fs');
const path = require('path');
const { legacyShellOrigins } = require('./shell-client-ids');

const COPY_DIRNAME = 'legacy-shell-client-id-import';

/** Where the import copies the default session's localStorage to read it. */
function legacyImportCopyDir(stateDir) {
  return path.join(stateDir, COPY_DIRNAME);
}

/** Remove the copy a previous launch's import left behind. */
function removeLegacyImportCopy(stateDir) {
  fs.rmSync(legacyImportCopyDir(stateDir), { recursive: true, force: true });
}

/** The forward's service_map.json, or null when it has never written one. */
function readServiceMapText(serviceMapPath) {
  try {
    return fs.readFileSync(serviceMapPath, 'utf8');
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    return null;
  }
}

/**
 * Import the default session's shell client ids into `store`, once: a failed
 * attempt is recorded as done too, then rethrown. When the import is already
 * recorded, the copy a previous launch's import left behind is removed
 * instead. Run only by the instance that holds the single-instance lock: the
 * running app's copy is open.
 *
 * @param {{ store: object, stateDir: string, serviceMapPath: string, forwardPort: number | null, readLegacyShellClientIds: (origins: string[]) => Promise<Map<string, string>> }} options
 *   `store` is a createShellClientIds store; `serviceMapPath` the forward's service_map.json;
 *   `forwardPort` null until the forward has reported it, which fails the import;
 *   `readLegacyShellClientIds` reads the ids the origins hold, by hostname (legacy-shell-client-ids.js).
 * @returns {Promise<{ importedIdsByHostname: Map<string, string>, originCount: number } | null>}
 *   What was imported, or null when the import had already been recorded.
 */
async function runLegacyShellClientIdImport({ store, stateDir, serviceMapPath, forwardPort, readLegacyShellClientIds }) {
  if (store.isLegacyImportDone()) {
    removeLegacyImportCopy(stateDir);
    return null;
  }
  try {
    if (forwardPort === null) throw new Error('the forward has not reported its port');
    const serviceMapText = readServiceMapText(serviceMapPath);
    const origins = serviceMapText === null ? [] : legacyShellOrigins(serviceMapText, forwardPort);
    const importedIdsByHostname = await readLegacyShellClientIds(origins);
    store.completeLegacyImport(importedIdsByHostname);
    return { importedIdsByHostname, originCount: origins.length };
  } catch (err) {
    // The launch goes on without the old ids and its shells register under
    // ids of their own, so a later import would replace whatever layout the
    // user arranged under those: a failed attempt is the only one.
    store.completeLegacyImport(new Map());
    throw err;
  }
}

module.exports = {
  legacyImportCopyDir,
  removeLegacyImportCopy,
  runLegacyShellClientIdImport,
};
