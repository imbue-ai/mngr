// Placeholder for the Backup group inside Machine settings. The backup
// surfaces (history, operation strip, storage config, verification) are owned
// by the backups tranche, which replaces this file's contents wholesale; the
// full history also lives at /workspace/<id>/backups.

import m from "mithril";
import { Notice } from "../../components/Notice";
import { SectionHeader } from "../../components/Layout";
import { routeLinkAttrs } from "../../components/route-link";

export interface BackupGroupSlotAttrs {
  agentId: string;
}

export function BackupGroupSlot(): m.Component<BackupGroupSlotAttrs> {
  return {
    view(vnode) {
      return m("div", [
        m(SectionHeader, "Backups"),
        m(Notice, { variant: "info" }, "The backups section is being rebuilt and is not available here yet."),
        m(
          "p",
          { class: "type-body text-secondary mt-3" },
          m(
            "a",
            { class: "text-primary underline", ...routeLinkAttrs(`/workspace/${vnode.attrs.agentId}/backups`) },
            "View backup history",
          ),
        ),
      ]);
    },
  };
}
