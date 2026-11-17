'use strict';

// Pure helpers for the per-account session partitions and the shared-workspace
// frame policy (specs/shared-with-me/spec.md, S10 and S11). Every window runs
// in its account's partition, so each account's session on the accounts origin
// is its own; a shared workspace's gateway admits only its own family and the
// hosted chrome as embedders, so the shell drops that one directive from the
// responses of a shared workspace's origins, and reads the frame document's
// status to learn whether the workspace admitted the account, whether the
// redeem that signs the partition in refused it, and whether the frame fell
// back to the accounts login page. Kept free of any `electron` import so it
// runs under plain node (see ../test/unit/account-partitions.test.js).

const { DEVICE_ACCOUNT_ID, parseAccountHubId, parseAccountQuery } = require('./account-routing');
const { parsePopoutRoute, parseSpaWorkspaceRouteId } = require('./surface-routing');

const PARTITION_PREFIX = 'persist:account-';
// The route alias of a pasted shared link's row (frontend/src/models/sharedWithMe.ts).
const SHARED_ROUTE_PREFIX = 'shared-';
const ACCOUNT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const CSP_HEADER_NAME = 'content-security-policy';
const FRAME_ANCESTORS_DIRECTIVE = 'frame-ancestors';
// The redeem page of the reverse device handoff on the accounts origin, which
// a shared workspace's frame is navigated to on every open.
const REDEEM_SESSION_PATH = '/accounts/session';
// The desktop backend's session cookie (desktop_client/cookie_manager.py), which
// every window's page and its /ui requests carry.
const BACKEND_SESSION_COOKIE_NAME = 'minds_session';

/** The partition a window of `accountId` runs in; an off-shape or missing id is the device's. */
function partitionNameForAccount(accountId) {
  const id = typeof accountId === 'string' && ACCOUNT_ID_PATTERN.test(accountId) ? accountId : DEVICE_ACCOUNT_ID;
  return `${PARTITION_PREFIX}${id}`;
}

/**
 * The account a window opening on `url` is for: a hub route names it, an
 * overlay carries it, a workspace route takes the workspace's owning account
 * (`workspaceAccountIdOf`, null for one no list knows), and anything else
 * answers null so the caller falls back to the focused window's account.
 *
 * @param {string} url
 * @param {(workspaceId: string) => string|null} workspaceAccountIdOf
 * @returns {string|null}
 */
function accountIdForWindowUrl(url, workspaceAccountIdOf) {
  const hubAccountId = parseAccountHubId(url);
  if (hubAccountId !== null) return hubAccountId;
  const queryAccountId = parseAccountQuery(url);
  if (queryAccountId !== null) return queryAccountId;
  const popoutRoute = parsePopoutRoute(url);
  const routeId = popoutRoute ? popoutRoute.workspaceId : parseSpaWorkspaceRouteId(url);
  if (routeId === null) return null;
  return workspaceAccountIdOf(workspaceRowKeyOfRouteId(routeId)) || null;
}

/** Whether a workspace route id is the `shared-<key>` alias of a pasted link's row. */
function isSharedRouteId(routeId) {
  return typeof routeId === 'string' && routeId.toLowerCase().startsWith(SHARED_ROUTE_PREFIX);
}

/**
 * The key a workspace route id's row is listed under: a pasted share link's
 * row goes by the `shared-<key>` alias of its bare digest key on the workspace
 * routes (and in a window's recorded content URL), while the rows list it
 * under the key itself; any other id is its own key.
 *
 * @param {string} routeId
 * @returns {string}
 */
function workspaceRowKeyOfRouteId(routeId) {
  if (typeof routeId !== 'string') return routeId;
  return routeId.toLowerCase().startsWith(SHARED_ROUTE_PREFIX) ? routeId.slice(SHARED_ROUTE_PREFIX.length) : routeId;
}

/**
 * The bare workspace domains of the shared rows in one relayed ``workspaces``
 * frame: every entry listed under "Shared with me" names the domain its
 * targets are labels under (a pasted link's row names the link's host).
 *
 * @param {object[]} entries  The frame's rows.
 * @returns {Set<string>}
 */
