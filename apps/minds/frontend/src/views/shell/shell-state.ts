// Cross-view shell state: which workspace is displayed, accent painting, and
// workspace-entry navigation. The mutable singleton the Shell + frame views
// share (mithril redraws pull from it; imperative document-level effects --
// CSS variables -- happen here, exactly as chrome.js did).

import m from "mithril";
import type { AppStores } from "../../models/boot";
import type { WorkspaceHealth } from "../../models/health";
import type { UiChannelClient } from "../../channel/client";
import type { RequestVerdict, ResolvedRequest } from "../../models/inbox";
import {
  accentSourceForRoute,
  isAppOverlayPath,
  isWorkspaceOverlayPath,
  overlayBehindWorkspaceId,
  workspaceSurfaceIdFromPath,
} from "./classify";

/** Posts one permission-resolution message into the mounted workspace frame
 * over the embed contract. Registered by WorkspaceFrame, which owns the
 * contract endpoint; the shell only decides whether the message is due. */
export type PermissionResolvedSender = (requestId: string, verdict: RequestVerdict) => void;

/** The mounted workspace content iframe, as the shell addresses it. */
export interface WorkspaceFrameHandle {
  /** The workspace the frame is navigated to, or null before it is first armed. */
  armedWorkspaceAnyId(): string | null;
  /** Re-navigate the frame to that workspace's root URL. */
  reload(): void;
}

export class ShellState {
  readonly stores: AppStores;
  channel: UiChannelClient | null = null;
  isMac = false;
  mngrForwardOrigin = "";
  isSidebarOpen = false;
  sidebarAnchor: { x: number; y: number; width: number; height: number } | null = null;
  /** The workspace whose CONTENT is displayed (null on hub pages). */
  displayedWorkspaceAnyId: string | null = null;
  /** The workspace-options route an app modal was opened over, so the Shell can
   * keep that panel painted (and mounted) beneath the modal: the live route is
   * the modal's, which carries none of the panel's params. Null whenever no
   * modal floats over the panel. */
  panelRouteBehindOverlay: string | null = null;
  /**
   * The one piece of recovery-card state: which machine's card is up, and
   * whether the shell raised it or the user did.
   *
   * Not derived from health. The card auto-raises on a single edge, and an edge
   * fires once, so there is nothing to re-derive and no dismissal to remember.
   * The isAutoRaised bit decides one thing: whether the card leaves on its own
   * when the machine comes back.
   */
  private openRecovery: { agentId: string; isAutoRaised: boolean } | null = null;
  /** The mounted content iframe, installed by WorkspaceFrame (null when none is
   * mounted: hub pages, recovery, destroying, the workspace sub-pages). The
   * frame is ALSO mounted behind an app modal that forwarded ?workspace=, which
   * is why the workspace it is showing is asked of it rather than read off
   * `displayedWorkspaceAnyId`. */
  workspaceFrame: WorkspaceFrameHandle | null = null;
  /** Re-entrancy guard for closeAppOverlay: its history.back() is not
   * idempotent, and a repeated Escape can reach it again before the first
   * back() has landed. Cleared on the next route change. */
  private isAppOverlayClosing = false;

  private permissionResolvedSender: PermissionResolvedSender | null = null;

  constructor(stores: AppStores) {
    this.stores = stores;
  }

  /** Rebuild this window's workspace view, if its frame is showing the one named. */
  reloadWorkspaceFrame(agentScopedId: string): void {
    const frame = this.workspaceFrame;
    if (frame === null) return;
    const armed = frame.armedWorkspaceAnyId();
    if (armed === null) return;
    if (this.stores.workspaces.toAgentScopedId(armed) !== agentScopedId) return;
    frame.reload();
  }

  currentRoutePath(): string {
    const route = m.route.get() ?? "/";
    return route.split("?")[0];
  }

  currentRouteSearch(): string {
    const route = m.route.get() ?? "";
    return route.split("?")[1] ?? "";
  }

  /** Enter a workspace: route to the content surface for its identity. */
  enterWorkspace(anyId: string): void {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    m.route.set(`/workspace/${agentScoped}`);
  }

