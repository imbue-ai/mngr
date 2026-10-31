'use strict';

// Unit tests for the immersive-mode decisions (electron/immersive-policy.js).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  LOCK_PERMISSIONS,
  decidePermissionRequest,
  isKeyRelaySilenced,
  originOf,
} = require('../../electron/immersive-policy');

const CHROME = 'http://localhost:8421';

test('the three lock permissions are granted to a page whose top level is the chrome', () => {
  assert.deepEqual([...LOCK_PERMISSIONS], ['fullscreen', 'keyboardLock', 'pointerLock']);
  for (const permission of LOCK_PERMISSIONS) {
    assert.equal(decidePermissionRequest({ permission, topLevelOrigin: CHROME, chromeOrigin: CHROME }), true);
  }
});

test('the lock permissions are refused to any other top-level page, and before the chrome exists', () => {
  for (const permission of LOCK_PERMISSIONS) {
    // The loading document (file:), a popup on another origin, a different port.
    assert.equal(decidePermissionRequest({ permission, topLevelOrigin: null, chromeOrigin: CHROME }), false);
    assert.equal(
      decidePermissionRequest({ permission, topLevelOrigin: 'https://example.com', chromeOrigin: CHROME }),
      false,
    );
    assert.equal(
      decidePermissionRequest({ permission, topLevelOrigin: 'http://localhost:9000', chromeOrigin: CHROME }),
      false,
    );
    assert.equal(decidePermissionRequest({ permission, topLevelOrigin: CHROME, chromeOrigin: null }), false);
  }
});

test('every other permission keeps the grant-everything default, whatever the origins', () => {
  for (const permission of ['media', 'notifications', 'clipboard-read', 'geolocation', 'openExternal', 'unknown']) {
    assert.equal(decidePermissionRequest({ permission, topLevelOrigin: CHROME, chromeOrigin: CHROME }), true);
    assert.equal(decidePermissionRequest({ permission, topLevelOrigin: null, chromeOrigin: null }), true);
    assert.equal(
      decidePermissionRequest({ permission, topLevelOrigin: 'https://example.com', chromeOrigin: CHROME }),
      true,
    );
  }
});

test('originOf reads an origin off a URL and answers null for opaque or missing ones', () => {
  assert.equal(originOf('http://localhost:8421/workspace/agent-ab12?x=1'), 'http://localhost:8421');
  assert.equal(originOf('https://web.agent-ab12.localhost:8443/page'), 'https://web.agent-ab12.localhost:8443');
  assert.equal(originOf('file:///app/shell.html#intro'), null);
  assert.equal(originOf('about:blank'), null);
  assert.equal(originOf('not a url'), null);
  assert.equal(originOf(''), null);
  assert.equal(originOf(null), null);
  assert.equal(originOf(undefined), null);
});

test('the key relays are silenced exactly while the window is immersive', () => {
  assert.equal(isKeyRelaySilenced({ isImmersive: true }), true);
  assert.equal(isKeyRelaySilenced({ isImmersive: false }), false);
  // A bundle from before the flag existed reads as not immersive.
  assert.equal(isKeyRelaySilenced({}), false);
});
