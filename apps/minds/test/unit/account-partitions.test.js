// Unit tests for the per-account partition helpers and the shared-workspace
// frame policy (electron/account-partitions.js).
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  accountIdForWindowUrl,
  backendSessionCookieOf,
  headersWithoutFrameAncestors,
  loginPageLanding,
  isAccountsOriginFrameDocument,
  isHostUnderSharedDomain,
  isNestedFrame,
  isSharedRouteId,
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
} = require('../../electron/account-partitions');

const USER_ID = '12345678-1234-5678-1234-567812345678';

test('a partition is named for its account and the device, and an off-shape id falls back to the device', () => {
  assert.equal(partitionNameForAccount(USER_ID), `persist:account-${USER_ID}`);
  assert.equal(partitionNameForAccount('local'), 'persist:account-local');
  assert.equal(partitionNameForAccount(null), 'persist:account-local');
  assert.equal(partitionNameForAccount('not valid!'), 'persist:account-local');
});

test('the account of a window URL comes from its hub route, its overlay query, or its workspace owner', () => {
  const ownerOf = (workspaceId) => (workspaceId === 'agent-abc' || workspaceId === '0f3a9c' ? USER_ID : null);
  assert.equal(accountIdForWindowUrl(`http://127.0.0.1:8420/account/${USER_ID}/create`, ownerOf), USER_ID);
  assert.equal(accountIdForWindowUrl(`http://127.0.0.1:8420/settings?account=${USER_ID}`, ownerOf), USER_ID);
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/workspace/agent-abc', ownerOf), USER_ID);
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/popout/agent-abc/win-1', ownerOf), USER_ID);
  // A pasted share link's row is listed under its bare digest; the route carries it as `shared-<digest>`.
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/workspace/shared-0f3a9c', ownerOf), USER_ID);
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/workspace/shared-ffffff', ownerOf), null);
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/workspace/agent-unknown', ownerOf), null);
  assert.equal(accountIdForWindowUrl('http://127.0.0.1:8420/', ownerOf), null);
  assert.equal(accountIdForWindowUrl('not a url', ownerOf), null);
});

test("a route id's row key is the bare key behind a pasted link's shared- alias, and any other id itself", () => {
  assert.equal(workspaceRowKeyOfRouteId('shared-0f3a9c'), '0f3a9c');
  assert.equal(workspaceRowKeyOfRouteId('SHARED-0f3a9c'), '0f3a9c');
  assert.equal(workspaceRowKeyOfRouteId('agent-abc'), 'agent-abc');
  assert.equal(workspaceRowKeyOfRouteId('host-abc'), 'host-abc');
  assert.equal(workspaceRowKeyOfRouteId(null), null);
});

test('the shared domains of a frame are the shared rows workspace domains, lowercased', () => {
  const rows = [
    { id: 'agent-own', is_owned: true, share: null },
    { id: 'agent-shared', is_owned: false, share: { workspace_domain: 'K3J9.a1b2.us1.shares.example' } },
    { id: 'manual', is_owned: false, share: { workspace_domain: 'shell-1.k3j9.a1b2.us1.shares.example' } },
    { id: 'broken', is_owned: false, share: null },
  ];
  assert.deepEqual(
    [...sharedDomainsOfRows(rows)],
    ['k3j9.a1b2.us1.shares.example', 'shell-1.k3j9.a1b2.us1.shares.example'],
  );
  assert.deepEqual([...sharedDomainsOfRows(null)], []);
});

test('the shared rows of a frame are keyed by their ids, owned rows and off-shape entries left out', () => {
  const rows = [
    { id: 'agent-' + 'a'.repeat(32), is_owned: true },
    { id: 'agent-' + 'b'.repeat(32), is_owned: false, share: { workspace_domain: 'k3j9.a1b2.us1.shares.example' } },
    { id: 'c'.repeat(40), is_owned: false, share: { workspace_domain: 'pasted.shares.example' } },
    { id: 'agent-' + 'd'.repeat(32), is_owned: false },
    null,
  ];
  assert.deepEqual([...sharedRowKeysOfRows(rows)], ['agent-' + 'b'.repeat(32), 'c'.repeat(40)]);
  assert.deepEqual([...sharedRowKeysOfRows(undefined)], []);
});

