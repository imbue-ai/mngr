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
import type { ShellState, WorkspaceFrameHandle } from "./shell-state";

// The embed contract module is served verbatim at /_static/embed_contract.js
// (single shared source with the workspace side; see docs/embed-contract.md).
interface EmbedContractModule {
  OPEN_REQUEST_MODAL: string;
  OPEN_HELP: string;
  OPEN_AI_KEYS_PAGE: string;
  OPEN_AI_KEYS_ACK: string;
  BRING_APP_TO_FRONT: string;
  CLOSE_ACTIVE_TAB: string;
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
        const handlers: Record<string, (message: Record<string, unknown>) => void> = {};
        handlers[loaded.OPEN_REQUEST_MODAL] = () => {
          // Float the inbox drawer over this machine (kept mounted), matching
          // OPEN_HELP below, rather than tearing the frame down to Home.
          const current = armedWorkspaceAnyId ?? workspaceAnyId;
          m.route.set("/inbox", { workspace: shell.stores.workspaces.toAgentScopedId(current) });
        };
        handlers[loaded.OPEN_HELP] = () => {
          // Float Get help over this machine (kept mounted), matching the
          // titlebar bug button, rather than tearing the frame down to a page.
          const current = armedWorkspaceAnyId ?? workspaceAnyId;
          m.route.set("/help", { workspace: shell.stores.workspaces.toAgentScopedId(current) });
        };
        handlers[loaded.OPEN_AI_KEYS_PAGE] = (message) => {
          // Float the AI-keys mint dialog over this machine (kept mounted),
          // matching OPEN_HELP above, rather than tearing the frame down to a
          // page. The mint page keys on the HOST id (ai_keys.py resolves the
          // owning account from the workspace record's host_id): prefer the host
          // id the workspace sent, else derive it from the mounted surface.
          const current = armedWorkspaceAnyId ?? workspaceAnyId;
          const messageHostId = typeof message.hostId === "string" ? message.hostId : null;
          const hostId = messageHostId ?? shell.stores.workspaces.toHostScopedId(current);
          m.route.set("/settings/ai-keys", { workspace: hostId });
          endpoint?.send(loaded.OPEN_AI_KEYS_ACK);
        };
        handlers[loaded.BRING_APP_TO_FRONT] = () => electronBridge.bringAppToFront();
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
