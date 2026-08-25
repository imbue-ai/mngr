// The pending permission requests, as last pushed over the channel.
//
// A pending request never opens anything on its own: it waits behind the
// in-chat card's "Review & respond" button, the Permissions tab's "Waiting
// on you" rows, and the notification feed's rows and toasts (whose display
// state lives in NotificationsStore; only the review gesture's is-it-still-
// pending check reads this set). This store is only the live set those
// surfaces (and the popup's own reconciliation) read.

import type { UiRequestsMessage } from "../channel/messages";
import {
  retainWarmedRequestDetails,
  warmRequestDetail,
} from "./requestDetailPrefetch";

export class RequestsStore {
  requestIds: readonly string[] = [];

  applyRequestsMessage(message: UiRequestsMessage): void {
    this.requestIds = message.request_ids;
    // Fetch what reviewing each of these will need as soon as we know they are
    // pending, rather than when one is opened. The app holds the request from
    // here on, so every way in -- the in-chat card's button, a "Waiting on
    // you" row, a deep link -- opens on a request that has already arrived.
    retainWarmedRequestDetails(this.requestIds);
    for (const id of this.requestIds) warmRequestDetail(id);
  }
}
