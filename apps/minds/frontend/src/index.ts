// SPA entry: read the inlined bootstrap, seed the stores, open the channel,
// wire the Electron bridge, and mount the router.

import m from "mithril";
import "./style.css";
import { registerAppContext } from "./app-context";
import { UiChannelClient } from "./channel/client";
import { electronBridge } from "./electron-bridge";
import { bootFromBootstrap, createEmptyStores } from "./models/boot";
import { setPendingHelpLaunch } from "./models/help";
import {
  NotificationsUiController,
  maybeProbeDesktopNotificationPermission,
  setReviewGestureContext,
} from "./models/notificationsUi";
import { consumeWebLoginParams, webLogin } from "./models/webLogin";
import { mountRouter, navigateExternalUrl } from "./router";
import { ShellState } from "./views/shell/shell-state";
import { installTooltips } from "./views/shell/tooltips";

// Stage the pre-filled agent-report launch, then route to the help page
// (which consumes it). Shared by the plain-browser open_help path and the
// Electron 'open-overlay' ask from the main process.
function openHelpFromShellAsk(
  shell: ShellState,
  workspaceAgentId: string,
  description: string,
): void {
  setPendingHelpLaunch({
    workspaceAgentId,
    description,
    isAgentReport: true,
    workspaceName:
      shell.stores.workspaces.accentEntry(workspaceAgentId)?.name ?? "",
  });
  m.route.set("/help");
}

