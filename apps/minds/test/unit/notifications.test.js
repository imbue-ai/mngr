'use strict';

// Unit tests for the pure notification helpers main.js renders banners and
// the link fallback from (electron/notifications.js). What is NOT covered
// here (main.js wiring): Notification construction, the clipboard write,
// and the toast IPC to the window. Click routing is shared and covered here.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { linkFallbackFor, nativeNotificationOptionsFor, routeNotificationClick } = require('../../electron/notifications');

test('feed-backed native and in-app clicks deliver the same entry action instead of just navigating', () => {
  const target = {};
  for (const source of [null, { name: 'source window' }]) {
    const actions = [];
    assert.equal(routeNotificationClick('http://localhost:7777/workspace/agent-abcd?chat=c', source, {
      findWindow: () => target,
      mostRecentWindow: () => target,
      focus: (window) => actions.push(['focus', window]),
      openEntry: (window) => actions.push(['open-entry', window]),
      navigate: () => assert.fail('must use the shared entry action'),
    }), true);
    assert.deepEqual(actions, [['focus', target], ['open-entry', target]]);
  }
});

test('native feed entries without a workspace still run their acknowledgement action', () => {
  const target = {};
  for (const url of ['http://localhost:7777/accounts', null]) {
    const opened = [];
    assert.equal(routeNotificationClick(url, null, {
      findWindow: () => assert.fail('no workspace to find'),
      mostRecentWindow: () => target,
      focus: () => {},
      openEntry: (window) => opened.push(window),
      navigate: () => assert.fail('must use the shared entry action'),
    }), true);
    assert.deepEqual(opened, [target]);
  }
});

for (const kind of ['native banner', 'in-app notification']) {
  test(`${kind} focuses the existing workspace window and delivers its destination there`, () => {
    const source = kind === 'native banner' ? null : { name: 'other workspace' };
    const existing = { name: 'notification workspace' };
    for (const suffix of ['?chat=chat-123', '?review=req-123', '/backups']) {
      const url = `http://localhost:7777/workspace/agent-abcd${suffix}`;
      const actions = [];
      assert.equal(routeNotificationClick(url, source, {
        findWindow: (id) => { assert.equal(id, 'agent-abcd'); return existing; },
        mostRecentWindow: () => assert.fail('must prefer the existing workspace'),
        focus: (target) => actions.push(['focus', target]),
        navigate: (target, destination) => actions.push(['navigate', target, destination]),
      }), true);
      assert.deepEqual(actions, [['focus', existing], ['navigate', existing, url]]);
    }
  });
}

test('notification clicks stay local when no other window shows the workspace', () => {
  const source = {};
  for (const target of [null, source]) {
    assert.equal(routeNotificationClick('http://localhost:7777/workspace/agent-abcd?chat=c', source, {
      findWindow: () => target,
      mostRecentWindow: () => assert.fail('must prefer the source'),
      openEntry: () => assert.fail('the source renderer must run its own action once'),
      focus: () => assert.fail('must stay local'),
      navigate: () => assert.fail('must stay local'),
    }), false);
  }
});

test('native clicks fall back to the most recent window for new workspaces and account events', () => {
  const recent = {};
  for (const url of ['http://localhost:7777/workspace/agent-abcd?chat=c', 'http://localhost:7777/accounts']) {
    const actions = [];
    assert.equal(routeNotificationClick(url, null, {
      findWindow: () => null,
      mostRecentWindow: () => recent,
      focus: (target) => actions.push(['focus', target]),
      navigate: (target, destination) => actions.push(['navigate', target, destination]),
    }), true);
    assert.deepEqual(actions, [['focus', recent], ['navigate', recent, url]]);
  }
});

test('workspace-origin notifications focus an existing window without resetting its page', () => {
  const existing = {};
  const focused = [];
  assert.equal(routeNotificationClick('http://agent-abcd.localhost:7777/chat', null, {
    findWindow: (id) => { assert.equal(id, 'agent-abcd'); return existing; },
    mostRecentWindow: () => assert.fail('must prefer the existing workspace'),
    focus: (target) => focused.push(target),
    navigate: () => assert.fail('must preserve the page'),
  }), true);
  assert.deepEqual(focused, [existing]);
});

test('a notification without a destination just raises the app, and no windows is a no-op', () => {
  for (const recent of [{}, null]) {
    const focused = [];
    assert.equal(routeNotificationClick(null, null, {
      findWindow: () => assert.fail('no workspace to look up'),
      mostRecentWindow: () => recent,
      focus: (target) => focused.push(target),
      navigate: () => assert.fail('no destination to navigate'),
    }), recent !== null);
    assert.deepEqual(focused, recent ? [recent] : []);
  }
});

test('a banner carries the workspace as title, headline as subtitle, detail as body on macOS', () => {
  const options = nativeNotificationOptionsFor(
    { title: 'alpha', subtitle: 'Gmail', body: 'Needs your inbox.' },
    'darwin',
  );
  assert.deepEqual(options, { title: 'alpha', subtitle: 'Gmail', body: 'Needs your inbox.' });
});

test('off macOS the headline folds into the body so it is never lost', () => {
  const options = nativeNotificationOptionsFor(
    { title: 'alpha', subtitle: 'Gmail', body: 'Needs your inbox.' },
    'linux',
  );
  assert.deepEqual(options, { title: 'alpha', body: 'Gmail\nNeeds your inbox.' });
  assert.deepEqual(
    nativeNotificationOptionsFor({ title: 'alpha', subtitle: 'Gmail', body: '' }, 'win32'),
    { title: 'alpha', body: 'Gmail' },
  );
});

test('a banner with no title or subtitle still names the app', () => {
  assert.deepEqual(nativeNotificationOptionsFor({ body: 'x' }, 'darwin'), { title: 'Mind', body: 'x' });
});

test('the link fallback copies the bare address for mailto and tel, and the whole URL otherwise', () => {
  const mailto = linkFallbackFor('mailto:someone@example.com');
  assert.equal(mailto.clipboardText, 'someone@example.com');
  assert.equal(mailto.title, "Couldn't open link");
  assert.match(mailto.body, /email address/);

  const tel = linkFallbackFor('tel:+15551234567');
  assert.equal(tel.clipboardText, '+15551234567');
  assert.match(tel.body, /phone number/);

  const other = linkFallbackFor('foo://bar/baz');
  assert.equal(other.clipboardText, 'foo://bar/baz');
  assert.match(other.body, /this link/);

  const unparseable = linkFallbackFor('not a url');
  assert.equal(unparseable.clipboardText, 'not a url');
});
