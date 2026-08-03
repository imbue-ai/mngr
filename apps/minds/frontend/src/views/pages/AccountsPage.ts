// Manage Accounts: signed-in account cards with default/log-out controls and
// per-account plan + usage (loaded asynchronously, never blocking first
// paint). Port of templates/pages/Accounts.jinja + accounts.js; the legacy
// accounts modal collapses into this page (modals are plain routes now).

import m from "mithril";
import { AccountsDetailModel } from "../../models/accountsDetail";
import { ButtonLink } from "../components/Button";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { AccountCard } from "./settings/AccountCard";

export function AccountsPage(): m.Component {
  const model = new AccountsDetailModel();
  return {
    oninit(): void {
      void model.load();
    },
    onremove(): void {
      model.dispose();
    },
    view(): m.Children {
      return m(PageContainer, [
        m("h1", { class: "type-heading text-primary mb-4" }, "Manage Accounts"),
        model.actionError !== ""
          ? m(
              "div",
              { class: "mb-4" },
              m(Notice, { variant: "error" }, model.actionError),
            )
          : null,
        model.isLoadFailed
          ? m(
              Notice,
              { variant: "error" },
              "Accounts could not be loaded. Refresh to try again.",
            )
          : !model.isListLoaded
            ? m(
                "div",
                { class: "flex items-center gap-2 type-helper text-tertiary" },
                [m(Spinner, { size: "sm" }), "Loading accounts…"],
              )
            : model.accounts.length > 0
              ? m(
                  "div",
                  { class: "flex flex-col gap-2" },
                  model.accounts.map((account) =>
                    m(AccountCard, { key: account.user_id, model, account }),
                  ),
                )
              : m("p", { class: "text-secondary" }, "No accounts logged in."),
        m(
          "div",
          { class: "mt-4" },
          m(
            ButtonLink,
            { href: "/auth/login", variant: "primary" },
            "Add account",
          ),
        ),
      ]);
    },
  };
}
