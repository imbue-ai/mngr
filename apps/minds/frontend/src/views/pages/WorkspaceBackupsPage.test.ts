import m from "mithril";
import { describe, expect, it } from "vitest";
import {
  BackupHistoryModel,
  BackupOperationController,
  backupSetupFailureNotice,
  type LifecycleDeps,
  type PreviousBackupRepository,
} from "../../models/backups";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes, settle } from "../../testing";
import { SnapshotTable } from "./lifecycle/SnapshotTable";
import {
  WorkspaceBackupsPage,
  previousStorageLabel,
} from "./WorkspaceBackupsPage";

const DAY_MS = 24 * 60 * 60 * 1000;

const UNUSED_DEPS: LifecycleDeps = {
  getJson: () => Promise.resolve(null),
  postJson: () => Promise.resolve({ status: 500, body: null }),
  deleteResource: () => Promise.resolve(500),
  openEventSource: () => {
    throw new Error("the page's view opens no stream");
  },
  schedule: () => undefined,
  redraw: () => undefined,
};

function previousRepository(
  archiveId: string,
  daysAgo: number,
): PreviousBackupRepository {
  return {
    archive_id: archiveId,
    archived_at: new Date(Date.now() - daysAgo * DAY_MS).toISOString(),
    snapshots: [
      {
        snapshot_id: `snap-${archiveId}`,
        time: new Date(Date.now() - (daysAgo + 1) * DAY_MS).toISOString(),
        tags: [],
      },
    ],
    snapshots_error: null,
  };
}

function renderPage(
  previousRepositories: PreviousBackupRepository[],
): AnyVnode {
  const history = new BackupHistoryModel("agent-11", UNUSED_DEPS);
  history.statusMessage = null;
  history.isBackupsConfigured = true;
  history.previousRepositories = previousRepositories;
  return renderWith(
    history,
    new BackupOperationController("agent-11", UNUSED_DEPS),
  );
}

function renderWith(
  history: BackupHistoryModel,
  controller: BackupOperationController,
): AnyVnode {
  const vnode = m(WorkspaceBackupsPage) as unknown as {
    state: Record<string, unknown>;
  };
  vnode.state = {
    history,
    controller,
    pendingRestore: null,
    onBecameObservable: () => undefined,
  };
  return (WorkspaceBackupsPage.view as (v: unknown) => AnyVnode).call(
    WorkspaceBackupsPage,
    vnode,
  );
}

/** The setup-failure notice's button labels and whether it offers a report,
 * on a page whose workspace's backups were never set up. */
function setupFailureNoticeButtons(controller: BackupOperationController): {
  actionLabels: string[];
  isBugReportOffered: unknown;
} {
  const history = new BackupHistoryModel("agent-11", UNUSED_DEPS);
  history.statusMessage = null;
  history.setupFailure = backupSetupFailureNotice({
    kind: "OTHER",
    detail: "workspace never became reachable",
  });
  const notice = collectVnodes(renderWith(history, controller)).find(
    (node) => attrsOf(node).id === "backup-setup-failure",
  );
  const attrs = attrsOf(notice!);
  const actions = attrs.actions as Array<{ label: string }>;
  return {
    actionLabels: actions.map((action) => action.label),
    isBugReportOffered: attrs.isBugReportOffered,
  };
}

function snapshotTableRepositoryIds(root: AnyVnode): unknown[] {
  return collectVnodes(root)
    .filter((node) => node.tag === SnapshotTable)
    .map((node) => attrsOf(node).repositoryId);
}

describe("WorkspaceBackupsPage", () => {
  it("renders one section per previous backup destination below the current table", () => {
    const older = previousRepository("20260101T000000Z", 30);
    const newer = previousRepository("20260201T000000Z", 3);

    const root = renderPage([newer, older]);

    expect(snapshotTableRepositoryIds(root)).toEqual([
      null,
      newer.archive_id,
      older.archive_id,
    ]);
    const text = allText(root);
    expect(text).toContain(previousStorageLabel(newer, Date.now()));
    expect(text).toContain(previousStorageLabel(older, Date.now()));
  });

  it("renders only the current table when there is no previous destination", () => {
    const root = renderPage([]);

    expect(snapshotTableRepositoryIds(root)).toEqual([null]);
    expect(allText(root)).not.toContain("Previous storage");
  });

  it("leaves running the setup again, and reporting it, to the failed retry's own notice", async () => {
    const posts: string[] = [];
    const controller = new BackupOperationController("agent-11", {
      ...UNUSED_DEPS,
      postJson: (url: string) => {
        posts.push(url);
        return Promise.resolve({ status: 202, body: null });
      },
      getJson: () =>
        Promise.resolve({
          status: "FAILED",
          kind: "backup_configure",
          error: "restic init failed (exit 1): permission denied",
        }),
      openEventSource: () => ({
        close: () => undefined,
        onmessage: null,
        onerror: null,
      }),
    });
    expect(setupFailureNoticeButtons(controller)).toEqual({
      actionLabels: ["Set up backups"],
      isBugReportOffered: true,
    });

    controller.startSetupRetry();
    await settle();
    await controller.pollOnce();

    expect(controller.isRetryOffered).toBe(true);
    expect(controller.isErrorReportOffered).toBe(true);
    // The failed retry's own notice carries the page's one Report a bug.
    expect(setupFailureNoticeButtons(controller)).toEqual({
      actionLabels: [],
      isBugReportOffered: false,
    });
    expect(posts).toEqual([
      "/api/v1/workspaces/agent-11/backup-service/setup/retry",
    ]);
  });
});
