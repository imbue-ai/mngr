// The workspace content surface (route /workspace/<id>): the sandboxed
// cross-origin iframe + the white anti-flash mirror behind it, and the embed
// contract endpoint.
//
// Faithful port of pages/Chrome.jinja + the frame parts of chrome.js. A
// machine's health is NOT drawn here: the shell owns that, so one band can
// speak for whichever condition is actually relevant (a dead discovery
// consumer outranks the stuck machine it produces) and so the surface
// shrinks under it instead of being obscured by it.

import m from "mithril";
import { electronBridge } from "../../electron-bridge";
import type { PermissionResolvedSender, ShellState, WorkspaceFrameHandle } from "./shell-state";

// The embed contract module is served verbatim at /_static/embed_contract.js
// (single shared source with the workspace side; see docs/embed-contract.md).
interface EmbedContractModule {
  OPEN_REQUEST_MODAL: string;
  OPEN_HELP: string;
  OPEN_AI_KEYS_PAGE: string;
  OPEN_AI_KEYS_ACK: string;
  BRING_APP_TO_FRONT: string;
  CLOSE_ACTIVE_TAB: string;
  PERMISSION_REQUEST_RESOLVED: string;
  REQUEST_ID_PATTERN: RegExp;
  createEmbedderEndpoint(options: {
    getFrameWindow: () => Window | null;
    isExpectedOrigin: (origin: string) => boolean;
    handlers: Record<string, (message: Record<string, unknown>) => void>;
  }): { send(type: string, payload?: Record<string, unknown>): void; dispose(): void };
}

const WORKSPACE_ORIGIN_FAMILY = /^(?:[a-z0-9_-]+\.)*host-[a-f0-9]+\.(?:localhost|127\.0\.0\.1)$/i;

async function loadEmbedContract(): Promise<EmbedContractModule> {
  // Runtime URL import (the module is served by Flask, not bundled); the
  // variable specifier keeps both Vite and tsc from trying to resolve it.
  const moduleUrl = "/_static/embed_contract.js";
  return (await import(/* @vite-ignore */ moduleUrl)) as EmbedContractModule;
}

export interface WorkspaceFrameAttrs {
  shell: ShellState;
  workspaceAnyId: string;
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
  if (typeof requestId !== "string" || !requestIdPattern.test(requestId)) return null;
  return requestId;
}

/** Dependencies the embed handlers act through, injected so the routing
 * decisions can be asserted without a DOM, a router, or the real contract. */
export interface EmbedHandlerDeps {
  contract: EmbedContractModule;
  navigate: (path: string, params?: Record<string, string>) => void;
  sendAck: (type: string) => void;
  bringAppToFront: () => void;
  /** The mounted machine's agent-scoped id, for the ?workspace= an overlay
   * floats over. */
  workspaceAgentId: () => string;
  /** The mounted machine's host-scoped id: the AI-keys mint endpoint resolves
   * the owning account from it. */
  workspaceHostId: () => string;
  openRequestPopup: (requestId: string | null) => void;
}

/** The message-type -> handler map the embedder endpoint dispatches through.
 * Built as a pure function of its dependencies so the mapping from a
 * workspace's message to the surface it opens is directly testable. */
