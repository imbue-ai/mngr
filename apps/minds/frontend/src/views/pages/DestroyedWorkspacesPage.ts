// Recently destroyed workspaces (/workspaces/destroyed): rows still inside
// the backup retention window (plus orphan backups on this device), each
// offering a latest-snapshot Download and an armed-confirm Remove that
// frees the backup quota now. Restoring into a new workspace is not offered
// -- these machines no longer exist; the download is the escape hatch.

import m from "mithril";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import {
  browserLifecycleDeps,
  DestroyedWorkspacesModel,
  type DestroyedWorkspaceRow,
  downloadSnapshotExport,
} from "../../models/backups";

interface DestroyedWorkspacesState {
  model: DestroyedWorkspacesModel;
  failedDownloadAgentIds: Set<string>;
}

function _rowMetaLine(row: DestroyedWorkspaceRow): string {
  const parts = [row.account_label];
  if (row.destroyed_at_display) parts.push(`destroyed ${row.destroyed_at_display}`);
  if (row.days_left_display) parts.push(row.days_left_display);
  return parts.join(" · ");
}

function DestroyedRow(): m.Component<{
  row: DestroyedWorkspaceRow;
  model: DestroyedWorkspacesModel;
  failedDownloadAgentIds: Set<string>;
}> {
  return {
    view(vnode) {
      const { row, model, failedDownloadAgentIds } = vnode.attrs;
      const isArmed = model.armedDeleteAgentId === row.agent_id;
      const isDeleting = model.deletingAgentIds.has(row.agent_id);
      const isDownloading = model.downloadingAgentIds.has(row.agent_id);
      const isDownloadFailed = failedDownloadAgentIds.has(row.agent_id);
      return m(Card, { layout: "row-spread" }, [
        m("div", { class: "min-w-0" }, [
          m("div", { class: "font-semibold break-words" }, row.display_name),
          m("div", { class: "type-helper text-tertiary" }, _rowMetaLine(row)),
          row.is_locked
            ? m("div", { class: "type-helper text-tertiary mt-1" }, "Unlock this account's sync password to download.")
            : null,
          !row.is_locked && !row.has_backup
            ? m("div", { class: "type-helper text-tertiary mt-1" }, "No backup was configured for this machine.")
            : null,
          row.delete_hint ? m("div", { class: "type-helper text-tertiary mt-1" }, row.delete_hint) : null,
        ]),
        m("div", { class: "flex items-center justify-end gap-2 shrink-0" }, [
          row.can_download
            ? m(
                Button,
                {
                  variant: "secondary",
                  disabled: isDownloading,
                  onclick: () => {
                    model.downloadingAgentIds.add(row.agent_id);
                    failedDownloadAgentIds.delete(row.agent_id);
                    m.redraw();
                    // The export route restores the latest snapshot server-side
                    // and streams a zip; "latest" is the shared alias.
                    void downloadSnapshotExport(row.agent_id, "latest").then((isOk) => {
                      model.downloadingAgentIds.delete(row.agent_id);
                      if (!isOk) failedDownloadAgentIds.add(row.agent_id);
                      m.redraw();
                    });
                  },
                },
                isDownloading ? "Downloading..." : isDownloadFailed ? "Download failed" : "Download",
              )
            : null,
          row.can_delete && !isArmed
            ? m(
                Button,
                {
                  variant: "danger",
                  disabled: isDeleting,
                  onclick: () => {
                    model.armedDeleteAgentId = row.agent_id;
                  },
                },
                isDeleting ? "Removing..." : "Remove",
              )
            : null,
          row.can_delete && isArmed
            ? m("span", { class: "flex flex-col items-end gap-1" }, [
                m(
                  "span",
                  { class: "type-helper text-secondary text-right max-w-xs" },
                  "Remove this machine? This can't be undone.",
                ),
                m("span", { class: "flex items-center gap-2" }, [
                  m(Button, { variant: "danger", onclick: () => void model.deleteBackup(row.agent_id) }, "Delete forever"),
                  m(
                    Button,
                    {
                      variant: "secondary",
                      onclick: () => {
                        model.armedDeleteAgentId = null;
                      },
                    },
                    "Cancel",
                  ),
                ]),
              ])
            : null,
        ]),
      ]);
    },
  };
}

export const DestroyedWorkspacesPage: m.Component<Record<string, never>, DestroyedWorkspacesState> = {
  oninit(vnode) {
    vnode.state.model = new DestroyedWorkspacesModel(browserLifecycleDeps(() => m.redraw()));
    vnode.state.failedDownloadAgentIds = new Set();
    void vnode.state.model.load();
  },
  view(vnode) {
    const { model } = vnode.state;
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, "Recently destroyed"),
        model.retentionDays > 0
          ? m(
              "p",
              { class: "type-body text-secondary" },
              `Backups of destroyed machines are kept for ${model.retentionDays} days, then deleted automatically.`,
            )
          : null,
        model.errorMessage !== null ? m(Notice, { variant: "error" }, model.errorMessage) : null,
        !model.isLoaded
          ? m("div", { class: "type-body text-secondary" }, "Loading...")
          : model.rows.length === 0
            ? m("div", { class: "type-body text-secondary" }, "No recently destroyed machines.")
            : m(
                "div",
                { class: "flex flex-col gap-2" },
                model.rows.map((row) =>
                  m(DestroyedRow, {
                    key: row.agent_id,
                    row,
                    model,
                    failedDownloadAgentIds: vnode.state.failedDownloadAgentIds,
                  }),
                ),
              ),
      ]),
    );
  },
};
