// The workspace content surface (route /workspace/<id>): the sandboxed
// cross-origin iframe + the white anti-flash mirror behind it, and the embed
// contract endpoint.
//
// A machine's health is NOT drawn here: the shell owns that, so one band can
// speak for whichever condition is actually relevant (a dead discovery
// consumer outranks the stuck machine it produces) and so the surface
// shrinks under it instead of being obscured by it.

import m from "mithril";
import { electronBridge } from "../../electron-bridge";
import { fetchJson } from "../../models/create";
import {
  armProviderRelay,
  type ProviderRelayAnswer,
  stopProviderRelay,
} from "../../models/providerRelay";
import { isWorkspaceOrigin } from "../../models/workspaces";
import type { ForwardPageActionKind, ForwardPageReport } from "../../models/forwardPages";
import { ForwardPageTracker, forwardPageNotice, isNoticeSpinnerOnly } from "../../models/forwardPages";
import { ForwardPageOverlay } from "./ForwardPageOverlay";
import { SharedFrameOverlay, sharedStallDelayMs } from "./SharedFrameOverlay";
import type { ImmersiveChange } from "./immersive";
import { ImmersiveController } from "./immersive";
import { LOCAL_FILES_SECTION } from "../../models/workspacePermissions";
import type {
  ChromeAnchor,
  DetachedWindowEntry,
  OpenLinkSender,
  PermissionResolvedSender,
  ReattachWindowSender,
  ShellState,
  TearOutSender,
  WorkspaceFrameHandle,
  WorkspaceMessageSender,
} from "./shell-state";
import { SETTINGS_PATH, isAppOverlayPath } from "./classify";
import { isHostOfShare, isSharedEntry } from "../../models/sharedWithMe";
import type { SharedEntry } from "../../models/sharedWithMe";
import { requestWallpaperRefresh } from "../../models/workspaceAssets";
import type {
  PopoutOpenRequest,
  TearOutReport,
  WorkspaceLinkHandlingReport,
  WorkspaceLinkPromises,
  WorkspaceWindowDragRequest,
} from "../../electron-bridge";
import { firstRunChrome } from "../../models/firstRunChrome";
import { welcomeChatArrival } from "../../models/welcomeChatArrival";
import type { WorkspaceReporting } from "../../models/welcomeChatArrival";

// The embed contract module is served verbatim at /_static/embed_contract.js
// (single shared source with the workspace side; see docs/embed-contract.md).
interface EmbedContractModule {
  OPEN_REQUEST_MODAL: string;
  OPEN_HELP: string;
  OPEN_AI_KEYS_PAGE: string;
  OPEN_AI_KEYS_ACK: string;
  BRING_APP_TO_FRONT: string;
  PROVIDER_SIGN_IN: string;
  PROVIDER_SIGN_IN_ACK: string;
  PROVIDER_SIGN_IN_END: string;
  OPEN_SHARE_SETTINGS: string;
  CLOSE_ACTIVE_TAB: string;
  PERMISSION_RESOLUTIONS: string;
  FOCUS_CHAT: string;
  WORKSPACE_READY: string;
  CHAT_ON_SCREEN: string;
  PROVIDER_CHOOSER_ON_SCREEN: string;
  FIRST_RUN_PROVIDER_STEP: string;
  FIRST_RUN_EXIT_COVER_MS: number;
  POP_OUT_WINDOW: string;
  WINDOW_DRAG_STARTED: string;
  WINDOW_DRAG_ENDED: string;
  DETACHED_WINDOWS: string;
  EMBEDDER_CAPABILITIES: string;
  REATTACH_WINDOW: string;
  TEAR_OUT: string;
  OPEN_LINK: string;
  OPEN_EXTERNAL: string;
  IMMERSIVE_REQUEST: string;
  IMMERSIVE_CHANGED: string;
  OPEN_LOCAL_FILES: string;
  HIGHLIGHT_CHROME: string;
  CHROME_ANCHORS: readonly string[];
  REQUEST_ID_PATTERN: RegExp;
  createEmbedderEndpoint(options: {
    getFrameWindow: () => Window | null;
    isExpectedOrigin: (origin: string) => boolean;
    handlers: Record<string, (message: Record<string, unknown>) => void>;
  }): {
    send(type: string, payload?: Record<string, unknown>): void;
    dispose(): void;
  };
}

/** One answered permission request, as relayed back into the asking frame. */
export interface PermissionResolutionEntry {
  requestId: string;
  resolution: "granted" | "denied";
}

// A feature missing here is refused to every page inside the workspace,
// however far down it is framed, before the OS is ever asked. `microphone`
// lets a workspace app listen to an audio input (the built-in mic, a headset
// on the audio port, a USB interface); the OS still asks the user first.
export const WORKSPACE_FRAME_ALLOW =
  "clipboard-read *; clipboard-write *; fullscreen *; microphone *";

async function loadEmbedContract(): Promise<EmbedContractModule> {
  // Runtime URL import (the module is served by Flask, not bundled); the
  // variable specifier keeps both Vite and tsc from trying to resolve it.
  const moduleUrl = "/_static/embed_contract.js";
  return (await import(/* @vite-ignore */ moduleUrl)) as EmbedContractModule;
}

export interface WorkspaceFrameAttrs {
  shell: ShellState;
  workspaceAnyId: string;
  /** The one window the workspace shell shows edge to edge (a popout), else
   * null for the whole desktop. */
  soloWindowId: string | null;
  /** Whether the app reopened that popout (a restore) rather than opening it
   * for a tear-out or the window menu just now. */
  isSoloReopened: boolean;
}

/** The request an OPEN_REQUEST_MODAL message names, or null when it names
 * none. The id reached us from foreign workspace content, so it is
 * re-validated here against the contract's server-issued shape before anything
 * selects on it (a receiver never trusts the sender); an off-shape id opens
 * the popup on whatever is pending instead. */
