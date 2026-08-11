// The workspace content surface (route /workspace/<id>): the sandboxed
// cross-origin iframe + the white anti-flash mirror behind it, the embed
// contract endpoint, and the health overlay.
//
// Faithful port of pages/Chrome.jinja + the frame parts of chrome.js, with
// two deliberate behavior changes from the plan: a STUCK workspace shows an
// overlay banner linking to Recovery (never auto-navigates), and a stopped
// workspace is never auto-restarted by observation.

import m from "mithril";
import { ButtonLink } from "../components/Button";
import { Notice } from "../components/Notice";
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
    view(vnode) {
      const { shell, workspaceAnyId } = vnode.attrs;
      const agentScoped = shell.stores.workspaces.toAgentScopedId(workspaceAnyId);
      const health = shell.stores.health.statusFor(agentScoped);
      const surfaceGeometry =
        "fixed left-[4px] top-[38px] rounded-[12px] " +
        "w-[calc(100%-8px)] h-[calc(100%-42px)]";

      return m("div", { style: "display: contents" }, [
        // White mirror behind the iframe: shows through whenever the frame's
        // compositor surface goes transparent on cross-origin navigation.
        m("div#content-bg-mirror", {
          class: `${surfaceGeometry} bg-surface-primary pointer-events-none`,
        }),
        m("iframe#content-frame", {
          sandbox:
            "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals",
          allow: "clipboard-read *; clipboard-write *; fullscreen *",
          class: `${surfaceGeometry} border-0 bg-surface-primary`,
          style: "box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;",
        }),
        health !== "healthy"
          ? m(
              "div",
              { class: `${surfaceGeometry} flex items-start justify-center pointer-events-none pt-10` },
              m(
                "div",
                { class: "pointer-events-auto max-w-[440px] w-full px-4" },
                m(
                  Notice,
                  { variant: health === "restarting" ? "info" : "warn" },
                  m("div", { class: "flex flex-col gap-2" }, [
                    m(
                      "span",
                      health === "restarting"
                        ? "This machine's interface is restarting…"
                        : "This machine's interface is not responding.",
                    ),
                    health !== "restarting"
                      ? m(
                          ButtonLink,
                          {
                            href: `#!/agents/${agentScoped}/recovery`,
                            variant: "secondary",
                            size: "md",
                            onclick: (event: MouseEvent) => {
                              event.preventDefault();
                              m.route.set(`/agents/${agentScoped}/recovery`);
                            },
                          },
                          "Open recovery",
                        )
                      : null,
                  ]),
                ),
              ),
            )
          : null,
      ]);
    },
  };
}
