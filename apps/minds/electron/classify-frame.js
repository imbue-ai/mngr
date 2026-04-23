// Classify a `console-message` event by inspecting the frame URL that produced
// it, deciding whether the record belongs in the local-only Electron log or
// should be forwarded to the mind's workspace-server log file.
//
// Rule (see docs/security-boundaries-audit.md or the branch PR for context):
//
//   localhost[:port]/*                                       -> LOCAL (our shell UI)
//   <agent-id>.localhost[:port]/  (path does not start /service/) -> LOCAL (workspace-server top-level frame)
//   <agent-id>.localhost[:port]/service/<name>/...           -> MIND  (agent-owned iframe)
//   anything else                                            -> LOCAL, tagged unclassified
//
// Stays a pure function so it can be exercised directly under `node --test`
// without an Electron runtime.

'use strict';

// Matches the subdomain shape produced by minds' desktop-client forwarding
// (`<agent-id>.localhost`). Agent IDs always have the `agent-<hex>` shape.
const WORKSPACE_SUBDOMAIN_RE = /^(agent-[a-f0-9]+)\.localhost$/i;

// Matches `/service/<name>/...` anchored at the path root.
const SERVICE_PATH_RE = /^\/service\/([^/]+)\//;

/**
 * @typedef {Object} Classification
 * @property {'local'|'mind'} destination
 * @property {string} source
 * @property {string|null} mindId
 * @property {string|null} serviceName
 */

/**
 * @param {string} frameUrl
 * @param {string} viewName - 'chrome' | 'sidebar' | 'requests-panel' | 'content' | 'main'
 * @returns {Classification}
 */
function classifyFrame(frameUrl, viewName) {
  let parsed;
  try {
    parsed = new URL(frameUrl);
  } catch {
    return {
      destination: 'local',
      source: 'electron/renderer/unclassified',
      mindId: null,
      serviceName: null,
    };
  }

  const workspaceMatch = parsed.hostname.match(WORKSPACE_SUBDOMAIN_RE);
  if (workspaceMatch) {
    const mindId = workspaceMatch[1].toLowerCase();
    const serviceMatch = parsed.pathname.match(SERVICE_PATH_RE);
    if (serviceMatch) {
      const serviceName = serviceMatch[1];
      return {
        destination: 'mind',
        source: `electron/renderer/service/${serviceName}/${mindId}`,
        mindId,
        serviceName,
      };
    }
    return {
      destination: 'local',
      source: `electron/renderer/workspace/${mindId}`,
      mindId,
      serviceName: null,
    };
  }

  if (parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1') {
    return {
      destination: 'local',
      source: `electron/renderer/local/${viewName}`,
      mindId: null,
      serviceName: null,
    };
  }

  return {
    destination: 'local',
    source: 'electron/renderer/unclassified',
    mindId: null,
    serviceName: null,
  };
}

module.exports = { classifyFrame };