export function requestIdFromMessage(
  message: Record<string, unknown>,
  requestIdPattern: RegExp,
): string | null {
  const requestId = message.requestId;
  if (typeof requestId !== "string" || !requestIdPattern.test(requestId))
    return null;
  return requestId;
}

/** Dependencies the embed handlers act through, injected so the routing
 * decisions can be asserted without a DOM, a router, or the real contract. */
export interface EmbedHandlerDeps {
  contract: EmbedContractModule;
  navigate: (path: string, params?: Record<string, string>) => void;
  sendAck: (type: string, payload?: Record<string, unknown>) => void;
  bringAppToFront: () => void;
  /** Arm the desktop relay for a sign-in of the mounted workspace; whether it is relaying, and why not. */
  armProviderRelay: (flowId: string, url: string) => Promise<ProviderRelayAnswer>;
  /** Stop the desktop relay for a sign-in of the mounted workspace that has ended. */
  stopProviderRelay: (flowId: string) => void;
  /** The mounted workspace's id, for the ?workspace= an overlay floats
   * over. */
  workspaceAgentId: () => string;
  /** Whether the mounted workspace is someone else's (a "Shared with me" row,
   * specs/shared-with-me S12): its owner-only overlays stay closed. */
  isSharedWorkspace: () => boolean;
  openRequestPopup: (requestId: string | null) => void;
  /** The mounted page announced that its endpoint is listening, and whether
   * it opens the links main hands back (minds:open-link), custom-scheme ones
   * included. */
  onWorkspaceReady: (promises: WorkspaceLinkPromises) => void;
  /** The mounted page said what it reports on screen (minds:chat-on-screen, minds:provider-chooser-on-screen);
   * answers whether to ask it to show its chat root window now (minds:focus-chat without a chat id). */
  onReportingKnown: (reporting: WorkspaceReporting) => boolean;
  /** A chat's transcript is drawn in a window the user can see. */
  onChatOnScreen: (chatId: string) => void;
  /** The AI-provider chooser is drawn in a window the user can see. */
  onProviderChooserOnScreen: () => void;
  /** The first run's provider page went up over the workspace (true), or ended (false). */
  onFirstRunProviderStep: (isPending: boolean) => void;
  /** Open an external link the workspace handed over outside the app (minds:open-external); null in a chrome that
   * cannot, which then says so in its capabilities and the workspace opens the link itself. */
  openExternalLink: ((url: string) => void) | null;
  /** Ring one of the chrome's own controls while the workspace's tour talks about it, or clear the ring (null). */
  highlightChrome: (anchor: ChromeAnchor | null) => void;
  /** The workspace asked to pull one of its windows out (the pull-out-window
   * spec); null in a chrome that cannot, which then never answers with the
   * capability either. */
  popout: {
    open: (request: PopoutOpenRequest) => void;
    /** A title-bar drag began (or the dragged window changed size); main watches the cursor from here. */
    beginDrag: (request: WorkspaceWindowDragRequest) => void;
    /** The shell's own drag gesture ended, having detached the window or not, and cancelled or released
     * (null from a shell that does not say). */
    endDrag: (
      workspaceId: string,
      windowId: string,
      isDetached: boolean,
      isCancelled: boolean | null,
    ) => void;
    detachedWindows: (windows: readonly DetachedWindowEntry[]) => void;
  } | null;
  /** The workspace asked to enter or leave immersive mode for one of its
   * windows (minds:immersive-request); null in a chrome that cannot, which
   * then never answers with the capability either. Called synchronously from
   * the handler, so the fullscreen request keeps the click's activation. */
  immersive: { request: (on: boolean) => void } | null;
}

/** The message-type -> handler map the embedder endpoint dispatches through.
 * Built as a pure function of its dependencies so the mapping from a
 * workspace's message to the surface it opens is directly testable. */
