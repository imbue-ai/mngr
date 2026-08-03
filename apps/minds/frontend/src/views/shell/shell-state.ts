// Cross-view shell state: which workspace is displayed, accent painting, and
// workspace-entry navigation. The mutable singleton the Shell + frame views
// share (mithril redraws pull from it; imperative document-level effects --
// CSS variables -- happen here, exactly as chrome.js did).

import m from "mithril";
import type { AppStores } from "../../models/boot";
import type { UiChannelClient } from "../../channel/client";
import { accentSourceForRoute, isWorkspaceOverlayPath, workspaceSurfaceIdFromPath } from "./classify";

export class ShellState {
  readonly stores: AppStores;
  channel: UiChannelClient | null = null;
  isMac = false;
  mngrForwardOrigin = "";
  isSidebarOpen = false;
  sidebarAnchor: { x: number; y: number; width: number; height: number } | null = null;
  /** The workspace whose CONTENT is displayed (null on hub pages). */
  displayedWorkspaceAnyId: string | null = null;

  constructor(stores: AppStores) {
    this.stores = stores;
  }

  currentRoutePath(): string {
    const route = m.route.get() ?? "/";
    return route.split("?")[0];
  }

  /** Enter a workspace: route to the content surface for its identity. */
  enterWorkspace(anyId: string): void {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    m.route.set(`/workspace/${agentScoped}`);
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

  /** Route-change hook: track displayed workspace, repaint accent, register. */
  handleRouteChanged(path: string): void {
    const accentSource = accentSourceForRoute(path);
    // The options overlay keeps the workspace surface mounted, so it still
    // counts as displaying that workspace.
    this.displayedWorkspaceAnyId = workspaceSurfaceIdFromPath(path);
    this.paintAccent(accentSource);
    const agentScoped =
      this.displayedWorkspaceAnyId === null
        ? null
        : this.stores.workspaces.toAgentScopedId(this.displayedWorkspaceAnyId);
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
    const path = this.currentRoutePath();
    this.paintAccent(accentSourceForRoute(path));
  }

  private setTitlebarSurface(isOn: boolean): void {
    document.getElementById("minds-titlebar")?.classList.toggle("titlebar-surface", isOn);
  }

  openSidebar(anchor: { x: number; y: number; width: number; height: number }): void {
    this.sidebarAnchor = anchor;
    this.isSidebarOpen = true;
  }

  closeSidebar(): void {
    this.isSidebarOpen = false;
  }
}
