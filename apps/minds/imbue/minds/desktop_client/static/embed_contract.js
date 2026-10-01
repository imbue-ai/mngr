// The Imbue Studio embed contract: the ONLY sanctioned postMessage channel between
// the trusted Imbue Studio chrome (the embedder) and untrusted workspace content
// (the embedded cross-origin iframe).
//
// This module is the single source of truth for that boundary, on both sides:
// the chrome page imports it from `/_static/embed_contract.js`, and the
// workspace UI (system_interface, in default-workspace-template) bundles the
// same file, fetched at build time from the mngr commit the template pins.
// Raw `postMessage` / `addEventListener('message')` usage outside this module
// is forbidden by ratchet tests in both repos, so the whole message surface
// stays greppable and auditable here. The prose contract lives in
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
// message types are ignored, and so are unknown payload fields. A shipped
// type's existing fields never change meaning -- evolve the contract by adding
// types, or by adding optional fields to an existing type. CONTRACT_VERSION
// below tracks doc revisions only.

export const CONTRACT_VERSION = "7";

// Message types

// workspace -> embedder: open the shell's permission-request modal focused on
// one request. Payload: { requestId }.
export const OPEN_REQUEST_MODAL = "minds:open-request-modal";
// workspace -> embedder: open the shell's get-help / report-a-bug modal.
// Payload: { agentId? } -- optional, scopes the report to that workspace.
export const OPEN_HELP = "minds:open-help";
// workspace -> embedder: open the shell's AI-key mint page for this
// workspace. Payload: { hostId? }. The embedder replies with
// OPEN_AI_KEYS_ACK so the workspace can tell "an Imbue Studio chrome is present"
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
// workspace -> embedder: this document's endpoint is listening, so anything
// the embedder held for it can be sent now. Payload: { opensLinks? }. Sent
// once per page load, after the workspace registers its handlers. Without it
// the embedder cannot tell a loaded frame from one whose page has not run its
// listener yet, since a send into a not-yet-listening document is simply lost.
// `opensLinks: true` promises the page handles OPEN_LINK; `false` or absent
// means it does not.
export const WORKSPACE_READY = "minds:workspace-ready";
// workspace -> embedder: open one of the workspace's windows in a desktop
// window of its own, placed beside the chrome window (the pull-out-window
// spec). Payload: { windowId, title, width, height }; `width` / `height` are
// the window's rendered size in CSS px. Sent again for a window already out
// to show its popout.
export const POP_OUT_WINDOW = "minds:pop-out-window";
// workspace -> embedder: a drag of a window's title bar began (or the dragged
// window changed size mid-drag). Payload: { windowId, title, width, height,
// grabX, grabY }: the window's rendered size in CSS px and where inside it
// the pointer holds it. The embedder watches the cursor from here: once it
// leaves the chrome window by the tear-out distance the embedder opens a
// popout under it and says so with TEAR_OUT, since the shell's own pointer
// events stop at the window's edge on some platforms.
export const WINDOW_DRAG_STARTED = "minds:window-drag-started";
// workspace -> embedder: the shell's own drag gesture ended. Payload:
// { windowId, isDetached, isCancelled? }: `isDetached` is true when the shell
// detached the window (its release arrived while torn out); `isCancelled` is
// true for a cancel (Escape), which drops any popout being dragged, and false
// for a release, after which a popout that is out stays and the shell takes
// the embedder's last TEAR_OUT word even when it lands after the release. A
// shell that omits `isCancelled` is taken at its `isDetached`.
export const WINDOW_DRAG_ENDED = "minds:window-drag-ended";
// workspace -> embedder: the pulled-out windows of the sending shell's active
// desktop, with their titles. Payload: { windows: [{ windowId, title }] }.
// Sent when the shell announces ready and whenever the set or a title
// changes; a popout closes itself when its own window is absent.
export const DETACHED_WINDOWS = "minds:detached-windows";