export function buildEmbedHandlers(
  deps: EmbedHandlerDeps,
): Record<string, (message: Record<string, unknown>) => void> {
  const {
    contract,
    navigate,
    sendAck,
    bringAppToFront,
    armProviderRelay: armRelay,
    stopProviderRelay: stopRelay,
    workspaceAgentId,
    isSharedWorkspace,
    openRequestPopup,
    onWorkspaceReady,
    onReportingKnown,
    onChatOnScreen,
    onProviderChooserOnScreen,
    onFirstRunProviderStep,
  } = deps;
  const handlers: Record<string, (message: Record<string, unknown>) => void> =
    {};
  handlers[contract.OPEN_REQUEST_MODAL] = (message) => {
    openRequestPopup(
      requestIdFromMessage(message, contract.REQUEST_ID_PATTERN),
    );
  };
  handlers[contract.OPEN_HELP] = () => {
    // Float Get help over this machine (kept mounted), matching the titlebar
    // bug button, rather than tearing the frame down to a page.
    navigate("/help", { workspace: workspaceAgentId() });
  };
  handlers[contract.OPEN_AI_KEYS_PAGE] = (message) => {
    // A key is the owner's to mint; the workspace is still answered so it
    // does not wait out its timeout.
    if (isSharedWorkspace()) {
      sendAck(contract.OPEN_AI_KEYS_ACK);
      return;
    }
    // Float the AI-keys mint dialog over this workspace (kept mounted),
    // matching OPEN_HELP above. The mint page keys on the workspace id
    // (ai_keys.py dual-accepts a legacy host id too, which is what workspaces
    // running pre-workspace-id template code still send in `hostId`): prefer
    // the coordinate the workspace sent, else this surface's workspace id.
    const messageCoordinate =
      typeof message.hostId === "string" && message.hostId
        ? message.hostId
        : null;
    navigate("/settings/ai-keys", {
      workspace: messageCoordinate ?? workspaceAgentId(),
    });
    sendAck(contract.OPEN_AI_KEYS_ACK);
  };
  handlers[contract.PROVIDER_SIGN_IN] = (message) => {
    // The contract validated the shapes; the relay re-validates the URL itself. The ack always
    // goes, so the workspace never waits out its timeout to fall back to manual sign-in.
    const flowId = String(message.flowId);
    const url = String(message.url);
    void armRelay(flowId, url).then((answer) =>
      sendAck(contract.PROVIDER_SIGN_IN_ACK, { ...answer }),
    );
  };
  handlers[contract.PROVIDER_SIGN_IN_END] = (message) => {
    stopRelay(String(message.flowId));
  };
  handlers[contract.OPEN_SHARE_SETTINGS] = (message) => {
    if (isSharedWorkspace()) return;
    // Float Settings over this machine (kept mounted), at Sharing: the asking
    // app's page, or the whole workspace's when the message names none
    // (contract v13). A name Sharing does not recognize falls back to the
    // whole workspace too (SharePanelModel.selectTarget).
    const serviceName =
      typeof message.serviceName === "string" ? message.serviceName : null;
    const place = { workspace: workspaceAgentId(), tab: "workspace", group: "sharing" };
    navigate(SETTINGS_PATH, serviceName === null ? place : { ...place, share_target: serviceName });
  };
  handlers[contract.OPEN_LOCAL_FILES] = () => {
    if (isSharedWorkspace()) return;
    // Float Settings over this machine, at Permissions' Local files.
    navigate(SETTINGS_PATH, {
      workspace: workspaceAgentId(),
      tab: "workspace",
      group: "permissions",
      perm_section: LOCAL_FILES_SECTION,
    });
  };
  handlers[contract.BRING_APP_TO_FRONT] = () => bringAppToFront();
  handlers[contract.WORKSPACE_READY] = (message) => {
    // Before the held message goes (onWorkspaceReady flushes it): a held ask for one chat must land after
    // the ask for the chat root, not be undone by it.
    const isChatRootWanted = onReportingKnown({
      reportsChatOnScreen: message.reportsChatOnScreen === true,
      reportsProviderChooserOnScreen:
        message.reportsProviderChooserOnScreen === true,
    });
    if (isChatRootWanted) sendAck(contract.FOCUS_CHAT, {});
    onWorkspaceReady({
      opensLinks: message.opensLinks === true,
      opensCustomSchemeLinks: message.opensCustomSchemeLinks === true,
    });
    // What this chrome can do, so the workspace's pull-out gesture turns on
    // only where a desktop window can be made, and it hands over the
    // external links it does not open itself only where main opens them.
    // Every chrome that runs these handlers has Settings, so each
    // takes OPEN_LOCAL_FILES. Someone else's workspace (specs/shared-with-me,
    // S12) gets neither: its windows are not this device's to pull out, and
    // its local files are its owner's.
    const isShared = isSharedWorkspace();
    sendAck(contract.EMBEDDER_CAPABILITIES, {
      canPopOut: deps.popout !== null && !isShared,
      opensExternalLinks: deps.openExternalLink !== null,
      immersive: deps.immersive !== null,
      opensCustomSchemeLinks: deps.openExternalLink !== null,
      opensLocalFiles: !isShared,
      washesFirstRunExit: !isShared,
    });
  };
  handlers[contract.CHAT_ON_SCREEN] = (message) =>
    onChatOnScreen(String(message.chatId));
  handlers[contract.PROVIDER_CHOOSER_ON_SCREEN] = () =>
    onProviderChooserOnScreen();
  handlers[contract.FIRST_RUN_PROVIDER_STEP] = (message) =>
    onFirstRunProviderStep(message.isPending === true);
  const immersive = deps.immersive;
  if (immersive !== null) {
    handlers[contract.IMMERSIVE_REQUEST] = (message) =>
      immersive.request(message.on === true);
  }
  const openExternalLink = deps.openExternalLink;
  if (openExternalLink !== null) {
    handlers[contract.OPEN_EXTERNAL] = (message) =>
      openExternalLink(String(message.url));
  }
  handlers[contract.HIGHLIGHT_CHROME] = (message) => {
    // The contract's list is the one source of anchor names; anything else clears the ring.
    const anchor = message.anchor;
    const isKnown =
      typeof anchor === "string" && contract.CHROME_ANCHORS.includes(anchor);
    deps.highlightChrome(isKnown ? (anchor as ChromeAnchor) : null);
  };
  const popout = deps.popout;
  if (popout !== null) {
    // The contract validated every field's shape; the fields are re-read
    // here so nothing else of a message rides into main.
    const openRequestOf = (
      message: Record<string, unknown>,
    ): PopoutOpenRequest => ({
      workspaceId: workspaceAgentId(),
      windowId: String(message.windowId),
      title: String(message.title),
      width: Number(message.width),
      height: Number(message.height),
    });
    handlers[contract.POP_OUT_WINDOW] = (message) =>
      popout.open(openRequestOf(message));
    handlers[contract.WINDOW_DRAG_STARTED] = (message) =>
      popout.beginDrag({
        ...openRequestOf(message),
        grabX: Number(message.grabX),
        grabY: Number(message.grabY),
      });
    handlers[contract.WINDOW_DRAG_ENDED] = (message) =>
      popout.endDrag(
        workspaceAgentId(),
        String(message.windowId),
        message.isDetached === true,
        typeof message.isCancelled === "boolean" ? message.isCancelled : null,
      );
    handlers[contract.DETACHED_WINDOWS] = (message) => {
      const windows = Array.isArray(message.windows) ? message.windows : [];
      const entries: DetachedWindowEntry[] = [];
      for (const raw of windows) {
        if (typeof raw !== "object" || raw === null) continue;
        const entry = raw as Record<string, unknown>;
        if (typeof entry.windowId !== "string") continue;
        entries.push({
          windowId: entry.windowId,
          title: typeof entry.title === "string" ? entry.title : "",
        });
      }
      popout.detachedWindows(entries);
    };
  }
  return handlers;
}

/** GET the mounted workspace's recorded verdicts from the desktop client,
 * mapped onto the contract's entry shape; null on any failure (network,
 * non-200, or an off-shape body). Exported for the frame's wiring and tests. */
