// SPA entry: read the inlined bootstrap, seed the stores, open the channel,
// wire the Electron bridge, and mount the router.

import m from "mithril";
import "./style.css";
import { registerAppContext } from "./app-context";
import { UiChannelClient } from "./channel/client";
import { electronBridge } from "./electron-bridge";
import { bootFromBootstrap, createEmptyStores } from "./models/boot";
import { setPendingHelpLaunch } from "./models/help";
import { attachInboxRequestsStore } from "./models/inbox";
import { consumeWebLoginParams, webLogin } from "./models/webLogin";
import { mountRouter, navigateExternalUrl } from "./router";
import { ShellState } from "./views/shell/shell-state";
import { installTooltips } from "./views/shell/tooltips";

// Stage the pre-filled agent-report launch, then route to the help page
// (which consumes it). Shared by the plain-browser open_help path and the
// Electron 'open-overlay' ask from the main process.
function openHelpFromShellAsk(shell: ShellState, workspaceAgentId: string, description: string): void {
  setPendingHelpLaunch({
    workspaceAgentId,
    description,
    isAgentReport: true,
    workspaceName: shell.stores.workspaces.accentEntry(workspaceAgentId)?.name ?? "",
  });
  m.route.set("/help");
}

function main(): void {
  const bootstrap = window.__MINDS_BOOTSTRAP__;
  const bootContext = bootstrap
    ? bootFromBootstrap(bootstrap)
    : {
        stores: createEmptyStores(),
        seed: { accent: "", isMac: electronBridge.isMacPlatform, mngrForwardOrigin: "" },
        // No inline bootstrap means no version to compare; null disables the
        // mismatch reload instead of guaranteeing one on the first hello.
        schemaVersion: null,
      };

  const shell = new ShellState(bootContext.stores);
  shell.isMac = bootContext.seed.isMac;
  shell.mngrForwardOrigin = bootContext.seed.mngrForwardOrigin;

  // Seed the accent before first paint so a workspace-scoped boot never
  // flashes neutral (the bootstrap seed carries the server-known accent).
  if (bootContext.seed.accent) {
    document.documentElement.style.setProperty("--titlebar-bg", bootContext.seed.accent);
  }

  const channel = new UiChannelClient({
    stores: bootContext.stores,
    expectedSchemaVersion: bootContext.schemaVersion,
    // Main keeps minimal window bookkeeping fed by this verbatim relay
    // (workspaces/health/workspace_stopped/open_help).
    relayShellEvent: (message) => electronBridge.sendShellEvent(message),
    onWorkspaceStopped: (message) => {
      // Main closes other windows showing this workspace (via the relay
      // above); locally, leave the dead workspace if we display it.
      const displayed = shell.displayedWorkspaceAnyId;
      if (displayed !== null && shell.stores.workspaces.toAgentScopedId(displayed) === message.agent_id) {
        m.route.set("/");
      }
    },
    onOpenHelp: (message) => {
      // An in-workspace agent escalated its diagnosis. In Electron, main
      // routes the (window-broadcast) event to exactly ONE window and asks
      // it to open help via 'open-overlay' (handled below); acting here too
      // would open help in every window. In plain-browser mode there is no
      // main process, so each tab handles it locally.
      if (electronBridge.isDesktop) return;
      openHelpFromShellAsk(shell, message.workspace_agent_id, message.description);
    },
    onWorkspaceRefresh: (message) => {
      // An in-workspace agent says the interface this view is running is stale.
      // Every window acts on its own frame -- no main process involvement,
      // unlike the pre-SPA content-view reload.
      shell.reloadWorkspaceFrame(message.agent_id);
    },
  });
  shell.channel = channel;

  // Register the shared page-level context BEFORE mounting so page
  // components (which the router mounts without attrs) can read the stores.
  registerAppContext({ stores: bootContext.stores, shell });

  // Let an open Inbox page react to live pending-set changes off the store.
  attachInboxRequestsStore(bootContext.stores.requests);

  // Requests auto-open: the SPA decides (the policy moved out of the main
  // process); main is only asked to focus the window.
  bootContext.stores.requests.onAutoOpen((newIds) => {
    electronBridge.sendShellEvent({ type: "focus_window" });
    // Pre-select the newest request so a single arriving request opens
    // straight onto its detail (the legacy auto-open behavior). Float over the
    // current workspace surface if one is displayed rather than dropping to Home.
    shell.openInbox(newIds.length > 0 ? { selected: newIds[0] } : {});
  });

  // Repaint the accent when the workspace list (and its accent cache)
  // changes -- covers the boot-before-mapping case and live color edits.
  bootContext.stores.workspaces.onChanged(() => shell.repaintAccentForCurrentRoute());

  electronBridge.onNavigate((url) => {
    navigateExternalUrl(shell, url);
    m.redraw();
  });
  // Main-process asks that target exactly ONE window (main picks it): the
  // deduped open_help routing sends {kind:'help'} to the window showing the
  // affected workspace (else the most recent one).
  electronBridge.onOpenOverlay((cmd) => {
    if (typeof cmd !== "object" || cmd === null) return;
    const record = cmd as Record<string, unknown>;
    if (record.kind !== "help") return;
    openHelpFromShellAsk(
      shell,
      typeof record.workspace === "string" ? record.workspace : "",
      typeof record.description === "string" ? record.description : "",
    );
    m.redraw();
  });
  // Esc forwarded by Electron main. Main forwards EVERY Escape, so a single
  // keypress arrives here and (when focus is in this document) as the normal
  // in-document keydown as well.
  //
  // The switcher popover is closed first, unconditionally: it has no
  // in-document Escape listener, so this forward is its only closer.
  //
  // The overlay chain runs only when focus sits inside a cross-origin iframe
  // (the workspace frame, or a service iframe in a panel), whose key events
  // never reach this document -- the case the forward exists for. Everywhere
  // else the keypress already reached the in-document listeners, which take
  // the topmost surface themselves: the recovery card's capture-phase listener
  // beats the overlays' bubble ones and stops in-document propagation.
  // Running the chain here too would spend one Escape on two surfaces -- the
  // card's listener closes the card, and this delivery (which no
  // stopPropagation can reach) would then find no card and close the options
  // overlay beneath it.
  //
  // The card comes before the two route-based overlays because it is not one
  // -- it can be raised over the workspace options overlay, which it sits
  // above. It is never raised over an app-level modal, so the chain never has
  // to choose between the two.
  electronBridge.onEscapePressed(() => {
    if (shell.isSidebarOpen) {
      shell.closeSidebar();
    } else if (document.activeElement instanceof HTMLIFrameElement) {
      if (!shell.closeOpenRecoveryModal() && !shell.closeWorkspaceOverlay()) {
        shell.closeAppOverlay();
      }
    }
    m.redraw();
  });

  const root = document.getElementById("app") ?? document.body.appendChild(document.createElement("div"));
  root.id = "app";
  mountRouter(root, shell);
  // Delegated hover/focus tooltips for the [data-tooltip] chrome (titlebar
  // buttons, etc.); one document-level install survives mithril's re-renders.
  installTooltips();
  channel.start();

  // ``?web-login=1`` asks this window to start the browser sign-in as soon
  // as it loads: the backend's legacy /auth page URLs redirect here, and the
  // Electron shell navigates here on auth_required events. The optional
  // message explains why the sign-in is being asked for.
  const bootParams = new URLSearchParams(window.location.search);
  const webLoginMessage = consumeWebLoginParams(bootParams);
  if (webLoginMessage !== null) {
    // The boot params are one-shot: the Electron shell reloads every window
    // on auth_success (keeping the URL), so they must be consumed here or the
    // post-sign-in reload would immediately restart the flow.
    const query = bootParams.toString();
    history.replaceState(null, "", window.location.pathname + (query ? `?${query}` : "") + window.location.hash);
    void webLogin.start(webLoginMessage);
  }
}

main();
