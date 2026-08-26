// The minds embed contract: the ONLY sanctioned postMessage channel between
// the trusted minds chrome (the embedder) and untrusted workspace content
// (the embedded cross-origin iframe).
//
// This module is the single source of truth for that boundary, on both sides:
// the chrome page imports it from `/_static/embed_contract.js`, and the
// workspace UI (system_interface, in default-workspace-template) imports the
// same file from its vendored mngr tree. Raw `postMessage` /
// `addEventListener('message')` usage outside this module is forbidden by
// ratchet tests in both repos, so the whole message surface stays greppable
// and auditable here. The prose contract lives in
// `apps/minds/docs/embed-contract.md` -- update both together.
//
// Security model (the three invariants; see the doc for the full argument):
//   1. The fronting proxy enforces `frame-ancestors` on every workspace
//      response, so a disallowed embedder can never load a workspace frame
//      at all. "Being framed" therefore proves the embedder was allowed,
//      and no origin allowlist is needed inside the workspace.
//   2. Structural source checks: the workspace only honours messages whose
//      `event.source` is its own `window.parent`; the embedder only honours
//      messages whose `event.source` is its own content iframe's window.
//      A nested third-party iframe can obtain window references but can
//      never forge either identity.
//   3. The embedder additionally checks `event.origin` against the
//      workspace-origin family it navigated the iframe to.
//
// Compatibility policy (tolerant): no version field on the wire. Unknown
// message types are ignored; existing types are immutable -- evolve the
// contract by ADDING types, never by changing the meaning or payload of an
// existing one. CONTRACT_VERSION below tracks doc revisions only.

export const CONTRACT_VERSION = "3";

// -- Message types ----------------------------------------------------------

// workspace -> embedder: open the shell's permission-request modal focused on
// one request. Payload: { requestId }.
export const OPEN_REQUEST_MODAL = "minds:open-request-modal";
// workspace -> embedder: open the shell's get-help / report-a-bug modal.
// Payload: { agentId? } -- optional, scopes the report to that workspace.
export const OPEN_HELP = "minds:open-help";
// workspace -> embedder: open the shell's AI-key mint page for this
// workspace. Payload: { hostId? }. The embedder replies with
// OPEN_AI_KEYS_ACK so the workspace can tell "a minds chrome is present"
// (with no chrome -- e.g. a direct share visit -- no ack ever arrives and
// the workspace shows its fallback text).
export const OPEN_AI_KEYS_PAGE = "minds:open-ai-keys-page";
// workspace -> embedder: OAuth finished in the external browser; ask the
// shell to bring the app window back to the front. A no-op in plain-browser
// chrome (there is no window to raise). Payload: {}.
export const BRING_APP_TO_FRONT = "minds:bring-app-to-front";
// workspace -> embedder: open the shell's workspace-options panel on its
// Share tab, focused on that app. Payload: { serviceName }.
export const OPEN_SHARE_SETTINGS = "minds:open-share-settings";

// embedder -> workspace: the user pressed the close-tab shortcut while this
// workspace was displayed; close the active dockview tab. Payload: {}.
export const CLOSE_ACTIVE_TAB = "minds:close-active-tab";
// embedder -> workspace: ack for OPEN_AI_KEYS_PAGE (see above). Payload: {}.
export const OPEN_AI_KEYS_ACK = "minds:open-ai-keys-ack";
// embedder -> workspace: the user resolved a permission request in the
// shell's review popup. Payload: { requestId, resolution }, where resolution
// is "granted" or "denied". Lets the asking workspace show the verdict at
// once, ahead of the agent transcript's own resolution message; the workspace
// still treats the transcript as authoritative once that lands.
export const PERMISSION_REQUEST_RESOLVED = "minds:permission-request-resolved";

// -- Payload validation ------------------------------------------------------

// Request ids are server-issued (`evt-<uuid hex>`). Only a conservative
// charset + length is accepted so a malicious page cannot smuggle path or
// query characters into URLs the receiver builds from the id.
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
// Agent ids are server-issued (`agent-<hex>`); host ids are `host-<hex>`.
// Same conservative-shape rationale. Receivers of ids re-validate on their
// own side as well (never trust the sender).
export const AGENT_ID_PATTERN = /^(?:agent|host)-[a-f0-9]{1,64}$/i;
export const HOST_ID_PATTERN = /^host-[a-f0-9]{1,64}$/i;
// Superset (plus a length cap) of the canonical registry rule, mngr_latchkey's
// SERVICE_NAME_PATTERN; an alignment test keeps the two in step.
export const SERVICE_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function isOptionalIdValid(value, pattern) {
  if (value === undefined || value === '') return true;
  return typeof value === 'string' && pattern.test(value);
}

// One validator per type; a message whose payload fails its validator is
// dropped before any handler runs. Types with no payload accept anything
// beyond the type field (extra fields are ignored, per the tolerant policy).
const WORKSPACE_TO_EMBEDDER_VALIDATORS = {
  [OPEN_REQUEST_MODAL]: function (data) {
    return typeof data.requestId === 'string' && REQUEST_ID_PATTERN.test(data.requestId);
  },
  [OPEN_HELP]: function (data) {
    return isOptionalIdValid(data.agentId, AGENT_ID_PATTERN);
  },
  [OPEN_AI_KEYS_PAGE]: function (data) {
    return isOptionalIdValid(data.hostId, HOST_ID_PATTERN);
  },
  [BRING_APP_TO_FRONT]: function () {
    return true;
  },
  [OPEN_SHARE_SETTINGS]: function (data) {
    return typeof data.serviceName === 'string' && SERVICE_NAME_PATTERN.test(data.serviceName);
  },
};

