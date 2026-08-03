// Shared chrome for every request-detail dialog: header, rationale card,
// Approve/Deny row, and the progress / error / manual-credentials notices
// (the SPA twin of the Permissions* JinjaX components + the inbox shell's
// submission UI states).

import m from "mithril";
import type { InboxModel } from "../../../models/inbox";
import { Button } from "../../components/Button";
import { Spinner } from "../../components/Spinner";

export interface PermissionsShellAttrs {
  model: InboxModel;
  headerLabel: string;
  wsName: string;
  rationale: string;
  /** Notice shown while an approval is running (kind-specific copy). */
  progressLabel: string;
  body: m.Children;
}

export function PermissionsShell(): m.Component<PermissionsShellAttrs> {
  return {
    view(vnode) {
      const { model, headerLabel, wsName, rationale, progressLabel, body } = vnode.attrs;
      return m("div", { class: "flex flex-col gap-4" }, [
        m("div", [
          m("h2", { class: "type-heading text-primary" }, headerLabel),
          m("p", { class: "type-helper text-tertiary mt-0.5" }, ["Requested by an agent in ", m("b", wsName)]),
        ]),
        rationale
          ? m("div", { class: "rounded-md border border-default bg-fill-subtle p-3 type-body text-secondary" }, rationale)
          : null,
        body,
        m("div", { class: "flex items-center gap-2" }, [
          m(
            Button,
            {
              variant: "primary",
              id: "permissions-approve-btn",
              disabled: !model.isApproveAllowed(),
              onclick: () => void model.approve(),
            },
            model.isApproveBusy
              ? [m(Spinner, { size: "sm", extra: "mr-1.5" }), "Approving…"]
              : "Approve",
          ),
          m(
            Button,
            {
              variant: "secondary",
              id: "permissions-deny-btn",
              disabled: model.isApproveBusy,
              onclick: () => model.deny(),
            },
            "Deny",
          ),
        ]),
        model.isProgressShown
          ? m(
              "div",
              { id: "permissions-progress", class: "rounded-md border border-default bg-fill-subtle p-3 type-body text-secondary" },
              progressLabel,
            )
          : null,
        model.errorMessage !== null
          ? m("div", { id: "permissions-error", class: "rounded-md border border-important bg-important/10 p-3" }, [
              m("p", { id: "permissions-error-message", class: "type-body text-important" }, model.errorMessage),
            ])
          : null,
        model.manualCredentials !== null
          ? m("div", { id: "permissions-manual-credentials", class: "rounded-md border border-default bg-fill-subtle p-3 flex flex-col gap-2" }, [
              m("p", { class: "type-body text-secondary" }, model.manualCredentials.message),
              model.manualCredentials.command
                ? m(
                    "code",
                    { class: "block rounded-md border border-default bg-surface-primary px-2 py-1 type-label font-mono text-primary overflow-x-auto" },
                    model.manualCredentials.command,
                  )
                : null,
            ])
          : null,
      ]);
    },
  };
}