function main(): void {
  const bootstrap = window.__MINDS_BOOTSTRAP__;
  const bootContext = bootstrap
    ? bootFromBootstrap(bootstrap)
    : {
        stores: createEmptyStores(),
        seed: {
          accent: "",
          isMac: electronBridge.isMacPlatform,
          mngrForwardOrigin: "",
        },
        // No inline bootstrap means no version to compare; null disables the
        // mismatch reload instead of guaranteeing one on the first hello.
        schemaVersion: null,
      };

  const shell = new ShellState(bootContext.stores);
  shell.isMac = bootContext.seed.isMac;
  shell.mngrForwardOrigin = bootContext.seed.mngrForwardOrigin;

  // Arrival behavior for the notification feed (toasts, dock badge, the OS
  // hint); the store itself stays a dumb wire mirror.
  const notificationsUi = new NotificationsUiController({
    onScreenWorkspaceAgentId: () => {
      const displayed = shell.displayedWorkspaceAnyId;
      return displayed === null
        ? null
        : bootContext.stores.workspaces.toAgentScopedId(displayed);
    },
    isFeedOverlayOpen: () => shell.isNotificationsOpen,
  });
  shell.notificationsUi = notificationsUi;
  // The review gesture's view of the world: it navigates only to machines
  // that are actually enterable, lands mid-create machines on their own
  // creating page, and otherwise opens the popup over the current surface.
  setReviewGestureContext({
    toAgentScopedId: (anyId) =>
      bootContext.stores.workspaces.toAgentScopedId(anyId),
    createAttemptStateOf: (agentScopedId) => {
      const entry = bootContext.stores.workspaces.entryByAnyId(agentScopedId);
      return entry === null ? null : (entry.create_attempt_state ?? "");
    },
    displayedWorkspaceAgentId: () => {
      const displayed = shell.displayedWorkspaceAnyId;
      return displayed === null
        ? null
        : bootContext.stores.workspaces.toAgentScopedId(displayed);
    },
    openInPlace: (requestId) => shell.openInbox({ selected: requestId }),
    currentRoutePath: () => shell.currentRoutePath(),
  });
  // The bootstrap snapshot is old news: seed the seen set silently (no
  // flashes for it) and tell the dock badge the starting count.
  notificationsUi.seedFromSnapshot(bootContext.stores.notifications);
  // Prefs (enabled + style) gate arrivals; the defaults stand until this
  // lands or the settings modal pushes fresher ones. Once the real prefs are
  // in (not the "both" default), a desktop build with unconfirmed OS
  // permission asks for it once here -- not on every focus-gain below, which
  // would otherwise re-show the probe's own banner on every alt-tab back in
  // while the user has left it undecided.
  void notificationsUi
    .loadPrefs()
    .then(() => maybeProbeDesktopNotificationPermission());
  // Cards flash only in the focused window, so refreshing prefs at focus-gain
  // closes the cross-window staleness gap (prefs changed in another window or
  // tab) without a new wire frame. loadPrefs dedupes concurrent loads and
  // discards a response that a newer local write outran. The catch-up flush
  // is chained onto the load (rather than fired alongside it) so it reads the
  // refreshed prefs, not whatever was applied before this window lost focus.
  window.addEventListener("focus", () => {
    void notificationsUi
      .loadPrefs()
      .then(() => notificationsUi.handleWindowFocusGained());
  });

  // Seed the accent before first paint so a workspace-scoped boot never
  // flashes neutral (the bootstrap seed carries the server-known accent).
  if (bootContext.seed.accent) {
    document.documentElement.style.setProperty(
      "--titlebar-bg",
      bootContext.seed.accent,
    );
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
      if (
        displayed !== null &&
        shell.stores.workspaces.toAgentScopedId(displayed) === message.agent_id
      ) {
        m.route.set("/");
      }
    },
    onHealthChanged: (message) => {
      // Optional in the generated type only because the field has a server-side
      // default; every frame on the wire sets it. Absent means live.
      shell.handleHealthChanged(
        message.agent_id,
        message.status,
        message.is_snapshot === true,
      );
    },
    onSnapshotStart: () => {
      shell.handleSnapshotStart();
    },
    onNotificationsChanged: (message) => {
      notificationsUi.handleNotificationsMessage(message);
    },
    onOpenHelp: (message) => {
      // An in-workspace agent escalated its diagnosis. In Electron, main
      // routes the (window-broadcast) event to exactly ONE window and asks
      // it to open help via 'open-overlay' (handled below); acting here too
      // would open help in every window. In plain-browser mode there is no
      // main process, so each tab handles it locally.
      if (electronBridge.isDesktop) return;
      openHelpFromShellAsk(
        shell,
        message.workspace_agent_id,
        message.description,
      );
    },
    onWorkspaceRefresh: (message) => {
      // An in-workspace agent says the interface this view is running is stale.
      // Every window acts on its own frame -- no main process involvement,
      // unlike the pre-SPA content-view reload.
      shell.reloadWorkspaceFrame(message.agent_id);
    },
  });
  shell.channel = channel;

  // The server's OS-dispatch gate needs this window's live focus state, not
  // just what it is displaying (see _ConnectedFocusedWorkspaceAgentIdsReader):
  // a window showing the asking workspace while alt-tabbed away should still
  // get an OS banner. Neither route nor workspace changes on a bare
  // focus/blur, so this can't piggyback on setClientState's own call sites.
  window.addEventListener("focus", () => channel.notifyFocusChanged());
  window.addEventListener("blur", () => channel.notifyFocusChanged());

  // Register the shared page-level context BEFORE mounting so page
  // components (which the router mounts without attrs) can read the stores.
  registerAppContext({ stores: bootContext.stores, shell });

  // Repaint the accent when the workspace list (and its accent cache)
  // changes -- covers the boot-before-mapping case and live color edits.
  bootContext.stores.workspaces.onChanged(() =>
    shell.repaintAccentForCurrentRoute(),
  );

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
  // The app's only in-document Escape handler. Capture phase so it runs ahead
  // of anything a page or panel registers, and the key is consumed only when a
  // surface actually took it -- an Escape nobody wanted still reaches whatever
  // is focused.
  document.addEventListener(
    "keydown",
    (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (!shell.handleEscape()) return;
      event.stopPropagation();
      m.redraw();
    },
    true,
  );
  // The same Escape, forwarded by Electron main, which forwards EVERY one. It
  // is needed only for the case the listener above cannot see -- focus inside a
  // cross-origin iframe, whose key events never reach this document -- so it is
  // gated on that: acting on both deliveries would spend one keypress on two
  // surfaces.
  electronBridge.onEscapePressed(() => {
    if (!(document.activeElement instanceof HTMLIFrameElement)) return;
    shell.handleEscape();
    m.redraw();
  });

  const root =
    document.getElementById("app") ??
    document.body.appendChild(document.createElement("div"));
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
    history.replaceState(
      null,
      "",
      window.location.pathname +
        (query ? `?${query}` : "") +
        window.location.hash,
    );
    void webLogin.start(webLoginMessage);
  }
}

main();
