'use strict';

const { parseWorkspaceId, parseSpaWorkspaceRouteId } = require('./surface-routing');

// All notification clicks share destination lookup, window selection, focus,
// and navigation. Native banners have no source window. In-app clicks can
// fall back to their source's local renderer gesture (e.g. an in-place review
// popup), signalled by false. Never create a window just for a notification.
function routeNotificationClick(url, source, { findWindow, mostRecentWindow, focus, navigate, openEntry }) {
  const originId = parseWorkspaceId(url);
  const workspaceId = originId || parseSpaWorkspaceRouteId(url);
  const existing = workspaceId ? findWindow(workspaceId) : null;
  const target = existing || source || mostRecentWindow();
  if (!target || target === source) return false;
  focus(target);
  // Feed-backed banners and in-app clicks enter the renderer's one action:
  // dismiss the reminder, close its menu, and open its destination.
  if (openEntry) {
    openEntry(target);
    return true;
  }
  // A workspace-origin link just raises an existing workspace; SPA links
  // must also deliver their chat/review query or subpage to that window.
  if (url && (!originId || !existing)) navigate(target, url);
  return true;
}

// Pure helpers behind main.js's native-notification and link-fallback paths.
// Kept free of any `electron` import so they can be unit-tested under plain
// node (see ../test/unit/notifications.test.js). main.js does the wiring:
// constructing the Notification, routing its click, writing the clipboard,
// and sending the fallback toast to the window the click happened in.

// The banner the backend's stdout `notification` event becomes, laid out the
// way Slack lays its banners out: the workspace (or account) as the title,
// the headline (chat name, request title, event name) as the subtitle, the
// detail as the body. `subtitle` is macOS-only in Electron; elsewhere it is
// folded into the body so the headline is never lost.
function nativeNotificationOptionsFor(event, platform) {
  const title = typeof event.title === 'string' && event.title ? event.title : 'Mind';
  const subtitle = typeof event.subtitle === 'string' ? event.subtitle : '';
  const body = typeof event.body === 'string' ? event.body : '';
  if (platform === 'darwin') {
    return subtitle ? { title, subtitle, body } : { title, body };
  }
  return { title, body: subtitle && body ? `${subtitle}\n${body}` : subtitle || body };
}

// What to tell the reader when no app handles a link. The address itself (or
// the whole URL for any other scheme) is what gets copied to the clipboard.
function linkFallbackFor(url) {
  let scheme = '';
  try {
    scheme = new URL(url).protocol.replace(':', '');
  } catch {
    // Unparseable url -- fall through with an empty scheme and copy verbatim.
  }
  const isAddressScheme = scheme === 'mailto' || scheme === 'tel';
  const clipboardText = isAddressScheme ? url.slice(url.indexOf(':') + 1) : url;
  const what = scheme === 'mailto' ? 'email address'
    : scheme === 'tel' ? 'phone number'
    : 'link';
  return {
    clipboardText,
    title: "Couldn't open link",
    body: `No app is set up to handle this ${what}. It has been copied to your clipboard.`,
  };
}

module.exports = {
  routeNotificationClick,
  nativeNotificationOptionsFor,
  linkFallbackFor,
};
