'use strict';

// Pure helpers for the account a desktop window shows. Every window is one
// account's: a SuperTokens user id, or `local` for the device. The SPA's hub
// routes carry it as `/account/<id>/...` (and its overlays as `?account=`), so
// main can read a window's account off the URL it committed, save a hub window
// under it, and open the next window on the focused window's account. Kept
// free of any `electron` import so it runs under plain node.

const DEVICE_ACCOUNT_ID = 'local';
const ACCOUNT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** The account a hub route names (`/account/<id>/...`), or null for any other URL. */
function parseAccountHubId(url) {
  if (!url) return null;
  try {
    const match = new URL(url).pathname.match(/^\/account\/([^/?#]+)(?:\/|$)/);
    if (!match) return null;
    const accountId = decodeURIComponent(match[1]);
    return ACCOUNT_ID_PATTERN.test(accountId) ? accountId : null;
  } catch {
    return null;
  }
}

/** The account an overlay route carries as `?account=`, or null when it carries none that is valid. */
function parseAccountQuery(url) {
  if (!url) return null;
  try {
    const accountId = new URL(url).searchParams.get('account');
    return accountId !== null && ACCOUNT_ID_PATTERN.test(accountId) ? accountId : null;
  } catch {
    return null;
  }
}

/** The hub path of an account: `/account/<id>/`. */
function accountHubPath(accountId) {
  return `/account/${encodeURIComponent(accountId)}/`;
}

/**
 * The account a window shows after committing `url`, given what the URL itself
 * says and what the workspace list says about the workspace it landed on.
 *
 * A hub route names it outright; a workspace route takes the workspace's owning
 * account (`workspaceAccountId`, null for one associated with none, which is the
 * device's); an overlay carries it; anything else keeps `previousAccountId`,
 * the account the window already showed, and a window that never showed one
 * is the device's.
 *
 * @param {string} url  The committed URL.
 * @param {string|null} workspaceAccountId  The owning account of the workspace the URL lands on, when it lands on one.
 * @param {boolean} isWorkspaceUrl  Whether the URL lands on a workspace (a route or a popout).
 * @param {string|null} previousAccountId  The account the window showed before.
 * @returns {string}
 */
function accountIdAfterNavigation({ url, workspaceAccountId, isWorkspaceUrl, previousAccountId }) {
  const hubAccountId = parseAccountHubId(url);
  if (hubAccountId !== null) return hubAccountId;
  if (isWorkspaceUrl) return workspaceAccountId || DEVICE_ACCOUNT_ID;
  const queryAccountId = parseAccountQuery(url);
  if (queryAccountId !== null) return queryAccountId;
  return previousAccountId || DEVICE_ACCOUNT_ID;
}

/**
 * The home of a window on `accountId`: its account's hub, so a restore puts a
 * saved hub window back on that account and a new window opens on the focused
 * window's account. With no account (a window whose account was never learned,
 * or no focused window) it is the bare home, which the SPA sends to the most
 * recently used account.
 */
function accountHomePath(accountId) {
  return accountId ? accountHubPath(accountId) : '/';
}

module.exports = {
  DEVICE_ACCOUNT_ID,
  accountHomePath,
  accountHubPath,
  accountIdAfterNavigation,
  parseAccountHubId,
  parseAccountQuery,
};
