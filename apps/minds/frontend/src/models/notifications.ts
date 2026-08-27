// The notification feed, as last pushed over the channel.
//
// State-only: applying a message replaces the feed wholesale, and wire order
// IS display order (unresolved first, then resolved, each newest-first), so
// a snapshot replay and a live edge land identically. Arrival behavior
// (flash, toast) belongs to the surfaces reading this store, not here.

import type { UiNotificationEntry, UiNotificationsMessage } from "../channel/messages";

export class NotificationsStore {
  entries: readonly UiNotificationEntry[] = [];
  unresolvedCount = 0;

  applyNotificationsMessage(message: UiNotificationsMessage): void {
    this.entries = message.entries;
    this.unresolvedCount = message.unresolved_count;
  }

  /** Whether `workspaceAgentId`'s machine is waiting on the user -- the
   * condition behind the key tab's red dot, on the titlebar's own button and
   * its raised copy alike. Null (no machine on screen) is never waiting. */
  hasUnresolvedForWorkspace(workspaceAgentId: string | null): boolean {
    return (
      workspaceAgentId !== null &&
      this.entries.some(
        (entry) =>
          !entry.is_resolved &&
          entry.workspace_agent_id === workspaceAgentId,
      )
    );
  }
}
