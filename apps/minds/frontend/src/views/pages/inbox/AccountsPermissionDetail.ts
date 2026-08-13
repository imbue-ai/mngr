// Accounts permission dialog: all-or-nothing approve for reading the
// device's signed-in account list (port of LatchkeyAccountsPermission.jinja).

import m from "mithril";
import type { AccountsPermissionDetail as Detail, InboxModel } from "../../../models/inbox";
import { PermissionsShell } from "./PermissionsShell";

export interface AccountsPermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

export function AccountsPermissionDetailView(): m.Component<AccountsPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      return m(PermissionsShell, {
        model,
        headerLabel: "Account access",
        wsName: detail.ws_name,
        rationale: detail.rationale,
        progressLabel: "Granting permission...",
        body: m(
          "p",
          { class: "type-body text-primary" },
          "The agent will be able to see which accounts are signed in on this device (names and emails only, no credentials).",
        ),
      });
    },
  };
}
