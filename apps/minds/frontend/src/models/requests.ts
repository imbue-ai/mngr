// Pending-request inbox summary + the auto-open decision.
//
// The auto-open policy moved here from the Electron main process (which used
// to diff request-id sets against its SSE stream): open the inbox once per
// genuinely NEW pending id, only when the user's auto_open preference allows
// it. Re-asserted snapshots of the same id set never re-fire.

import type { UiRequestsMessage } from "../channel/messages";

export class RequestsStore {
  count = 0;
  requestIds: readonly string[] = [];
  isAutoOpenAllowed = true;

  private seenIds = new Set<string>();
  private isFirstMessage = true;
  private autoOpenListeners = new Set<(newIds: readonly string[]) => void>();

  applyRequestsMessage(message: UiRequestsMessage): void {
    this.count = message.count;
    this.requestIds = message.request_ids;
    this.isAutoOpenAllowed = message.auto_open;

    const newIds = message.request_ids.filter((id) => !this.seenIds.has(id));
    for (const id of newIds) this.seenIds.add(id);
    // Ids that left the pending set are forgotten so a later re-request
    // (deny -> new request) counts as new again.
    const pending = new Set(message.request_ids);
    for (const id of [...this.seenIds]) {
      if (!pending.has(id)) this.seenIds.delete(id);
    }

    // The connect-time snapshot describes requests that were already pending
    // before this window existed; only genuinely new arrivals auto-open.
    if (this.isFirstMessage) {
      this.isFirstMessage = false;
      return;
    }
    if (newIds.length > 0 && message.auto_open) {
      for (const listener of this.autoOpenListeners) listener(newIds);
    }
  }

  onAutoOpen(listener: (newIds: readonly string[]) => void): () => void {
    this.autoOpenListeners.add(listener);
    return () => this.autoOpenListeners.delete(listener);
  }
}