function sharedDomainsOfRows(entries) {
  const domains = new Set();
  if (!Array.isArray(entries)) return domains;
  for (const entry of entries) {
    if (!entry || entry.is_owned !== false || !entry.share) continue;
    const domain = entry.share.workspace_domain;
    if (typeof domain === 'string' && domain) domains.add(domain.toLowerCase());
  }
  return domains;
}

/**
 * The row keys of the shared rows in one relayed ``workspaces`` frame: the
 * ids a shared row is listed under, which its workspace routes name directly
 * or through the `shared-` alias.
 *
 * @param {object[]} entries  The frame's rows.
 * @returns {Set<string>}
 */
function sharedRowKeysOfRows(entries) {
  const keys = new Set();
  if (!Array.isArray(entries)) return keys;
  for (const entry of entries) {
    if (!entry || entry.is_owned !== false || !entry.share) continue;
    if (typeof entry.id === 'string' && entry.id) keys.add(entry.id);
  }
  return keys;
}

/** Whether `hostname` is one of `domains` or a label under one of them. */
function isHostUnderSharedDomain(hostname, domains) {
  if (typeof hostname !== 'string' || !hostname) return false;
  const host = hostname.toLowerCase();
  for (const domain of domains) {
    if (host === domain || host.endsWith(`.${domain}`)) return true;
  }
  return false;
}

/**
 * The cookies of a partition that a share gateway set, as `cookies.remove`
 * takes them (`{url, name}`): every cookie whose domain is one of the shared
 * domains or a label under one. A gateway's session cookie carries the
 * account the partition was signed in as when it visited, so a partition
 * signed out of the accounts origin must lose these too, or the gateway
 * admits the next account as the previous one without asking the broker.
 *
 * @param {{name: string, domain?: string, path?: string}[]} cookies  A `cookies.get` answer.
 * @param {Set<string>} domains
 * @returns {{url: string, name: string}[]}
 */
function sharedCookieRemovals(cookies, domains) {
  const removals = [];
  if (!Array.isArray(cookies)) return removals;
  for (const cookie of cookies) {
    if (!cookie || typeof cookie.name !== 'string') continue;
    const domain = String(cookie.domain || '').replace(/^\./, '');
    if (!isHostUnderSharedDomain(domain, domains)) continue;
    const cookiePath = typeof cookie.path === 'string' && cookie.path.startsWith('/') ? cookie.path : '/';
    removals.push({ url: `https://${domain}${cookiePath}`, name: cookie.name });
  }
  return removals;
}

/**
 * One Content-Security-Policy header value with its `frame-ancestors`
 * directive removed and every other directive kept verbatim; the value itself
 * when it carries none.
 */
function policyWithoutFrameAncestors(value) {
  const directives = String(value).split(';');
  const kept = directives.filter((directive) => {
    const name = directive.trim().split(/\s+/)[0] || '';
    return name.toLowerCase() !== FRAME_ANCESTORS_DIRECTIVE;
  });
  if (kept.length === directives.length) return value;
  return kept
    .map((directive) => directive.trim())
    .filter((directive) => directive.length > 0)
    .join('; ');
}

/**
 * The response headers with every Content-Security-Policy value's
 * `frame-ancestors` dropped, or null when nothing changes (so the caller
 * leaves the response untouched). Header names are matched case-insensitively
 * and every other header is kept as it came.
 *
 * @param {Record<string, string[]>|undefined} responseHeaders
 * @returns {Record<string, string[]>|null}
 */
function headersWithoutFrameAncestors(responseHeaders) {
  if (!responseHeaders || typeof responseHeaders !== 'object') return null;
  let isChanged = false;
  const rewritten = {};
  for (const [name, values] of Object.entries(responseHeaders)) {
    if (name.toLowerCase() !== CSP_HEADER_NAME || !Array.isArray(values)) {
      rewritten[name] = values;
      continue;
    }
    const kept = [];
    for (const value of values) {
      const stripped = policyWithoutFrameAncestors(value);
      if (stripped !== value) isChanged = true;
      if (stripped.trim().length > 0) kept.push(stripped);
    }
    if (kept.length > 0) rewritten[name] = kept;
    else isChanged = true;
  }
  return isChanged ? rewritten : null;
}

