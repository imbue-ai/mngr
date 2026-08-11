// Cross-view shell state: which workspace is displayed, accent painting, and
// workspace-entry navigation. The mutable singleton the Shell + frame views
// share (mithril redraws pull from it; imperative document-level effects --
// CSS variables -- happen here, exactly as chrome.js did).

import m from "mithril";
import type { AppStores } from "../../models/boot";
import type { UiChannelClient } from "../../channel/client";
import {
  accentSourceForRoute,
  isAppOverlayPath,
  isWorkspaceOverlayPath,
  overlayBehindWorkspaceId,
  workspaceSurfaceIdFromPath,
} from "./classify";

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
  /**
   * The one piece of recovery-card state: which machine's card is up.
   *
   * Not derived from health. The card is raised by asking for it -- the band's
   * "Open recovery" -- so there is nothing to re-derive from a state that is
   * already on screen in the band.
   */
  private openRecoveryAgentId: string | null = null;
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

  /** Open the Requests inbox over the current surface: forward the displayed
   * workspace as ?workspace so the drawer floats over that live workspace (kept
   * mounted) instead of navigating the base layer to Home, mirroring how Get
   * help forwards ?workspace. Opened from Home (no workspace displayed), it
   * carries none and floats over Home. Extra query params (e.g. a pre-selected
   * request on auto-open) are merged in. */
  openInbox(params: Record<string, string> = {}): void {
    const displayed = this.displayedWorkspaceAnyId;
    const query =
      displayed === null
        ? params
        : { ...params, workspace: this.stores.workspaces.toAgentScopedId(displayed) };
    m.route.set("/inbox", query);
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

  /** Dismiss an open app-level modal (Minds settings / Accounts / Get help),
   * returning to the surface it was opened over. Prefers history so the opener
   * (Home, Create, or the workspace) is restored exactly; falls back to routing
   * to the base when there is no history (a cold-start deep link). */
  closeAppOverlay(): void {
    const path = this.currentRoutePath();
    const search = this.currentRouteSearch();
    // The fixed app modals are always closeable; the New machine inspiration
    // stepper is a closeable modal only while it floats over a machine
    // (?workspace=) -- with none it is a redirect, not an overlay.
    const isCloseable =
      isAppOverlayPath(path) ||
      (path === "/create/inspiration" && overlayBehindWorkspaceId(path, search) !== null);
    if (!isCloseable) return;
    // history.back() does not update the route synchronously, so a second
    // dismissal arriving before it lands (a repeated Escape) would fire
    // another back() and over-navigate past the opener.
    if (this.isAppOverlayClosing) return;
    this.isAppOverlayClosing = true;
    if (window.history.length > 1) {
      window.history.back();
      return;
    }
    const behind = overlayBehindWorkspaceId(path, search);
    m.route.set(behind !== null ? `/workspace/${behind}` : "/");
  }

  /** Route-change hook: track displayed workspace, repaint accent, register. */
  handleRouteChanged(path: string, search = ""): void {
    // The dismissal navigation has landed; clear the closeAppOverlay guard.
    this.isAppOverlayClosing = false;
    // Pass the query so an app modal opened over a workspace (/help?workspace=)
    // keeps that workspace's accent painting behind it.
    const accentSource = accentSourceForRoute(path, search);
    // The options overlay keeps the workspace surface mounted, so it still
    // counts as displaying that workspace.
    this.displayedWorkspaceAnyId = workspaceSurfaceIdFromPath(path);
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
    if (this.openRecoveryAgentId !== null && this.openRecoveryAgentId !== heldAgentScoped) {
      this.openRecoveryAgentId = null;
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
    return this.openRecoveryAgentId === agentId;
  }

  /** The user asked for the card, from the band's "Open recovery". */
  openRecoveryModal(agentId: string): void {
    this.openRecoveryAgentId = agentId;
  }

  /**
   * The user closed the card, and the frame behind it is reloaded.
   *
   * The frame is still holding whatever the machine served while it was down
   * -- an error page, or a half-loaded one -- and nothing about the recovery
   * changes its URL, so it would sit there until the user navigated away and
   * back. A card is only ever up mid-episode, so uncovering that dead page
   * would report a recovery the window does not show.
   */
  closeRecoveryModal(): void {
    this.workspaceFrame?.reload();
    this.openRecoveryAgentId = null;
  }

  /** Close the recovery card if one is up over the displayed machine,
   * reporting whether there was one. For the Escape that Electron forwards
   * out of the workspace iframe, whose keydowns never reach this document. */
  closeOpenRecoveryModal(): boolean {
    const displayed = this.displayedWorkspaceAnyId;
    if (displayed === null) return false;
    if (!this.isRecoveryModalOpenFor(this.stores.workspaces.toAgentScopedId(displayed))) return false;
    this.closeRecoveryModal();
    return true;
  }

  openSidebar(anchor: { x: number; y: number; width: number; height: number }): void {
    this.sidebarAnchor = anchor;
    this.isSidebarOpen = true;
  }

  closeSidebar(): void {
    this.isSidebarOpen = false;
  }
}