test("the gateway cookies under a shared domain are named for removal, every other cookie left alone", () => {
  const domains = new Set(['k3j9.a1b2.us1.shares.example']);
  const cookies = [
    { name: 'share_session', domain: 'shell-1.k3j9.a1b2.us1.shares.example', path: '/' },
    { name: 'share_session', domain: '.notes-7f2k.k3j9.a1b2.us1.shares.example', path: '/app' },
    { name: 'sAccessToken', domain: 'accounts.example', path: '/' },
    { name: 'mngr_forward_session', domain: 'localhost', path: '/' },
    { name: 'odd', domain: 'k3j9.a1b2.us1.shares.example' },
    null,
  ];
  assert.deepEqual(sharedCookieRemovals(cookies, domains), [
    { url: 'https://shell-1.k3j9.a1b2.us1.shares.example/', name: 'share_session' },
    { url: 'https://notes-7f2k.k3j9.a1b2.us1.shares.example/app', name: 'share_session' },
    { url: 'https://k3j9.a1b2.us1.shares.example/', name: 'odd' },
  ]);
  assert.deepEqual(sharedCookieRemovals(undefined, domains), []);
});

test('a host is under a shared domain when it is the domain or a label under it, and nothing else', () => {
  const domains = new Set(['k3j9.a1b2.us1.shares.example']);
  assert.equal(isHostUnderSharedDomain('shell-1.k3j9.a1b2.us1.shares.example', domains), true);
  assert.equal(isHostUnderSharedDomain('K3J9.A1B2.us1.shares.example', domains), true);
  assert.equal(isHostUnderSharedDomain('evil-k3j9.a1b2.us1.shares.example', domains), false);
  assert.equal(isHostUnderSharedDomain('agent-abc.localhost', domains), false);
  assert.equal(isHostUnderSharedDomain('', domains), false);
});

test('only the frame-ancestors directive leaves a policy; every other directive stays verbatim', () => {
  assert.equal(
    policyWithoutFrameAncestors("frame-ancestors 'self' https://*.d.example https://studio.example; default-src 'self'"),
    "default-src 'self'",
  );
  assert.equal(
    policyWithoutFrameAncestors("default-src 'self'; Frame-Ancestors 'none'; img-src *"),
    "default-src 'self'; img-src *",
  );
  assert.equal(policyWithoutFrameAncestors("default-src 'self'"), "default-src 'self'");
});

test('response headers are rewritten only when a policy carried frame-ancestors, and other headers are kept', () => {
  const untouched = headersWithoutFrameAncestors({
    'Content-Type': ['text/html'],
    'content-security-policy': ["default-src 'self'"],
  });
  assert.equal(untouched, null);

  const rewritten = headersWithoutFrameAncestors({
    'Content-Type': ['text/html'],
    'Content-Security-Policy': ["frame-ancestors 'self'", "img-src *; frame-ancestors 'none'"],
    'strict-transport-security': ['max-age=1'],
  });
  assert.deepEqual(rewritten, {
    'Content-Type': ['text/html'],
    'Content-Security-Policy': ['img-src *'],
    'strict-transport-security': ['max-age=1'],
  });
  assert.equal(headersWithoutFrameAncestors(undefined), null);
});

test('the frame status maps onto the verdicts a tile records', () => {
  assert.equal(verdictForFrameStatus(200), 'admitted');
  assert.equal(verdictForFrameStatus(403), 'refused');
  assert.equal(verdictForFrameStatus(503), 'misconfigured');
  assert.equal(verdictForFrameStatus(302), null);
  assert.equal(verdictForFrameStatus(500), null);
});

test("only the frame document's own response and its outright load failure report a verdict", () => {
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'subFrame', statusCode: 200 }), 'admitted');
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'subFrame', statusCode: 403 }), 'refused');
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'subFrame', statusCode: 302 }), null);
  // The window's own page and the frame's subresources say nothing about the share.
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'mainFrame', statusCode: 200 }), null);
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'script', statusCode: 403 }), null);
  assert.equal(sharedFrameVerdictOfLoadFailure({ resourceType: 'subFrame', error: 'net::ERR_CONNECTION_REFUSED' }), 'offline');
  assert.equal(sharedFrameVerdictOfLoadFailure({ resourceType: 'subFrame', error: 'net::ERR_ABORTED' }), null);
  assert.equal(sharedFrameVerdictOfLoadFailure({ resourceType: 'image', error: 'net::ERR_CONNECTION_REFUSED' }), null);
  // A frame the shared workspace nests (even on the frame's own host) speaks for nothing; the window's frame does.
  const top = { parent: null };
  const windowFrame = { parent: top, top };
  const nested = { parent: windowFrame, top };
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'subFrame', statusCode: 403, frame: nested }), null);
  assert.equal(sharedFrameVerdictOfResponse({ resourceType: 'subFrame', statusCode: 403, frame: windowFrame }), 'refused');
  assert.equal(sharedFrameVerdictOfLoadFailure({ resourceType: 'subFrame', error: 'net::ERR_CONNECTION_REFUSED', frame: nested }), null);
  assert.equal(sharedFrameVerdictOfLoadFailure({ resourceType: 'subFrame', error: 'net::ERR_CONNECTION_REFUSED', frame: windowFrame }), 'offline');
});

