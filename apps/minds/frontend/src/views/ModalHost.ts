// The browser-mode modal layer: a full-viewport iframe of the same server
// modal pages Electron's overlay view loads (/settings/modal,
// /accounts/modal, /auth/signin-modal, /inbox, /help, /sharing/.../modal).
// This deliberately mirrors Electron's overlay-iframe architecture instead of
// componentizing modal content -- the modal pages stay JinjaX and work in
// both hosts unchanged.
//
// The hosted page is same-origin and transparent (OverlaySurface), painting
// its own dim backdrop and dismissal affordances. Those affordances all
// branch on ``window.minds``: in Electron the preload provides the real IPC
// bridge, and here the parent exposes a bridge-compatible object
// (``window.__mindsModalHostBridge``) that the bundle adopts when it loads
// inside a modal-host iframe (see adoptParentModalBridge + index.ts). The
// pages therefore take their Electron code paths -- closeModal dismisses the
// host, navigateContent lands in the page BEHIND the modal and dismisses
// (the sign-in modal's MINDS_AUTH_NAV return-to flow), and chrome events are
// fanned into the frame from this document's shared EventSource.
import m from "mithril";

import type { ChromeEvent } from "../chrome_state";
import type { MindsBridge, ModalRequest } from "../host";
import { getHost, registerModalHost } from "../host";
import { mountPersistent, requireElement } from "../mount";

// Local path a successful sign-in should land on. Must start with a single
// '/' (never '//') and stay within a conservative charset; the server
// re-validates with safe_local_redirect_path. Anything else falls back to
// the server default (the create screen). Mirrors main.js's
// SIGNIN_RETURN_TO_PATTERN.
const SIGNIN_RETURN_TO_PATTERN = /^\/(?!\/)[A-Za-z0-9\-._~/?=&%]*$/;

// The overlay page each modal kind loads -- the same URLs Electron's
// openModal routes to (main.js's *UrlFor builders; keep in step).
export function modalPageUrl(request: ModalRequest): string | null {
  switch (request.kind) {
    case "minds-settings":
      return "/settings/modal";
    case "accounts":
      return "/accounts/modal";
    case "signin": {
      const params = new URLSearchParams();
      if (request.returnTo !== "" && SIGNIN_RETURN_TO_PATTERN.test(request.returnTo)) {
        params.set("return_to", request.returnTo);
      }
      // Only the literal 'signin' switches the leading tab (for "Log In"
      // callers); the server keeps the sign-up default otherwise.
      if (request.mode === "signin") params.set("mode", "signin");
      const query = params.toString();
      return `/auth/signin-modal${query !== "" ? `?${query}` : ""}`;
    }
    case "inbox":
      // ``keep_open=1`` marks an intentional open of the whole inbox
      // (resolve advances instead of dismissing); a targeted open selects
      // one request.
      return request.selectedRequestId !== undefined
        ? `/inbox?selected=${encodeURIComponent(request.selectedRequestId)}`
        : "/inbox?keep_open=1";
    case "help": {
      const params = new URLSearchParams();
      if (request.workspaceAgentId !== undefined && request.workspaceAgentId !== "") {
        params.set("workspace", request.workspaceAgentId);
      }
      if (request.isAssistAvailable === true) params.set("assist", "1");
      const query = params.toString();
      return `/help${query !== "" ? `?${query}` : ""}`;
    }
    case "sharing":
      if (request.serviceName === undefined || request.serviceName === "") return null;
      return `/sharing/${encodeURIComponent(request.agentId)}/${encodeURIComponent(request.serviceName)}/modal`;
  }
}

declare global {
  interface Window {
    // The bridge a modal-host iframe adopts as its ``window.minds`` (set
    // while this document has a mounted ModalHost).
    __mindsModalHostBridge?: MindsBridge;
    // Inline entry points on trusted local pages (welcome / create /
    // workspace settings) use this to open the in-document modal when the
    // Electron bridge is absent; set by chrome.js after the mount.
    __mindsOpenModal?: (request: ModalRequest) => void;
  }
}

export interface ModalHostHandleForPage {
  open(request: ModalRequest): void;
  close(): void;
  isOpen(): boolean;
  // The titlebar-button semantics: close the inbox if it is the open modal,
  // otherwise open the whole inbox (mirrors main.js's toggleInbox).
  toggleInbox(): void;
}

