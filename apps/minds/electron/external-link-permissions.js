// Which workspaces the user lets open links of a scheme in another app of
// their system (a mail or phone app, or any app a custom scheme belongs to)
// without asking: the ask-first prompt's "Always allow" choice.
//
// Deliberately free of any `electron` import (paths.js pulls in `app`) so the
// rules are unit-testable under plain node -- the caller passes the data
// directory in.

const fs = require('fs');
const path = require('path');

const PERMISSIONS_FILENAME = 'external-link-permissions.json';

// A URL scheme as URL.protocol spells it: `mailto:`, `paseo:`.
const SCHEME_PATTERN = /^[a-z][a-z0-9+.-]*:$/;

function permissionsPath(dataDir) {
  return path.join(dataDir, PERMISSIONS_FILENAME);
}

/** The stored allowances as workspace id -> set of schemes; anything off-shape is skipped. */
function readAllowances(dataDir) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(permissionsPath(dataDir), 'utf8'));
  } catch (err) {
    const reason = err.code === 'ENOENT' ? null : `unreadable (${err.message})`;
    return { allowances: new Map(), reason };
  }
  const allowances = new Map();
  const stored = raw && typeof raw.allowed === 'object' && raw.allowed !== null ? raw.allowed : {};
  for (const [workspaceId, schemes] of Object.entries(stored)) {
    if (!Array.isArray(schemes)) continue;
    allowances.set(workspaceId, new Set(schemes.filter((scheme) => typeof scheme === 'string' && SCHEME_PATTERN.test(scheme))));
  }
  return { allowances, reason: null };
}

function writeAllowances(dataDir, allowances) {
  const allowed = {};
  for (const [workspaceId, schemes] of allowances) allowed[workspaceId] = [...schemes].sort();
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(permissionsPath(dataDir), JSON.stringify({ allowed }, null, 2) + '\n');
}

/**
 * The allowances stored under `dataDir`, read once and written through on
 * every change.
 *
 * @param {string} dataDir
 * @param {(message: string) => void} warn  Told when the stored file could not be read or written.
 */
function createExternalLinkPermissions(dataDir, warn) {
  const { allowances, reason } = readAllowances(dataDir);
  if (reason !== null) warn(`[external-link] ignoring stored permissions: ${reason}`);
  return {
    isAllowed(workspaceId, scheme) {
      const schemes = allowances.get(workspaceId);
      return schemes !== undefined && schemes.has(scheme.toLowerCase());
    },
    allow(workspaceId, scheme) {
      const normalized = scheme.toLowerCase();
      if (!SCHEME_PATTERN.test(normalized)) throw new Error(`Refusing to store unknown scheme ${JSON.stringify(scheme)}`);
      if (!allowances.has(workspaceId)) allowances.set(workspaceId, new Set());
      allowances.get(workspaceId).add(normalized);
      // The choice still holds for this run, so the link the user just allowed opens.
      try {
        writeAllowances(dataDir, allowances);
      } catch (err) {
        warn(`[external-link] could not store permissions: ${err.message}`);
      }
    },
  };
}

module.exports = {
  createExternalLinkPermissions,
};
