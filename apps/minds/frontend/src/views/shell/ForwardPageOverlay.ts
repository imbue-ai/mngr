// The notice drawn over a workspace frame while the frame holds one of the
// workspace connection's own pages (see models/forwardPages.ts).

import m from "mithril";
import type {
  ForwardPageActionKind,
  ForwardPageNotice,
} from "../../models/forwardPages";
import { ErrorNotice } from "../components/ErrorDisplay";
import { Spinner } from "../components/Spinner";

export interface ForwardPageOverlayAttrs {
  notice: ForwardPageNotice;
  /** Still in the first seconds of a wait: only the spinner and heading show. */
  isSpinnerOnly: boolean;
  workspaceAgentId: string | null;
  onAction: (kind: ForwardPageActionKind) => void;
}

export function ForwardPageOverlay(): m.Component<ForwardPageOverlayAttrs> {
  return {
    view(vnode) {
      const { notice, isSpinnerOnly, workspaceAgentId, onAction } = vnode.attrs;
      return m(
        "div#forward-page-overlay",
        {
          class:
            "workspace-surface bg-surface-primary flex items-center justify-center p-6",
        },
        m(
          "div",
          {
            class:
              "w-[560px] max-w-full flex flex-col gap-2 rounded-[12px] border border-subtle " +
              "bg-surface-primary shadow-overlay px-6 py-5",
          },
          [
            notice.heading === null
              ? null
              : m("div", { class: "flex items-center gap-3 type-heading" }, [
                  notice.isWaiting ? m(Spinner, { size: "md" }) : null,
                  m("span", notice.heading),
                ]),
            isSpinnerOnly || notice.sentence === null
              ? null
              : m("p", { class: "m-0 type-body text-secondary" }, notice.sentence),
            isSpinnerOnly || notice.error === null
              ? null
              : m(ErrorNotice, {
                  id: "forward-page-error",
                  error: notice.error,
                  actions: notice.actions.map((action) => ({
                    label: action.label,
                    onclick: () => onAction(action.kind),
                  })),
                  isBugReportOffered: notice.isBugReportOffered,
                  workspaceAgentId,
                }),
          ],
        ),
      );
    },
  };
}