// embedder -> workspace: the user pressed the close-tab shortcut while this
// workspace was displayed; close the focused window. Payload: {}.
export const CLOSE_ACTIVE_TAB = "minds:close-active-tab";
// embedder -> workspace: ack for OPEN_AI_KEYS_PAGE (see above). Payload: {}.
export const OPEN_AI_KEYS_ACK = "minds:open-ai-keys-ack";
// embedder -> workspace: permission-request verdicts, each entry
// { requestId, resolution: "granted" | "denied" }. Sent two ways with one
// meaning: the mounted workspace's recent-verdicts snapshot whenever its
// frame (re)loads -- so a page rebuilt after a verdict was given while it was
// not live never offers Approve/Deny for a decided request -- and a single
// unsolicited entry the moment the user resolves a request in the shell's
// review popup, flipping the card ahead of the transcript's own notice.
export const PERMISSION_RESOLUTIONS = "minds:permission-resolutions";
// embedder -> workspace: the user opened a chat's notification; show that
// chat. Payload: { chatId } -- the chat's id (its first agent's id). Which
// window it lands in is the workspace's choice. Sent only to a workspace that
// has announced WORKSPACE_READY, which every workspace handling this type
// does; a workspace on an older template announces nothing, never receives
// the ask, and the user just lands on the workspace.
export const FOCUS_CHAT = "minds:focus-chat";
// embedder -> workspace: what this chrome can do, sent right after
// WORKSPACE_READY. Payload: { canPopOut }. A workspace that never receives it
// (an older chrome, a plain browser) keeps its pull-out gesture off.
export const EMBEDDER_CAPABILITIES = "minds:embedder-capabilities";
// embedder -> workspace: return a pulled-out window to the desktop, shown and
// raised. Payload: { windowId, frame? }; `frame` ({ x, y, width, height } in
// fractions of the workspace surface, the frame's whole viewport, which the
// receiver maps onto its own backdrop and clamps) places it where a re-dock
// drag dropped it, else it lands at its kept frame.
export const REATTACH_WINDOW = "minds:reattach-window";
// embedder -> workspace: the state of a title-bar drag the embedder is
// watching (see WINDOW_DRAG_STARTED). Payload: { windowId, phase }: "out" --
// the cursor left the chrome window by the tear-out distance and a popout now
// follows it, so the shell detaches the window (saved at once, so the
// popout's own shell reads it) and hides it; "in" -- the cursor came back
// inside and the popout is gone, so the shell brings the window back and
// shows it again; "released" -- the button was released while out, so the
// shell ends its gesture, the detach already saved. A word that lands after
// the shell's own release of the drag still stands (see WINDOW_DRAG_ENDED).
export const TEAR_OUT = "minds:tear-out";
// embedder -> workspace: open a link inside the workspace. Payload: { url },
// an absolute http(s) URL. The embedder caught a popup the workspace's page
// asked for (a `target="_blank"` link, `window.open`) and hands its URL back
// instead of opening a window of its own; the workspace routes it (the
// in-workspace browser for a local URL, the app's own window for one of its
// app addresses, a refusal notice for another workspace's). Sent only to a
// page that announced WORKSPACE_READY with `opensLinks: true`.
export const OPEN_LINK = "minds:open-link";

// Upper bound on entries per message, bounding the work it can demand; the
// snapshot carries the newest verdicts and older cards fall back to the
// transcript's own resolution notices.
export const MAX_PERMISSION_RESOLUTION_ENTRIES = 64;
// Upper bound on entries in a DETACHED_WINDOWS message.
export const MAX_DETACHED_WINDOW_ENTRIES = 128;
// The shell's own bound on a window title.
export const MAX_WINDOW_TITLE_LENGTH = 256;
// Upper bound on an OPEN_LINK URL, bounding the work it can demand.
export const MAX_OPEN_LINK_URL_LENGTH = 8192;

// Payload validation

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
// Window ids are minted by the workspace shell (`win-<hex>`).
export const WINDOW_ID_PATTERN = /^win-[a-f0-9]{1,64}$/i;
// The phases of an embedder-watched drag (see TEAR_OUT).
export const TEAR_OUT_PHASES = ['out', 'in', 'released'];

