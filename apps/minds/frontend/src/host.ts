// The host adapter: one interface, two implementations, chosen once per
// document by the presence of the window.minds Electron bridge. Components
// and the store call host methods instead of sprinkling
// `window.minds && window.minds.X ? ... : ...` branches (the pattern this
// migration deletes); do not add new sprinkled branches.
import type { ChromeEvent } from "./chrome_state";

// The subset of the Electron preload bridge (electron/preload.js) the host
// adapter and mount glue consume. All members exist whenever window.minds
// exists.
export interface MindsBridge {
  onChromeEvent(callback: (event: ChromeEvent) => void): void;
  // The accent-source workspace (the active scope incl. settings / sharing
  // screens), pushed by main on every navigation and replayed on view load.
  onAccentChanged(callback: (agentId: string | null) => void): void;
  // The workspace ACTUALLY DISPLAYED in the content view (null on its
  // settings / sharing screens); narrower than the accent source.
  onCurrentWorkspaceChanged(callback: (agentId: string | null, isContentReady: boolean) => void): void;
  // The content view's URL, pushed by main on every navigation (and replayed
  // when a chrome view (re)loads).
  onContentURLChange(callback: (url: string | null) => void): void;
  navigateContent(url: string): void;
  contentGoBack(): void;
  openWorkspaceInNewWindow(agentId: string): void;
  showWorkspaceContextMenu(agentId: string, x: number, y: number): void;
  confirmStopMind(agentId: string, name: string): void;
  openMindsSettings(): void;
  openAccounts(): void;
  openSigninModal(returnTo: string, mode: string): void;
  toggleInbox(): void;
  toggleHelp(agentId: string, assistAvailable: boolean): void;
  openSharingModal(agentId: string, serviceName: string): void;
  closeModal(): void;
  minimize(): void;
  maximize(): void;
  close(): void;
}

declare global {
  interface Window {
    minds?: MindsBridge;
    // chrome.js's swap-engine-aware navigation, exported for the browser
    // host so hub-page navigations ride the in-place swap instead of a
    // full document load.
    __mindsNavigateContent?: (url: string) => void;
  }
}

// A modal open request. Electron routes each kind to its overlay-view IPC;
// browser mode navigates to the full-page fallback route until Phase 7's
// in-document ModalHost lands.
export type ModalRequest =
  | { kind: "minds-settings" }
  | { kind: "accounts" }
  | { kind: "signin"; returnTo: string; mode: "signin" | "signup" }
  | { kind: "inbox"; selectedRequestId?: string }
  | { kind: "help"; workspaceAgentId?: string; isAssistAvailable?: boolean }
  | { kind: "sharing"; agentId: string; serviceName?: string };

export interface Host {
  kind: "electron" | "browser";
  // Subscribe to chrome events (IPC push in Electron; the shared per-document
  // EventSource in browser mode -- one reconnect loop for the whole document).
  onChromeEvent(callback: (event: ChromeEvent) => void): void;
  navigate(url: string): void;
  goBack(): void;
  openWorkspaceInNewWindow(agentId: string): void;
  // Native context menu in Electron; no-op in browser mode.
  showWorkspaceContextMenu(agentId: string, x: number, y: number): void;
  // Resolves to whether the CALLER should run the in-page stop flow
  // (optimistic transient + fetch). Electron resolves false: main owns the
  // native dialog AND issues the stop itself, with the result arriving over
  // the chrome events stream.
  confirmStopMind(agentId: string, name: string): Promise<boolean>;
  openModal(request: ModalRequest): void;
  closeModal(): void;
  // Native window controls; no-ops in browser mode (the buttons render but
  // are inert there, matching the pre-component behavior).
  minimizeWindow(): void;
  maximizeWindow(): void;
  closeWindow(): void;
}

// Mirrors the confirm text Landing's inline script uses in browser mode.
function stopMindConfirmText(name: string): string {
  return (
    `Stop "${name}"? Its agents will stop and its services become inaccessible. ` +
    "Data is preserved and you can start it again."
  );
}

export function createElectronHost(bridge: MindsBridge): Host {
  return {
    kind: "electron",
    onChromeEvent: (callback) => bridge.onChromeEvent(callback),
    navigate: (url) => bridge.navigateContent(url),
    goBack: () => bridge.contentGoBack(),
    openWorkspaceInNewWindow: (agentId) => bridge.openWorkspaceInNewWindow(agentId),
    showWorkspaceContextMenu: (agentId, x, y) => bridge.showWorkspaceContextMenu(agentId, x, y),
    confirmStopMind: (agentId, name) => {
      bridge.confirmStopMind(agentId, name);
      return Promise.resolve(false);
    },
    minimizeWindow: () => bridge.minimize(),
    maximizeWindow: () => bridge.maximize(),
    closeWindow: () => bridge.close(),
    openModal: (request) => {
      switch (request.kind) {
        case "minds-settings":
          bridge.openMindsSettings();
          break;
        case "accounts":
          bridge.openAccounts();
          break;
        case "signin":
          bridge.openSigninModal(request.returnTo, request.mode);
          break;
        case "inbox":
          bridge.toggleInbox();
          break;
        case "help":
          bridge.toggleHelp(request.workspaceAgentId ?? "", request.isAssistAvailable ?? false);
          break;
        case "sharing":
          bridge.openSharingModal(request.agentId, request.serviceName ?? "");
          break;
      }
    },
    closeModal: () => bridge.closeModal(),
  };
}

