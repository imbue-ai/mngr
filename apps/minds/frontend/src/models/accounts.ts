// Account-launcher identity (bottom-left launcher label + signed-in flag).

import type { UiAccountsMessage } from "../channel/messages";

export class AccountsStore {
  hasAccounts = false;
  accountEmail = "";
  extraAccountCount = 0;

  applyAccountsMessage(message: UiAccountsMessage): void {
    this.hasAccounts = message.has_accounts;
    this.accountEmail = message.account_email;
    this.extraAccountCount = message.extra_account_count;
  }
}
