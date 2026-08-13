// The pending permission requests, as last pushed over the channel.
//
// A pending request never opens anything on its own: it waits behind the
// in-chat card's "Review & respond" button and the Permissions tab's
// "Waiting on you" rows. This store is only the live set those surfaces (and
// the popup's own reconciliation) read.

import type { UiRequestsMessage } from "../channel/messages";

export class RequestsStore {
  requestIds: readonly string[] = [];

  applyRequestsMessage(message: UiRequestsMessage): void {
    this.requestIds = message.request_ids;
  }
}