// Minimal EventSource surface, injectable so jsdom tests can supply a fake
// (jsdom implements no EventSource). Method syntax keeps the DOM EventSource
// structurally assignable.
export interface EventSourceLike {
  addEventListener(type: "message", listener: (event: { data: string }) => void): void;
  addEventListener(type: "error", listener: () => void): void;
  close(): void;
}

export interface BrowserHostOptions {
  createEventSource: (url: string) => EventSourceLike;
  // Reconnect delay after a dropped stream; mirrors chrome.js's 5s loop.
  reconnectDelayMs: number;
}

const CHROME_EVENTS_URL = "/_chrome/events";

// The browser-mode full-page fallback for each modal kind (Electron shows
// these as overlay modals; browser mode navigates until Phase 7).
function browserModalUrl(request: ModalRequest): string {
  switch (request.kind) {
    case "minds-settings":
      return "/settings";
    case "accounts":
      return "/accounts";
    case "signin":
      return "/auth/login";
    case "inbox":
      // ``keep_open=1`` marks an intentional open of the whole inbox (resolve
      // advances instead of dismissing); a targeted open selects one request.
      return request.selectedRequestId !== undefined
        ? `/inbox?selected=${encodeURIComponent(request.selectedRequestId)}`
        : "/inbox?keep_open=1";
    case "help": {
      const agentId = request.workspaceAgentId ?? "";
      if (agentId === "") return "/help";
      const assist = request.isAssistAvailable === true ? "&assist=1" : "";
      return `/help?workspace=${encodeURIComponent(agentId)}${assist}`;
    }
    case "sharing":
      return `/sharing/${encodeURIComponent(request.agentId)}`;
  }
}

export function createBrowserHost(options: BrowserHostOptions): Host {
  // One EventSource + one reconnect loop per document, shared by every
  // subscriber (chrome.js keeps its own separate stream until its consumers
  // are ported; converted surfaces all go through this one).
  const subscribers: Array<(event: ChromeEvent) => void> = [];
  let eventSource: EventSourceLike | null = null;

  function dispatch(raw: string): void {
    let parsed: ChromeEvent;
    try {
      parsed = JSON.parse(raw) as ChromeEvent;
    } catch {
      return;
    }
    subscribers.forEach((subscriber) => subscriber(parsed));
  }

  function connect(): void {
    if (eventSource !== null) eventSource.close();
    const source = options.createEventSource(CHROME_EVENTS_URL);
    eventSource = source;
    source.addEventListener("message", (event) => dispatch(event.data));
    source.addEventListener("error", () => {
      source.close();
      if (eventSource === source) {
        eventSource = null;
        window.setTimeout(() => {
          if (eventSource === null && subscribers.length > 0) connect();
        }, options.reconnectDelayMs);
      }
    });
  }

  function navigate(url: string): void {
    if (window.__mindsNavigateContent !== undefined) window.__mindsNavigateContent(url);
    else window.location.href = url;
  }

  return {
    kind: "browser",
    onChromeEvent: (callback) => {
      subscribers.push(callback);
      if (eventSource === null) connect();
    },
    navigate,
    goBack: () => {
      // On the agent-wrapper page the content lives in a child iframe; going
      // back must traverse ITS history, not the shell document's. Trusted
      // local pages (no #content-frame) are their own main frame.
      const contentFrame = document.getElementById("content-frame") as HTMLIFrameElement | null;
      if (contentFrame === null) {
        window.history.back();
        return;
      }
      try {
        contentFrame.contentWindow?.history.back();
      } catch {
        // Cross-origin content: nothing to traverse from here.
      }
    },
    openWorkspaceInNewWindow: (agentId) => {
      // No multi-window concept in browser mode: open the workspace in a new
      // tab via the mngr-forward origin the shell stamped on the body.
      const origin = document.body.dataset.mngrForwardOrigin ?? "";
      window.open(`${origin}/goto/${encodeURIComponent(agentId)}/`, "_blank", "noopener");
    },
    showWorkspaceContextMenu: () => {
      // Browser mode has no native context menus.
    },
    confirmStopMind: (agentId, name) => Promise.resolve(window.confirm(stopMindConfirmText(name))),
    minimizeWindow: () => undefined,
    maximizeWindow: () => undefined,
    closeWindow: () => undefined,
    openModal: (request) => navigate(browserModalUrl(request)),
    closeModal: () => {
      // Browser-mode modals are full pages until Phase 7; leaving one is a
      // navigation, so there is nothing to close here.
    },
  };
}

let singletonHost: Host | null = null;

// The document's host adapter, chosen once by the presence of the Electron
// bridge. Browser mode uses the document's real EventSource.
export function getHost(): Host {
  if (singletonHost === null) {
    singletonHost =
      window.minds !== undefined
        ? createElectronHost(window.minds)
        : createBrowserHost({
            createEventSource: (url) => new EventSource(url),
            reconnectDelayMs: 5000,
          });
  }
  return singletonHost;
}

export function resetHostForTesting(): void {
  singletonHost = null;
}
