import m from "mithril";
import { describe, expect, it } from "vitest";
import {
  BackupHistoryModel,
  BackupOperationController,
  type LifecycleDeps,
  type PreviousBackupRepository,
} from "../../models/backups";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes } from "../../testing";
import { SnapshotTable } from "./lifecycle/SnapshotTable";
import {
  WorkspaceBackupsPage,
  previousStorageLabel,
} from "./WorkspaceBackupsPage";

const DAY_MS = 24 * 60 * 60 * 1000;

const UNUSED_DEPS: LifecycleDeps = {
  getJson: () => Promise.resolve(null),
  postJson: () => Promise.resolve({ status: 500, json: null }),
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
  const vnode = m(WorkspaceBackupsPage) as unknown as {
    state: Record<string, unknown>;
  };
  vnode.state = {
    history,
    controller: new BackupOperationController("agent-11", UNUSED_DEPS),
    pendingRestore: null,
    onBecameObservable: () => undefined,
  };
  return (WorkspaceBackupsPage.view as (v: unknown) => AnyVnode).call(
    WorkspaceBackupsPage,
    vnode,
  );
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
});
