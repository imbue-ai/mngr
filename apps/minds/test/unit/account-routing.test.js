'use strict';

// Unit tests for the account a desktop window shows (electron/account-routing.js):
// which account a committed URL puts the window on, and what a hub window is
// saved as.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  DEVICE_ACCOUNT_ID,
  accountHomePath,
  accountHubPath,
  accountIdAfterNavigation,
  parseAccountHubId,
  parseAccountQuery,
} = require('../../electron/account-routing');

const BASE = 'http://localhost:8080';
const USER = '3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c';

test('parseAccountHubId reads the account off a hub route and nothing else', () => {
  assert.equal(parseAccountHubId(`${BASE}/account/${USER}/`), USER);
  assert.equal(parseAccountHubId(`${BASE}/account/${USER}/create?git_url=x`), USER);
  assert.equal(parseAccountHubId(`${BASE}/account/local`), 'local');
  assert.equal(parseAccountHubId(`${BASE}/`), null);
  assert.equal(parseAccountHubId(`${BASE}/workspace/agent-0a1b`), null);
  assert.equal(parseAccountHubId(`${BASE}/accounts?account=${USER}`), null);
  // An id that is not one (a path injection) names no account.
  assert.equal(parseAccountHubId(`${BASE}/account/${encodeURIComponent('../x')}/`), null);
  assert.equal(parseAccountHubId('not a url'), null);
});

test('parseAccountQuery reads the account an overlay carries', () => {
  assert.equal(parseAccountQuery(`${BASE}/settings?account=${USER}`), USER);
  assert.equal(parseAccountQuery(`${BASE}/help?workspace=agent-0a1b&account=local`), 'local');
  assert.equal(parseAccountQuery(`${BASE}/settings`), null);
  assert.equal(parseAccountQuery(`${BASE}/settings?account=`), null);
});

test('accountIdAfterNavigation: the hub segment wins, then the workspace, then the query, then the window', () => {
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/account/${USER}/`, workspaceAccountId: null, isWorkspaceUrl: false, previousAccountId: 'local' }),
    USER,
  );
  // A workspace's window is its owner's; one with no owner is the device's.
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/workspace/agent-0a1b`, workspaceAccountId: USER, isWorkspaceUrl: true, previousAccountId: 'local' }),
    USER,
  );
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/workspace/agent-0a1b`, workspaceAccountId: null, isWorkspaceUrl: true, previousAccountId: USER }),
    DEVICE_ACCOUNT_ID,
  );
  // An overlay carries the account it was opened under.
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/settings?account=${USER}`, workspaceAccountId: null, isWorkspaceUrl: false, previousAccountId: 'local' }),
    USER,
  );
  // Anything else keeps the window's account; a window that never had one is the device's.
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/start`, workspaceAccountId: null, isWorkspaceUrl: false, previousAccountId: USER }),
    USER,
  );
  assert.equal(
    accountIdAfterNavigation({ url: `${BASE}/`, workspaceAccountId: null, isWorkspaceUrl: false, previousAccountId: null }),
    DEVICE_ACCOUNT_ID,
  );
});

test('a window on an account is saved as, and a new window opens on, its hub; one with no account takes the bare home', () => {
  assert.equal(accountHubPath(USER), `/account/${USER}/`);
  assert.equal(accountHomePath(USER), `/account/${USER}/`);
  assert.equal(accountHomePath('local'), '/account/local/');
  assert.equal(accountHomePath(null), '/');
});
