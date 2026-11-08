// Backup history for one workspace (/workspace/<id>/backups): the paginated
// snapshot table with Download / Restore, plus the tracked-operation strip.
// Below the current backups, one section per previous backup destination
// (an archived restic.env) lists the snapshots still readable there, with
// the same Download / Restore controls, until that storage is reaped.
// An operation started on any surface is reattached to on load and on
// window focus/visibility, so this page never shows idle controls over a
// busy workspace.

import m from "mithril";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PageContainer } from "../components/Layout";
import {
  BackupHistoryModel,
  BackupOperationController,
  type BackupSnapshot,
  type PreviousBackupRepository,
  browserLifecycleDeps,
  formatRelativeAgo,
} from "../../models/backups";
import { OperationStrip } from "./lifecycle/OperationStrip";
import { RestoreDialog } from "./lifecycle/RestoreDialog";
import { SnapshotTable } from "./lifecycle/SnapshotTable";

interface PendingRestore {
  snapshot: BackupSnapshot;
  /** The archive id when the snapshot lives at a previous destination; null for current backups. */
  repositoryId: string | null;
}

interface WorkspaceBackupsState {
  history: BackupHistoryModel;
  controller: BackupOperationController;
  pendingRestore: PendingRestore | null;
  onBecameObservable: () => void;
}

/** The heading of one previous-destination section, e.g. "Previous storage (retired 3 days ago)". */
export function previousStorageLabel(
  repository: PreviousBackupRepository,
  nowMs: number,
): string {
  const ago = formatRelativeAgo(repository.archived_at, nowMs);
  return ago ? `Previous storage (retired ${ago})` : "Previous storage";
}

export const WorkspaceBackupsPage: m.Component<
  Record<string, never>,
  WorkspaceBackupsState
> = {
  oninit(vnode) {
    const agentId = m.route.param("agentId");
    const deps = browserLifecycleDeps(() => m.redraw());
    vnode.state.history = new BackupHistoryModel(agentId, deps);
    vnode.state.controller = new BackupOperationController(agentId, deps);
    vnode.state.controller.onSuccess = () =>
      void vnode.state.history.loadPage();
    vnode.state.pendingRestore = null;
    // The server is the single source of truth; re-validate exactly when the
    // page becomes observable again (an operation may have been started from
    // another window while this one sat in the background).
    vnode.state.onBecameObservable = () => {
      if (!document.hidden) void vnode.state.controller.reattach();
    };
    document.addEventListener(
      "visibilitychange",
      vnode.state.onBecameObservable,
    );
    window.addEventListener("focus", vnode.state.onBecameObservable);
    void vnode.state.controller.reattach();
    void vnode.state.history.loadPage();
    void vnode.state.history.fetchCheckState();
  },
  onremove(vnode) {
    document.removeEventListener(
      "visibilitychange",
      vnode.state.onBecameObservable,
    );
    window.removeEventListener("focus", vnode.state.onBecameObservable);
    vnode.state.controller.stop();
  },
  view(vnode) {
    const { history, controller } = vnode.state;
    const restoreDisabledReason = history.isRestoreDisabledByCheck()
      ? "This workspace is offline; start it to restore a backup."
      : null;
    // A restore re-injects the workspace's current backup credentials when it
    // ends, so previous storage is only restorable while backups are on.
    const previousStorageRestoreDisabledReason =
      restoreDisabledReason ??
      (history.isBackupsConfigured
        ? null
        : "Turn backups on for this workspace to restore from its previous storage.");
    const nowMs = Date.now();
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, "Backups"),
        m(OperationStrip, { controller }),
        history.statusMessage !== null
          ? m(
              "div",
              { class: "type-body text-secondary" },
              history.statusMessage,
            )
          : m(Card, { padding: "tight" }, [
              m(SnapshotTable, {
                agentId: history.agentId,
                snapshots: history.snapshots,
                controller,
                restoreDisabledReason,
                repositoryId: null,
                onRestoreRequested: (snapshot) => {
                  vnode.state.pendingRestore = { snapshot, repositoryId: null };
                },
              }),
            ]),
        history.statusMessage === null && history.isPaginationShown
          ? m("div", { class: "flex items-center justify-between" }, [
              m(
                "span",
                { class: "type-helper text-tertiary" },
                history.rangeText,
              ),
              m("div", { class: "flex items-center gap-2" }, [
                m(
                  Button,
                  {
                    variant: "secondary",
                    disabled: !history.canGoNewer,
                    onclick: () => history.goNewer(),
                  },
                  "Newer",
                ),
                m(
                  Button,
                  {
                    variant: "secondary",
                    disabled: !history.canGoOlder,
                    onclick: () => history.goOlder(),
                  },
                  "Older",
                ),
              ]),
            ])
          : null,
        // Keyed vnodes may only have keyed siblings, so the sections get a
        // fragment of their own among the page's unkeyed children.
        m.fragment(
          {},
          history.previousRepositories.map((repository) =>
            m(
              "div",
              { key: repository.archive_id, class: "flex flex-col gap-2 pt-4" },
              [
                m(
                  "h2",
                  { class: "type-heading-sm" },
                  previousStorageLabel(repository, nowMs),
                ),
                m(
                  "div",
                  { class: "type-helper text-tertiary" },
                  "Backups made before this workspace's storage changed. They stay readable until the previous storage is cleaned up.",
                ),
                repository.snapshots_error
                  ? m(
                      "div",
                      { class: "type-body text-secondary" },
                      "Couldn't load these backups right now.",
                    )
                  : (repository.snapshots ?? []).length === 0
                    ? m(
                        "div",
                        { class: "type-body text-secondary" },
                        "No backups at this storage.",
                      )
                    : m(Card, { padding: "tight" }, [
                        m(SnapshotTable, {
                          agentId: history.agentId,
                          snapshots: repository.snapshots ?? [],
                          controller,
                          restoreDisabledReason:
                            previousStorageRestoreDisabledReason,
                          repositoryId: repository.archive_id,
                          onRestoreRequested: (snapshot) => {
                            vnode.state.pendingRestore = {
                              snapshot,
                              repositoryId: repository.archive_id,
                            };
                          },
                        }),
                      ]),
              ],
            ),
          ),
        ),
        m(RestoreDialog, {
          snapshot: vnode.state.pendingRestore?.snapshot ?? null,
          onCancel: () => {
            vnode.state.pendingRestore = null;
          },
          onConfirm: (snapshot, choices) => {
            const repositoryId =
              vnode.state.pendingRestore?.repositoryId ?? null;
            vnode.state.pendingRestore = null;
            controller.startRestore(
              snapshot,
              formatRelativeAgo(snapshot.time, Date.now()),
              {
                updateAfter: choices.updateAfter,
                repositoryId,
              },
            );
          },
        }),
      ]),
    );
  },
};