export async function fetchPermissionResolutionEntries(
  workspaceAgentId: string,
): Promise<PermissionResolutionEntry[] | null> {
  const url = `/ui/api/inbox/resolutions?workspace=${encodeURIComponent(workspaceAgentId)}`;
  let body: { resolutions?: { request_id?: unknown; resolution?: unknown }[] };
  try {
    body = await fetchJson(url);
  } catch {
    return null;
  }
  if (!Array.isArray(body.resolutions)) return null;
  const entries: PermissionResolutionEntry[] = [];
  for (const raw of body.resolutions) {
    if (typeof raw.request_id !== "string") continue;
    if (raw.resolution !== "granted" && raw.resolution !== "denied") continue;
    entries.push({ requestId: raw.request_id, resolution: raw.resolution });
  }
  return entries;
}

/** Push the workspace's verdict snapshot into its freshly loaded frame, so a
 * rebuilt page never offers Approve/Deny for an already-decided request.
 * Sent once on load and once shortly after (both idempotent), covering a page
 * whose contract endpoint registers a beat after the document's load event; a
 * failed lookup sends nothing, leaving the cards to the transcript's own
 * resolution notices. Exported for the frame's wiring and tests. */
export async function pushResolutionSnapshot(
  fetchEntries: (
    workspaceAgentId: string,
  ) => Promise<PermissionResolutionEntry[] | null>,
  send: (entries: PermissionResolutionEntry[]) => void,
  workspaceAgentId: string,
): Promise<void> {
  const entries = await fetchEntries(workspaceAgentId);
  if (entries !== null && entries.length > 0) send(entries);
}

const NO_LINK_PROMISES: WorkspaceLinkPromises = {
  opensLinks: false,
  opensCustomSchemeLinks: false,
};

/** What main is told about the mounted frame's link handling, and the gate on
 * handing its page a link main caught. Only the page's own announcement sets
 * its promises, so sending the frame to a new page forgets them until that
 * page announces: a URL sent before a page listens is lost. Repeated reports
 * of the same state are not sent again. */
export class FrameLinkHandling {
  private workspaceId: string | null = null;
  private promises: WorkspaceLinkPromises = NO_LINK_PROMISES;
  private lastReport: WorkspaceLinkHandlingReport | null | undefined =
    undefined;
  private readonly report: (report: WorkspaceLinkHandlingReport | null) => void;
  private readonly sendOpenLink: (url: string) => void;

  constructor(
    report: (report: WorkspaceLinkHandlingReport | null) => void,
    sendOpenLink: (url: string) => void,
  ) {
    this.report = report;
    this.sendOpenLink = sendOpenLink;
  }

  /** The frame was armed on a workspace (its agent-scoped id); `isNavigating`
   * when that sent the frame to a new page. */
  frameArmed(workspaceId: string, isNavigating: boolean): void {
    this.workspaceId = workspaceId;
    if (isNavigating) this.promises = NO_LINK_PROMISES;
    this.publish();
  }

  /** The frame's page announced that it is listening. */
  pageReady(promises: WorkspaceLinkPromises): void {
    this.promises = promises;
    this.publish();
  }

  /** The frame is no longer mounted. */
  unmounted(): void {
    this.workspaceId = null;
    this.promises = NO_LINK_PROMISES;
    this.publish();
  }

  /** Main handed back a link (a popup's or a frame navigation's URL): it goes
   * to a page that announced it opens links, and nowhere otherwise. */
  openLink(url: string): void {
    if (this.workspaceId === null || !this.promises.opensLinks) {
      const why =
        this.workspaceId === null
          ? "no workspace is mounted"
          : "its page has not announced opensLinks";
      console.warn(
        `[link-routing] dropped a link main handed back, since ${why}: ${url}`,
      );
      return;
    }
    this.sendOpenLink(url);
  }

  private publish(): void {
    const next =
      this.workspaceId === null
        ? null
        : { workspaceId: this.workspaceId, ...this.promises };
    const last = this.lastReport;
    const isUnchanged =
      last !== undefined &&
      (last === null || next === null
        ? last === next
        : last.workspaceId === next.workspaceId &&
          last.opensLinks === next.opensLinks &&
          last.opensCustomSchemeLinks === next.opensCustomSchemeLinks);
    if (isUnchanged) return;
    this.lastReport = next;
    this.report(next);
  }
}

/** The frame's `navigator.keyboard`, where the browser has one. Typed here
 * because lib.dom knows no Keyboard Lock API. */
function keyboardLockSurfaceOf(
  nav: Navigator,
): { lock(): Promise<void>; unlock(): void } | null {
  const keyboard = (
    nav as Navigator & { keyboard?: { lock?: unknown; unlock?: unknown } }
  ).keyboard;
  if (
    !keyboard ||
    typeof keyboard.lock !== "function" ||
    typeof keyboard.unlock !== "function"
  )
    return null;
  return {
    lock: () => (keyboard.lock as () => Promise<void>).call(keyboard),
    unlock: () => (keyboard.unlock as () => void).call(keyboard),
  };
}

/** Immersive mode over the real frame, document and desktop bridge; null
 * without a frame element to take fullscreen. */
function buildImmersiveController(
  frame: HTMLIFrameElement | null,
  doc: Document,
  send: (change: ImmersiveChange) => void,
): ImmersiveController | null {
  if (frame === null) return null;
  return new ImmersiveController({
    requestFullscreen: () => frame.requestFullscreen(),
    isFullscreen: () => doc.fullscreenElement !== null,
    exitFullscreen: () => doc.exitFullscreen(),
    keyboard: keyboardLockSurfaceOf(navigator),
    setDesktopImmersive: (on) => electronBridge.setImmersive(on),
    send,
  });
}

// electronBridge.onCloseActiveTab has no unregister, so the preload callback
// is registered ONCE at module scope and forwards to whichever frame is
// currently mounted; mount/unmount only swap this ref.
let activeCloseActiveTabForwarder: (() => void) | null = null;
let isCloseActiveTabRegistered = false;