export function buildEmbedHandlers(
  deps: EmbedHandlerDeps,
): Record<string, (message: Record<string, unknown>) => void> {
  const { contract, navigate, sendAck, bringAppToFront, workspaceAgentId, workspaceHostId, openRequestPopup } =
    deps;
  const handlers: Record<string, (message: Record<string, unknown>) => void> = {};
  handlers[contract.OPEN_REQUEST_MODAL] = (message) => {
    openRequestPopup(requestIdFromMessage(message, contract.REQUEST_ID_PATTERN));
  };
  handlers[contract.OPEN_HELP] = () => {
    // Float Get help over this machine (kept mounted), matching the titlebar
    // bug button, rather than tearing the frame down to a page.
    navigate("/help", { workspace: workspaceAgentId() });
  };
  handlers[contract.OPEN_AI_KEYS_PAGE] = (message) => {
    // Float the AI-keys mint dialog over this machine (kept mounted), matching
    // OPEN_HELP above. The mint page keys on the HOST id (ai_keys.py resolves
    // the owning account from the workspace record's host_id): prefer the host
    // id the workspace sent, else derive it from the mounted surface.
    const messageHostId = typeof message.hostId === "string" ? message.hostId : null;
    navigate("/settings/ai-keys", { workspace: messageHostId ?? workspaceHostId() });
    sendAck(contract.OPEN_AI_KEYS_ACK);
  };
  handlers[contract.BRING_APP_TO_FRONT] = () => bringAppToFront();
  return handlers;
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
  let endpoint: { send(type: string, payload?: Record<string, unknown>): void; dispose(): void } | null = null;
  let contract: EmbedContractModule | null = null;
  let armedWorkspaceAnyId: string | null = null;
  let unsubscribeWorkspaces: (() => void) | null = null;
  let closeActiveTabForwarder: (() => void) | null = null;
  let permissionResolvedSender: PermissionResolvedSender | null = null;
  let frameHandle: WorkspaceFrameHandle | null = null;
  let isRemoved = false;

  function armFrame(shell: ShellState, workspaceAnyId: string): void {
    if (frameElement === null) return;
    armedWorkspaceAnyId = workspaceAnyId;
    const expected = shell.stores.workspaces.workspaceFrameUrl(workspaceAnyId);
    if (frameElement.getAttribute("src") !== expected) {
      frameElement.src = expected;
    }
  }

  // Re-navigate the frame even though the URL is unchanged: assigning src
  // always processes the iframe attributes and navigates, and it is the only
  // reload the embedder has -- the frame is cross-origin, so its
  // contentWindow.location is unreachable. Consequence: the frame comes back
  // at the workspace root rather than wherever its own app had routed itself.
  function reloadFrame(shell: ShellState): void {
    if (frameElement === null || armedWorkspaceAnyId === null) return;
    frameElement.src = shell.stores.workspaces.workspaceFrameUrl(armedWorkspaceAnyId);
  }

  return {
    oncreate(vnode) {
      const { shell, workspaceAnyId } = vnode.attrs;
      frameElement = vnode.dom.querySelector("#content-frame");
      armFrame(shell, workspaceAnyId);
      // The armed id, not the attr, is what the shell asks about: this frame is
      // re-armed by onupdate, and it is mounted for the workspace an app modal
      // floats over as well as for the routed workspace surface.
      frameHandle = {
        armedWorkspaceAnyId: () => armedWorkspaceAnyId,
        reload: () => reloadFrame(shell),
      };
      shell.workspaceFrame = frameHandle;

      // A frame armed before the workspace list arrives may be keyed by an
      // agent id with no host mapping yet (/goto/ only routes host ids);
      // re-arm when the mapping lands and the URL therefore changes.
      unsubscribeWorkspaces = shell.stores.workspaces.onChanged(() => {
        if (armedWorkspaceAnyId !== null) armFrame(shell, armedWorkspaceAnyId);
      });

      void loadEmbedContract().then((loaded) => {
        // The module load can outlive the frame: registering the endpoint
        // after onremove would leak a window message listener that still
        // routes for an abandoned workspace.
        if (isRemoved) return;
        contract = loaded;
        const mountedAnyId = (): string => armedWorkspaceAnyId ?? workspaceAnyId;
        const handlers = buildEmbedHandlers({
          contract: loaded,
          navigate: (path, params) => m.route.set(path, params),
          sendAck: (type) => endpoint?.send(type),
          bringAppToFront: () => electronBridge.bringAppToFront(),
          workspaceAgentId: () => shell.stores.workspaces.toAgentScopedId(mountedAnyId()),
          workspaceHostId: () => shell.stores.workspaces.toHostScopedId(mountedAnyId()),
          openRequestPopup: (requestId) => {
            shell.openInbox(requestId === null ? {} : { selected: requestId });
            m.redraw();
          },
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
            try {
              return WORKSPACE_ORIGIN_FAMILY.test(new URL(origin).hostname);
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
          endpoint?.send(loaded.PERMISSION_REQUEST_RESOLVED, { requestId, resolution: verdict });
        };
        shell.registerPermissionResolvedSender(permissionResolvedSender);
      });
    },
    onupdate(vnode) {
      armFrame(vnode.attrs.shell, vnode.attrs.workspaceAnyId);
    },
    onremove(vnode) {
      isRemoved = true;
      if (activeCloseActiveTabForwarder === closeActiveTabForwarder) {
        activeCloseActiveTabForwarder = null;
      }
      if (permissionResolvedSender !== null) {
        vnode.attrs.shell.unregisterPermissionResolvedSender(permissionResolvedSender);
        permissionResolvedSender = null;
      }
      // Clear the shell's handle only if it is still ours, so this teardown can
      // never unhook a frame that is actually mounted.
      if (vnode.attrs.shell.workspaceFrame === frameHandle) {
        vnode.attrs.shell.workspaceFrame = null;
      }
      frameHandle = null;
      endpoint?.dispose();
      endpoint = null;
      unsubscribeWorkspaces?.();
      unsubscribeWorkspaces = null;
      frameElement = null;
    },
    view() {
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
          sandbox:
            "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals",
          allow: "clipboard-read *; clipboard-write *; fullscreen *",
          class: "workspace-surface border-0 bg-surface-primary",
          style: "box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;",
        }),
      ]);
    },
  };
}
