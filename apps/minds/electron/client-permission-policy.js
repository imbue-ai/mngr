'use strict';

// Client permissions (specs/workspace-microphone-permission/spec.md): whether
// a workspace's pages may use a capability of this computer, the microphone
// first, decided per workspace by this install. The backend holds the
// decisions; this module owns everything the gate in main.js decides without
// Electron: which permission requests are client permissions, whose request
// one is, the short-lived cache in front of the backend, the session grants,
// the prompt and its coalescing, and the toast shown when a stored denial
// refuses a page. Kept free of any `electron` import so it can be unit-tested
// under plain node; main.js supplies the backend call, the mount lookup and
// the sends.

const { isAliasOfMount, workspaceIdOfUrl } = require('./link-routing');

// Permission names are the W3C Permissions Policy feature names (D2);
// Electron's own (`media` with media types) are mapped to them here and
// appear nowhere else.
const CLIENT_PERMISSIONS = Object.freeze({ MICROPHONE: 'microphone' });

const DECISIONS = Object.freeze({ GRANTED: 'granted', DENIED: 'denied', ASK: 'ask' });
const DECISION_VALUES = Object.freeze(Object.values(DECISIONS));

// Where a stored decision came from: this workspace's own entry, or the Imbue
// default. A denial from either is one the user can change in the Permissions
// tab, which is what the toast tells them.
const STORED_SOURCES = Object.freeze(['workspace', 'default']);

/**
 * The client permission an Electron permission request or check is for, or
 * null for one that is none (which keeps its current treatment). A `media`
 * request names its devices in `details.mediaTypes`; a `media` check names
 * the one device in `details.mediaType`. Only an audio input maps to
 * anything in phase 1 (D13): a video-only request is not a client permission.
 *
 * @param {string} permission  Electron's permission name.
 * @param {object|null|undefined} details  The request's or check's details.
 * @returns {string|null}
 */
function clientPermissionNameOf(permission, details) {
  if (permission !== 'media' || details === null || typeof details !== 'object') return null;
  const isAudio =
    (Array.isArray(details.mediaTypes) && details.mediaTypes.includes('audio')) || details.mediaType === 'audio';
  return isAudio ? CLIENT_PERMISSIONS.MICROPHONE : null;
}

/**
 * The workspace a requesting frame's URL names (`[<app>.]agent-<hex>.localhost`,
 * or the legacy `host-<hex>` form), lowercased, or null for any other URL:
 * the chrome's own page, a popup on some other origin, the loading document.
 *
 * @param {string|null|undefined} url
 * @returns {string|null}
 */
function workspaceIdOfRequestingUrl(url) {
  return workspaceIdOfUrl(url);
}

/**
 * Whether a request is the mounted workspace's: the id its frame's URL names
 * is one the mounted workspace goes by (D8). False with no mount at all.
 *
 * @param {string|null} requestingWorkspaceId  As workspaceIdOfRequestingUrl spells it.
 * @param {{workspaceId: string, workspaceIds: string[]} | null} mount  The window's mount (link-routing.js).
 * @returns {boolean}
 */
function isRequestOfMountedWorkspace(requestingWorkspaceId, mount) {
  return isAliasOfMount(requestingWorkspaceId, mount);
}

/** The key the cache, the session grants and the prompts share. */
function decisionKey(workspaceId, permission) {
  return `${workspaceId}\n${permission}`;
}

/**
 * A cache of decisions that forgets each one `ttlMs` after it was set.
 *
 * @param {object} options
 * @param {number} options.ttlMs
 * @param {() => number} [options.now]
 */
function createDecisionCache({ ttlMs, now = Date.now }) {
  const entries = new Map();
  return {
    get(key) {
      const entry = entries.get(key);
      if (entry === undefined) return undefined;
      if (now() - entry.setAt >= ttlMs) {
        entries.delete(key);
        return undefined;
      }
      return entry.decision;
    },
    set(key, decision) {
      entries.set(key, { decision, setAt: now() });
    },
    delete(key) {
      entries.delete(key);
    },
    clear() {
      entries.clear();
    },
  };
}

/**
 * The unremembered Allows of this app run (D12): an "Allow for this session"
 * holds for its workspace and permission, in every window, until the app quits.
 */
function createSessionGrants() {
  const keys = new Set();
  return {
    has(key) {
      return keys.has(key);
    },
    add(key) {
      keys.add(key);
    },
  };
}

/**
 * The decision route's answer, or null when the body is not one. The decision
 * and its source must be what the route declares; the workspace's display
 * name is null when the body lacks one, and the caller names the workspace by
 * its id then.
 *
 * @param {string} body
 * @returns {{decision: string, source: string, workspaceName: string | null} | null}
 */
