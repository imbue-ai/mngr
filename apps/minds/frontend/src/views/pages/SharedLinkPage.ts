// The landing of an invitation email's "Open it in the app" link
// (imbue-studio://shared/<host-id>, specs/shared-with-me 4.11): every account
// signed in here refreshes its listing at once, and the window lands on the
// share's account's home with the tile in view, or on its own home with a note
// when no account of this device was shared it (or none could be checked).

import m from "mithril";
import { getAppContext } from "../../app-context";
import {
  sharedWithMeApi,
  type RefreshedSharedListing,
} from "../../models/sharedWithMe";
import { accountHubRoute } from "../shell/classify";
import { PageContainer } from "../components/Layout";

const HOST_ID_RE = /^host-[a-f0-9]{32}$/i;
const NOT_SHARED_TITLE = "Not shared with you";
const NOT_SHARED_BODY =
  "That workspace is not shared with any of the accounts signed in here.";
const NOT_CHECKED_TITLE = "Could not check your shares";
const NOT_CHECKED_BODY =
  "What is shared with you could not be refreshed. Open the link again once Imbue Studio is back online.";

/** What looking a host id up across the signed-in accounts found. */
export interface SharedLinkLookup {
  /** The account whose listing names the host, and the share's key; null when none does. */
  hit: { accountId: string; shareKey: string } | null;
  /** Whether at least one account's listing was refreshed; false means nothing was checked. */
  isAnyRefreshed: boolean;
}

/** Refresh every account's listing at once and answer the first whose rows name `hostId`. */
export async function findSharedAccount(
  accountIds: readonly string[],
  hostId: string,
  refresh: (accountId: string) => Promise<RefreshedSharedListing>,
): Promise<SharedLinkLookup> {
  const outcomes = await Promise.allSettled(accountIds.map((accountId) => refresh(accountId)));
  let isAnyRefreshed = false;
  for (const [index, outcome] of outcomes.entries()) {
    const accountId = accountIds[index];
    if (outcome.status === "rejected") {
      // One account's listing could not be refreshed; the others may still name the share.
      console.warn(
        `[shared-with-me] could not refresh the listing of ${accountId}: ${String(outcome.reason)}`,
      );
      continue;
    }
    isAnyRefreshed = true;
    const hit = outcome.value.shares.find(
      (row) => row.host_id.toLowerCase() === hostId.toLowerCase(),
    );
    if (hit) return { hit: { accountId, shareKey: hit.share_key }, isAnyRefreshed };
  }
  return { hit: null, isAnyRefreshed };
}

interface SharedLinkState {
  isSettled: boolean;
  // Set once the page is left: a late lookup must not route or toast then.
  isRemoved: boolean;
}

export const SharedLinkPage: m.Component<
  Record<string, never>,
  SharedLinkState
> = {
  oninit(vnode) {
    vnode.state.isSettled = false;
    vnode.state.isRemoved = false;
    const hostId = String(m.route.param("hostId") ?? "");
    const { shell, stores } = getAppContext();
    if (!HOST_ID_RE.test(hostId)) {
      shell.routeToHub("/", { replace: true });
      return;
    }
    const accountIds = stores.accounts.accounts.map(
      (account) => account.user_id,
    );
    if (accountIds.length === 0) {
      // Nothing to check: no account signed in here can have been shared it.
      vnode.state.isSettled = true;
      shell.notificationsUi?.showTransientToast(NOT_SHARED_TITLE, NOT_SHARED_BODY);
      shell.routeToHub("/", { replace: true });
      return;
    }
    void findSharedAccount(accountIds, hostId, (accountId) =>
      sharedWithMeApi.refresh(accountId),
    ).then(({ hit, isAnyRefreshed }) => {
      if (vnode.state.isRemoved) return;
      vnode.state.isSettled = true;
      if (hit === null) {
        if (isAnyRefreshed) {
          shell.notificationsUi?.showTransientToast(NOT_SHARED_TITLE, NOT_SHARED_BODY);
        } else {
          shell.notificationsUi?.showTransientToast(NOT_CHECKED_TITLE, NOT_CHECKED_BODY);
        }
        shell.routeToHub("/", { replace: true });
        return;
      }
      // The share's account's home, with the tile named so the page marks it and scrolls it into view.
      m.route.set(
        accountHubRoute(hit.accountId),
        { shared: hit.shareKey },
        { replace: true },
      );
    });
  },
  onremove(vnode) {
    vnode.state.isRemoved = true;
  },
  view(vnode) {
    return m(PageContainer, [
      m(
        "p",
        { class: "text-tertiary text-center py-12", id: "shared-link-status" },
        vnode.state.isSettled
          ? "Opening..."
          : "Looking for the workspace you were shared...",
      ),
    ]);
  },
};
