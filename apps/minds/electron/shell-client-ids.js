// The workspace shell's client id, kept by main per shell hostname.
//
// The shell (the workspace's system interface page) identifies the browser it
// runs in by a uuid in its own localStorage (`si-client-id`), and the workspace
// keys that client's window layout by it. In the desktop app localStorage is
// per session partition as well as per origin, and a window's partition is
// its account's, fixed when the window is created (account-partitions.js), so
// the same workspace shown in two windows, or in a popout and its window, or
// before and after the partitions arrived, would read two different ids and
// register as two clients, the second with every window minimized. Main
// therefore keeps one id per shell hostname and the preload writes it into
// the shell frame's localStorage before the shell's own scripts read it
// (preload.js, seedShellClientId).
//
// Keyed by hostname rather than origin, so a forward that comes up on a
// different port keeps its clients. Deliberately free of any `electron` import
// so the rules are unit-testable under plain node; the caller passes the data
// directory in.

const fs = require('fs');
const path = require('path');

const STORE_FILENAME = 'shell-client-ids.json';
const STORE_FORMAT_VERSION = 1;

// A shell origin's leading label: `<service name>-<random>` for the
// `system_interface` service, whose underscore older workspaces kept in the
// label (default-workspace-template's forward_port.py, mint_service_label;
// the SPA's isShellLabel in frontend/src/models/sharedWithMe.ts holds the same
// rule). The rest of the hostname is the workspace's coordinate on the local forward
// (`agent-<hex>.localhost`) or its share domain.
const SHELL_LABEL_PATTERN = /^system[-_]interface-[a-z0-9]+$/;

// The template's client id contract (desktop-interface contracts.md), so no id
// a shell may legitimately hold is replaced with a new one.
const CLIENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

// CLEANUP: remove these and legacyShellOrigins with legacy-shell-client-ids.js
// (see its header).
// The forward's persisted label map (mngr_forward service_map_cache.py).
const SERVICE_MAP_FORMAT_VERSION = 2;
const SHELL_SERVICE_NAME = 'system_interface';
const AGENT_INSTANCE_PATTERN = /^(agent-[0-9a-f]+)@host-[0-9a-f]+$/;
// The port the forward binds unless it is taken (mngr_forward cli.py,
// _DEFAULT_PORT), so the one a shell's origin kept across launches.
const DEFAULT_FORWARD_PORT = 8421;

/** The shell hostname an https URL is on, lowercased, or null for any other URL. */
function shellHostnameOf(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:') return null;
  const hostname = parsed.hostname.toLowerCase();
  const labels = hostname.split('.');
  if (labels.length < 2 || !SHELL_LABEL_PATTERN.test(labels[0])) return null;
  return hostname;
}

/**
 * The shell hostname a frame asking for its client id is on, or null unless
 * it is framed directly by the window's own page and that page is trusted.
 *
 * @param {{ url: string, parent: object | null } | null} frame  The asking frame.
 * @param {{ frameTreeNodeId: number }} mainFrame  The window's own page's frame.
 * @param {(page: object) => boolean} isTrustedPage
 */
function askingShellHostname(frame, mainFrame, isTrustedPage) {
  if (!frame || !frame.parent || frame.parent.frameTreeNodeId !== mainFrame.frameTreeNodeId) return null;
  if (!isTrustedPage(frame.parent)) return null;
  return shellHostnameOf(frame.url);
}

function isClientId(value) {
  return typeof value === 'string' && CLIENT_ID_PATTERN.test(value);
}

function isStorableEntry(hostname, clientId) {
  return shellHostnameOf(`https://${hostname}/`) === hostname && isClientId(clientId);
}

function storePath(dataDir) {
  return path.join(dataDir, STORE_FILENAME);
}

function readStore(dataDir) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(storePath(dataDir), 'utf8'));
  } catch (err) {
    const reason = err.code === 'ENOENT' ? null : `unreadable (${err.message})`;
    return { idsByHostname: new Map(), isLegacyImportDone: false, reason };
  }
  if (!raw || raw.version !== STORE_FORMAT_VERSION) {
    return { idsByHostname: new Map(), isLegacyImportDone: false, reason: `unknown version ${raw && raw.version}` };
  }
  const idsByHostname = new Map();
  const stored = typeof raw.ids === 'object' && raw.ids !== null ? raw.ids : {};
  for (const [hostname, clientId] of Object.entries(stored)) {
    if (isStorableEntry(hostname, clientId)) idsByHostname.set(hostname, clientId);
  }
  return { idsByHostname, isLegacyImportDone: raw.is_legacy_import_done === true, reason: null };
}