const EMBEDDER_TO_WORKSPACE_VALIDATORS = {
  [CLOSE_ACTIVE_TAB]: function () {
    return true;
  },
  [OPEN_AI_KEYS_ACK]: function () {
    return true;
  },
  [PERMISSION_REQUEST_RESOLVED]: function (data) {
    if (typeof data.requestId !== 'string' || !REQUEST_ID_PATTERN.test(data.requestId)) return false;
    return data.resolution === 'granted' || data.resolution === 'denied';
  },
};

// -- Debug logging -----------------------------------------------------------

function isDebugLoggingEnabled() {
  try {
    if (typeof window !== 'undefined' && window.MINDS_DEBUG_EMBED) return true;
    return typeof localStorage !== 'undefined' && localStorage.getItem('minds-debug-embed') === '1';
  } catch (e) {
    return false;
  }
}

function debugLog(side, direction, type, origin) {
  // Types and origins only -- payloads may carry ids worth keeping quiet.
  if (!isDebugLoggingEnabled()) return;
  // eslint-disable-next-line no-console
  console.debug('[embed-contract ' + side + '] ' + direction + ' ' + type + (origin ? ' (' + origin + ')' : ''));
}

// -- Endpoints ---------------------------------------------------------------

function dispatchValidated(validators, handlers, data, side, origin) {
  const validator = validators[data.type];
  if (validator === undefined) {
    // Unknown (or wrong-direction) type: ignored, per the tolerant policy.
    debugLog(side, 'ignored', String(data.type), origin);
    return;
  }
  if (!validator(data)) {
    debugLog(side, 'rejected-payload', data.type, origin);
    return;
  }
  debugLog(side, 'received', data.type, origin);
  const handler = handlers[data.type];
  if (handler) handler(data);
}

/**
 * The workspace side of the contract (runs inside the embedded iframe, or
 * top-level on a direct visit -- the code is identical; with no embedder the
 * outbound messages simply have no listener).
 *
 * `handlers` maps EMBEDDER_TO_WORKSPACE types to callbacks receiving the
 * validated message object. Returns `{ send(type, payload), dispose() }`;
 * `send` posts a workspace->embedder message to `window.parent`.
 * `targetOrigin` is deliberately `'*'`: the proxy's `frame-ancestors`
 * enforcement means only an allowed embedder can be the parent at all.
 */
export function createWorkspaceEndpoint(options) {
  const handlers = (options && options.handlers) || {};
  // Bind to the window that exists at creation time so dispose() detaches
  // from the same window it attached to (unit tests swap the global between
  // creation and teardown).
  const boundWindow = window;
  function onMessage(event) {
    // Only the direct parent document may drive the workspace. A nested
    // third-party iframe can post to this window but can never satisfy
    // `event.source === window.parent`.
    if (event.source !== boundWindow.parent) return;
    const data = event.data;
    if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
    dispatchValidated(EMBEDDER_TO_WORKSPACE_VALIDATORS, handlers, data, 'workspace', event.origin);
  }
  boundWindow.addEventListener('message', onMessage);
  return {
    send: function (type, payload) {
      debugLog('workspace', 'sent', type, '');
      boundWindow.parent.postMessage(Object.assign({ type: type }, payload || {}), '*');
    },
    dispose: function () {
      boundWindow.removeEventListener('message', onMessage);
    },
  };
}

/**
 * The embedder side of the contract (runs in the trusted minds chrome page).
 *
 * `getFrameWindow` returns the content iframe's `contentWindow` (or null
 * when no workspace is mounted); `isExpectedOrigin(origin)` confirms the
 * sender's origin belongs to the workspace-origin family the chrome
 * navigated the iframe to. `handlers` maps WORKSPACE_TO_EMBEDDER types to
 * callbacks receiving the validated message object plus the event origin.
 */
export function createEmbedderEndpoint(options) {
  const handlers = options.handlers || {};
  const getFrameWindow = options.getFrameWindow;
  const isExpectedOrigin = options.isExpectedOrigin || function () { return true; };
  const boundWindow = window;
  function onMessage(event) {
    const frameWindow = getFrameWindow();
    if (!frameWindow || event.source !== frameWindow) return;
    if (!isExpectedOrigin(event.origin)) {
      debugLog('embedder', 'rejected-origin', String(event.data && event.data.type), event.origin);
      return;
    }
    const data = event.data;
    if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
    dispatchValidated(WORKSPACE_TO_EMBEDDER_VALIDATORS, handlers, data, 'embedder', event.origin);
  }
  boundWindow.addEventListener('message', onMessage);
  return {
    send: function (type, payload) {
      const frameWindow = getFrameWindow();
      if (!frameWindow) return;
      debugLog('embedder', 'sent', type, '');
      frameWindow.postMessage(Object.assign({ type: type }, payload || {}), '*');
    },
    dispose: function () {
      boundWindow.removeEventListener('message', onMessage);
    },
  };
}