function isWindowIdValid(value) {
  return typeof value === 'string' && WINDOW_ID_PATTERN.test(value);
}

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function isTitleValid(value) {
  return typeof value === 'string' && value.length <= MAX_WINDOW_TITLE_LENGTH;
}

// A window's rendered size: two finite, positive numbers.
function isSizeValid(data) {
  return isFiniteNumber(data.width) && isFiniteNumber(data.height) && data.width > 0 && data.height > 0;
}

function isDetachedWindowEntryValid(entry) {
  if (!entry || typeof entry !== 'object') return false;
  return isWindowIdValid(entry.windowId) && isTitleValid(entry.title);
}

// A frame in fractions of the workspace surface; the receiver maps it onto its
// backdrop and clamps it, so only the numbers' finiteness is checked here.
function isFrameValid(value) {
  if (value === undefined) return true;
  if (!value || typeof value !== 'object') return false;
  return ['x', 'y', 'width', 'height'].every(function (key) {
    return isFiniteNumber(value[key]);
  });
}

function isOpenLinkUrlValid(value) {
  if (typeof value !== 'string' || value.length > MAX_OPEN_LINK_URL_LENGTH) return false;
  let parsed;
  try {
    parsed = new URL(value);
  } catch (e) {
    return false;
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:';
}

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
  [WORKSPACE_READY]: function (data) {
    return data.opensLinks === undefined || typeof data.opensLinks === 'boolean';
  },
  [POP_OUT_WINDOW]: function (data) {
    return isWindowIdValid(data.windowId) && isTitleValid(data.title) && isSizeValid(data);
  },
  [WINDOW_DRAG_STARTED]: function (data) {
    if (!isWindowIdValid(data.windowId) || !isTitleValid(data.title) || !isSizeValid(data)) return false;
    return isFiniteNumber(data.grabX) && isFiniteNumber(data.grabY);
  },
  [WINDOW_DRAG_ENDED]: function (data) {
    if (!isWindowIdValid(data.windowId) || typeof data.isDetached !== 'boolean') return false;
    return data.isCancelled === undefined || typeof data.isCancelled === 'boolean';
  },
  [DETACHED_WINDOWS]: function (data) {
    if (!Array.isArray(data.windows)) return false;
    if (data.windows.length > MAX_DETACHED_WINDOW_ENTRIES) return false;
    return data.windows.every(isDetachedWindowEntryValid);
  },
};

function isResolutionEntryValid(entry) {
  if (!entry || typeof entry !== 'object') return false;
  if (typeof entry.requestId !== 'string' || !REQUEST_ID_PATTERN.test(entry.requestId)) return false;
  return entry.resolution === 'granted' || entry.resolution === 'denied';
}

const EMBEDDER_TO_WORKSPACE_VALIDATORS = {
  [CLOSE_ACTIVE_TAB]: function () {
    return true;
  },
  [OPEN_AI_KEYS_ACK]: function () {
    return true;
  },
  [PERMISSION_RESOLUTIONS]: function (data) {
    if (!Array.isArray(data.resolutions)) return false;
    if (data.resolutions.length > MAX_PERMISSION_RESOLUTION_ENTRIES) return false;
    return data.resolutions.every(isResolutionEntryValid);
  },
  [FOCUS_CHAT]: function (data) {
    // A chat's id is its first agent's id, so it takes the agent-id shape.
    return typeof data.chatId === 'string' && AGENT_ID_PATTERN.test(data.chatId);
  },
  [EMBEDDER_CAPABILITIES]: function (data) {
    return typeof data.canPopOut === 'boolean';
  },
  [REATTACH_WINDOW]: function (data) {
    return isWindowIdValid(data.windowId) && isFrameValid(data.frame);
  },
  [TEAR_OUT]: function (data) {
    return isWindowIdValid(data.windowId) && TEAR_OUT_PHASES.indexOf(data.phase) !== -1;
  },
  [OPEN_LINK]: function (data) {
    return isOpenLinkUrlValid(data.url);
  },
};

// Debug logging

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

// Endpoints

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
 * The embedder side of the contract (runs in the trusted Imbue Studio chrome page).
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