  /** Open the request-review popup over the current surface: forward the
   * displayed workspace as ?workspace so it floats over that live workspace
   * (kept mounted) instead of navigating the base layer to Home, mirroring how
   * Get help forwards ?workspace. Opened from Home (no workspace displayed), it
   * carries none and floats over Home. Extra query params (`selected` for a
   * named request) are merged in.
   *
   * Opened from the workspace-options panel, the route it was opened from is
   * remembered so that panel stays mounted underneath rather than being torn
   * down and rebuilt around the popup. */
  openInbox(params: Record<string, string> = {}): void {
    const path = this.currentRoutePath();
    // Only ever set here (handleRouteChanged clears it), so a second request
    // arriving while the popup is already up does not drop the panel it floats
    // over -- the route is /inbox by then, which names no panel.
    if (isWorkspaceOverlayPath(path)) this.panelRouteBehindOverlay = m.route.get() ?? null;
    const displayed = this.displayedWorkspaceAnyId;
    const query =
      displayed === null ? params : { ...params, workspace: this.stores.workspaces.toAgentScopedId(displayed) };
    // Swinging the OPEN popup onto another request replaces its history entry
    // rather than stacking a second one, so one dismissal still lands back on
    // the surface the popup was opened over instead of on the request before it.
    m.route.set("/inbox", query, path === "/inbox" ? { replace: true } : undefined);
  }

  /** Register the mounted workspace frame's contract sender. */
  registerPermissionResolvedSender(sender: PermissionResolvedSender): void {
    this.permissionResolvedSender = sender;
  }

  /** Drop `sender` if it is still the registered one (a frame torn down after
   * its successor registered must not clear the successor's). */
  unregisterPermissionResolvedSender(sender: PermissionResolvedSender): void {
    if (this.permissionResolvedSender === sender) this.permissionResolvedSender = null;
  }

  /** Tell the workspace that asked that its request now has a verdict, so its
   * in-chat card flips without waiting for the agent transcript to carry the
   * resolution back.
   *
   * Only the displayed workspace is told, and only when it is the one that
   * asked: the chrome mounts a single workspace frame, so no other workspace
   * has a live page in this window, and posting a request id into a workspace
   * that did not ask would hand it to foreign content for nothing. A verdict
   * given while looking at some other workspace is simply not relayed -- that
   * page is rebuilt from the transcript when the user returns to it, by which
   * point the agent's own resolution message has landed.
   *
   * Both sides of the comparison are WORKSPACE agent ids. The request's own
   * ``agent_id`` is not usable here: latchkey requests are filed by the
   * workspace's system-services sibling agent, so it never equals the id of
   * the tile on screen -- which is why the card carries the workspace it
   * belongs to, resolved server-side by name. */
  notifyRequestResolved(resolved: ResolvedRequest): void {
    const sender = this.permissionResolvedSender;
    const displayed = this.displayedWorkspaceAnyId;
    if (sender === null || displayed === null || resolved.agentId === null) return;
    if (this.stores.workspaces.toAgentScopedId(displayed) !== resolved.agentId) return;
    sender(resolved.requestId, resolved.verdict);
  }

  /** Close the options overlay if one is open, returning whether it was. */
  closeWorkspaceOverlay(): boolean {
    const path = this.currentRoutePath();
    if (!isWorkspaceOverlayPath(path)) return false;
    const surfaceId = workspaceSurfaceIdFromPath(path);
    if (surfaceId === null) return false;
    m.route.set(`/workspace/${surfaceId}`);
    return true;
  }

  /** Dismiss an open app-level modal (the request popup, Minds settings,
   * Accounts, Get help), returning to the surface it was opened over, and
   * report whether there was one. Prefers history so the opener (Home, Create,
   * the workspace, or its options panel) is restored exactly; falls back to
   * routing to the base when there is no history (a cold-start deep link). */
  closeAppOverlay(): boolean {
    const path = this.currentRoutePath();
    const search = this.currentRouteSearch();
    // The fixed app modals are always closeable; the New machine template
    // stepper is a closeable modal only while it floats over a machine
    // (?workspace=) -- with none it is a redirect, not an overlay.
    const isCloseable =
      isAppOverlayPath(path) ||
      (path === "/create/template" && overlayBehindWorkspaceId(path, search) !== null);
    if (!isCloseable) return false;
    // history.back() does not update the route synchronously, so a second
    // dismissal arriving before it lands (a repeated Escape) would fire
    // another back() and over-navigate past the opener. Reported as handled
    // even so: the key belongs to the overlay that is still on its way out.
    if (this.isAppOverlayClosing) return true;
    this.isAppOverlayClosing = true;
    if (window.history.length > 1) {
      window.history.back();
      return true;
    }
    const behind = overlayBehindWorkspaceId(path, search);
    m.route.set(behind !== null ? `/workspace/${behind}` : "/");
    return true;
  }

