// The restore confirmation dialog: names the snapshot's time, carries the
// "update backup software afterwards" checkbox (checked on every open), and
// hands the choice back to the page.

import m from "mithril";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import type { BackupSnapshot } from "../../../models/backups";

export interface RestoreDialogAttrs {
  snapshot: BackupSnapshot | null;
  onCancel: () => void;
  onConfirm: (snapshot: BackupSnapshot, choices: { updateAfter: boolean }) => void;
}

interface RestoreDialogState {
  isUpdateAfterChecked: boolean;
  openSnapshotId: string | null;
}

export function RestoreDialog(): m.Component<RestoreDialogAttrs, RestoreDialogState> {
  return {
    oninit(vnode) {
      vnode.state.isUpdateAfterChecked = true;
      vnode.state.openSnapshotId = null;
    },
    view(vnode) {
      const { snapshot, onCancel, onConfirm } = vnode.attrs;
      // Recommended default: every open starts checked, regardless of what a
      // previous restore chose.
      if (snapshot !== null && vnode.state.openSnapshotId !== snapshot.snapshot_id) {
        vnode.state.openSnapshotId = snapshot.snapshot_id;
        vnode.state.isUpdateAfterChecked = true;
      }
      if (snapshot === null && vnode.state.openSnapshotId !== null) {
        vnode.state.openSnapshotId = null;
      }
      return m(Modal, { isOpen: snapshot !== null, onClose: onCancel }, [
        snapshot === null
          ? null
          : m("div", { class: "flex flex-col gap-4" }, [
              m("h2", { class: "type-heading" }, "Restore this backup?"),
              m(
                "p",
                { class: "type-body text-secondary" },
                `Your machine's files will be replaced with the backup from ${new Date(snapshot.time).toLocaleString()}. ` +
                  "A safety backup of the current state is saved first.",
              ),
              m("label", { class: "flex items-center gap-2 type-body text-primary cursor-pointer" }, [
                m("input", {
                  type: "checkbox",
                  checked: vnode.state.isUpdateAfterChecked,
                  onchange: (event: Event) => {
                    vnode.state.isUpdateAfterChecked = (event.target as HTMLInputElement).checked;
                  },
                }),
                "Update the backup software afterwards (recommended)",
              ]),
              m("div", { class: "flex items-center justify-end gap-2" }, [
                m(Button, { variant: "secondary", onclick: onCancel }, "Cancel"),
                m(
                  Button,
                  {
                    variant: "primary",
                    onclick: () => onConfirm(snapshot, { updateAfter: vnode.state.isUpdateAfterChecked }),
                  },
                  "Restore",
                ),
              ]),
            ]),
      ]);
    },
  };
}
