// Workspace-list store: the SPA-side mirror of the server's `workspaces`
// channel message, plus the agent<->host coordinate alias maps every
// navigation and accent lookup goes through.
//
// Entries key on `id` (the workspace's agent_id -- its stable, singular
// identity), and carry `host_id` (the logical machine currently running it:
// VM / container / remote host). Content URLs (`/goto/<workspace-id>/`, the
// `agent-<hex>.localhost` origin family) are keyed by the workspace id too,
// so they survive the workspace changing machines; host_id remains only for
// legacy-coordinate resolution.

import type { UiWorkspaceEntry, UiWorkspacesMessage } from "../channel/messages";
import type { RemoteTileState } from "../generated/ui";
import {
  isSharedEntry,
  isSharedRouteId,
  primaryLink,
  shareKeyOfRouteId,
} from "./sharedWithMe";

// The canonical content origin is agent-keyed (the workspace id); host-keyed
// origins are the legacy shape a stale frame may still sit on before the
// plugin's redirect heals it, so both families are expected senders.
const WORKSPACE_ORIGIN_FAMILY =
  /^(?:[a-z0-9_-]+\.)*(?:host|agent)-[a-f0-9]+\.(?:localhost|127\.0\.0\.1)$/i;

/** Whether `origin` is one of the workspace content origins the forward serves. */
export function isWorkspaceOrigin(origin: string): boolean {
  try {
    return WORKSPACE_ORIGIN_FAMILY.test(new URL(origin).hostname);
  } catch {
    return false;
  }
}

export interface AccentCacheEntry {
  accent: string | null;
  name: string | null;
}

export class WorkspacesStore {
  workspaces: readonly UiWorkspaceEntry[] = [];
  destroyingAgentIds: readonly string[] = [];
  restorableWorkspaceIds: readonly string[] = [];
  /** agent id -> the derived access state (and its detail) of each remote tile. */
  remoteWorkspaceStates: Readonly<Record<string, RemoteTileState>> = {};
  /** account id ('local' for the device) -> how many rows that account's windows list, every account's. */
  workspaceCountByAccountId: Readonly<Record<string, number>> = {};

  private accentByAnyId = new Map<string, AccentCacheEntry>();
  private agentIdByHostId = new Map<string, string>();
  private listeners = new Set<() => void>();
  /** share key -> the target link the last open chose (the shell's link otherwise), for the frame of a shared row. */
  private chosenSharedLinkByShareKey = new Map<string, string>();
  /** share key -> the URL the frame loads for that open instead of the link itself: the reverse handoff's redeem
   * page, which signs the window's partition in and sends the frame on to the link (specs/shared-with-me, 4.8). */
  private pendingSharedLoadUrlByShareKey = new Map<string, string>();
  /** Every id (and host id) a shared row has ever been listed under: a share stays one after its row is gone. */
  private sharedAnyIds = new Set<string>();