// Through a sibling file and a rename, so a crash mid-write leaves the last
// complete store rather than a truncated one.
function writeStore(dataDir, idsByHostname, isLegacyImportDone) {
  const ids = {};
  for (const hostname of [...idsByHostname.keys()].sort()) ids[hostname] = idsByHostname.get(hostname);
  const body = JSON.stringify({ version: STORE_FORMAT_VERSION, is_legacy_import_done: isLegacyImportDone, ids }, null, 2) + '\n';
  fs.mkdirSync(dataDir, { recursive: true });
  const target = storePath(dataDir);
  const partial = `${target}.partial`;
  fs.writeFileSync(partial, body);
  fs.renameSync(partial, target);
}

/**
 * The shell client ids stored under `dataDir`, read once and written through
 * on every change.
 *
 * @param {string} dataDir
 * @param {{ mintClientId: () => string, warn: (message: string) => void }} deps
 */
function createShellClientIds(dataDir, { mintClientId, warn }) {
  const stored = readStore(dataDir);
  if (stored.reason !== null) warn(`[shell-client-id] ignoring the stored ids: ${stored.reason}`);
  const idsByHostname = stored.idsByHostname;
  let isLegacyImportDone = stored.isLegacyImportDone;

  const persist = () => {
    try {
      writeStore(dataDir, idsByHostname, isLegacyImportDone);
    } catch (err) {
      warn(`[shell-client-id] could not save the ids: ${err.message}`);
    }
  };

  return {
    /**
     * The client id the shell on `hostname` runs as: the stored one, else the
     * id the frame already had (`reportedId`), else a fresh one.
     */
    clientIdFor(hostname, reportedId) {
      const known = idsByHostname.get(hostname);
      if (known !== undefined) return known;
      const clientId = isClientId(reportedId) ? reportedId : mintClientId();
      idsByHostname.set(hostname, clientId);
      persist();
      return clientId;
    },

    isLegacyImportDone() {
      return isLegacyImportDone;
    },

    /**
     * Record the ids the shells had before the account partitions; each one
     * replaces whatever a partition minted since, since the workspace's
     * layout was arranged under it.
     *
     * @param {Map<string, string>} importedIdsByHostname
     */
    completeLegacyImport(importedIdsByHostname) {
      for (const [hostname, clientId] of importedIdsByHostname) {
        if (isStorableEntry(hostname, clientId)) idsByHostname.set(hostname, clientId);
      }
      isLegacyImportDone = true;
      persist();
    },
  };
}

/**
 * The shell origins the forward has served, from its persisted label map: each
 * workspace's shell label under its `agent-<hex>` coordinate (the forward only
 * redirects the pre-agent-keying `host-<hex>` ones), on the forward's default
 * port and then, when it differs, on `forwardPort`, most likely first. Throws
 * for a map that is unparseable or of another format, failing the import
 * rather than finding no origins.
 *
 * @param {string} serviceMapText  The forward's service_map.json.
 * @param {number} forwardPort  The port the forward is on this launch.
 * @returns {string[]}
 */
function legacyShellOrigins(serviceMapText, forwardPort) {
  const ports = [...new Set([DEFAULT_FORWARD_PORT, forwardPort])];
  const serviceMap = JSON.parse(serviceMapText);
  if (!serviceMap || serviceMap.format_version !== SERVICE_MAP_FORMAT_VERSION) {
    throw new Error(`the forward's service map has unknown format version ${serviceMap && serviceMap.format_version}`);
  }
  const labelsByInstance = serviceMap.label_to_name_by_instance;
  if (typeof labelsByInstance !== 'object' || labelsByInstance === null) {
    throw new Error('the forward\'s service map has no label map');
  }
  const origins = new Set();
  for (const [instance, labelToName] of Object.entries(labelsByInstance)) {
    const instanceMatch = AGENT_INSTANCE_PATTERN.exec(instance);
    if (instanceMatch === null || typeof labelToName !== 'object' || labelToName === null) continue;
    const agentId = instanceMatch[1];
    for (const [label, name] of Object.entries(labelToName)) {
      if (name !== SHELL_SERVICE_NAME || !SHELL_LABEL_PATTERN.test(label.toLowerCase())) continue;
      for (const port of ports) origins.add(`https://${label.toLowerCase()}.${agentId}.localhost:${port}`);
    }
  }
  return [...origins];
}

module.exports = {
  askingShellHostname,
  createShellClientIds,
  legacyShellOrigins,
  shellHostnameOf,
};
