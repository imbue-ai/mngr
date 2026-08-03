// Workspace-list store: the SPA-side mirror of the server's `workspaces`
// channel message, plus the agent<->host coordinate alias maps every
// navigation and accent lookup goes through.
//
// Entries key on `id` (the workspace's agent_id -- its stable, singular
// identity), and carry `host_id` (the logical machine currently running it:
// VM / container / remote host). The two are mostly 1:1 today but diverge
// under future operations: "clone" mints a new agent_id AND host_id, while
// "move" mints a new host_id but preserves the agent_id. UI state therefore
// keys on agent_id everywhere, and host_id is used only to build content
// URLs (`/goto/<host-id>/`, the `host-<hex>.localhost` origin family).

import type { UiWorkspaceEntry, UiWorkspacesMessage } from "../channel/messages";

export interface AccentCacheEntry {
  accent: string | null;
  name: string | null;
}

export class WorkspacesStore {
  workspaces: readonly UiWorkspaceEntry[] = [];
  destroyingAgentIds: readonly string[] = [];
  restorableWorkspaceIds: readonly string[] = [];
  remoteWorkspaceStates: Readonly<Record<string, string>> = {};

  private accentByAnyId = new Map<string, AccentCacheEntry>();
  private agentIdByHostId = new Map<string, string>();
  private hostIdByAgentId = new Map<string, string>();
  private listeners = new Set<() => void>();

  applyWorkspacesMessage(message: UiWorkspacesMessage): void {
    this.workspaces = message.workspaces;
    this.destroyingAgentIds = message.destroying_agent_ids;
    this.restorableWorkspaceIds = message.restorable_workspace_ids;
    this.remoteWorkspaceStates = message.remote_workspace_states as Record<string, string>;
    for (const entry of message.workspaces) {
      const cached: AccentCacheEntry = {
        accent: entry.accent || null,
        name: entry.name || null,
      };
      this.accentByAnyId.set(entry.id, cached);
      if (entry.host_id) {
        this.accentByAnyId.set(entry.host_id, cached);
        this.agentIdByHostId.set(entry.host_id, entry.id);
        this.hostIdByAgentId.set(entry.id, entry.host_id);
      }
    }
    this.emitChanged();
  }

  /** Live accent preview during color editing (no full list round trip). */
  applyAccentPreview(anyId: string, accent: string): void {
    const previous = this.accentByAnyId.get(anyId);
    this.accentByAnyId.set(anyId, { accent, name: previous?.name ?? null });
    this.emitChanged();
  }

  entryByAnyId(anyId: string): UiWorkspaceEntry | null {
    const agentId = this.toAgentScopedId(anyId);
    return this.workspaces.find((entry) => entry.id === agentId) ?? null;
  }

  accentEntry(anyId: string): AccentCacheEntry | null {
    return this.accentByAnyId.get(anyId) ?? this.accentByAnyId.get(this.toAgentScopedId(anyId)) ?? null;
  }

  /** Translate either coordinate to the stable agent id (identity). */
  toAgentScopedId(anyId: string): string {
    return this.agentIdByHostId.get(anyId) ?? anyId;
  }

  /** Translate either coordinate to the host id content URLs need. */
  toHostScopedId(anyId: string): string {
    return this.hostIdByAgentId.get(anyId) ?? anyId;
  }

  workspaceFrameUrl(anyId: string): string {
    const hostScoped = this.toHostScopedId(anyId);
    return "/forward-bridge?next=" + encodeURIComponent("/goto/" + hostScoped + "/");
  }

  isDestroying(anyId: string): boolean {
    return this.destroyingAgentIds.includes(this.toAgentScopedId(anyId));
  }

  onChanged(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emitChanged(): void {
    for (const listener of this.listeners) listener();
  }
}