  applyWorkspacesMessage(message: UiWorkspacesMessage): void {
    this.workspaces = message.workspaces;
    this.destroyingAgentIds = message.destroying_agent_ids;
    this.restorableWorkspaceIds = message.restorable_workspace_ids;
    this.remoteWorkspaceStates = message.remote_workspace_states;
    this.workspaceCountByAccountId = message.workspace_count_by_account_id ?? {};
    for (const entry of message.workspaces) {
      const cached: AccentCacheEntry = {
        accent: entry.accent || null,
        name: entry.name || null,
      };
      this.accentByAnyId.set(entry.id, cached);
      if (entry.host_id) {
        this.accentByAnyId.set(entry.host_id, cached);
        this.agentIdByHostId.set(entry.host_id, entry.id);
      }
      if (isSharedEntry(entry)) {
        this.sharedAnyIds.add(entry.id);
        if (entry.host_id) this.sharedAnyIds.add(entry.host_id);
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
    return (
      this.workspaces.find((entry) => entry.id === agentId) ??
      // A shared row's route id may be the `shared-<key>` alias of its share key.
      this.sharedEntryByShareKey(shareKeyOfRouteId(anyId))
    );
  }

  /** The row shared with the window's account under `shareKey`, or null. */
  sharedEntryByShareKey(shareKey: string): UiWorkspaceEntry | null {
    return this.workspaces.find((entry) => isSharedEntry(entry) && entry.share.share_key === shareKey) ?? null;
  }

  /** Remember which of a shared row's targets the next frame load shows (the open action's choice), and the URL
   * that load goes through when the open redeems a sign-in on the way (null loads the link itself). */
  chooseSharedLink(shareKey: string, link: string, loadUrl: string | null): void {
    this.chosenSharedLinkByShareKey.set(shareKey, link);
    if (loadUrl === null) this.pendingSharedLoadUrlByShareKey.delete(shareKey);
    else this.pendingSharedLoadUrlByShareKey.set(shareKey, loadUrl);
    // A frame mounted on the share re-arms from this onto the new URL.
    this.emitChanged();
  }

  /** Forget the URL an open had the frame go through: a redeem is single-use, so a frame that comes back to the share
   * without a fresh open loads the link itself (the partition is signed in by then). */
  forgetSharedLoadUrl(anyId: string): void {
    const entry = this.entryByAnyId(anyId);
    if (entry !== null && isSharedEntry(entry)) this.pendingSharedLoadUrlByShareKey.delete(entry.share.share_key);
    else this.pendingSharedLoadUrlByShareKey.delete(shareKeyOfRouteId(anyId));
  }

  /** The share URL a shared row's frame is on: the open action's choice, else the shell's link; null for an owned row
   * or a shared row with no address yet. What the frame reports (its verdict, a login landing) is matched against
   * this, never against the redeem page an open had it go through. */
  sharedFrameLink(anyId: string): string | null {
    const entry = this.entryByAnyId(anyId);
    if (entry === null || !isSharedEntry(entry)) return null;
    return this.chosenSharedLinkByShareKey.get(entry.share.share_key) ?? primaryLink(entry.share);
  }

  /** What a shared row's frame loads: the open's redeem URL while one is pending, else the link itself. */
  private sharedFrameLoadUrl(anyId: string): string | null {
    const link = this.sharedFrameLink(anyId);
    if (link === null) return null;
    const entry = this.entryByAnyId(anyId);
    const shareKey = entry !== null && isSharedEntry(entry) ? entry.share.share_key : shareKeyOfRouteId(anyId);
    return this.pendingSharedLoadUrlByShareKey.get(shareKey) ?? link;
  }

  /** Whether a route id names a shared row: the `shared-` alias always does, a coordinate when its current row is
   * shared, or, with no current row, when it was ever listed as shared (a row gone from the list is still no
   * workspace of this account's; one listed again as owned, after the window switched to the owner's account, is). */
  isSharedRow(anyId: string): boolean {
    if (isSharedRouteId(anyId)) return true;
    const entry = this.entryByAnyId(anyId);
    if (entry !== null) return isSharedEntry(entry);
    return this.sharedAnyIds.has(anyId);
  }

  accentEntry(anyId: string): AccentCacheEntry | null {
    return this.accentByAnyId.get(anyId) ?? this.accentByAnyId.get(this.toAgentScopedId(anyId)) ?? null;
  }

  /** Translate either coordinate to the stable agent id (identity). */
  toAgentScopedId(anyId: string): string {
    return this.agentIdByHostId.get(anyId) ?? anyId;
  }

  workspaceFrameUrl(anyId: string, soloWindowId: string | null = null, isSoloReopened = false): string {
    // Content URLs are keyed by the workspace id (the /goto/ bridge and the
    // origin family both route it); a host id from persisted state resolves
    // through the alias map. A popout asks the workspace shell for one window
    // edge to edge: the shell's `?solo=` boot parameter rides through the
    // bridge's own `next`, which the /goto/ handler forwards as a same-origin
    // path, with `reopened=1` beside it for a popout the app reopened (a
    // restore), whose shell then trusts the stored layout at once.
    // A shared row's frame goes to the share URL (specs/shared-with-me, S11),
    // through the open's redeem page when the open signs the partition in on
    // the way: there is no local forward for someone else's workspace, and the
    // relay origin's gateway signs the account in through the broker. A
    // popout of it is not offered, so the solo parameters do not apply.
    const sharedLoadUrl = this.sharedFrameLoadUrl(anyId);
    if (sharedLoadUrl !== null) return sharedLoadUrl;
    // A shared row with no address (none registered yet, or its row gone from
    // the list) has nothing to load: never the local forward of someone
    // else's workspace id.
    if (this.isSharedRow(anyId)) return "about:blank";
    if (soloWindowId === null) return this.workspacePageUrl(anyId, null);
    const soloPath = "/?solo=" + encodeURIComponent(soloWindowId) + (isSoloReopened ? "&reopened=1" : "");
    return this.workspacePageUrl(anyId, soloPath);
  }

  /** The bridge URL that lands a frame on one page of the workspace's shell (its desktop for a null path). */
  workspacePageUrl(anyId: string, shellPath: string | null): string {
    const gotoPath = "/goto/" + this.toAgentScopedId(anyId) + "/";
    if (shellPath === null) return "/forward-bridge?next=" + encodeURIComponent(gotoPath);
    const bridgeNext = gotoPath + "?next=" + encodeURIComponent(shellPath);
    return "/forward-bridge?next=" + encodeURIComponent(bridgeNext);
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
