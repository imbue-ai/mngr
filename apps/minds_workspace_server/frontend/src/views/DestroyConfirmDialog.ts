/**
 * Confirmation dialog for destroying an agent.
 * Lists child chat agents when destroying a worktree/sidebar agent.
 */

import m from "mithril";
import type { AgentState } from "../models/AgentManager";

interface DestroyConfirmDialogAttrs {
  agentName: string;
  chatChildren: AgentState[];
  onConfirm: () => void;
  onCancel: () => void;
}

export const DestroyConfirmDialog: m.Component<DestroyConfirmDialogAttrs> = {
  view(vnode) {
    const { agentName, chatChildren, onConfirm, onCancel } = vnode.attrs;

    const hasChildren = chatChildren.length > 0;

    return m("div.destroy-dialog-overlay", { onclick: (e: Event) => { if (e.target === e.currentTarget) onCancel(); } }, [
      m("div.destroy-dialog", [
        m("h3.destroy-dialog-title", "Destroy Agent"),
        m("p.destroy-dialog-message", [
          `Are you sure you want to destroy `,
          m("strong", agentName),
          `? This cannot be undone.`,
        ]),

        hasChildren
          ? m("div.destroy-dialog-children", [
              m("p.destroy-dialog-children-warning",
                "The following chat agents will also be destroyed:"
              ),
              m("ul.destroy-dialog-children-list",
                chatChildren.map((child) =>
                  m("li", { key: child.id }, child.name)
                )
              ),
            ])
          : null,

        m("div.destroy-dialog-actions", [
          m("button.destroy-dialog-btn.destroy-dialog-btn-cancel", { onclick: onCancel }, "Cancel"),
          m("button.destroy-dialog-btn.destroy-dialog-btn-destroy", { onclick: onConfirm }, "Destroy"),
        ]),
      ]),
    ]);
  },
};
