// The undismissable card over a machine whose update is landing: the apply
// takes its services down, and a band over a still-usable surface invites
// attempts that look like the machine broke. Dimming starts below the titlebar
// so the switcher stays reachable.

import m from "mithril";
import { Spinner } from "./Spinner";

export interface UpdateApplyModalAttrs {
  workspaceName: string;
}

export function UpdateApplyModal(): m.Component<UpdateApplyModalAttrs> {
  return {
    view(vnode) {
      const { workspaceName } = vnode.attrs;
      return m(
        "div#update-apply-modal-backdrop",
        {
          class:
            "fixed left-0 right-0 top-[38px] bottom-0 z-[120] bg-black/20 " +
            "flex items-start justify-center p-4 pt-10",
        },
        m(
          "div#update-apply-modal-panel",
          {
            class:
              "relative w-[480px] max-w-full flex flex-col gap-3 rounded-[12px] " +
              "border border-subtle bg-surface-primary shadow-overlay px-6 py-5",
          },
          [
            m("div", { class: "flex items-center gap-3" }, [
              m(Spinner, { size: "sm" }),
              m("div", { class: "type-heading" }, `Updating ${workspaceName}`),
            ]),
            m(
              "div",
              { class: "type-body text-secondary" },
              "The update is landing. This machine's services restart while it does, " +
                "so it is unavailable until that finishes -- usually a few minutes.",
            ),
            m(
              "div",
              { class: "type-helper text-secondary" },
              "Your other machines are still reachable from the machine switcher.",
            ),
          ],
        ),
      );
    },
  };
}
