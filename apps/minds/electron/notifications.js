'use strict';

const { parseWorkspaceId, parseSpaWorkspaceRouteId } = require('./surface-routing');
const { PRODUCT_DISPLAY_NAME } = require('./product-name');
const { decideWorkspaceWindowTarget } = require('./window-policy');

// All notification clicks share destination lookup, window selection, focus,
// and navigation. Native banners have no source window. A click for a
// workspace lands in that workspace's main window, and opens one when there
// is none: it never takes over a window showing another workspace. A click
// the source window can answer itself (it shows the workspace, or shows none)
// is left to its renderer's local gesture (e.g. an in-place review popup),
// signalled by false.
function routeNotificationClick(
  url,
  source,
  { findWindow, mostRecentWindow, showsWorkspace, focus, navigate, openWindow, openEntry },
) {
  const originId = parseWorkspaceId(url);
  const workspaceId = originId || parseSpaWorkspaceRouteId(url);
  if (!workspaceId) {
    if (source) return false;
    const target = mostRecentWindow();
    if (!target) {
      landInNewWindow(url, { openWindow, openEntry });
      return true;
    }
    focus(target);
    if (openEntry) openEntry(target, { isNewWindow: false });
    else if (url) navigate(target, url);
    return true;
  }
  const existing = findWindow(workspaceId);
  const decision = decideWorkspaceWindowTarget({
    existing,
    source,
    mayNavigateSource: !!source && !showsWorkspace(source),
  });
  if (decision === 'navigate-source') return false;
  if (decision === 'open-new') {
    landInNewWindow(url, { openWindow, openEntry });
    return true;
  }
  focus(existing);
  // Feed-backed banners and in-app clicks enter the renderer's one action:
  // dismiss the reminder, close its menu, and open its destination.
  if (openEntry) {
    openEntry(existing, { isNewWindow: false });
    return true;
  }
  // A workspace-origin link just raises the workspace's window; SPA links
  // must also deliver their chat/review query or subpage to it.
  if (!originId) navigate(existing, url);
  return true;
}

// A new window loads the destination itself; an entry action waits there for
// the page to come up rather than navigating it a second time.
function landInNewWindow(url, { openWindow, openEntry }) {
  const opened = openWindow(url);
  if (opened && openEntry) openEntry(opened, { isNewWindow: true });
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
  const title = typeof event.title === 'string' && event.title ? event.title : PRODUCT_DISPLAY_NAME;
  const subtitle = typeof event.subtitle === 'string' ? event.subtitle : '';
  const body = typeof event.body === 'string' ? event.body : '';
  if (platform === 'darwin') {
    return subtitle ? { title, subtitle, body } : { title, body };
  }
  return { title, body: subtitle && body ? `${subtitle}\n${body}` : subtitle || body };
}

// The banners the OS may still be showing, per chat. A banner leaves once it
// is clicked or closed; closing a chat's (it was read) takes the rest down.
// Takes anything with Electron Notification's on('close'|'click') and close().
function createBannerRegistry() {
  const bannersByChatAgentId = new Map();
  return {
    remember(chatAgentId, notification) {
      if (!chatAgentId) return;
      let banners = bannersByChatAgentId.get(chatAgentId);
      if (!banners) {
        banners = new Set();
        bannersByChatAgentId.set(chatAgentId, banners);
      }
      banners.add(notification);
      const forget = () => {
        banners.delete(notification);
        if (banners.size === 0 && bannersByChatAgentId.get(chatAgentId) === banners) {
          bannersByChatAgentId.delete(chatAgentId);
        }
      };
      notification.on('close', forget);
      notification.on('click', forget);
    },
    /** Close the chat's live banners; returns how many there were. */
    closeChat(chatAgentId) {
      const banners = bannersByChatAgentId.get(chatAgentId);
      if (!banners) return 0;
      bannersByChatAgentId.delete(chatAgentId);
      const closing = [...banners];
      for (const notification of closing) notification.close();
      return closing.length;
    },
  };
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
  createBannerRegistry,
  routeNotificationClick,
  nativeNotificationOptionsFor,
  linkFallbackFor,
};
