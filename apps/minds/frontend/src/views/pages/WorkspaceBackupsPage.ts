// Backup history for one workspace (/workspace/<id>/backups): the paginated
// snapshot table with Download / Restore, plus the tracked-operation strip.
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
  browserLifecycleDeps,
  formatRelativeAgo,
} from "../../models/backups";
import { OperationStrip } from "./lifecycle/OperationStrip";
import { RestoreDialog } from "./lifecycle/RestoreDialog";
import { SnapshotTable } from "./lifecycle/SnapshotTable";

interface WorkspaceBackupsState {
  history: BackupHistoryModel;
  controller: BackupOperationController;
  pendingRestoreSnapshot: BackupSnapshot | null;
  onBecameObservable: () => void;
}

export const WorkspaceBackupsPage: m.Component<Record<string, never>, WorkspaceBackupsState> = {
  oninit(vnode) {
    const agentId = m.route.param("agentId");
    const deps = browserLifecycleDeps(() => m.redraw());
    vnode.state.history = new BackupHistoryModel(agentId, deps);
    vnode.state.controller = new BackupOperationController(agentId, deps);
    vnode.state.controller.onSuccess = () => void vnode.state.history.loadPage();
    vnode.state.pendingRestoreSnapshot = null;
    // The server is the single source of truth; re-validate exactly when the
    // page becomes observable again (an operation may have been started from
    // another window while this one sat in the background).
    vnode.state.onBecameObservable = () => {
      if (!document.hidden) void vnode.state.controller.reattach();
    };
    document.addEventListener("visibilitychange", vnode.state.onBecameObservable);
    window.addEventListener("focus", vnode.state.onBecameObservable);
    void vnode.state.controller.reattach();
    void vnode.state.history.loadPage();
    void vnode.state.history.fetchCheckState();
  },
  onremove(vnode) {
    document.removeEventListener("visibilitychange", vnode.state.onBecameObservable);
    window.removeEventListener("focus", vnode.state.onBecameObservable);
    vnode.state.controller.stop();
  },
  view(vnode) {
    const { history, controller } = vnode.state;
    const restoreDisabledReason = history.isRestoreDisabledByCheck()
      ? "This machine is offline; start it to restore a backup."
      : null;
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, "Backups"),
        m(OperationStrip, { controller }),
        history.statusMessage !== null
          ? m("div", { class: "type-body text-secondary" }, history.statusMessage)
          : m(Card, { padding: "tight" }, [
              m(SnapshotTable, {
                agentId: history.agentId,
                snapshots: history.snapshots,
                controller,
                restoreDisabledReason,
                onRestoreRequested: (snapshot) => {
                  vnode.state.pendingRestoreSnapshot = snapshot;
                },
              }),
            ]),
        history.statusMessage === null && history.isPaginationShown
          ? m("div", { class: "flex items-center justify-between" }, [
              m("span", { class: "type-helper text-tertiary" }, history.rangeText),
              m("div", { class: "flex items-center gap-2" }, [
                m(
                  Button,
                  { variant: "secondary", disabled: !history.canGoNewer, onclick: () => history.goNewer() },
                  "Newer",
                ),
                m(
                  Button,
                  { variant: "secondary", disabled: !history.canGoOlder, onclick: () => history.goOlder() },
                  "Older",
                ),
              ]),
            ])
          : null,
        m(RestoreDialog, {
          snapshot: vnode.state.pendingRestoreSnapshot,
          onCancel: () => {
            vnode.state.pendingRestoreSnapshot = null;
          },
          onConfirm: (snapshot, choices) => {
            vnode.state.pendingRestoreSnapshot = null;
            controller.startRestore(snapshot, formatRelativeAgo(snapshot.time, Date.now()), {
              updateAfter: choices.updateAfter,
            });
          },
        }),
      ]),
    );
  },
};