function parseDecisionBody(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== 'object') return null;
  if (!DECISION_VALUES.includes(parsed.decision) || !STORED_SOURCES.includes(parsed.source)) return null;
  return {
    decision: parsed.decision,
    source: parsed.source,
    workspaceName: typeof parsed.workspace_name === 'string' ? parsed.workspace_name : null,
  };
}

/**
 * The toast shown in the asking window when a stored denial refuses the
 * microphone (D15): the page's own notice cannot tell a Studio refusal from
 * an OS or browser one, so Studio says where the answer is changed.
 *
 * @param {string} workspaceName
 * @returns {{title: string, body: string}}
 */
function describeDenialToast(workspaceName) {
  return {
    title: `Microphone blocked for "${workspaceName}"`,
    body: "This workspace isn't allowed to use the microphone on this computer. Change this under Permissions, Client permissions.",
  };
}

/**
 * Whether a page's `client-permission-answer` payload is well-formed:
 * `{ promptId, decision: 'granted' | 'denied', isRemembered }`.
 *
 * @param {unknown} payload
 * @returns {boolean}
 */
function isClientPermissionAnswer(payload) {
  return (
    payload !== null &&
    typeof payload === 'object' &&
    typeof payload.promptId === 'string' &&
    payload.promptId !== '' &&
    (payload.decision === DECISIONS.GRANTED || payload.decision === DECISIONS.DENIED) &&
    typeof payload.isRemembered === 'boolean'
  );
}

/**
 * The id a mounted workspace is decided under: its `agent-<hex>` alias, which
 * is the only form the backend's decision route accepts, or the id the page
 * reported when no agent alias is known. A legacy `host-<hex>` frame origin
 * thereby needs no translation past the mount check.
 *
 * @param {{workspaceId: string, workspaceIds: string[]}} mount
 * @returns {string}
 */
function canonicalWorkspaceIdOf(mount) {
  const agentId = mount.workspaceIds.find((id) => /^agent-/i.test(id));
  return (agentId === undefined ? mount.workspaceId : agentId).toLowerCase();
}

/**
 * The gate: the client-permission half of the session's permission request
 * and check handlers.
 *
 * `decide` answers `granted`, `denied` or `ask` in the spec's order (section
 * 5.4): a request is the mounted workspace's or it is denied (D8); then the
 * stored decision, from the cache while fresh and from the backend otherwise,
 * a failure to get it counting as denied, uncached (D9); then, only when that
 * is `ask`, a session grant turns it into `granted`. The stored decision comes
 * first so an unremembered "Allow for this session" never outranks a "Don't
 * allow" the user sets in the Permissions tab later in the run.
 *
 * `request` acts on it: `granted` grants, `denied` denies and, when the
 * denial is a stored or default one, shows the toast once per workspace and
 * permission per run, and `ask` opens the prompt. One prompt is open per
 * workspace and permission, in the window whose request came first; a
 * request arriving meanwhile, from any window showing that workspace, waits
 * for its answer. An answer is taken only from the window that was asked.
 * Every waiting request is denied when that window goes away or leaves the
 * workspace, and after `promptTimeoutMs` with no answer, so no callback is
 * left unanswered.
 *
 * @param {object} deps
 * @param {(webContents: object) => {workspaceId: string, workspaceIds: string[]} | null} deps.mountOf
 *   The workspace the window's page has mounted (link-routing.js), or null.
 * @param {(workspaceId: string) => {workspaceId: string, workspaceIds: string[]} | null} deps.mountShowing
 *   A mount of the workspace in any window, for a check Electron attributes to no webContents.
 * @param {(workspaceId: string, permission: string) => Promise<{decision: string, source: string, workspaceName: string | null} | null>} deps.fetchDecision
 *   The backend's stored decision (see parseDecisionBody), or null when it could not be had.
 * @param {(webContents: object, prompt: {promptId: string, workspaceId: string, permission: string, workspaceName: string}) => boolean} deps.sendPrompt
 *   Ask the window's page to draw the prompt; false when the page is gone.
 * @param {(webContents: object, toast: {title: string, body: string}) => void} deps.showToast
 * @param {() => string} deps.newPromptId
 * @param {(message: string) => void} deps.warn
 * @param {number} deps.cacheTtlMs
 * @param {number} deps.promptTimeoutMs
 * @param {() => number} [deps.now]
 * @param {(fn: () => void, ms: number) => unknown} [deps.schedule]
 * @param {(handle: unknown) => void} [deps.cancel]
 */