/**
 * What a shared workspace's frame document said about the account
 * (specs/shared-with-me, 4.7): 200 admitted it, 403 is the gateway's "Not
 * shared with you", 503 its "Sharing is misconfigured"; anything else says
 * nothing a tile should record.
 */
function verdictForFrameStatus(statusCode) {
  if (statusCode === 200) return 'admitted';
  if (statusCode === 403) return 'refused';
  if (statusCode === 503) return 'misconfigured';
  return null;
}

/**
 * The verdict one response under a shared workspace's domain reports for the
 * tile, or null: only the frame document's own response speaks (the window's
 * workspace frame, not its page, a subresource, or a frame the workspace
 * nests), and only a status the gateway answers a visitor with.
 */
function sharedFrameVerdictOfResponse({ resourceType, statusCode, frame }) {
  if (resourceType !== 'subFrame' || isNestedFrame(frame)) return null;
  return verdictForFrameStatus(statusCode);
}

/**
 * The verdict one failed load under a shared workspace's domain reports, or
 * null: the frame document itself not fetched at all means the tunnel is
 * down, while an aborted load (the frame navigated on, or was taken down) or
 * a frame the workspace nests says nothing.
 */
function sharedFrameVerdictOfLoadFailure({ resourceType, error, frame }) {
  if (resourceType !== 'subFrame' || error === 'net::ERR_ABORTED' || isNestedFrame(frame)) return null;
  return 'offline';
}

/**
 * What the frame policy does with one response of a partition (the "header
 * rewrite's reach" of specs/shared-with-me, section 5): every response of a
 * host under a shared workspace domain has its `frame-ancestors` dropped,
 * and the window's own frame document from it reports the tile's verdict; of
 * the accounts origin only a frame document directly under the window's page
 * (the redeem page the frame is navigated to, the refusal it may render, and
 * the broker's sign-in and "Continue as" steps) is rewritten, and it reports
 * no verdict (the redeem page's refusal is `redeemFrameRefusal`'s to read);
 * everything else is left as it came.
 *
 * @param {{url: string, resourceType: string, statusCode?: number, frame?: object}} details
 * @param {{sharedDomains: Set<string>, accountsOrigin: string|null}} policy
 * @returns {{isRewritten: boolean, verdict: string|null}}
 */
function sharedFramePolicyFor(details, { sharedDomains, accountsOrigin }) {
  if (isHostUnderSharedDomain(hostnameOfUrl(details.url), sharedDomains)) {
    return { isRewritten: true, verdict: sharedFrameVerdictOfResponse(details) };
  }
  const isAccountsPage = isAccountsOriginFrameDocument(details, accountsOrigin) && !isNestedFrame(details.frame);
  return { isRewritten: isAccountsPage, verdict: null };
}

/** The host of a URL, '' for one that does not parse. */
function hostnameOfUrl(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

/**
 * Whether a frame navigation landed on the accounts origin's login page. The
 * redeem the shared workspace's frame was navigated to did not carry it onto
 * the share: a spent or refused code lands on the login page with an error
 * (`isSignInFailed`), and a session the broker could not confirm lands on it
 * with the broker's `next` (the "Continue as" step, which the desktop never
 * shows: right after an open the shell shows that open as failed, and later it
 * opens the workspace again, which redeems and confirms).
 *
 * @returns {{isSignInFailed: boolean}|null}  null for any other page.
 */
function loginPageLanding(url, accountsOrigin) {
  if (!accountsOrigin) return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.origin !== accountsOrigin || parsed.pathname !== '/login') return null;
  return { isSignInFailed: parsed.searchParams.has('error') };
}

/**
 * Whether a response is the document of a frame on the accounts origin: the
 * shared workspace's frame is navigated to the redeem page there, and the
 * broker may send it back there to sign in or to confirm "Continue as ...";
 * those pages carry the same `frame-ancestors` policy as every page of the
 * connector, which admits the hosted chrome but not the desktop's own page.
 * Only the frame's document needs the directive dropped; its subresources
 * carry none that matters.
 */
