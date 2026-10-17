// The signed-in accounts: the bottom-left launcher's identity and the full
// list Manage Accounts renders, both from the channel's accounts frame.

import type { UiAccountEntry, UiAccountsMessage } from "../channel/messages";

export class AccountsStore {
  hasAccounts = false;
  accountEmail = "";
  extraAccountCount = 0;
  accounts: readonly UiAccountEntry[] = [];

  applyAccountsMessage(message: UiAccountsMessage): void {
    this.hasAccounts = message.has_accounts;
    this.accountEmail = message.account_email;
    this.extraAccountCount = message.extra_account_count;
    this.accounts = message.accounts;
  }
}