function createClientPermissionGate({
  mountOf,
  mountShowing,
  fetchDecision,
  sendPrompt,
  showToast,
  newPromptId,
  warn,
  cacheTtlMs,
  promptTimeoutMs,
  now = Date.now,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  const cache = createDecisionCache({ ttlMs: cacheTtlMs, now });
  const sessionGrants = createSessionGrants();
  const inFlight = new Map();
  const promptsByKey = new Map();
  const promptsById = new Map();
  const toastedKeys = new Set();

  // The id the request is decided under, or null when it is not the mounted
  // workspace's. `webContents` is null for a check Electron attributes to no
  // window, and the workspace is then looked for among every window's mounts.
  function mountedWorkspaceOf(webContents, requestingUrl) {
    const requestingId = workspaceIdOfRequestingUrl(requestingUrl);
    if (requestingId === null) return null;
    const mount = webContents === null ? mountShowing(requestingId) : mountOf(webContents);
    return isRequestOfMountedWorkspace(requestingId, mount) ? canonicalWorkspaceIdOf(mount) : null;
  }

  // The stored decision, asked of the backend once however many requests
  // arrive while the answer is in flight. A backend that could not answer
  // reads as an uncached denial whose source is not a stored one.
  function readStoredDecision(workspaceId, permission) {
    const key = decisionKey(workspaceId, permission);
    const cached = cache.get(key);
    if (cached !== undefined) return Promise.resolve(cached);
    let pending = inFlight.get(key);
    if (pending === undefined) {
      const unavailable = { decision: DECISIONS.DENIED, source: null, workspaceName: workspaceId };
      pending = Promise.resolve()
        .then(() => fetchDecision(workspaceId, permission))
        .then(
          (answer) => {
            if (answer === null) return unavailable;
            const stored = {
              decision: answer.decision,
              source: answer.source,
              workspaceName: answer.workspaceName === null ? workspaceId : answer.workspaceName,
            };
            cache.set(key, stored);
            return stored;
          },
          (err) => {
            warn(`[client-permissions] denying ${permission} for ${workspaceId}: the decision request threw: ${err && err.message}`);
            return unavailable;
          },
        )
        .finally(() => inFlight.delete(key));
      inFlight.set(key, pending);
    }
    return pending;
  }

  async function decide(webContents, permission, requestingUrl) {
    const workspaceId = mountedWorkspaceOf(webContents, requestingUrl);
    if (workspaceId === null) {
      return { decision: DECISIONS.DENIED, workspaceId: null, workspaceName: null, isStoredDenial: false };
    }
    const stored = await readStoredDecision(workspaceId, permission);
    const isAsk = stored.decision === DECISIONS.ASK;
    return {
      decision: isAsk && sessionGrants.has(decisionKey(workspaceId, permission)) ? DECISIONS.GRANTED : stored.decision,
      workspaceId,
      workspaceName: stored.workspaceName,
      isStoredDenial: stored.decision === DECISIONS.DENIED && STORED_SOURCES.includes(stored.source),
    };
  }

  function closePrompt(prompt) {
    if (prompt.timer !== null) cancel(prompt.timer);
    promptsByKey.delete(prompt.key);
    promptsById.delete(prompt.promptId);
  }

  function settle(prompt, isGranted) {
    for (const callback of prompt.callbacks) callback(isGranted);
  }

  function dropPrompt(prompt, why) {
    closePrompt(prompt);
    warn(`[client-permissions] denying ${prompt.callbacks.length} waiting ${prompt.permission} request(s) of ${prompt.workspaceId}: ${why}`);
    settle(prompt, false);
  }

  function openPrompt(webContents, workspaceId, permission, workspaceName, callback) {
    const key = decisionKey(workspaceId, permission);
    const pending = promptsByKey.get(key);
    if (pending !== undefined) {
      pending.callbacks.push(callback);
      return;
    }
    const prompt = { promptId: newPromptId(), key, webContents, workspaceId, permission, callbacks: [callback], timer: null };
    promptsByKey.set(key, prompt);
    promptsById.set(prompt.promptId, prompt);
    if (!sendPrompt(webContents, { promptId: prompt.promptId, workspaceId, permission, workspaceName })) {
      dropPrompt(prompt, 'its window is gone');
      return;
    }
    prompt.timer = schedule(() => dropPrompt(prompt, `no answer within ${promptTimeoutMs}ms`), promptTimeoutMs);
  }

  return {
    /**
     * The decision for a client-permission request: `{ decision, workspaceId,
     * workspaceName, isStoredDenial }`, where `decision` is `granted`,
     * `denied` or `ask`, `workspaceId` is null when the request is not the
     * mounted workspace's, and `isStoredDenial` says the denial is the
     * workspace's stored or default decision (not a mount or backend failure).
     */
    decide,

    /**
     * The request handler's branch for a client permission: calls `callback`
     * exactly once, now or once the backend or the user has answered.
     * Resolves once that is arranged; never rejects.
     */
    async request(webContents, permission, requestingUrl, callback) {
      let isAnswered = false;
      const answer = (isGranted) => {
        if (isAnswered) return;
        isAnswered = true;
        callback(isGranted);
      };
      try {
        const verdict = await decide(webContents, permission, requestingUrl);
        if (verdict.decision === DECISIONS.GRANTED) {
          answer(true);
        } else if (verdict.decision === DECISIONS.DENIED) {
          answer(false);
          const key = verdict.workspaceId === null ? null : decisionKey(verdict.workspaceId, permission);
          if (verdict.isStoredDenial && !toastedKeys.has(key)) {
            toastedKeys.add(key);
            showToast(webContents, describeDenialToast(verdict.workspaceName));
          }
        } else if (mountedWorkspaceOf(webContents, requestingUrl) !== verdict.workspaceId) {
          // The window left the workspace while the backend was being asked.
          answer(false);
        } else {
          openPrompt(webContents, verdict.workspaceId, permission, verdict.workspaceName, answer);
        }
      } catch (err) {
        warn(`[client-permissions] denying ${permission}: deciding it threw: ${err && err.message}`);
        answer(false);
      }
    },

    /**
     * The check handler's branch for a client permission. Electron's check
     * handler is synchronous, so this answers from the cache alone: a hit by
     * its decision (`ask` is true, so a page that asks before it requests
     * still requests and reaches the prompt), a miss true, warming the cache
     * so the next check is right.
     */
    check(webContents, permission, requestingUrl) {
      const workspaceId = mountedWorkspaceOf(webContents, requestingUrl);
      if (workspaceId === null) return false;
      const cached = cache.get(decisionKey(workspaceId, permission));
      if (cached === undefined) {
        readStoredDecision(workspaceId, permission);
        return true;
      }
      return cached.decision !== DECISIONS.DENIED;
    },

    /**
     * The page's answer to a prompt, from `sender`, which must be the window
     * that was asked. A remembered answer is already in the backend's store
     * (the page writes before it answers), so the cached decision is dropped
     * for the store to be read again; an unremembered Allow becomes a session
     * grant, and an unremembered Don't allow is one-shot. Answers whether
     * the answer was taken.
     */
    answer(payload, sender) {
      if (!isClientPermissionAnswer(payload)) {
        warn('[client-permissions] dropped a malformed prompt answer');
        return false;
      }
      const prompt = promptsById.get(payload.promptId);
      if (prompt === undefined) {
        warn(`[client-permissions] dropped an answer to no open prompt (${payload.promptId})`);
        return false;
      }
      if (sender !== prompt.webContents) {
        warn('[client-permissions] dropped a prompt answer from a window that was not asked');
        return false;
      }
      closePrompt(prompt);
      const isGranted = payload.decision === DECISIONS.GRANTED;
      if (payload.isRemembered) cache.delete(prompt.key);
      else if (isGranted) sessionGrants.add(prompt.key);
      settle(prompt, isGranted);
      return true;
    },

    /** The window's page is gone (closed, crashed): its prompt is dropped, denying every waiting request. */
    forgetWebContents(webContents) {
      for (const prompt of [...promptsById.values()]) {
        if (prompt.webContents === webContents) dropPrompt(prompt, 'its window went away');
      }
    },

    /**
     * The window's mount changed (a new report, or forgotten on navigation):
     * a prompt for a workspace it no longer shows is dropped.
     */
    mountChanged(webContents) {
      const mount = mountOf(webContents);
      for (const prompt of [...promptsById.values()]) {
        if (prompt.webContents !== webContents) continue;
        if (!isRequestOfMountedWorkspace(prompt.workspaceId, mount)) dropPrompt(prompt, 'its window left the workspace');
      }
    },
  };
}

module.exports = {
  CLIENT_PERMISSIONS,
  DECISIONS,
  clientPermissionNameOf,
  workspaceIdOfRequestingUrl,
  isRequestOfMountedWorkspace,
  canonicalWorkspaceIdOf,
  decisionKey,
  createDecisionCache,
  createSessionGrants,
  parseDecisionBody,
  describeDenialToast,
  isClientPermissionAnswer,
  createClientPermissionGate,
};