  /**
   * Dismiss the topmost dismissible surface, reporting whether there was one.
   *
   * The one place that knows what is stacked over what, so the precedence is a
   * plain ordered list rather than something the surfaces negotiate through
   * listener registration order (which follows mount order, not z-order).
   *
   * The switcher popover leads: it is the only surface that can open over the
   * recovery card. The card comes before the two route-based overlays because
   * it is not one -- it can be raised over the workspace options overlay, and
   * it sits above it. It is never raised over an app-level modal, so this never
   * has to choose between those two. The two route-based closers gate on the
   * live route, so a request popup floating over the options panel closes
   * alone (the route is the popup's) and leaves the panel standing.
   */
  handleEscape(): boolean {
    if (this.isSidebarOpen) {
      this.closeSidebar();
      return true;
    }
    return this.closeOpenRecoveryModal() || this.closeWorkspaceOverlay() || this.closeAppOverlay();
  }

  /** Route-change hook: track displayed workspace, repaint accent, register. */
  handleRouteChanged(path: string, search = ""): void {
    // The dismissal navigation has landed; clear the closeAppOverlay guard.
    this.isAppOverlayClosing = false;
    // The panel underneath belongs to the modal that was opened over it; once
    // the route is no longer a modal's, the panel is (or is not) the route.
    if (!isAppOverlayPath(path)) this.panelRouteBehindOverlay = null;
    // Pass the query so an app modal opened over a workspace (/help?workspace=)
    // keeps that workspace's accent painting behind it.
    const accentSource = accentSourceForRoute(path, search);
    // The options overlay and the app modals both keep the workspace surface
    // mounted behind them, so either still counts as displaying that
    // workspace -- which is what addresses a verdict to the machine that asked
    // while its request popup is the current route.
    this.displayedWorkspaceAnyId =
      workspaceSurfaceIdFromPath(path) ?? overlayBehindWorkspaceId(path, search);
    this.paintAccent(accentSource);
    const agentScoped =
      this.displayedWorkspaceAnyId === null
        ? null
        : this.stores.workspaces.toAgentScopedId(this.displayedWorkspaceAnyId);
    // A card belongs to one machine; arriving anywhere else does not carry it
    // along. Keyed on the card's own machine rather than on "the route
    // changed", so a card opened for a machine the window is still navigating
    // to survives its arrival.
    //
    // "Anywhere else" means the machine is off screen entirely, not merely
    // covered: an app-level modal opened over it (Get help, the Requests inbox)
    // keeps its surface mounted behind the backdrop, and the Shell renders no
    // card while one is up but expects it back on the way out. The card's own
    // "Report a problem" opens exactly such a modal.
    const heldWorkspaceAnyId = this.displayedWorkspaceAnyId ?? overlayBehindWorkspaceId(path, search);
    const heldAgentScoped =
      heldWorkspaceAnyId === null ? null : this.stores.workspaces.toAgentScopedId(heldWorkspaceAnyId);
    if (this.openRecovery !== null && this.openRecovery.agentId !== heldAgentScoped) {
      this.openRecovery = null;
    }
    this.channel?.setClientState(path, agentScoped);
  }

  paintAccent(workspaceAnyId: string | null): void {
    const root = document.documentElement;
    if (workspaceAnyId === null) {
      root.style.removeProperty("--workspace-accent");
      root.style.removeProperty("--titlebar-bg");
      this.setTitlebarSurface(false);
      return;
    }
    const cached = this.stores.workspaces.accentEntry(workspaceAnyId);
    if (cached?.accent == null) return; // painted when the list next lands
    root.style.setProperty("--workspace-accent", cached.accent);
    root.style.setProperty("--titlebar-bg", cached.accent);
    this.setTitlebarSurface(true);
  }

  /** Re-derive the accent for the current route (list updates, previews). */
  repaintAccentForCurrentRoute(): void {
    this.paintAccent(accentSourceForRoute(this.currentRoutePath(), this.currentRouteSearch()));
  }

  private setTitlebarSurface(isOn: boolean): void {
    document.getElementById("minds-titlebar")?.classList.toggle("titlebar-surface", isOn);
  }

  /** Publish the failure band's measured height so the workspace surface can
   * shrink by it (see .workspace-surface). A CSS variable rather than view
   * state: nothing needs to re-render for the surface to follow. */
  setNoticeBandHeight(height: number): void {
    document.documentElement.style.setProperty("--notice-band-height", `${height}px`);
  }

  /** Whether the recovery card is up over `agentId`. */
  isRecoveryModalOpenFor(agentId: string): boolean {
    return this.openRecovery?.agentId === agentId;
  }

