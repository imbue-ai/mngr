// Manage Accounts: signed-in account cards with default/log-out controls and
// per-account plan + usage (loaded asynchronously, never blocking first
// paint). Port of templates/pages/Accounts.jinja + accounts.js; the legacy
// accounts modal collapses into this page (modals are plain routes now).
//
// The list is the accounts store's, the same channel frame the bottom-left
// launcher renders from, so an account signed in or out while the page is
// open shows up here as it does there.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { AccountsDetailModel } from "../../models/accountsDetail";
import { webLogin } from "../../models/webLogin";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { AccountCard } from "./settings/AccountCard";

export function AccountsPage(): m.Component {
  const model = new AccountsDetailModel();
  return {
    oninit(): void {
      model.syncPlans(getAppContext().stores.accounts.accounts);
    },
    onupdate(): void {
      model.syncPlans(getAppContext().stores.accounts.accounts);
    },
    onremove(): void {
      model.dispose();
    },
    view(): m.Children {
      const { accounts } = getAppContext().stores.accounts;
      // Rendered inside the AppOverlay card (Shell), which supplies the width,
      // padding, scroll, and close X -- so no PageContainer wrapper.
      return [
        m("h1", { class: "type-heading text-primary mb-4" }, "Manage Accounts"),
        model.actionError !== ""
          ? m(
              "div",
              { class: "mb-4" },
              m(Notice, { variant: "error" }, model.actionError),
            )
          : null,
        accounts.length > 0
          ? m(
              "div",
              { class: "flex flex-col gap-2" },
              accounts.map((account) =>
                m(AccountCard, { key: account.user_id, model, account }),
              ),
            )
          : m("p", { class: "text-secondary" }, "No accounts logged in."),
        m(
          "div",
          { class: "mt-4" },
          m(
            Button,
            {
              variant: "primary",
              disabled: webLogin.isOpen,
              onclick: () =>
                void webLogin.start("", { isClosedOnSignIn: true }),
            },
            webLogin.isOpen
              ? [m(Spinner, { size: "sm" }), "Opening browser…"]
              : "Add account",
          ),
        ),
      ];
    },
  };
}