function ensureCloseActiveTabRegistered(): void {
  if (isCloseActiveTabRegistered) return;
  isCloseActiveTabRegistered = true;
  electronBridge.onCloseActiveTab(() => activeCloseActiveTabForwarder?.());
}

export function WorkspaceFrame(): m.Component<WorkspaceFrameAttrs> {
  let frameElement: HTMLIFrameElement | null = null;
  let endpoint: {
    send(type: string, payload?: Record<string, unknown>): void;
    dispose(): void;
  } | null = null;
  let contract: EmbedContractModule | null = null;
  let armedWorkspaceAnyId: string | null = null;
  let unsubscribeWorkspaces: (() => void) | null = null;
  let closeActiveTabForwarder: (() => void) | null = null;
  let onFrameLoad: (() => void) | null = null;
  // Stamped by a listener of its own, attached at mount: `onFrameLoad` waits
  // for the embed contract and is also invoked by hand, so it cannot say when
  // a page actually loaded.
  let lastLoadedAtMs: number | null = null;
  const stampFrameLoad = (): void => {
    lastLoadedAtMs = Date.now();
  };
  let snapshotResendTimer: ReturnType<typeof setTimeout> | null = null;
  let permissionResolvedSender: PermissionResolvedSender | null = null;
  let workspaceMessageSender: WorkspaceMessageSender | null = null;
  let reattachWindowSender: ReattachWindowSender | null = null;
  let tearOutSender: TearOutSender | null = null;
  let openLinkSender: OpenLinkSender | null = null;
  const linkHandling = new FrameLinkHandling(
    (report) => electronBridge.reportWorkspaceLinkHandling(report),
    (url) => {
      if (contract !== null) endpoint?.send(contract.OPEN_LINK, { url });
    },
  );
  let frameHandle: WorkspaceFrameHandle | null = null;
  let isRemoved = false;
  // Immersive mode for the mounted frame (immersive.ts); built at mount so
  // the frame element exists, and told about every navigation and the
  // document's fullscreenchange, which is where it ends.
  let immersive: ImmersiveController | null = null;
  const onFullscreenChange = (): void => immersive?.fullscreenChanged();
  // Whether the page the frame currently holds has announced that its
  // endpoint is listening; a send before that is lost, so a focus-chat ask
  // waits in the shell until it arrives. Cleared at the navigation, the only
  // moment that is neither too early nor too late: leaving it to the frame's
  // load event keeps the outgoing page's readiness standing for the whole
  // load, and that event fires after the new page has already announced
  // itself -- which it does once.
  let isMountedPageReady = false;

  let armedSoloWindowId: string | null = null;
  let armedIsSoloReopened = false;

  // The workspace connection's own page the frame holds, if any, and the
  // redraw that brings in its explanation once its spinner-only seconds end.
  const forwardPage = new ForwardPageTracker();
  let forwardPageTimer: ReturnType<typeof setTimeout> | null = null;
  // The redraw that turns a shared open's connecting overlay into its
  // still-connecting state once the open has run long (SharedFrameOverlay).
  // Armed from view(), so a redraw re-arms it for an open that is still
  // connecting and not yet stalled; a stalled open arms nothing more.
  let sharedStallTimer: ReturnType<typeof setTimeout> | null = null;

  function armSharedStallTimer(shell: ShellState): void {
    if (sharedStallTimer !== null) return;
    const progress = shell.sharedFrameProgress;
    const delayMs = progress === null ? null : sharedStallDelayMs(progress, Date.now());
    if (delayMs === null) return;
    sharedStallTimer = setTimeout(() => {
      sharedStallTimer = null;
      m.redraw();
    }, delayMs);
  }

  /** The shared row the frame is armed on while its open is in progress or failed, else null. */
  function sharedProgressEntry(shell: ShellState): SharedEntry | null {
    const progress = shell.sharedFrameProgress;
    if (progress === null || armedWorkspaceAnyId === null) return null;
    const entry = shell.stores.workspaces.entryByAnyId(armedWorkspaceAnyId);
    if (entry === null || !isSharedEntry(entry) || entry.share.share_key !== progress.shareKey) return null;
    return entry;
  }

  function clearForwardPage(): boolean {
    if (forwardPageTimer !== null) {
      clearTimeout(forwardPageTimer);
      forwardPageTimer = null;
    }
    return forwardPage.clear();
  }

  function armForwardPageTimer(): void {
    forwardPageTimer = setTimeout(() => {
      forwardPageTimer = null;
      // A timer can fire a moment before Date.now says the seconds are up.
      if (forwardPage.report !== null && forwardPage.msUntilExplained() > 0) armForwardPageTimer();
      m.redraw();
    }, forwardPage.msUntilExplained());
  }

  const onWindowMessage = (event: MessageEvent): void => {
    if (frameElement === null) return;
    const isChanged = forwardPage.receive({
      data: event.data,
      origin: event.origin,
      isFromFrame: event.source === frameElement.contentWindow,
    });
    if (!isChanged) return;
    if (forwardPage.report === null) {
      clearForwardPage();
    } else if (forwardPageTimer === null) {
      armForwardPageTimer();
    }
    m.redraw();
  };

  function armFrame(
    shell: ShellState,
    workspaceAnyId: string,
    soloWindowId: string | null,
    isSoloReopened: boolean,
  ): void {
    if (frameElement === null) return;
    // A shared row's open had the frame go through a single-use redeem; a
    // frame that leaves the share and comes back without a fresh open loads
    // the share itself (specs/shared-with-me, 4.8).
    if (armedWorkspaceAnyId !== null && armedWorkspaceAnyId !== workspaceAnyId) {
      shell.stores.workspaces.forgetSharedLoadUrl(armedWorkspaceAnyId);
      firstRunChrome.release(shell.stores.workspaces.toAgentScopedId(armedWorkspaceAnyId));
    }
    armedWorkspaceAnyId = workspaceAnyId;
    armedSoloWindowId = soloWindowId;
    armedIsSoloReopened = isSoloReopened;
    const expected = shell.stores.workspaces.workspaceFrameUrl(
      workspaceAnyId,
      soloWindowId,
      isSoloReopened,
    );
    const isNavigating = frameElement.getAttribute("src") !== expected;
    if (isNavigating) {
      clearForwardPage();
      isMountedPageReady = false;
      immersive?.frameNavigated();
      shell.highlightedChromeAnchor = null;
      frameElement.src = expected;
    }
    linkHandling.frameArmed(
      shell.stores.workspaces.toAgentScopedId(workspaceAnyId),
      isNavigating,
    );
  }

  // Re-navigate the frame even though the URL is unchanged: assigning src
  // always processes the iframe attributes and navigates, and it is the only
  // reload the embedder has -- the frame is cross-origin, so its
  // contentWindow.location is unreachable. Consequence: the frame comes back
  // at the workspace root rather than wherever its own app had routed itself.
  function reloadFrame(shell: ShellState): void {
    if (frameElement === null || armedWorkspaceAnyId === null) return;
    // A reload never replays a shared open's redeem (its code is spent); the
    // signed-in partition reaches the share directly.
    shell.stores.workspaces.forgetSharedLoadUrl(armedWorkspaceAnyId);
    clearForwardPage();
    isMountedPageReady = false;
    immersive?.frameNavigated();
    shell.highlightedChromeAnchor = null;
    frameElement.src = shell.stores.workspaces.workspaceFrameUrl(
      armedWorkspaceAnyId,
      armedSoloWindowId,
      armedIsSoloReopened,
    );
    linkHandling.frameArmed(
      shell.stores.workspaces.toAgentScopedId(armedWorkspaceAnyId),
      true,
    );
  }

  function forwardPageOverlay(
    shell: ShellState,
    workspaceAnyId: string,
    report: ForwardPageReport,
  ): m.Children {
    const notice = forwardPageNotice(report, {
      isRestartAppAvailable: electronBridge.isDesktop,
      isWorkspaceListed: shell.stores.workspaces.entryByAnyId(workspaceAnyId) !== null,
    });
    const onAction = (kind: ForwardPageActionKind): void => {
      switch (kind) {
        case "try-again":
          reloadFrame(shell);
          return;
        case "restart-app":
          electronBridge.restartApp();
          return;
        case "back-to-workspaces":
          m.route.set("/");
          return;
      }
    };
    return m(ForwardPageOverlay, {
      notice,
      isSpinnerOnly: isNoticeSpinnerOnly(notice, forwardPage.msUntilExplained()),
      workspaceAgentId: shell.stores.workspaces.toAgentScopedId(workspaceAnyId),
      onAction,
    });
  }

  return {
    oncreate(vnode) {
      const { shell, workspaceAnyId, soloWindowId, isSoloReopened } =
        vnode.attrs;
      frameElement = vnode.dom.querySelector("#content-frame");
      frameElement?.addEventListener("load", stampFrameLoad);
      window.addEventListener("message", onWindowMessage);
      immersive = buildImmersiveController(frameElement, document, (change) => {
        if (contract !== null)
          endpoint?.send(contract.IMMERSIVE_CHANGED, { ...change });
      });
      document.addEventListener("fullscreenchange", onFullscreenChange);
      armFrame(shell, workspaceAnyId, soloWindowId, isSoloReopened);
      // The armed id, not the attr, is what the shell asks about: this frame is
      // re-armed by onupdate, and it is mounted for the workspace an app modal
      // floats over as well as for the routed workspace surface.
      frameHandle = {
        armedWorkspaceAnyId: () => armedWorkspaceAnyId,
        reload: () => reloadFrame(shell),
        lastLoadedAtMs: () => lastLoadedAtMs,
      };
      shell.workspaceFrame = frameHandle;

      // A frame armed before the workspace list arrives may be keyed by a
      // legacy host id with no alias mapping yet; re-arm when the mapping
      // lands and the URL therefore changes to the workspace-id form.
      unsubscribeWorkspaces = shell.stores.workspaces.onChanged(() => {
        if (armedWorkspaceAnyId !== null) {
          armFrame(
            shell,
            armedWorkspaceAnyId,
            armedSoloWindowId,
            armedIsSoloReopened,
          );
        }
      });

      void loadEmbedContract().then((loaded) => {
        // The module load can outlive the frame: registering the endpoint
        // after onremove would leak a window message listener that still
        // routes for an abandoned workspace.
        if (isRemoved) return;
        contract = loaded;
        const mountedAnyId = (): string =>
          armedWorkspaceAnyId ?? workspaceAnyId;
        const handlers = buildEmbedHandlers({
          contract: loaded,
          // An overlay floated over the frame carries the window's account,
          // so a reload keeps it; a workspace sub-page names the workspace.
          navigate: (path, params) =>
            m.route.set(
              path,
              isAppOverlayPath(path) ? shell.overlayQuery(params ?? {}) : params,
            ),
          sendAck: (type, payload) => endpoint?.send(type, payload),
          bringAppToFront: () => electronBridge.bringAppToFront(),
          armProviderRelay: (flowId, url) =>
            armProviderRelay(
              shell.stores.workspaces.toAgentScopedId(mountedAnyId()),
              flowId,
              url,
            ),
          stopProviderRelay: (flowId) =>
            stopProviderRelay(
              shell.stores.workspaces.toAgentScopedId(mountedAnyId()),
              flowId,
            ),
          workspaceAgentId: () =>
            shell.stores.workspaces.toAgentScopedId(mountedAnyId()),
          isSharedWorkspace: () => {
            const mounted = shell.stores.workspaces.entryByAnyId(mountedAnyId());
            return mounted !== null && isSharedEntry(mounted);
          },
          openRequestPopup: (requestId) => {
            shell.openInbox(requestId === null ? {} : { selected: requestId });
            m.redraw();
          },
          onWorkspaceReady: (promises) => {
            isMountedPageReady = true;
            // The workspace's own page is up, so no proxy page is left to
            // speak for, whether or not its going-away message arrived.
            if (clearForwardPage()) m.redraw();
            linkHandling.pageReady(promises);
            // A message held while the page was not listening -- a fresh
            // mount, or a click from another workspace -- goes now.
            shell.flushPendingWorkspaceMessage();
            // The workspace is up and serving its shell: the one moment its
            // wallpaper can be read for the home page's card
            // (specs/home-screen-redesign, 4.12). Someone else's workspace
            // cannot be read by the desktop.
            const mounted = shell.stores.workspaces.entryByAnyId(mountedAnyId());
            if (mounted !== null && !isSharedEntry(mounted)) void requestWallpaperRefresh(mounted.id);
            // A shared workspace's page being up is its open succeeding: the
            // connecting overlay comes down (specs/shared-with-me, 4.7).
            if (mounted !== null && isSharedEntry(mounted)) {
              shell.sharedFrameReady(mounted.share.share_key);
              m.redraw();
            }
          },
          onReportingKnown: (reporting) =>
            welcomeChatArrival.noteWorkspaceReady(reporting),
          onChatOnScreen: (chatId) => welcomeChatArrival.noteChatOnScreen(chatId),
          onProviderChooserOnScreen: () =>
            welcomeChatArrival.noteProviderChooserOnScreen(),
          onFirstRunProviderStep: (isPending) => {
            // The provider page is the owner's; someone else's workspace never holds this chrome.
            const mounted = shell.stores.workspaces.entryByAnyId(mountedAnyId());
            if (mounted !== null && isSharedEntry(mounted)) return;
            const agentId = shell.stores.workspaces.toAgentScopedId(mountedAnyId());
            if (isPending) {
              firstRunChrome.hold(agentId);
            } else {
              const accent = shell.stores.workspaces.accentEntry(agentId)?.accent ?? null;
              firstRunChrome.exit(agentId, accent, loaded.FIRST_RUN_EXIT_COVER_MS);
            }
          },
          openExternalLink: electronBridge.opensExternalLinks
            ? (url) => electronBridge.openExternalLink(url)
            : null,
          immersive:
            immersive === null
              ? null
              : { request: (on) => immersive?.request(on) },
          highlightChrome: (anchor) => {
            shell.highlightedChromeAnchor = anchor;
            m.redraw();
          },
          popout: electronBridge.canPopOut
            ? {
                open: (request) => electronBridge.openPopoutWindow(request),
                beginDrag: (request) =>
                  electronBridge.beginWorkspaceWindowDrag(request),
                endDrag: (workspaceId, windowId, isDetached, isCancelled) =>
                  electronBridge.endWorkspaceWindowDrag(
                    workspaceId,
                    windowId,
                    isDetached,
                    isCancelled,
                  ),
                detachedWindows: (windows) =>
                  shell.handleDetachedWindows(
                    windows,
                    (title) => electronBridge.setPopoutTitle(title),
                    () => electronBridge.closePopout(),
                  ),
              }
            : null,
        });
        endpoint = loaded.createEmbedderEndpoint({
          getFrameWindow: () => {
            if (frameElement === null) return null;
            try {
              return frameElement.contentWindow;
            } catch {
              return null;
            }
          },
          isExpectedOrigin: (origin: string) => {
            if (isWorkspaceOrigin(origin)) return true;
            // A shared workspace's frame is on the share's relay origin, a
            // label under the share's domain (specs/shared-with-me, S11).
            try {
              const hostname = new URL(origin).hostname;
              const mounted = shell.stores.workspaces.entryByAnyId(mountedAnyId());
              return mounted !== null && isSharedEntry(mounted) && isHostOfShare(hostname, mounted.share);
            } catch {
              return false;
            }
          },
          handlers,
        });
        closeActiveTabForwarder = () => {
          if (contract !== null) endpoint?.send(contract.CLOSE_ACTIVE_TAB);
        };
        activeCloseActiveTabForwarder = closeActiveTabForwarder;
        ensureCloseActiveTabRegistered();
        permissionResolvedSender = (requestId, verdict) => {
          // The same message type as the load-time snapshot, with one
          // unsolicited entry: the workspace keeps a single verdict code path.
          endpoint?.send(loaded.PERMISSION_RESOLUTIONS, {
            resolutions: [{ requestId, resolution: verdict }],
          });
        };
        shell.registerPermissionResolvedSender(permissionResolvedSender);
        workspaceMessageSender = (workspaceAgentId, message) => {
          if (
            shell.stores.workspaces.toAgentScopedId(mountedAnyId()) !==
            workspaceAgentId
          )
            return false;
          if (!isMountedPageReady) return false;
          switch (message.kind) {
            case "focus-chat":
              endpoint?.send(loaded.FOCUS_CHAT, {
                chatId: message.chatAgentId,
              });
              return true;
            case "app-message":
              // The workspace shell routes the type to the app whose
              // manifest registers it; the fields ride beside the type.
              endpoint?.send(message.type, message.fields);
              return true;
            case "open-link":
              // The app's own window, for one of the workspace's app
              // addresses (an own app opened from the home page).
              endpoint?.send(loaded.OPEN_LINK, { url: message.url });
              return true;
          }
        };
        shell.registerWorkspaceMessageSender(workspaceMessageSender);
        reattachWindowSender = (windowId) => {
          endpoint?.send(loaded.REATTACH_WINDOW, { windowId });
        };
        shell.registerReattachWindowSender(reattachWindowSender);
        tearOutSender = (report: TearOutReport) => {
          if (
            report.workspaceId !==
            shell.stores.workspaces.toAgentScopedId(mountedAnyId())
          )
            return;
          endpoint?.send(loaded.TEAR_OUT, {
            windowId: report.windowId,
            phase: report.phase,
          });
        };
        shell.registerTearOutSender(tearOutSender);
        openLinkSender = (url) => linkHandling.openLink(url);
        shell.registerOpenLinkSender(openLinkSender);
        // Every (re)load of the workspace page starts it with an empty verdict
        // cache; push its snapshot so no card offers Approve/Deny for a
        // request decided while the page was not live. Sent twice (idempotent)
        // in case the page registers its endpoint a beat after its load event.
        const sendSnapshot = () => {
          void pushResolutionSnapshot(
            fetchPermissionResolutionEntries,
            (entries) =>
              endpoint?.send(loaded.PERMISSION_RESOLUTIONS, {
                resolutions: entries,
              }),
            shell.stores.workspaces.toAgentScopedId(mountedAnyId()),
          );
        };
        onFrameLoad = () => {
          // A page the workspace itself reloaded is a fresh page with no tour on it. The load listener is
          // a DOM one, outside Mithril's redraw, so the ring is redrawn away here.
          if (shell.highlightedChromeAnchor !== null) {
            shell.highlightedChromeAnchor = null;
            m.redraw();
          }
          sendSnapshot();
          if (snapshotResendTimer !== null) clearTimeout(snapshotResendTimer);
          snapshotResendTimer = setTimeout(sendSnapshot, 2000);
        };
        frameElement?.addEventListener("load", onFrameLoad);
        // The frame usually finished loading before the contract module did;
        // that load event predates the listener, so push once now as well.
        onFrameLoad();
      });
    },
    onupdate(vnode) {
      const { shell, workspaceAnyId, soloWindowId, isSoloReopened } =
        vnode.attrs;
      armFrame(shell, workspaceAnyId, soloWindowId, isSoloReopened);
    },
    onremove(vnode) {
      isRemoved = true;
      // Leaving the workspace while its provider page is up leaves the chrome to the page it goes to.
      firstRunChrome.release(
        vnode.attrs.shell.stores.workspaces.toAgentScopedId(armedWorkspaceAnyId ?? vnode.attrs.workspaceAnyId),
      );
      if (activeCloseActiveTabForwarder === closeActiveTabForwarder) {
        activeCloseActiveTabForwarder = null;
      }
      if (permissionResolvedSender !== null) {
        vnode.attrs.shell.unregisterPermissionResolvedSender(
          permissionResolvedSender,
        );
        permissionResolvedSender = null;
      }
      if (workspaceMessageSender !== null) {
        vnode.attrs.shell.unregisterWorkspaceMessageSender(
          workspaceMessageSender,
        );
        workspaceMessageSender = null;
      }
      if (reattachWindowSender !== null) {
        vnode.attrs.shell.unregisterReattachWindowSender(reattachWindowSender);
        reattachWindowSender = null;
      }
      if (tearOutSender !== null) {
        vnode.attrs.shell.unregisterTearOutSender(tearOutSender);
        tearOutSender = null;
      }
      if (openLinkSender !== null) {
        vnode.attrs.shell.unregisterOpenLinkSender(openLinkSender);
        openLinkSender = null;
      }
      // Clear the shell's handle only if it is still ours, so this teardown can
      // never unhook a frame that is actually mounted, nor tell main that no
      // frame is mounted when a successor already reported its own.
      if (vnode.attrs.shell.workspaceFrame === frameHandle) {
        vnode.attrs.shell.workspaceFrame = null;
        vnode.attrs.shell.highlightedChromeAnchor = null;
        linkHandling.unmounted();
      }
      frameHandle = null;
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      immersive?.frameNavigated();
      immersive = null;
      if (onFrameLoad !== null)
        frameElement?.removeEventListener("load", onFrameLoad);
      onFrameLoad = null;
      frameElement?.removeEventListener("load", stampFrameLoad);
      window.removeEventListener("message", onWindowMessage);
      clearForwardPage();
      if (snapshotResendTimer !== null) {
        clearTimeout(snapshotResendTimer);
        snapshotResendTimer = null;
      }
      endpoint?.dispose();
      endpoint = null;
      unsubscribeWorkspaces?.();
      unsubscribeWorkspaces = null;
      if (sharedStallTimer !== null) {
        clearTimeout(sharedStallTimer);
        sharedStallTimer = null;
      }
      // Home unmounts the frame; a frame mounted again on the share without
      // a fresh open must load the share itself, never the spent redeem.
      if (armedWorkspaceAnyId !== null) vnode.attrs.shell.stores.workspaces.forgetSharedLoadUrl(armedWorkspaceAnyId);
      frameElement = null;
    },
    view(vnode) {
      const { shell } = vnode.attrs;
      const sharedEntry = sharedProgressEntry(shell);
      if (sharedEntry !== null) armSharedStallTimer(shell);
      // Health is reported by the shell's band, which shrinks this surface
      // rather than covering it: an unresponsive machine's last frame is
      // often the thing the user needs to read, and the band sits clear of it.
      return m("div", { style: "display: contents" }, [
        // White mirror behind the iframe: shows through whenever the frame's
        // compositor surface goes transparent on cross-origin navigation.
        m("div#content-bg-mirror", {
          class: "workspace-surface bg-surface-primary pointer-events-none",
        }),
        m("iframe#content-frame", {
          // allow-pointer-lock: a pointer lock needs it in every sandbox
          // list on the way (the Screen's viewer takes one; the workspace
          // shell's page frames add it too).
          sandbox:
            "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals allow-pointer-lock",
          allow: WORKSPACE_FRAME_ALLOW,
          class: "workspace-surface border-0 bg-surface-primary",
          style: "box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;",
        }),
        forwardPage.report === null || armedWorkspaceAnyId === null
          ? null
          : forwardPageOverlay(shell, armedWorkspaceAnyId, forwardPage.report),
        sharedEntry === null || shell.sharedFrameProgress === null
          ? null
          : m(SharedFrameOverlay, {
              progress: shell.sharedFrameProgress,
              workspaceName: sharedEntry.name,
              nowMs: Date.now(),
              onRetry: () => void shell.retryDisplayedSharedOpen(),
            }),
      ]);
    },
  };
}
