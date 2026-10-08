'use strict';

// Unit tests for the pure notification helpers main.js renders banners and
// the link fallback from (electron/notifications.js). What is NOT covered
// here (main.js wiring): Notification construction, the clipboard write,
// and the toast IPC to the window. Click routing is shared and covered here.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const {
  createBannerRegistry,
  linkFallbackFor,
  nativeNotificationOptionsFor,
  routeNotificationClick,
} = require('../../electron/notifications');

// The window-set hooks every test starts from; each test overrides the ones it exercises.
function windows(overrides) {
  return {
    findWindow: () => assert.fail('no workspace to look up'),
    mostRecentWindow: () => assert.fail('no fallback window expected'),
    showsWorkspace: () => true,
    focus: () => assert.fail('nothing to focus'),
    navigate: () => assert.fail('nothing to navigate'),
    openWindow: () => assert.fail('no window should open'),
    ...overrides,
  };
}

test('feed-backed native and in-app clicks deliver the same entry action instead of just navigating', () => {
  const target = {};
  for (const source of [null, { name: 'source window' }]) {
    const actions = [];
    assert.equal(routeNotificationClick('http://localhost:7777/workspace/agent-abcd?chat=c', source, windows({
      findWindow: () => target,
      focus: (window) => actions.push(['focus', window]),
      openEntry: (window, how) => actions.push(['open-entry', window, how]),
    })), true);
    assert.deepEqual(actions, [['focus', target], ['open-entry', target, { isNewWindow: false }]]);
  }
});

test('native feed entries without a workspace still run their acknowledgement action', () => {
  const target = {};
  for (const url of ['http://localhost:7777/accounts', null]) {
    const opened = [];
    assert.equal(routeNotificationClick(url, null, windows({
      mostRecentWindow: () => target,
      focus: () => {},
      openEntry: (window) => opened.push(window),
    })), true);
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
      assert.equal(routeNotificationClick(url, source, windows({
        findWindow: (id) => { assert.equal(id, 'agent-abcd'); return existing; },
        focus: (target) => actions.push(['focus', target]),
        navigate: (target, destination) => actions.push(['navigate', target, destination]),
      })), true);
      assert.deepEqual(actions, [['focus', existing], ['navigate', existing, url]]);
    }
  });
}

test('a click the source window can answer itself stays local', () => {
  const url = 'http://localhost:7777/workspace/agent-abcd?chat=c';
  // The source already shows the workspace.
  const onWorkspace = {};
  assert.equal(routeNotificationClick(url, onWorkspace, windows({ findWindow: () => onWorkspace })), false);
  // The source shows no workspace, and none shows this one: it takes the workspace on.
  const blank = {};
  assert.equal(routeNotificationClick(url, blank, windows({
    findWindow: () => null,
    showsWorkspace: () => false,
  })), false);
});

for (const kind of ['native banner', 'in-app notification']) {
  test(`${kind} for a workspace with no window opens one, never taking over another workspace's`, () => {
    const source = kind === 'native banner' ? null : { name: 'shows another workspace' };
    const url = 'http://localhost:7777/workspace/agent-abcd?chat=c';
    const opened = { name: 'new window' };
    const actions = [];
    assert.equal(routeNotificationClick(url, source, windows({
      findWindow: () => null,
      showsWorkspace: () => true,
      openWindow: (destination) => { actions.push(['open', destination]); return opened; },
      openEntry: (window, how) => actions.push(['open-entry', window, how]),
    })), true);
    // The new window loads the destination itself; the entry action waits for its page.
    assert.deepEqual(actions, [['open', url], ['open-entry', opened, { isNewWindow: true }]]);
  });
}

test('account-level clicks fall back to the most recent window, or open one when none is open', () => {
  const url = 'http://localhost:7777/accounts';
  const recent = {};
  const actions = [];
  assert.equal(routeNotificationClick(url, null, windows({
    mostRecentWindow: () => recent,
    focus: (target) => actions.push(['focus', target]),
    navigate: (target, destination) => actions.push(['navigate', target, destination]),
  })), true);
  assert.deepEqual(actions, [['focus', recent], ['navigate', recent, url]]);

  const opened = [];
  assert.equal(routeNotificationClick(url, null, windows({
    mostRecentWindow: () => null,
    openWindow: (destination) => { opened.push(destination); return {}; },
  })), true);
  assert.deepEqual(opened, [url]);
});

test('workspace-origin notifications focus an existing window without resetting its page', () => {
  const existing = {};
  const focused = [];
  assert.equal(routeNotificationClick('http://agent-abcd.localhost:7777/chat', null, windows({
    findWindow: (id) => { assert.equal(id, 'agent-abcd'); return existing; },
    focus: (target) => focused.push(target),
    navigate: () => assert.fail('must preserve the page'),
  })), true);
  assert.deepEqual(focused, [existing]);
});

test('a notification without a destination just raises the app, opening a window when none is open', () => {
  const recent = {};
  const focused = [];
  assert.equal(routeNotificationClick(null, null, windows({
    mostRecentWindow: () => recent,
    focus: (target) => focused.push(target),
  })), true);
  assert.deepEqual(focused, [recent]);

  const opened = [];
  assert.equal(routeNotificationClick(null, null, windows({
    mostRecentWindow: () => null,
    openWindow: (destination) => { opened.push(destination); return {}; },
  })), true);
  assert.deepEqual(opened, [null]);
});

// Stands in for Electron's Notification: an emitter whose close() also emits 'close', as the OS's does.
class FakeBanner extends EventEmitter {
  constructor() {
    super();
    this.closeCount = 0;
  }

  close() {
    this.closeCount += 1;
    this.emit('close');
  }
}

test("reading a chat closes that chat's live banners and leaves every other chat's up", () => {
  const registry = createBannerRegistry();
  const [first, second, otherChat, clicked] = [new FakeBanner(), new FakeBanner(), new FakeBanner(), new FakeBanner()];
  registry.remember('chat-a', first);
  registry.remember('chat-a', second);
  registry.remember('chat-a', clicked);
  registry.remember('chat-b', otherChat);
  clicked.emit('click');

  assert.equal(registry.closeChat('chat-a'), 2);

  assert.deepEqual([first.closeCount, second.closeCount, clicked.closeCount, otherChat.closeCount], [1, 1, 0, 0]);
  // Read again: nothing is left to close.
  assert.equal(registry.closeChat('chat-a'), 0);
  assert.equal(registry.closeChat('chat-b'), 1);
});

test('a banner with no chat is never tracked', () => {
  const registry = createBannerRegistry();
  const banner = new FakeBanner();
  registry.remember('', banner);
  registry.remember(undefined, banner);

  assert.equal(registry.closeChat(''), 0);
  assert.equal(banner.listenerCount('close'), 0);
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
  assert.deepEqual(nativeNotificationOptionsFor({ body: 'x' }, 'darwin'), { title: 'Imbue Studio', body: 'x' });
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