// Mount the modal layer into its persistent shell container (#minds-modal-host,
// outside #local-page-root) and register it as the browser host's openModal
// target. Browser mode only; Electron routes modals through main's overlay.
export function mountModalHost(target: Element | null): ModalHostHandleForPage {
  const el = requireElement(target, "modal host container");
  const host = getHost();

  let currentRequest: ModalRequest | null = null;
  let currentUrl: string | null = null;
  // Bumped on every open; keys the iframe vnode so a reopen of the SAME URL
  // still recreates the element (a fresh load, like Electron's loadURL-per-
  // open). Without it a same-URL reopen would clear frameCallbacks below
  // while mithril leaves the already-loaded frame in place, silently killing
  // the hosted page's chrome-event feed (it never re-subscribes).
  let openSeq = 0;
  // The open frame's chrome-event subscribers (the hosted inbox page's store
  // connect). Cleared on close so a dead frame's callbacks are dropped.
  let frameCallbacks: Array<(event: ChromeEvent) => void> = [];
  // One parent-side subscription forwards this document's chrome events into
  // whichever frame is open.
  host.onChromeEvent((event) => {
    frameCallbacks.forEach((callback) => {
      try {
        callback(event);
      } catch {
        // A torn-down frame's callback; dropped on the next open/close.
      }
    });
  });

  const close = (): void => {
    if (currentRequest === null) return;
    currentRequest = null;
    currentUrl = null;
    frameCallbacks = [];
    m.redraw();
  };

  const open = (request: ModalRequest): void => {
    // A whole-inbox open while the inbox is already up closes it: the
    // electron host maps openModal({kind:"inbox"}) to the toggle-inbox IPC,
    // and the titlebar's Requests button relies on that toggle in both
    // hosts. Targeted opens (a selected request id) always show it.
    if (request.kind === "inbox" && request.selectedRequestId === undefined && currentRequest?.kind === "inbox") {
      close();
      return;
    }
    // Help toggles the same way (the electron host maps it to main's
    // toggleHelp, which closes an open help modal regardless of its
    // ?workspace= / ?assist= query).
    if (request.kind === "help" && currentRequest?.kind === "help") {
      close();
      return;
    }
    const url = modalPageUrl(request);
    if (url === null) return;
    frameCallbacks = [];
    currentRequest = request;
    currentUrl = url;
    openSeq += 1;
    m.redraw();
  };

  const isOpen = (): boolean => currentRequest !== null;

  const toggleInbox = (): void => {
    if (currentRequest?.kind === "inbox") close();
    else open({ kind: "inbox" });
  };

  // The ``window.minds``-compatible bridge hosted iframes adopt, so the modal
  // pages' Electron code paths drive this host. Members that only make sense
  // for the real desktop shell (window controls, native menus/dialogs) are
  // deliberate no-ops -- same as browser mode everywhere else.
  const frameBridge: MindsBridge = {
    onChromeEvent: (callback) => frameCallbacks.push(callback),
    onAccentChanged: () => undefined,
    onCurrentWorkspaceChanged: () => undefined,
    onContentURLChange: () => undefined,
    navigateContent: (url) => {
      // Post-auth return-to and other "land BEHIND the modal" navigations:
      // dismiss, then navigate this document (swap-engine aware).
      close();
      host.navigate(url);
    },
    contentGoBack: () => host.goBack(),
    openWorkspaceInNewWindow: (agentId) => host.openWorkspaceInNewWindow(agentId),
    showWorkspaceContextMenu: () => undefined,
    confirmStopMind: () => undefined,
    openMindsSettings: () => open({ kind: "minds-settings" }),
    openAccounts: () => open({ kind: "accounts" }),
    openSigninModal: (returnTo, mode) =>
      open({ kind: "signin", returnTo, mode: mode === "signin" ? "signin" : "signup" }),
    toggleInbox,
    toggleHelp: (agentId, assistAvailable) =>
      open({ kind: "help", workspaceAgentId: agentId, isAssistAvailable: assistAvailable }),
    openSharingModal: (agentId, serviceName) => open({ kind: "sharing", agentId, serviceName }),
    closeModal: close,
    minimize: () => undefined,
    maximize: () => undefined,
    close: () => undefined,
  };
  window.__mindsModalHostBridge = frameBridge;

  // Escape dismisses any modal. In Electron main's before-input-event does
  // this for every modal-view page; here the keydown lands wherever focus
  // is -- THIS document right after an open (the titlebar button keeps
  // focus), or the IFRAME's document once the user interacts with the
  // modal -- so listen in both. The parent-side listener is inert while no
  // modal is open; the frame side is wired on every load (same-origin).
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && currentRequest !== null) close();
  });
  const wireFrameEscape = (frame: HTMLIFrameElement): void => {
    const contentDocument = frame.contentDocument;
    if (contentDocument === null) return;
    contentDocument.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });
  };

  mountPersistent(el, {
    view: () =>
      currentUrl === null
        ? null
        : [
            // Keyed (inside a fragment, where mithril's keyed diff applies)
            // per open: every open() loads the page fresh, even at an
            // unchanged URL (see openSeq above).
            m("iframe", {
              key: openSeq,
              id: "minds-modal-frame",
              src: currentUrl,
              // Above the titlebar (z-[100]) and the switcher backdrop
              // (z-50): the Electron modal view covers the whole window
              // including the titlebar, and this layer mirrors it. The
              // hosted OverlaySurface page is transparent and paints its
              // own dim backdrop.
              class: "fixed inset-0 w-full h-full border-0 z-[200]",
              onload: (event: Event) => wireFrameEscape(event.target as HTMLIFrameElement),
            }),
          ],
  });

  const handle: ModalHostHandleForPage = { open, close, isOpen, toggleInbox };
  registerModalHost(handle);
  return handle;
}

// Adopt the parent document's modal-host bridge as this document's
// ``window.minds`` when the bundle loads inside a modal-host iframe. Runs at
// bundle load, before the page's inline scripts (OverlaySurface orders the
// bundle tag first), so every ``window.minds`` branch -- inline dismissal
// handlers, MINDS_AUTH_NAV, and getHost()'s electron-host selection -- takes
// the Electron path against the parent's bridge.
export function adoptParentModalBridge(): void {
  if (window.minds !== undefined) return;
  if (window.parent === window) return;
  try {
    const bridge = (window.parent as Window & typeof globalThis).__mindsModalHostBridge;
    if (bridge !== undefined) window.minds = bridge;
  } catch {
    // Cross-origin parent (embedded somewhere that is not our modal host).
  }
}
