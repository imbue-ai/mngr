// The accounts: the signed-in ones, the signed-out ones whose workspaces are
// still on this install, and the device entry, as the titlebar's account
// switcher and Manage Accounts render them from the channel's accounts frame.
// Which account THIS window shows is the shell's (ShellState.accountId); the
// frame's own account_id is the server's echo of it.

import type { UiAccountEntry, UiAccountsMessage } from "../channel/messages";
import { LOCAL_PROFILE_USER_ID } from "./providers";

/** What the switcher shows for the device's pseudo-account. */
export const DEVICE_ACCOUNT_LABEL = "This device";

/** How an account is named wherever one is shown: its display name, else its email, else its id. */
export function accountLabelFor(
  entry: Pick<UiAccountEntry, "display_name" | "email" | "user_id">,
): string {
  return entry.display_name || entry.email || entry.user_id;
}

export class AccountsStore {
  hasAccounts = false;
  accounts: readonly UiAccountEntry[] = [];
  signedOutAccounts: readonly UiAccountEntry[] = [];

  applyAccountsMessage(message: UiAccountsMessage): void {
    this.hasAccounts = message.has_accounts;
    this.accounts = message.accounts;
    this.signedOutAccounts = message.signed_out_accounts ?? [];
  }

  /** The signed-in account with this id, or null when none is (the device, or a signed-out account). */
  signedInAccount(accountId: string): UiAccountEntry | null {
    return (
      this.accounts.find((account) => account.user_id === accountId) ?? null
    );
  }

  isSignedIn(accountId: string): boolean {
    return this.signedInAccount(accountId) !== null;
  }

  /** What the switcher shows for `accountId`: the device's label, an account's name, or the id when nothing names it. */
  labelFor(accountId: string): string {
    if (accountId === LOCAL_PROFILE_USER_ID) return DEVICE_ACCOUNT_LABEL;
    const known =
      this.signedInAccount(accountId) ??
      this.signedOutAccounts.find((account) => account.user_id === accountId) ??
      null;
    return known === null ? accountId : accountLabelFor(known);
  }
}