test("the frame policy rewrites every response under a shared domain and only the accounts origin's own frame document, relaying a verdict for the share's frame alone", () => {
  const accountsOrigin = 'https://accounts.example.com';
  const sharedDomains = new Set(['k3j9.a1b2.us1.shares.example']);
  const policy = (details) => sharedFramePolicyFor(details, { sharedDomains, accountsOrigin });
  const top = { parent: null };
  top.top = top;
  const windowFrame = { parent: top, top };
  const nested = { parent: windowFrame, top };
  const share = 'https://shell-1.k3j9.a1b2.us1.shares.example';
  // The share's frame document reports its status; its subresources and nested frames are rewritten but say nothing.
  assert.deepEqual(policy({ url: `${share}/`, resourceType: 'subFrame', statusCode: 403, frame: windowFrame }), { isRewritten: true, verdict: 'refused' });
  assert.deepEqual(policy({ url: `${share}/app.js`, resourceType: 'script', statusCode: 200, frame: windowFrame }), { isRewritten: true, verdict: null });
  assert.deepEqual(policy({ url: `${share}/embed`, resourceType: 'subFrame', statusCode: 200, frame: nested }), { isRewritten: true, verdict: null });
  // The accounts origin: the window's own frame document only, never nested, never a subresource, never the page.
  assert.deepEqual(policy({ url: `${accountsOrigin}/login`, resourceType: 'subFrame', statusCode: 200, frame: windowFrame }), { isRewritten: true, verdict: null });
  assert.deepEqual(policy({ url: `${accountsOrigin}/login`, resourceType: 'subFrame', statusCode: 200, frame: nested }), { isRewritten: false, verdict: null });
  assert.deepEqual(policy({ url: `${accountsOrigin}/app.js`, resourceType: 'script', statusCode: 200, frame: windowFrame }), { isRewritten: false, verdict: null });
  assert.deepEqual(policy({ url: `${accountsOrigin}/login`, resourceType: 'mainFrame', statusCode: 200 }), { isRewritten: false, verdict: null });
  // Every other host, an owned workspace's local forward included, keeps its policy.
  assert.deepEqual(policy({ url: 'https://localhost:8421/', resourceType: 'subFrame', statusCode: 200, frame: windowFrame }), { isRewritten: false, verdict: null });
  assert.deepEqual(policy({ url: 'https://evil.example.com/', resourceType: 'subFrame', statusCode: 200, frame: windowFrame }), { isRewritten: false, verdict: null });
  // Without a known accounts origin only the shared hosts are rewritten.
  assert.deepEqual(sharedFramePolicyFor({ url: `${accountsOrigin}/login`, resourceType: 'subFrame', statusCode: 200, frame: windowFrame }, { sharedDomains, accountsOrigin: null }), { isRewritten: false, verdict: null });
});

test("the frame policy covers the accounts origin's frame documents, and only in the window's own frame", () => {
  const origin = 'https://accounts.example.com';
  assert.equal(isAccountsOriginFrameDocument({ url: `${origin}/login?next=%2Fshare`, resourceType: 'subFrame' }, origin), true);
  assert.equal(isAccountsOriginFrameDocument({ url: `${origin}/share/authorize?x=1`, resourceType: 'subFrame' }, origin), true);
  // The window's own page, the page's subresources, and other origins are left alone.
  assert.equal(isAccountsOriginFrameDocument({ url: `${origin}/login`, resourceType: 'mainFrame' }, origin), false);
  assert.equal(isAccountsOriginFrameDocument({ url: `${origin}/app.js`, resourceType: 'script' }, origin), false);
  assert.equal(isAccountsOriginFrameDocument({ url: 'https://evil.example.com/login', resourceType: 'subFrame' }, origin), false);
  assert.equal(isAccountsOriginFrameDocument({ url: `${origin}/login`, resourceType: 'subFrame' }, null), false);
  assert.equal(isAccountsOriginFrameDocument({ url: 'not a url', resourceType: 'subFrame' }, origin), false);

  const top = { parent: null };
  top.top = top;
  const child = { parent: top, top };
  const grandchild = { parent: child, top };
  assert.equal(isNestedFrame(child), false);
  assert.equal(isNestedFrame(grandchild), true);
  assert.equal(isNestedFrame(top), false);
  assert.equal(isNestedFrame(null), false);
  assert.equal(isNestedFrame(undefined), false);
  // A disposed WebFrameMain throws on every property; it reads as the window's own.
  const disposed = {
    isDestroyed: () => true,
    get parent() {
      throw new Error('Render frame was disposed before WebFrameMain could be accessed');
    },
    get top() {
      throw new Error('Render frame was disposed before WebFrameMain could be accessed');
    },
  };
  assert.equal(isNestedFrame(disposed), false);
});

