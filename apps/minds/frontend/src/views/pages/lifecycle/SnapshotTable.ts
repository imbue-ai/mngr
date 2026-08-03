// The backup snapshot table shared shape: relative time (+ "Restored from"
// lineage label), Download, and Restore/Restoring.../Cancel per row.

import m from "mithril";
import {
  type BackupOperationController,
  type BackupSnapshot,
  downloadSnapshotExport,
  formatRelativeAgo,
  restoredFromLabel,
} from "../../../models/backups";

export interface SnapshotTableAttrs {
  agentId: string;
  snapshots: readonly BackupSnapshot[];
  controller: BackupOperationController;
  /** Non-null disables every Restore with this tooltip (e.g. offline gate). */
  restoreDisabledReason: string | null;
  onRestoreRequested: (snapshot: BackupSnapshot) => void;
}

interface SnapshotTableState {
  downloadingSnapshotIds: Set<string>;
  failedSnapshotIds: Set<string>;
}

export function SnapshotTable(): m.Component<SnapshotTableAttrs, SnapshotTableState> {
  return {
    oninit(vnode) {
      vnode.state.downloadingSnapshotIds = new Set();
      vnode.state.failedSnapshotIds = new Set();
    },
    view(vnode) {
      const { agentId, snapshots, controller, restoreDisabledReason, onRestoreRequested } = vnode.attrs;
      const nowMs = Date.now();
      return m(
        "div",
        snapshots.map((snapshot, index) => {
          const lineageLabel = restoredFromLabel(snapshot.tags);
          const isRestoringThis = controller.restoringSnapshotId === snapshot.snapshot_id;
          const isDownloading = vnode.state.downloadingSnapshotIds.has(snapshot.snapshot_id);
          const isDownloadFailed = vnode.state.failedSnapshotIds.has(snapshot.snapshot_id);
          const isRestoreOffered = restoreDisabledReason === null && !controller.isRunning;
          return m(
            "div",
            {
              key: snapshot.snapshot_id,
              class: "flex items-center gap-4 px-4 py-3" + (index === 0 ? "" : " border-t border-default"),
            },
            [
              m("div", { class: "flex-1 flex items-center gap-2 min-w-0" }, [
                m("span", { class: "type-body text-primary" }, formatRelativeAgo(snapshot.time, nowMs)),
                lineageLabel !== null
                  ? m(
                      "span",
                      { class: "inline-flex items-center px-2 py-0.5 rounded-md type-label bg-fill-hover text-secondary" },
                      lineageLabel,
                    )
                  : null,
              ]),
              m("div", { class: "flex items-center gap-4 shrink-0" }, [
                m(
                  "button",
                  {
                    type: "button",
                    class: "bg-transparent border-0 p-0 type-body text-accent hover:underline cursor-pointer",
                    disabled: isDownloading,
                    onclick: () => {
                      vnode.state.downloadingSnapshotIds.add(snapshot.snapshot_id);
                      vnode.state.failedSnapshotIds.delete(snapshot.snapshot_id);
                      void downloadSnapshotExport(agentId, snapshot.snapshot_id).then((isOk) => {
                        vnode.state.downloadingSnapshotIds.delete(snapshot.snapshot_id);
                        if (!isOk) vnode.state.failedSnapshotIds.add(snapshot.snapshot_id);
                        m.redraw();
                      });
                    },
                  },
                  isDownloading ? "Downloading..." : isDownloadFailed ? "Download failed" : "Download",
                ),
                m(
                  "button",
                  {
                    type: "button",
                    class:
                      "bg-transparent border-0 p-0 type-body " +
                      (isRestoreOffered || isRestoringThis
                        ? "text-accent cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                        : "text-tertiary cursor-not-allowed"),
                    disabled: !isRestoreOffered,
                    title: restoreDisabledReason ?? undefined,
                    onclick: () => onRestoreRequested(snapshot),
                  },
                  isRestoringThis ? "Restoring..." : "Restore",
                ),
                isRestoringThis && controller.isCancellable
                  ? m(
                      "button",
                      {
                        type: "button",
                        class: "bg-transparent border-0 p-0 type-body text-accent hover:underline cursor-pointer",
                        onclick: () => void controller.requestCancel(),
                      },
                      "Cancel",
                    )
                  : null,
              ]),
            ],
          );
        }),
      );
    },
  };
}
