// Cross-workspace (minds-workspaces) permission dialog: verb checkboxes in
// general vs machine-specific groups plus the all-vs-selected target radio
// (port of LatchkeyWorkspacePermission.jinja).

import m from "mithril";
import type { InboxModel, WorkspacePermissionDetail as Detail, WorkspaceVerbChoice } from "../../../models/inbox";
import { Icon16 } from "../../components/Icon";
import { Notice } from "../../components/Notice";
import { PermissionsShell } from "./PermissionsShell";

export interface WorkspacePermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

function verbCheckbox(model: InboxModel, verb: WorkspaceVerbChoice): m.Children {
  const checked = model.checkedPermissions;
  return m("label", { class: "flex items-start gap-2 cursor-pointer" }, [
    m("input", {
      type: "checkbox",
      name: "permissions",
      value: verb.permission,
      class: "mt-1 shrink-0",
      checked: checked.has(verb.permission),
      onchange: (event: Event) => {
        const target = event.target as HTMLInputElement;
        if (target.checked) checked.add(verb.permission);
        else checked.delete(verb.permission);
      },
    }),
    m("span", [
      m("span", { class: "type-body text-primary" }, verb.display_name),
      m("span", { class: "block type-helper text-tertiary" }, verb.description),
    ]),
  ]);
}

export function WorkspacePermissionDetailView(): m.Component<WorkspacePermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      const generalVerbs = detail.verbs.filter((verb) => !verb.is_targeted);
      const targetedVerbs = detail.verbs.filter((verb) => verb.is_targeted);
      return m(PermissionsShell, {
        model,
        headerLabel: "Other machines",
        mark: m(Icon16, { name: "key", extra: "text-primary" }),
        rationale: detail.rationale,
        progressLabel: "Granting permission...",
        body: m("div", { class: "flex flex-col gap-4" }, [
          m("div", { class: "flex flex-col gap-1.5" }, [
            m("p", { class: "type-label text-secondary" }, "General permissions"),
            ...generalVerbs.map((verb) => verbCheckbox(model, verb)),
          ]),
          m("div", { class: "flex flex-col gap-1.5" }, [
            m("p", { class: "type-label text-secondary" }, "Machine-specific permissions"),
            detail.show_target_choice
              ? m("p", { class: "type-helper text-tertiary" }, "These act on individual machines.")
              : m(
                  Notice,
                  { variant: "warn" },
                  "No specific machine was named, so these permissions apply to all machines.",
                ),
            ...targetedVerbs.map((verb) => verbCheckbox(model, verb)),
            detail.show_target_choice
              ? m("div", { class: "flex flex-col gap-1.5 mt-1" }, [
                  m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
                    m("input", {
                      type: "radio",
                      name: "target_scope",
                      value: "selected",
                      checked: model.targetScope === "selected",
                      onchange: () => {
                        model.targetScope = "selected";
                      },
                    }),
                    m("span", { class: "type-body text-primary" }, [
                      "Only ",
                      m("b", detail.target_workspace_name ?? "the selected machine"),
                    ]),
                  ]),
                  m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
                    m("input", {
                      type: "radio",
                      name: "target_scope",
                      value: "all",
                      checked: model.targetScope === "all",
                      onchange: () => {
                        model.targetScope = "all";
                      },
                    }),
                    m("span", { class: "type-body text-primary" }, "All machines"),
                  ]),
                ])
              : null,
          ]),
        ]),
      });
    },
  };
}