test('the backend session cookie is mirrored into a partition from the default session, and nothing is set without one', () => {
  const backendBaseUrl = 'http://localhost:8420';
  const jar = [
    { name: 'other', value: 'x' },
    { name: 'minds_session', value: 's3ss10n', path: '/', httpOnly: true, secure: false, session: true },
  ];
  assert.deepEqual(backendSessionCookieOf(jar, backendBaseUrl), {
    url: backendBaseUrl,
    name: 'minds_session',
    value: 's3ss10n',
    path: '/',
    httpOnly: true,
    sameSite: 'lax',
  });
  assert.equal(backendSessionCookieOf([{ name: 'other', value: 'x' }], backendBaseUrl), null);
  assert.equal(backendSessionCookieOf([], backendBaseUrl), null);
  assert.equal(backendSessionCookieOf(jar, null), null);
  assert.equal(backendSessionCookieOf(undefined, backendBaseUrl), null);
});

test('a pasted link\'s route alias is told apart from a workspace coordinate', () => {
  assert.equal(isSharedRouteId('shared-0f3a9c'), true);
  assert.equal(isSharedRouteId('SHARED-0F3A9C'), true);
  assert.equal(isSharedRouteId('agent-0f3a9c'), false);
  assert.equal(isSharedRouteId('host-0f3a9c'), false);
  assert.equal(isSharedRouteId(null), false);
});

test('a login page landing says whether the redeem failed outright, and nothing else is one', () => {
  const origin = 'https://accounts.example.com';
  assert.deepEqual(loginPageLanding(`${origin}/login?error=link_expired`, origin), { isSignInFailed: true });
  assert.deepEqual(loginPageLanding(`${origin}/login?next=%2Fshare%2Fauthorize%3Fmachine_domain%3Dx`, origin), {
    isSignInFailed: false,
  });
  assert.equal(loginPageLanding(`${origin}/manage`, origin), null);
  assert.equal(loginPageLanding('https://evil.example.com/login', origin), null);
  assert.equal(loginPageLanding(`${origin}/login`, null), null);
  assert.equal(loginPageLanding('not a url', origin), null);
});

test("the redeem page's refusal is read off the frame document alone, and a redirect or another page is none", () => {
  const origin = 'https://accounts.example.com';
  const redeem = (statusCode, extra = {}) => ({
    url: `${origin}/accounts/session?code=c&next=x`,
    resourceType: 'subFrame',
    statusCode,
    ...extra,
  });
  assert.equal(redeemFrameRefusal(redeem(409), origin), 'session_mismatch');
  assert.equal(redeemFrameRefusal(redeem(400), origin), 'refused');
  assert.equal(redeemFrameRefusal(redeem(500), origin), 'refused');
  assert.equal(redeemFrameRefusal(redeem(302), origin), null);
  assert.equal(redeemFrameRefusal(redeem(200), origin), null);
  assert.equal(redeemFrameRefusal(redeem(409, { resourceType: 'xhr' }), origin), null);
  const top = {};
  const nested = { parent: {}, top };
  assert.equal(redeemFrameRefusal(redeem(409, { frame: nested }), origin), null);
  assert.equal(redeemFrameRefusal({ url: `${origin}/login?error=x`, resourceType: 'subFrame', statusCode: 409 }, origin), null);
  assert.equal(redeemFrameRefusal(redeem(409), null), null);
  assert.equal(redeemFrameRefusal({ url: 'not a url', resourceType: 'subFrame', statusCode: 409 }, origin), null);
});