function isAccountsOriginFrameDocument({ url, resourceType }, accountsOrigin) {
  if (resourceType !== 'subFrame' || !accountsOrigin) return false;
  try {
    return new URL(url).origin === accountsOrigin;
  } catch {
    return false;
  }
}

/**
 * Whether a `WebFrameMain` sits below another frame of its page (its parent is
 * not the page's top frame). The accounts origin's pages may be shown in the
 * window's own workspace frame, never in a frame a workspace nests inside it,
 * where a page could dress them up for a click; a frame the request does not
 * name (an older Electron, a request of no frame) counts as the window's own,
 * as does one disposed before the event reached the shell, whose every
 * property throws.
 */
function isNestedFrame(frame) {
  if (!frame || (typeof frame.isDestroyed === 'function' && frame.isDestroyed())) return false;
  if (!frame.parent || !frame.top) return false;
  return frame.parent !== frame.top;
}

/**
 * How the redeem page refused the window's workspace frame, read off the
 * frame document's response (specs/shared-with-me, 4.8), or null when the
 * response is not the redeem page's own refusal: a 302 is the redeem
 * succeeding (the frame goes on to the share), and any other page of the
 * accounts origin is reached past the redeem. `session_mismatch` is the 409
 * for a partition holding another account's session, which main signs out
 * before the page opens the share again; `refused` is any other refusal (a
 * `next` the connector would not send the browser to, a 5xx), which the page
 * shows as the open having failed.
 *
 * @param {{url: string, resourceType: string, statusCode?: number, frame?: object}} details
 * @returns {'session_mismatch'|'refused'|null}
 */
function redeemFrameRefusal(details, accountsOrigin) {
  if (!isAccountsOriginFrameDocument(details, accountsOrigin) || isNestedFrame(details.frame)) return null;
  let parsed;
  try {
    parsed = new URL(details.url);
  } catch {
    return null;
  }
  if (parsed.pathname !== REDEEM_SESSION_PATH) return null;
  const status = typeof details.statusCode === 'number' ? details.statusCode : 0;
  if (status === 409) return 'session_mismatch';
  if (status >= 400) return 'refused';
  return null;
}

/**
 * The backend session cookie to set in an account partition, from the cookies
 * of the session that consumed the one-time login code: the one-time code is
 * spent once, on the default session, and every partition's pages carry the
 * same cookie. Null when the jar holds none. The backend's cookie is a
 * session cookie (a fresh code is consumed per backend run), so the mirror
 * carries no expiry either.
 *
 * @param {{name: string, value: string}[]} cookies  A `cookies.get` answer.
 * @param {string} backendBaseUrl
 * @returns {object|null}  Details for `cookies.set`.
 */
function backendSessionCookieOf(cookies, backendBaseUrl) {
  if (!Array.isArray(cookies) || typeof backendBaseUrl !== 'string' || !backendBaseUrl) return null;
  const cookie = cookies.find((c) => c && c.name === BACKEND_SESSION_COOKIE_NAME && typeof c.value === 'string');
  if (!cookie) return null;
  return {
    url: backendBaseUrl,
    name: BACKEND_SESSION_COOKIE_NAME,
    value: cookie.value,
    path: '/',
    httpOnly: true,
    sameSite: 'lax',
  };
}

module.exports = {
  BACKEND_SESSION_COOKIE_NAME,
  accountIdForWindowUrl,
  backendSessionCookieOf,
  headersWithoutFrameAncestors,
  isAccountsOriginFrameDocument,
  isHostUnderSharedDomain,
  isNestedFrame,
  isSharedRouteId,
  loginPageLanding,
  partitionNameForAccount,
  policyWithoutFrameAncestors,
  redeemFrameRefusal,
  sharedCookieRemovals,
  sharedDomainsOfRows,
  sharedFramePolicyFor,
  sharedFrameVerdictOfLoadFailure,
  sharedRowKeysOfRows,
  sharedFrameVerdictOfResponse,
  verdictForFrameStatus,
  workspaceRowKeyOfRouteId,
};