  /** Whether the card that is up was raised by the shell rather than asked for. */
  isRecoveryModalAutoRaised(agentId: string): boolean {
    return this.openRecovery?.agentId === agentId && this.openRecovery.isAutoRaised;
  }

  /** The user asked for the card, from the band's "Open recovery". */
  openRecoveryModal(agentId: string): void {
    this.openRecovery = { agentId, isAutoRaised: false };
  }

  /**
   * A machine came back under an auto-raised card: drop the card.
   *
   * Does not reload the stale frame behind it. The server owns that: the
   * tracker's recovery edge broadcasts a ``workspace_refresh``, which every
   * window applies to its own frame -- including the ones with no card up.
   */
  finishRecovery(): void {
    this.openRecovery = null;
  }

  /** The user closed the card, which can happen while the machine is still
   * down -- so no recovery edge is coming, and this is the only thing that
   * repaints the dead page the frame is holding. */
  closeRecoveryModal(): void {
    this.workspaceFrame?.reload();
    this.openRecovery = null;
  }

  /** Close the recovery card if one is up over the displayed machine,
   * reporting whether there was one. ``handleEscape``'s step for the card,
   * ahead of the two route-based overlays it can be raised over.
   *
   * Keyed on ``displayedWorkspaceAnyId``, which ``handleRouteChanged`` derives
   * with the same ``workspaceSurfaceIdFromPath`` the router computes the
   * Shell's ``workspaceParam`` from -- the condition the card is rendered
   * under. The two must keep sharing that derivation: a card held through an
   * app-level modal (see ``handleRouteChanged``) is not rendered, and reporting
   * the key as spent on it would leave Escape unable to dismiss the modal that
   * is actually on top. */
  closeOpenRecoveryModal(): boolean {
    const displayed = this.displayedWorkspaceAnyId;
    if (displayed === null) return false;
    if (!this.isRecoveryModalOpenFor(this.stores.workspaces.toAgentScopedId(displayed))) return false;
    this.closeRecoveryModal();
    return true;
  }

  /**
   * A fresh snapshot is starting: give up any card this shell raised itself.
   *
   * After a reconnect the window no longer knows the edge that raised the card
   * still stands. A machine that recovered while the socket was down sends no
   * frame to say so -- hello clears the health store and the snapshot carries
   * only unhealthy agents -- so nothing else would ever drop the card. If the
   * failure does stand, the snapshot says so a moment later and the band
   * offers "Open recovery". A card the user opened themselves survives.
   */
  handleSnapshotStart(): void {
    if (this.openRecovery?.isAutoRaised === true) this.openRecovery = null;
  }

  /**
   * Raise the card on the edge into restart_failed for the displayed machine,
   * and drop an auto-raised one on the edge back into healthy.
   *
   * restart_failed means the app restarted the machine and it is still
   * unresponsive. That is the end of the unattended path, and a one-line band
   * is too quiet for it; every other condition leaves the band as the sole
   * surface and waits to be asked.
   *
   * Only an edge raises it -- ``isSnapshotFrame`` marks the connect-time replay
   * of current state -- so a window that opens onto a machine already in this
   * state gets the band, whose "Open recovery" is one click away.
   *
   * Nothing raises itself while the discovery consumer is dead: every machine
   * reads unhealthy then, and the card's actions all route through the forward
   * that consumer feeds, so it would offer "Restart Machine" over a band
   * correctly saying only restarting Minds can help.
   *
   * A card the user opened stays up when the machine answers -- they asked to
   * be there, and it gets to tell them how it ended.
   */
  handleHealthChanged(agentId: string, health: WorkspaceHealth, isSnapshotFrame: boolean): void {
    if (health === "healthy") {
      if (this.openRecovery?.agentId === agentId && this.openRecovery.isAutoRaised) this.finishRecovery();
      return;
    }
    if (isSnapshotFrame || health !== "restart_failed") return;
    if (this.openRecovery !== null) return;
    if (this.stores.health.discoveryHealth === "blocked") return;
    const displayed = this.displayedWorkspaceAnyId;
    if (displayed === null || this.stores.workspaces.toAgentScopedId(displayed) !== agentId) return;
    this.openRecovery = { agentId, isAutoRaised: true };
  }

  openSidebar(anchor: { x: number; y: number; width: number; height: number }): void {
    this.sidebarAnchor = anchor;
    this.isSidebarOpen = true;
  }

  closeSidebar(): void {
    this.isSidebarOpen = false;
  }
}
