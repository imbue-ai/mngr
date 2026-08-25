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
}
