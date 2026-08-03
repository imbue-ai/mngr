// Predefined (catalog-backed) permission dialog: account picker + one
// checkbox per permission schema, with wildcard exclusivity and the
// will-open-browser progress notice (port of LatchkeyPredefinedPermission.jinja).

import m from "mithril";
import type { InboxModel, PredefinedPermissionDetail as Detail } from "../../../models/inbox";
import { isPermissionCheckboxDisabled } from "../../../models/inbox";
import { PermissionsShell } from "./PermissionsShell";

export interface PredefinedPermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

function permissionLabel(detail: Detail, permission: string): string {
  return permission === detail.wildcard_permission ? detail.wildcard_label : permission;
}

export function PredefinedPermissionDetailView(): m.Component<PredefinedPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      const checked = model.checkedPermissions;
      return m(PermissionsShell, {
        model,
        headerLabel: `${detail.display_name} permissions`,
        wsName: detail.ws_name,
        rationale: detail.rationale,
        progressLabel: detail.will_open_browser
          ? `Opening a browser window for you to sign in to ${detail.display_name}…`
          : "Granting permission...",
        body: m("div", { class: "flex flex-col gap-4" }, [
          detail.account_choices.length > 0
            ? m("div", { class: "flex flex-col gap-1.5" }, [
                m("p", { class: "type-label text-secondary" }, "Account"),
                ...detail.account_choices.map((choice) =>
                  m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
                    m("input", {
                      type: "radio",
                      name: "account",
                      value: choice.value,
                      checked: model.selectedAccount === choice.value,
                      onchange: () => {
                        model.selectedAccount = choice.value;
                      },
                    }),
                    m("span", { class: "type-body text-primary" }, choice.label),
                    choice.hint ? m("span", { class: "type-helper text-tertiary" }, choice.hint) : null,
                  ]),
                ),
              ])
            : null,
          m("div", { class: "flex flex-col gap-1.5" }, [
            m("p", { class: "type-label text-secondary" }, "Permissions"),
            ...detail.permission_schemas.map((permission) => {
              const isDisabled = isPermissionCheckboxDisabled(permission, detail.wildcard_permission, checked);
              const description = detail.description_by_permission_name[permission] ?? "";
              return m("label", { class: `flex items-start gap-2 ${isDisabled ? "opacity-50" : "cursor-pointer"}` }, [
                m("input", {
                  type: "checkbox",
                  name: "permissions",
                  value: permission,
                  class: "mt-1 shrink-0",
                  checked: checked.has(permission),
                  disabled: isDisabled,
                  onchange: (event: Event) => {
                    const target = event.target as HTMLInputElement;
                    if (target.checked) checked.add(permission);
                    else checked.delete(permission);
                  },
                }),
                m("span", [
                  m("span", { class: "type-body text-primary font-mono" }, permissionLabel(detail, permission)),
                  description ? m("span", { class: "block type-helper text-tertiary" }, description) : null,
                ]),
              ]);
            }),
          ]),
        ]),
      });
    },
  };
}
