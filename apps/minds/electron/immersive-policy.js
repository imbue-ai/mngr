'use strict';

// Pure decisions behind immersive mode (specs/screen-studio-integration/spec.md,
// section 5): which permission requests the default session grants, and
// whether a window's key relays are silenced. Kept free of any `electron`
// import so it can be unit-tested under plain node; main.js looks the
// origins up and acts on the answer.

// The permissions a page takes to run a remote desktop in a frame. Each is
// granted only to a page whose top-level origin is the chrome's own, so no
// Chrome lock prompt can appear inside the app and no other top-level page
// (the loading document, a stray popup) can take the window's keyboard.
const LOCK_PERMISSIONS = Object.freeze(['fullscreen', 'keyboardLock', 'pointerLock']);

/**
 * The origin of a URL, or null when it has none (an opaque origin such as
 * `file:` or `about:blank`, or no URL at all).
 *
 * @param {string|null|undefined} url
 * @returns {string|null}
 */
function originOf(url) {
  if (!url) return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  return parsed.origin === 'null' ? null : parsed.origin;
}

/**
 * Whether a permission request is granted.
 *
 * Electron's default with no handler installed is to grant everything, and
 * that stays the policy for every permission but the three a remote-desktop
 * frame takes, which are granted when the requesting frame's top-level page
 * is the chrome's own (the workspace iframe, cross-origin as it is, counts:
 * its top-level page is the chrome).
 *
 * @param {object} request
 * @param {string} request.permission  Electron's permission name.
 * @param {string|null} request.topLevelOrigin  The origin of the requesting webContents' top-level page.
 * @param {string|null} request.chromeOrigin  The origin the chrome page is served from; null before the backend is up.
 * @returns {boolean}
 */
function decidePermissionRequest({ permission, topLevelOrigin, chromeOrigin }) {
  if (!LOCK_PERMISSIONS.includes(permission)) return true;
  return topLevelOrigin !== null && chromeOrigin !== null && topLevelOrigin === chromeOrigin;
}

/**
 * Whether main relays nothing from a window's `before-input-event` (no
 * `escape-pressed`, no `close-active-tab`, no shortcut of its own): while the
 * window's frame is immersive, every key belongs to the remote desktop and
 * the one way out is the held Escape, which Chromium handles before the page.
 *
 * @param {object} bundle
 * @param {boolean} bundle.isImmersive
 * @returns {boolean}
 */
function isKeyRelaySilenced({ isImmersive }) {
  return isImmersive === true;
}

module.exports = {
  LOCK_PERMISSIONS,
  decidePermissionRequest,
  isKeyRelaySilenced,
  originOf,
};
