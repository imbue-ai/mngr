import { afterEach, describe, expect, it, vi } from "vitest";
import { settle } from "../testing";
import {
  BACKUP_HISTORY_PAGE_SIZE,
  BackupHistoryModel,
  BackupOperationController,
  BackupSettingsModel,
  DestroyedWorkspacesModel,
  DestroyingModel,
  MAX_CONSECUTIVE_POLL_FAILURES,
  RecoveryModel,
  browserLifecycleDeps,
  formatRelativeAgo,
  restoredFromLabel,
  type EventSourceLike,
  type LifecycleDeps,
} from "./backups";
import { errorMessageFromBody } from "./errors";
import type { RequestOutcome } from "./errors";

class FakeEventSource implements EventSourceLike {
  isClosed = false;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  close(): void {
    this.isClosed = true;
  }

  emit(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

class FakeDeps implements LifecycleDeps {
  getResponses: Array<unknown | null> = [];
  /**
   * Answers for the recovery-info route, kept out of the positional queue.
   *
   * The recovery card runs two pollers at once (its own state and a restart
   * operation), so a single in-order queue cannot say which of them a given
   * response was meant for -- whichever fires first would take it. Recovery-info
   * reads are served only from here, and answer null when it is empty, which is
   * exactly the transient-failure case the model already handles.
   */
  recoveryInfoResponses: Array<unknown | null> = [];
  /**
   * Answers for the backup-check route, likewise kept out of the positional
   * queue: the settings model starts its snapshot read and its check read
   * together, so whichever resolved first would take the other's answer.
   */
  checkResponses: Array<unknown | null> = [];
  /** While true, recovery-info reads hang until `resolveRecoveryInfo` answers
   * them -- the only way to hold one in flight while something else moves. */
  isRecoveryInfoHeld = false;
  private heldRecoveryInfo: ((payload: unknown | null) => void) | null = null;
  getUrls: string[] = [];
  postResponses: RequestOutcome[] = [];
  postCalls: Array<{ url: string; body: unknown }> = [];
  deleteStatuses: number[] = [];
  deleteUrls: string[] = [];
  sources: FakeEventSource[] = [];
  scheduled: Array<() => void> = [];
  redrawCount = 0;

  async getJson(url: string): Promise<unknown | null> {
    this.getUrls.push(url);
    if (url.includes("/backup-check")) {
      return this.checkResponses.length > 0
        ? this.checkResponses.shift()!
        : null;
    }
    if (url.includes("/recovery-info")) {
      if (this.isRecoveryInfoHeld) {
        return new Promise((resolve) => {
          this.heldRecoveryInfo = resolve;
        });
      }
      return this.recoveryInfoResponses.length > 0
        ? this.recoveryInfoResponses.shift()!
        : null;
    }
    return this.getResponses.length > 0 ? this.getResponses.shift()! : null;
  }

  async postJson(url: string, body: unknown): Promise<RequestOutcome> {
    this.postCalls.push({ url, body });
    return this.postResponses.length > 0
      ? this.postResponses.shift()!
      : { status: 500, body: null };
  }

  async deleteResource(url: string): Promise<number> {
    this.deleteUrls.push(url);
    return this.deleteStatuses.length > 0 ? this.deleteStatuses.shift()! : 200;
  }

  openEventSource(): EventSourceLike {
    const source = new FakeEventSource();
    this.sources.push(source);
    return source;
  }

  schedule(callback: () => void): void {
    this.scheduled.push(callback);
  }

  redraw(): void {
    this.redrawCount += 1;
  }

  runScheduled(): void {
    const pending = this.scheduled.splice(0);
    for (const callback of pending) callback();
  }

  /** Answer the recovery-info read left hanging by `isRecoveryInfoHeld`. */
  resolveRecoveryInfo(payload: unknown | null): void {
    const resolve = this.heldRecoveryInfo;
    if (resolve === null) throw new Error("no recovery-info read is in flight");
    this.heldRecoveryInfo = null;
    resolve(payload);
  }
}

describe("formatRelativeAgo", () => {
  it("buckets seconds through years like the legacy table", () => {
    const now = Date.parse("2026-06-15T12:00:00Z");
    expect(formatRelativeAgo("2026-06-15T11:59:40Z", now)).toBe("just now");
    expect(formatRelativeAgo("2026-06-15T11:58:00Z", now)).toBe("2 mins ago");
    expect(formatRelativeAgo("2026-06-15T09:00:00Z", now)).toBe("3 hours ago");
    expect(formatRelativeAgo("2026-06-10T12:00:00Z", now)).toBe("5 days ago");
    expect(formatRelativeAgo("2026-03-15T12:00:00Z", now)).toBe("3 months ago");
    expect(formatRelativeAgo("2024-05-15T12:00:00Z", now)).toBe("2 years ago");
    expect(formatRelativeAgo("not-a-date", now)).toBe("");
  });
});

describe("restoredFromLabel", () => {
  it("labels restored snapshots and names their lineage when tagged", () => {
    expect(restoredFromLabel(undefined)).toBeNull();
    expect(restoredFromLabel(["hourly"])).toBeNull();
    expect(restoredFromLabel(["restored"])).toBe("Restored");
    const withLineage = restoredFromLabel([
      "restored",
      "restored-from:2026-01-02T03:04:05Z",
    ]);
    expect(withLineage).toContain("Restored from ");
  });
});

describe("BackupHistoryModel", () => {
  it("walks the listing states: unconfigured, error, empty, and a real page", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);

    deps.getResponses.push({ is_configured: false });
    await model.loadPage();
    expect(model.statusMessage).toBe(
      "Backups are turned off for this workspace.",
    );

    deps.getResponses.push({ is_configured: true, snapshots_error: "boom" });
    await model.loadPage();
    expect(model.statusMessage).toBeNull();
    expect(model.loadError).toEqual({
      message:
        "Couldn't load this workspace's backup history. Try again. If this persists, report a bug.",
      details: "boom",
    });

    // A route that did not answer is the same failed read, with nothing under it.
    await model.loadPage();
    expect(model.loadError?.details).toBe("");

    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 0,
    });
    await model.loadPage();
    expect(model.loadError).toBeNull();
    expect(model.statusMessage).toBe(
      "No backups yet. The first backup runs within the hour.",
    );

    deps.getResponses.push({
      is_configured: true,
      snapshots: [{ snapshot_id: "s1", time: "2026-01-01T00:00:00Z" }],
      snapshots_total: 40,
    });
    await model.loadPage();
    expect(model.statusMessage).toBeNull();
    expect(model.total).toBe(40);
    expect(model.isPaginationShown).toBe(true);
    expect(model.rangeText).toBe("Showing 1-1 of 40 backups");
  });

  it("shows why backups were never set up, until a setup lands", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);

    deps.getResponses.push({
      is_configured: false,
      setup_failure: {
        kind: "STORAGE_QUOTA",
        detail: "Bucket create refused: storage quota exceeded",
      },
    });
    await model.loadPage();
    expect(model.setupFailure).toEqual({
      error: {
        message:
          "Your Imbue Cloud plan has no room for this workspace's backups.",
        details: "Bucket create refused: storage quota exceeded",
      },
      isBugReportOffered: false,
    });
    expect(model.statusMessage).toBeNull();

    // A cause Imbue Studio cannot name may be a bug.
    deps.getResponses.push({
      is_configured: false,
      setup_failure: {
        kind: "OTHER",
        detail: "workspace never became reachable",
      },
    });
    await model.loadPage();
    expect(model.setupFailure?.isBugReportOffered).toBe(true);

    // A listing that cannot be read after the setup ran again leaves no stale notice.
    deps.getResponses.push({ is_configured: true, snapshots_error: "boom" });
    await model.loadPage();
    expect(model.setupFailure).toBeNull();
    expect(model.loadError?.details).toBe("boom");

    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 0,
    });
    await model.loadPage();
    expect(model.setupFailure).toBeNull();
  });

  it("pages by the fixed page size and gates Restore on the OFFLINE verdict", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);
    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 40,
    });
    model.goOlder();
    await settle();
    expect(model.offset).toBe(BACKUP_HISTORY_PAGE_SIZE);
    expect(deps.getUrls[0]).toContain(`offset=${BACKUP_HISTORY_PAGE_SIZE}`);
    // Only the first page asks for the previous destinations.
    expect(deps.getUrls[0]).not.toContain("include_previous");

    deps.checkResponses.push({ check_state: "OFFLINE" });
    await model.fetchCheckState();
    expect(model.isRestoreDisabledByCheck()).toBe(true);
  });

  it("asks for previous destinations and keeps them even when current backups are off", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);
    deps.getResponses.push({
      is_configured: false,
      previous_repositories: [
        {
          archive_id: "20260101T000000Z",
          archived_at: "2026-01-01T00:00:00Z",
          snapshots: [{ snapshot_id: "old-1", time: "2025-12-31T00:00:00Z" }],
        },
      ],
    });
    await model.loadPage();
    expect(deps.getUrls[0]).toContain("include_previous=1");
    expect(model.statusMessage).toBe(
      "Backups are turned off for this workspace.",
    );
    expect(model.previousRepositories).toHaveLength(1);
    expect(model.previousRepositories[0].archive_id).toBe("20260101T000000Z");
    expect(model.isBackupsConfigured).toBe(false);
  });

  it("keeps previous destinations restorable when the current listing cannot be read", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);
    deps.getResponses.push({
      is_configured: true,
      snapshots_error: "restic snapshots timed out",
      previous_repositories: [
        {
          archive_id: "20260101T000000Z",
          archived_at: "2026-01-01T00:00:00Z",
          snapshots: [{ snapshot_id: "old-1", time: "2025-12-31T00:00:00Z" }],
        },
      ],
    });
    await model.loadPage();
    expect(model.loadError?.details).toBe("restic snapshots timed out");
    expect(model.previousRepositories).toHaveLength(1);
    expect(model.isBackupsConfigured).toBe(true);
  });
});

describe("BackupOperationController restore from a previous destination", () => {
  it("names the archive in the restore body, and null for current backups", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startRestore(
      { snapshot_id: "old-1", time: "2025-12-31T00:00:00Z" },
      "",
      { repositoryId: "20260101T000000Z" },
    );
    await settle();
    expect(deps.postCalls[0].body).toMatchObject({
      repository: "20260101T000000Z",
    });

    deps.postResponses.push({ status: 202, body: null });
    const current = new BackupOperationController("agent-12", deps);
    current.startRestore(
      { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
      "",
      {},
    );
    await settle();
    expect(deps.postCalls[1].body).toMatchObject({ repository: null });
  });
});

describe("BackupOperationController", () => {
  it("surfaces a non-202 dispatch as an error without entering the running state", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    const conflict =
      "A restore is already in progress for this workspace. Wait for it to finish before starting another operation.";
    deps.postResponses.push({ status: 409, body: { error: conflict } });

    controller.dispatch(
      "/x",
      {},
      { label: "Working...", kind: "backup_update" },
    );
    await settle();

    expect(controller.isRunning).toBe(false);
    expect(controller.error?.message).toBe(
      "Another task is already running on this workspace. *Wait for it to finish, then try again.*",
    );
    expect(controller.error?.details).toContain(conflict);
    expect(controller.isErrorReportOffered).toBe(false);
  });

  it("tells each coded or reconnect refusal of a dispatch apart from a conflicting operation", async () => {
    const belowFloor =
      "This workspace was created before minds-v0.4.0, and today's backup service does not work on it.";
    const cases: Array<{
      answer: RequestOutcome;
      message: string;
      isRestartAppOffered: boolean;
    }> = [
      {
        answer: {
          status: 409,
          body: { error: belowFloor, code: "BELOW_UPDATE_FLOOR" },
        },
        message: belowFloor,
        isRestartAppOffered: false,
      },
      {
        answer: {
          status: 409,
          body: {
            error: "Backups are not configured for agent-11",
            code: "BACKUPS_NOT_CONFIGURED",
          },
        },
        message:
          "Backups are turned off for this workspace, so it can't be restored here. " +
          "*Reload the page to see its backup settings.*",
        isRestartAppOffered: false,
      },
      {
        answer: { status: 401, body: { error: "Not authenticated" } },
        message: "Imbue Studio needs to restart to reconnect this window.",
        isRestartAppOffered: true,
      },
    ];
    for (const { answer, message, isRestartAppOffered } of cases) {
      const deps = new FakeDeps();
      const controller = new BackupOperationController("agent-11", deps);
      deps.postResponses.push(answer);

      controller.dispatch(
        "/x",
        {},
        { label: "Working...", kind: "backup_update" },
      );
      await settle();

      expect(controller.error?.message).toBe(message);
      expect(controller.isRestartAppOffered).toBe(isRestartAppOffered);
      expect(controller.isErrorReportOffered).toBe(false);
    }
  });

  it("says a refused cancel came too late or found nothing to cancel, and offers a report only for an unexplained one", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push(
      {
        status: 409,
        body: { error: "The operation can no longer be cancelled." },
      },
      {
        status: 404,
        body: { error: "No cancellable backup operation for agent-11" },
      },
      { status: 500, body: null },
    );

    await controller.requestCancel();
    expect(controller.error?.message).toBe(
      "It's too late to cancel: this task has started making changes and will run to the end.",
    );
    expect(controller.isErrorReportOffered).toBe(false);

    await controller.requestCancel();
    expect(controller.error?.message).toBe(
      "There's nothing to cancel: this task has already finished.",
    );
    expect(controller.isErrorReportOffered).toBe(false);

    await controller.requestCancel();
    expect(controller.error?.message).toBe(
      "Couldn't cancel this task. Try again. If this persists, report a bug.",
    );
    expect(controller.isErrorReportOffered).toBe(true);
  });

  it("drops a refused cancel's error once the task it was about ends", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push(
      { status: 202, body: null },
      {
        status: 409,
        body: { error: "The operation can no longer be cancelled." },
      },
    );
    controller.startUpdate();
    await settle();
    await controller.requestCancel();
    expect(controller.error).not.toBeNull();

    deps.getResponses.push({
      status: "DONE",
      kind: "backup_update",
      is_done: true,
    });
    await controller.pollOnce();

    expect(controller.error).toBeNull();
    expect(controller.isErrorReportOffered).toBe(false);
    expect(controller.successMessage).not.toBeNull();
  });

  it("runs a restore to success: row-driven state, log stream, and the page refresh hook", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    let successCount = 0;
    controller.onSuccess = () => {
      successCount += 1;
    };
    deps.postResponses.push({ status: 202, body: null });

    controller.startRestore(
      { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
      "5 days ago",
      {},
    );
    expect(controller.isRunning).toBe(true);
    expect(controller.isRestore).toBe(true);
    expect(controller.restoringSnapshotId).toBe("snap-1");
    await settle();

    expect(deps.sources).toHaveLength(1);
    deps.sources[0].emit({ log: "restoring files" });
    expect(controller.logLines).toEqual(["restoring files"]);
    // A restore never paints the strip progress line; the row speaks for it.
    expect(controller.progressLine).toBeNull();

    deps.getResponses.push({ status: "RUNNING", is_cancellable: true });
    deps.runScheduled();
    await settle();
    expect(controller.isCancellable).toBe(true);

    deps.getResponses.push({
      status: "DONE",
      kind: "backup_restore",
      is_done: true,
    });
    deps.runScheduled();
    await settle();
    expect(controller.isRunning).toBe(false);
    expect(controller.successMessage).toContain(
      "Workspace restored to the backup from 5 days ago",
    );
    expect(successCount).toBe(1);
  });

  it("offers the stop-chats retry only when this page dispatched the operation", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startRestore(
      { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
      "",
      {},
    );
    await settle();

    deps.getResponses.push({ status: "FAILED", blocked_chats: ["main"] });
    deps.runScheduled();
    await settle();

    expect(controller.error?.message).toContain(
      "Chats are running in this workspace (main)",
    );
    expect(controller.isStopChatsRetryOffered).toBe(true);

    // The retry re-dispatches the same restore with stop_chats flipped on.
    deps.postResponses.push({ status: 202, body: null });
    controller.runStopChatsRetry();
    await settle();
    const retryBody = deps.postCalls[1].body as { stop_chats: boolean };
    expect(retryBody.stop_chats).toBe(true);
  });

  it("reattaches to a restore started elsewhere without offering retries", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.getResponses.push({
      status: "RUNNING",
      kind: "backup_restore",
      is_cancellable: true,
      snapshot_id: "snap-9",
    });

    await controller.reattach();

    expect(controller.isRunning).toBe(true);
    expect(controller.isRestore).toBe(true);
    expect(controller.restoringSnapshotId).toBe("snap-9");

    deps.getResponses.push({
      status: "FAILED",
      error: "pre-restore safety snapshot failed",
      failure_reason: "SAFETY_SNAPSHOT_FAILED",
    });
    deps.runScheduled();
    await settle();
    // No dispatch context: the safety-skip retry must NOT be offered.
    expect(controller.isSkipSafetyRetryOffered).toBe(false);
    expect(controller.isRetryOffered).toBe(false);
    expect(controller.error).toEqual({
      message:
        "Imbue Studio couldn't save a copy of this workspace's current state before restoring.",
      details: "pre-restore safety snapshot failed",
    });
  });

  it("drops this page's last failure and its retries when it reattaches to an operation started elsewhere", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startUpdate();
    await settle();
    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_update",
      error: "git commit failed",
      failure_reason: null,
    });
    await controller.pollOnce();
    expect(controller.isRetryOffered).toBe(true);

    deps.getResponses.push({ status: "RUNNING", kind: "backup_restore" });
    await controller.reattach();

    // The adopted operation has no retry closures, so a retry left on offer
    // would be a button that does nothing.
    expect(controller.isRunning).toBe(true);
    expect(controller.error).toBeNull();
    expect(controller.isRetryOffered).toBe(false);
  });

  it("gives up after a bounded run of unreadable status polls", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.dispatch(
      "/x",
      {},
      { label: "Working...", kind: "backup_update" },
    );
    await settle();

    // Every poll answers null (FakeDeps with no queued responses): the
    // controller keeps polling until the bound, then declares the poll lost.
    for (
      let attempt = 0;
      attempt < MAX_CONSECUTIVE_POLL_FAILURES;
      attempt += 1
    ) {
      expect(controller.isRunning).toBe(true);
      deps.runScheduled();
      await settle();
    }

    expect(controller.isRunning).toBe(false);
    expect(controller.error?.message).toContain(
      "Lost contact with the backup operation",
    );
    expect(deps.scheduled).toHaveLength(0);
  });

  it("keeps polling when a null result is followed by a real payload", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.dispatch(
      "/x",
      {},
      { label: "Working...", kind: "backup_update" },
    );
    await settle();

    // One null tick, then a real RUNNING payload: still running, no error.
    deps.runScheduled();
    await settle();
    deps.getResponses.push({ status: "RUNNING", is_cancellable: false });
    deps.runScheduled();
    await settle();

    expect(controller.isRunning).toBe(true);
    expect(controller.error).toBeNull();
  });

  it("reports a cancelled operation as a neutral outcome", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.dispatch(
      "/x",
      {},
      { label: "Updating...", isCancellable: true, kind: "backup_update" },
    );
    await settle();

    deps.getResponses.push({ status: "CANCELLED", kind: "backup_update" });
    deps.runScheduled();
    await settle();

    expect(controller.cancelledMessage).toBe(
      "Update cancelled. Nothing was changed.",
    );
    expect(controller.error).toBeNull();
  });
});

describe("backup operation failures", () => {
  async function restoreEndingWith(payload: unknown): Promise<{
    deps: FakeDeps;
    controller: BackupOperationController;
  }> {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startRestore(
      { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
      "",
      { updateAfter: false },
    );
    await settle();
    deps.getResponses.push(payload);
    deps.runScheduled();
    await settle();
    return { deps, controller };
  }

  it("says a part-way restore can be run again, and Try again re-runs the same restore", async () => {
    const { deps, controller } = await restoreEndingWith({
      status: "FAILED",
      kind: "backup_restore",
      error:
        "restored, but uv sync failed: boom A safety backup of the pre-restore state was taken first.",
      failure_reason: "RESTORE_INCOMPLETE_AFTER_SAFETY_BACKUP",
    });

    expect(controller.error?.message).toBe(
      "The restore didn't finish. Running it again picks up where it stopped. " +
        "A safety backup of the pre-restore state was taken first.",
    );
    expect(controller.error?.details).toContain("uv sync failed: boom");
    expect(controller.isRetryOffered).toBe(true);

    deps.postResponses.push({ status: 202, body: null });
    controller.runRetry();
    await settle();
    expect(deps.postCalls).toHaveLength(2);
    expect(deps.postCalls[1]).toEqual(deps.postCalls[0]);
  });

  it("offers the failure-specific restore retries by the reason the restore failed for", async () => {
    const safety = await restoreEndingWith({
      status: "FAILED",
      kind: "backup_restore",
      error: "pre-restore safety snapshot failed: disk full",
      failure_reason: "SAFETY_SNAPSHOT_FAILED",
    });
    expect(safety.controller.isSkipSafetyRetryOffered).toBe(true);
    expect(safety.controller.isForceRetryOffered).toBe(false);

    for (const reason of ["CHAT_GATE_UNDETERMINED", "WORKSPACE_PROBE_FAILED"]) {
      const { controller } = await restoreEndingWith({
        status: "FAILED",
        kind: "backup_restore",
        error: "boom",
        failure_reason: reason,
      });
      expect(controller.isForceRetryOffered).toBe(true);
      expect(controller.isSkipSafetyRetryOffered).toBe(false);
    }

    const unexplained = await restoreEndingWith({
      status: "FAILED",
      kind: "backup_restore",
      error:
        "pre-restore safety snapshot failed; cannot determine running chats",
      failure_reason: null,
    });
    expect(unexplained.controller.isSkipSafetyRetryOffered).toBe(false);
    expect(unexplained.controller.isForceRetryOffered).toBe(false);
  });

  it("offers stopping the chats again when one would not stop", async () => {
    const { controller } = await restoreEndingWith({
      status: "FAILED",
      kind: "backup_restore",
      error: "could not stop chat review: timeout",
      failure_reason: "CHAT_STOP_FAILED",
    });

    expect(controller.error?.message).toBe(
      "Imbue Studio couldn't stop the chats in this workspace.",
    );
    expect(controller.isStopChatsRetryOffered).toBe(true);
    expect(controller.isRetryOffered).toBe(false);
  });

  it("keeps a backup that cannot be restored from offering a retry", async () => {
    const { controller } = await restoreEndingWith({
      status: "FAILED",
      kind: "backup_restore",
      error: "Snapshot abc records no paths; it cannot be restored",
      failure_reason: "SNAPSHOT_UNRESTORABLE",
    });

    expect(controller.error?.message).toContain(
      "This backup doesn't contain a copy of your workspace",
    );
    expect(controller.isRetryOffered).toBe(false);
    expect(controller.isErrorReportOffered).toBe(false);
  });

  it("words an update failure with no known reason by the fallback, its text behind details", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startUpdate();
    await settle();
    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_update",
      error: "git commit failed: index.lock exists (changes were rolled back)",
      failure_reason: null,
    });
    await controller.pollOnce();

    expect(controller.error).toEqual({
      message:
        "Couldn't update this workspace's backup software. Try again. If this persists, report a bug.",
      details:
        "git commit failed: index.lock exists (changes were rolled back)",
    });
    expect(controller.isErrorReportOffered).toBe(true);

    deps.postResponses.push({ status: 202, body: null });
    controller.runRetry();
    await settle();
    expect(deps.postCalls[1].url).toBe(deps.postCalls[0].url);
  });

  it("says which configure input is wrong, and never offers Try again for Save's job", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({
      status: 400,
      body: { error: "Invalid backup_provider", code: "BACKUP_CHOICE_INVALID" },
    });
    controller.startStorageChange("BOGUS", "");
    await settle();
    expect(controller.error?.message).toBe(
      "*Choose where your backups should be stored, then press Save.*",
    );
    expect(controller.error?.details).toContain("Invalid backup_provider");
    expect(controller.isRetryOffered).toBe(false);

    deps.postResponses.push({
      status: 400,
      body: {
        error: "imbue_cloud backups require a selected account.",
        code: "BACKUP_ACCOUNT_MISSING",
      },
    });
    controller.startStorageChange("IMBUE_CLOUD", "");
    await settle();
    expect(controller.error?.message).toContain(
      "Backing up to Imbue Cloud needs this workspace to be linked to an account.",
    );
    expect(controller.isErrorReportOffered).toBe(false);

    deps.postResponses.push({
      status: 502,
      body: {
        error: "Workspace agent-11 has no discovered host",
        code: "WORKSPACE_MACHINE_NOT_FOUND",
      },
    });
    controller.startStorageChange("IMBUE_CLOUD", "");
    await settle();
    expect(controller.error?.message).toContain(
      "Imbue Studio can't find this workspace's machine",
    );

    deps.postResponses.push({ status: 0, body: null });
    controller.startStorageChange("IMBUE_CLOUD", "");
    await settle();
    expect(controller.error?.message).toBe(
      "Imbue Studio didn't respond. Try again in a moment.",
    );
    expect(controller.isRetryOffered).toBe(false);
  });

  it("offers Try again for an update whose dispatch got no answer, sending the same update", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 0, body: null });
    controller.startUpdate();
    await settle();

    expect(controller.error?.message).toBe(
      "Imbue Studio didn't respond. Try again in a moment.",
    );
    expect(controller.isRetryOffered).toBe(true);

    deps.postResponses.push({ status: 202, body: null });
    controller.runRetry();
    await settle();
    expect(deps.postCalls).toHaveLength(2);
    expect(deps.postCalls[1].url).toBe(deps.postCalls[0].url);
  });

  it("words a turn-off that failed for no known reason as the turn-off, not a setup", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startStorageChange("NONE", "");
    await settle();
    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_configure",
      error: "The backup worker failed unexpectedly: boom",
      failure_reason: null,
    });
    await controller.pollOnce();

    expect(controller.error).toEqual({
      message:
        "Couldn't turn backups off for this workspace. *Press Save to try again.* If this persists, report a bug.",
      details: "The backup worker failed unexpectedly: boom",
    });
  });

  it("runs a failed setup again from the Backups page, offering it again if that fails too", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startSetupRetry();
    await settle();
    expect(deps.postCalls[0].url).toBe(
      "/api/v1/workspaces/agent-11/backup-service/setup/retry",
    );

    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_configure",
      error: "restic init failed (exit 1): permission denied",
    });
    await controller.pollOnce();
    expect(controller.error?.message).toBe(
      "Couldn't set up backups for this workspace. Try again. If this persists, report a bug.",
    );
    expect(controller.isRetryOffered).toBe(true);
  });

  it("says a setup the plan has no room for in the plan's words, asking for no bug report", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startSetupRetry();
    await settle();
    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_configure",
      error: "bucket create: storage quota exceeded",
      failure_reason: "STORAGE_QUOTA_EXCEEDED",
    });
    await controller.pollOnce();

    expect(controller.error).toEqual({
      message:
        "Your Imbue Cloud plan has no room for this workspace's backups.",
      details: "bucket create: storage quota exceeded",
    });
    expect(controller.isErrorReportOffered).toBe(false);
  });

  it("words the Backups page's setup refusals as a setup", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({
      status: 404,
      body: {
        error: "No failed backup setup to retry for agent-11",
        code: "NO_SETUP_TO_RETRY",
      },
    });
    controller.startSetupRetry();
    await settle();
    expect(controller.error?.message).toBe(
      "This workspace's backups may already be set up. *Reload the page to check.*",
    );
    expect(controller.error?.details).toContain("No failed backup setup");
    expect(controller.isErrorReportOffered).toBe(false);

    deps.postResponses.push({
      status: 502,
      body: {
        error: "Workspace agent-11 has no discovered host",
        code: "WORKSPACE_MACHINE_NOT_FOUND",
      },
    });
    controller.startSetupRetry();
    await settle();
    expect(controller.error?.message).toContain(
      "so it can't set up its backups.",
    );
  });

  it("carries the programs a finished restore left down", async () => {
    const { controller } = await restoreEndingWith({
      status: "DONE",
      kind: "backup_restore",
      is_done: true,
      services_down: ["browser", "xvfb"],
    });

    expect(controller.error).toBeNull();
    expect(controller.successMessage).not.toBeNull();
    expect(controller.servicesDown).toEqual(["browser", "xvfb"]);
  });
});

describe("DestroyingModel", () => {
  it("streams logs, applies terminal statuses, and fires onDone exactly on done", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    let doneCount = 0;
    model.onDone = () => {
      doneCount += 1;
    };
    deps.getResponses.push({ status: "RUNNING" });
    model.start();
    await settle();

    expect(deps.sources).toHaveLength(1);
    deps.sources[0].emit({ log: "stopping host\n" });
    expect(model.logText).toBe("stopping host\n");

    deps.sources[0].emit({ done: true, status: "DONE" });
    expect(model.status).toBe("done");
    expect(doneCount).toBe(1);
    expect(deps.sources[0].isClosed).toBe(true);
  });

  it("marks the destroy failed after a bounded run of unreadable status polls", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    model.start();
    await settle();

    for (
      let attempt = 0;
      attempt < MAX_CONSECUTIVE_POLL_FAILURES;
      attempt += 1
    ) {
      deps.runScheduled();
      await settle();
    }

    expect(model.status).toBe("failed");
    expect(deps.scheduled).toHaveLength(0);
  });

  it("keeps checking a finished removal whose workspace is not yet seen gone, until it is", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    let doneCount = 0;
    model.onDone = () => {
      doneCount += 1;
    };
    deps.getResponses.push({ status: "RUNNING" });
    model.start();
    await settle();

    // The log stream ends on the finished process, before the host is gone.
    deps.sources[0].emit({ done: true, status: "CONFIRMING" });
    expect(model.status).toBe("confirming");
    expect(doneCount).toBe(0);

    deps.getResponses.push({ status: "CONFIRMING" });
    deps.runScheduled();
    await settle();
    expect(model.status).toBe("confirming");
    expect(deps.scheduled).toHaveLength(1);

    deps.getResponses.push({ status: "DONE" });
    deps.runScheduled();
    await settle();
    expect(model.status).toBe("done");
    expect(doneCount).toBe(1);
  });

  it("says the log is unavailable only when its stream failed before delivering anything", () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    model.start();
    deps.sources[0].onerror?.(new Error("404"));
    expect(model.isLogUnavailable).toBe(true);

    const second = new DestroyingModel("agent-23", deps);
    second.start();
    const source = deps.sources[deps.sources.length - 1];
    source.emit({ log: "stopping host\n" });
    source.onerror?.(new Error("dropped"));
    expect(second.isLogUnavailable).toBe(false);
    expect(second.logText).toBe("stopping host\n");
  });

  it("shows the failed state and lets a retry restart the flow", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.getResponses.push({ status: "FAILED" });
    model.start();
    await settle();
    expect(model.status).toBe("failed");

    deps.postResponses.push({ status: 202, body: null });
    deps.getResponses.push({ status: "RUNNING" });
    await model.retry();
    expect(model.status).toBe("running");
    expect(model.logText).toBe("");
    expect(model.retryFailure).toBeNull();
  });

  it("surfaces a refused retry instead of silently staying failed", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.getResponses.push({ status: "FAILED" });
    model.start();
    await settle();

    deps.postResponses.push({
      status: 409,
      body: { error: "managed by a newer version", code: "RECORD_TOO_NEW" },
    });
    await model.retry();
    expect(model.status).toBe("failed");
    expect(model.retryFailure?.error.message).toBe(
      "This workspace was set up by a newer version of Imbue Studio. *Update Imbue Studio to remove it.*",
    );
    expect(model.retryFailure?.error.details).toContain(
      "managed by a newer version",
    );

    // A later successful retry clears it.
    deps.postResponses.push({ status: 202, body: null });
    deps.getResponses.push({ status: "RUNNING" });
    await model.retry();
    expect(model.retryFailure).toBeNull();
    expect(model.status).toBe("running");
  });

  it("says Imbue Studio didn't respond when the retry post cannot connect", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.postResponses.push({ status: 0, body: null });

    await model.retry();

    expect(model.retryFailure?.error.message).toBe(
      "Imbue Studio didn't respond. Try again in a moment.",
    );
  });
});

describe("RecoveryModel", () => {
  const info = {
    agent_id: "agent-33",
    workspace_name: "my-machine",
    health: "stuck",
    health_error: "",
    recovery_kind: null,
    ssh_command: "ssh -p 22 user@host",
    is_host_offline: false,
    device_environment: "NONE",
    is_backend_unreachable: false,
    provider_label: "",
    unreachable_reason: "",
    is_device_cannot_connect: false,
    device_error_detail: "",
  };

  it("loads recovery info and dispatches a manual host restart to success", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("host-abc", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    expect(model.info?.workspace_name).toBe("my-machine");
    expect(model.agentId).toBe("agent-33");

    deps.postResponses.push({
      status: 202,
      body: null,
    });
    await model.dispatchRecovery();
    expect(model.isRecoveryRunning).toBe(true);
    expect(deps.postCalls[0].url).toContain(
      "/api/v1/workspaces/agent-33/restart",
    );
    expect(deps.postCalls[0].body).toEqual({
      scope: "host",
      start_only: false,
    });

    deps.getResponses.push({ status: "DONE", is_done: true });
    deps.runScheduled();
    await settle();
    expect(model.isRecoveryRunning).toBe(false);
    expect(model.isRecoverySucceeded).toBe(true);
  });

  it("surfaces a rejected restart dispatch and a failed restart operation", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    deps.postResponses.push({
      status: 409,
      body: {
        error: "A restore is already in progress for this workspace.",
        operation_kind: "BACKUP_RESTORE",
      },
    });
    await model.dispatchRecovery();
    expect(model.isRecoveryRunning).toBe(false);
    // The conflict is worded from the operation that holds the workspace, with
    // the server's own sentence behind it.
    expect(model.recoveryError?.message).toBe(
      "Imbue Studio is already working on this workspace (restoring a backup). " +
        "Wait for that to finish, then try again.",
    );
    expect(model.recoveryError?.details).toContain("A restore is already in progress");

    deps.postResponses.push({
      status: 503,
      body: { error: "Could not start the recovery worker: group exited" },
    });
    await model.dispatchRecovery();
    expect(model.recoveryError?.message).toBe(
      "Imbue Studio couldn't restart this workspace. Try again. If this persists, report a bug.",
    );
    expect(model.recoveryError?.details).toBe(
      "HTTP 503\nCould not start the recovery worker: group exited",
    );

    // A POST the app never answered dispatched nothing: there is no restart to follow.
    deps.postResponses.push({ status: 0, body: null });
    await model.dispatchRecovery();
    expect(model.isRecoveryRunning).toBe(false);
    expect(model.recoveryError?.message).toBe("Imbue Studio didn't respond. Try again in a moment.");
    expect(deps.sources).toHaveLength(0);

    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();
    deps.getResponses.push({
      status: "FAILED",
      is_done: false,
      error: "host did not come back",
    });
    deps.runScheduled();
    await settle();
    expect(model.recoveryError?.details).toBe("host did not come back");
    expect(model.recoveryError?.message).toBe(
      "Imbue Studio couldn't restart this workspace. Try again. If this persists, report a bug.",
    );
  });

  it("reads a declined start as neither a success nor a failure", async () => {
    // The connector refused the start because an operator holds the machine:
    // nothing changed, so the machine is not answering (no success the page
    // could leave on) and nothing failed (no error card). The reason is shown.
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery("start");

    deps.getResponses.push({
      status: "DECLINED",
      is_done: false,
      warning: "This workspace is undergoing maintenance.",
    });
    deps.runScheduled();
    await settle();

    expect(model.isRecoveryRunning).toBe(false);
    expect(model.isRecoverySucceeded).toBe(false);
    expect(model.recoveryError).toBeNull();
    expect(model.recoveryNotice).toBe(
      "This workspace is undergoing maintenance.",
    );

    // Once the operator's start brings the machine back, the notice is stale.
    deps.recoveryInfoResponses.push({
      ...info,
      health: "healthy",
      is_host_offline: false,
    });
    deps.runScheduled();
    await settle();
    expect(model.recoveryNotice).toBeNull();
  });

  it("bounds consecutive failed restart-status polls like the sibling pollers", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();

    // Every status poll answers null (no queued responses): the model keeps
    // polling until the bound, then surfaces a lost-contact error instead of
    // pinning "Restarting..." forever.
    for (
      let attempt = 0;
      attempt < MAX_CONSECUTIVE_POLL_FAILURES;
      attempt += 1
    ) {
      expect(model.isRecoveryRunning).toBe(true);
      deps.runScheduled();
      await settle();
    }

    expect(model.isRecoveryRunning).toBe(false);
    expect(model.isRecoveryFollowLost).toBe(true);

    // The restart poll is what has to stop -- a lost restart must not keep
    // asking about itself forever. The card's own state poll is a separate
    // loop and outlives it, so counting pending callbacks would not tell the
    // two apart.
    const restartPolls = () =>
      deps.getUrls.filter((url) => url.includes("/operations/restart/")).length;
    const pollsBefore = restartPolls();
    deps.runScheduled();
    await settle();
    expect(restartPolls()).toBe(pollsBefore);
  });

  it("does not re-attach to the restart it just gave up following", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();
    for (
      let attempt = 0;
      attempt < MAX_CONSECUTIVE_POLL_FAILURES;
      attempt += 1
    ) {
      deps.runScheduled();
      await settle();
    }
    expect(model.isRecoveryFollowLost).toBe(true);
    const streamCount = deps.sources.length;

    // The tracker goes on calling that same restart running -- it is the state
    // whose status could not be read. Attaching to it would clear the report
    // the bound exists to make and start the whole run over, every poll.
    deps.recoveryInfoResponses.push({ ...info, health: "recovering" });
    deps.runScheduled();
    await settle();
    expect(model.isRecoveryFollowLost).toBe(true);
    expect(model.isRecoveryRunning).toBe(false);
    expect(deps.sources).toHaveLength(streamCount);

    // A restart that starts after the tracker has left this one is a different
    // run, and is followed like any other.
    deps.recoveryInfoResponses.push(info);
    deps.runScheduled();
    await settle();
    // That run has ended, so there is nothing left to have lost track of.
    expect(model.isRecoveryFollowLost).toBe(false);
    deps.recoveryInfoResponses.push({ ...info, health: "recovering" });
    deps.runScheduled();
    await settle();
    expect(model.isRecoveryRunning).toBe(true);
    expect(model.isRecoveryFollowLost).toBe(false);
    expect(deps.sources).toHaveLength(streamCount + 1);
  });

  it("reattaches to an in-flight restart when the page loads mid-restart", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push({ ...info, health: "recovering" });
    await model.load();
    expect(model.isRecoveryRunning).toBe(true);
    expect(deps.sources).toHaveLength(1);
  });

  it("follows the machine's state for as long as the card is open", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    expect(model.info?.is_backend_unreachable).toBe(false);

    // The outage lands after the card opened -- the case the whole poll exists
    // for. The verdict must replace the machine-level one, not wait for a reopen.
    deps.recoveryInfoResponses.push({
      ...info,
      is_backend_unreachable: true,
      provider_label: "Docker",
      unreachable_reason: "Cannot connect to the Docker daemon",
    });
    deps.runScheduled();
    await settle();

    expect(model.info?.is_backend_unreachable).toBe(true);
    expect(model.info?.provider_label).toBe("Docker");
    expect(model.info?.unreachable_reason).toBe(
      "Cannot connect to the Docker daemon",
    );
  });

  it("keeps the last good state when a poll cannot be read, and keeps polling", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    // No queued answer: the read failed. A fetch error is not news about the
    // machine, so what is on screen stays and the loop lives on.
    deps.runScheduled();
    await settle();

    expect(model.info?.workspace_name).toBe("my-machine");
    expect(model.loadError).toBeNull();
    expect(deps.scheduled).toHaveLength(1);
  });

  it("keeps reading after a first read that could not be made", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);

    // No queued answer: the first read failed. That is not more final than any
    // later one, and the surface must not hold the error until a reload.
    await model.load();
    expect(model.loadError).not.toBeNull();

    deps.recoveryInfoResponses.push(info);
    deps.runScheduled();
    await settle();

    expect(model.loadError).toBeNull();
    expect(model.info?.workspace_name).toBe("my-machine");
  });

  it("attaches to a restart that something else started while the card was open", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    expect(model.isRecoveryRunning).toBe(false);

    // Something else -- the same machine's card in another window -- restarts
    // it. The card shows that restart rather than an idle machine.
    deps.recoveryInfoResponses.push({ ...info, health: "recovering" });
    deps.runScheduled();
    await settle();
    expect(model.isRecoveryRunning).toBe(true);
    expect(deps.sources).toHaveLength(1);

    // The next poll must not open a second stream for the same restart.
    deps.recoveryInfoResponses.push({ ...info, health: "recovering" });
    deps.runScheduled();
    await settle();
    expect(deps.sources).toHaveLength(1);
  });

  it("words an attached recovery's failure by the kind the tracker reported for it", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    // Another window's restart, attached to: this card never dispatched it.
    deps.recoveryInfoResponses.push({ ...info, health: "recovering", recovery_kind: "restart" });
    deps.runScheduled();
    await settle();
    expect(model.dispatchedRecoveryKind).toBeNull();

    deps.getResponses.push({ status: "FAILED", is_done: false, error: "Start step failed" });
    deps.runScheduled();
    await settle();
    expect(model.recoveryError?.message).toBe(
      "Imbue Studio couldn't restart this workspace. Try again. If this persists, report a bug.",
    );
  });

  it("drops a reading taken before the restart it was watching finished", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();

    // The server moves the tracker out of "recovering" before the operation
    // reports done, so a read whose answer was decided just before that
    // transition still says "recovering" -- and can land after the status poll
    // has already reported success. Adopting it would re-report a restart that
    // is over: the success would vanish, the log would clear, and a second log
    // stream would open for a finished operation.
    deps.isRecoveryInfoHeld = true;
    deps.getResponses.push({ status: "DONE", is_done: true });
    deps.runScheduled();
    await settle();
    expect(model.isRecoverySucceeded).toBe(true);
    const streamCount = deps.sources.length;

    deps.resolveRecoveryInfo({ ...info, health: "recovering" });
    await settle();

    expect(model.isRecoverySucceeded).toBe(true);
    expect(model.isRecoveryRunning).toBe(false);
    expect(model.info?.health).toBe("stuck");
    expect(deps.sources).toHaveLength(streamCount);
    // Dropping a reading is not a reason to stop reading.
    expect(deps.scheduled).toHaveLength(1);
  });

  it("stops polling once the card is gone", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    model.stop();
    deps.runScheduled();
    await settle();

    expect(deps.scheduled).toHaveLength(0);
  });

  it("follows nothing from a restart dispatched off a card that closed mid-flight", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();
    const pendingBefore = deps.scheduled.length;

    // The dispatch POST is still in flight when the card goes away (the fake's
    // postJson resolves on a microtask, so stop() lands first). The POST is
    // already out -- the server may restart the machine -- but a stopped model
    // must not open a log stream nothing will ever close, or arm a poller.
    deps.postResponses.push({ status: 202, body: null });
    const dispatching = model.dispatchRecovery();
    model.stop();
    await dispatching;

    expect(deps.sources).toHaveLength(0);
    expect(deps.scheduled).toHaveLength(pendingBefore);
  });

  it("adopts nothing from a first read that lands after the card is gone", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.isRecoveryInfoHeld = true;
    const loading = model.load();

    // Navigating away mid-read. The surfaces dispatch their post-load work off
    // this promise -- the recovery page's ?intent=restart runs whenever the read
    // succeeded -- so a reading adopted here would act on a machine the user left.
    model.stop();
    deps.resolveRecoveryInfo(info);
    await loading;

    expect(model.info).toBeNull();
    expect(deps.scheduled).toHaveLength(0);
  });

  it("never asks for the health verdict on the card's behalf", async () => {
    // The verdict has a reader inside the app now -- the restart sequence runs
    // it alongside the restart it is deciding about. Nothing the card renders
    // depends on it, and it execs into the container behind a ~30s cap.
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();

    expect(deps.getUrls.some((url) => url.includes("/health"))).toBe(false);
  });

  it("separates opening a stopped machine from bouncing a wedged one", async () => {
    // Stopping a host that is already stopped buys nothing and is what let a
    // plain open tear down a container; the card's own Restart button still
    // asks for the full bounce, since that is what a wedged container needs.
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.recoveryInfoResponses.push(info);
    await model.load();

    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery("start");
    expect(deps.postCalls.at(-1)?.body).toEqual({
      scope: "host",
      start_only: true,
    });

    model.isRecoveryRunning = false;
    deps.postResponses.push({ status: 202, body: null });
    await model.dispatchRecovery();
    expect(deps.postCalls.at(-1)?.body).toEqual({
      scope: "host",
      start_only: false,
    });
  });
});

describe("BackupSettingsModel", () => {
  it("keeps the fast snapshot half readable when the slow check half never answers", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    deps.getResponses.push({
      is_configured: true,
      snapshots: [{ snapshot_id: "s1", time: "2026-01-01T00:00:00Z" }],
      snapshots_total: 12,
    });
    // No check answer queued: FakeDeps returns null, which is what an
    // unreachable machine's (slow, exec-based) check looks like.
    await model.load();

    expect(model.isConfigured).toBe(true);
    expect(model.snapshotsTotal).toBe(12);
    expect(model.statusLine).toContain("Last backup:");
    // No verdict is not a clean bill of health, and not a reason to hide the
    // one action that fixes things.
    expect(model.checkLine).toBe("");
    expect(model.isCheckVerdictMissing).toBe(true);
    expect(model.problemLines).toEqual([]);
    expect(model.isUpdateOffered(false)).toBe(true);
    expect(model.isViewAllShown).toBe(true);
  });

  it("says what the machine's backups are doing across the states the summary can be in", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    expect(model.statusLine).toBe("Loading backup status...");

    deps.getResponses.push({ is_configured: false, snapshots: [] });
    await model.loadSnapshots();
    expect(model.statusLine).toBe("Backups are turned off for this workspace.");
    expect(model.emptyHistoryMessage).toContain("Backups are turned off");

    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_error: "restic snapshots timed out",
    });
    await model.loadSnapshots();
    expect(model.statusLine).toBe("");
    expect(model.statusError).toEqual({
      message:
        "Imbue Studio couldn't check this workspace's backups. Try again. If this persists, report a bug.",
      details: "restic snapshots timed out",
    });
    expect(model.emptyHistoryMessage).toBe(
      "Couldn't load this workspace's backup history. Try again. If this persists, report a bug.",
    );

    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 0,
      is_backing_up: true,
    });
    await model.loadSnapshots();
    expect(model.statusError).toBeNull();
    expect(model.statusLine).toBe("Backing up now...");
    expect(model.emptyHistoryMessage).toContain(
      "first backup will appear shortly",
    );

    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 0,
    });
    await model.loadSnapshots();
    expect(model.statusLine).toBe("No successful backup yet.");
    expect(model.emptyHistoryMessage).toBe(
      "No backups yet. The first backup runs within the hour.",
    );
  });

  it("does not read a listing route that never answered as backups being turned off", async () => {
    // A fresh model, because the wrong answer comes from `isConfigured` still
    // being its default: nothing queued, so the route does not answer, and that
    // is no evidence either way about whether this machine backs up.
    const model = new BackupSettingsModel("agent-11", new FakeDeps());

    await model.loadSnapshots();

    expect(model.statusLine).toBe("");
    expect(model.statusError?.message).toContain(
      "Imbue Studio couldn't check this workspace's backups.",
    );
    expect(model.emptyHistoryMessage).toContain(
      "Couldn't load this workspace's backup history.",
    );
  });

  it("turns each reported problem into guidance, and keeps the check's own detail for See details", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    deps.checkResponses.push({
      check_state: "PROBLEMS",
      problems: ["CODE_OUTDATED", "SERVICE_NOT_RUNNING", "SOMETHING_NEW"],
      check_detail: "installed minds-v0.4.1, minimum minds-v0.5.0",
      installed_version: "minds-v0.4.1",
      minimum_version: "minds-v0.5.0",
      update_target_version: "minds-v0.5.2",
    });
    await model.loadCheck();

    expect(model.problemLines[0]).toContain("out of date");
    expect(model.problemLines[1]).toContain("is not running");
    // A problem code this build has no words for still reaches the reader.
    expect(model.problemLines[2]).toBe("SOMETHING_NEW");
    expect(model.problemLines).toHaveLength(3);
    expect(model.problemDetail).toBe(
      "installed minds-v0.4.1, minimum minds-v0.5.0",
    );
    expect(model.versionLine).toBe(
      "Installed backup service: minds-v0.4.1 / minimum required: minds-v0.5.0 / update installs: minds-v0.5.2",
    );
  });

  it("offers the converge on a healthy machine but not on one that cannot run it", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);

    deps.checkResponses.push({
      check_state: "OK",
      is_verification_enabled: true,
    });
    await model.loadCheck();
    // Idempotent: resetting a wedged service is what it is for, so a machine
    // with nothing wrong still gets the button.
    expect(model.isUpdateOffered(false)).toBe(true);
    expect(model.checkLine).toBe("The backup service is up to date.");
    expect(model.isRestoreDisabledByCheck).toBe(false);
    // A machine below the in-place floor: the server refuses this update, so
    // the button must not be offered.
    expect(model.isUpdateOffered(true)).toBe(false);

    deps.checkResponses.push({ check_state: "OFFLINE" });
    await model.loadCheck();
    expect(model.isUpdateOffered(false)).toBe(false);
    expect(model.isRestoreDisabledByCheck).toBe(true);
  });

  it("re-reads the verdict after a verification write, and reports a write that failed", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    deps.checkResponses.push({
      check_state: "OK",
      is_verification_enabled: true,
    });
    await model.loadCheck();
    expect(model.isVerificationEnabled).toBe(true);

    deps.postResponses.push({ status: 200, body: null });
    deps.checkResponses.push({
      check_state: "DISABLED",
      is_verification_enabled: false,
    });
    await model.toggleVerification();

    expect(deps.postCalls[0].url).toContain("/backup-service/verification");
    expect(deps.postCalls[0].body).toEqual({ enabled: false });
    // The fresh verdict, not an optimistic flip: the re-check is what decides.
    expect(model.isVerificationEnabled).toBe(false);
    expect(model.checkLine).toBe(
      "Backup service verification is disabled for this workspace.",
    );
    expect(model.isVerificationPending).toBe(false);
    expect(model.verificationError).toBeNull();

    deps.postResponses.push({ status: 500, body: null });
    await model.toggleVerification();
    expect(model.verificationError).toEqual({
      message: "Could not change the verification setting. Try again.",
      details: "HTTP 500",
    });
    expect(model.isVerificationPending).toBe(false);
  });

  it("says when the check reached no verdict, and clears it once a re-check does", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    // Still checking: not yet a missing verdict.
    expect(model.isCheckVerdictMissing).toBe(false);

    deps.checkResponses.push({ check_state: "UNKNOWN" });
    await model.loadCheck();
    expect(model.isCheckVerdictMissing).toBe(true);
    expect(model.problemLines).toEqual([]);

    deps.checkResponses.push({ check_state: "OK" });
    await model.loadCheck();
    expect(model.isCheckVerdictMissing).toBe(false);
  });

  it("words an unverifiable check as one that could not finish, with its detail kept apart", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    deps.checkResponses.push({
      check_state: "PROBLEMS",
      problems: ["UNVERIFIABLE"],
      check_detail: "check could not run: ssh: connect to host refused",
    });
    await model.loadCheck();

    expect(model.problemLines).toEqual([
      "Imbue Studio couldn't finish checking this workspace's backups. " +
        'Try "Update backup software"; if that fails too, try again in a few minutes.',
    ]);
    expect(model.problemDetail).toBe(
      "check could not run: ssh: connect to host refused",
    );
  });

  it("does not adopt a read that lands after the group was closed", async () => {
    const deps = new FakeDeps();
    const model = new BackupSettingsModel("agent-11", deps);
    deps.getResponses.push({
      is_configured: true,
      snapshots: [],
      snapshots_total: 3,
    });
    const inFlight = model.loadSnapshots();
    model.stop();
    await inFlight;
    expect(model.isSnapshotsLoaded).toBe(false);
    expect(model.snapshotsTotal).toBe(0);
  });
});

describe("the backup settings actions", () => {
  it("dispatches the update, and retries it with the stop-chats flag when chats block it", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, body: null });
    controller.startUpdate();
    await settle();

    expect(deps.postCalls[0].url).toBe(
      "/api/v1/workspaces/agent-11/backup-service/update",
    );
    expect(deps.postCalls[0].body).toEqual({ stop_chats: false });

    deps.getResponses.push({
      status: "FAILED",
      kind: "backup_update",
      blocked_chats: ["review"],
    });
    await controller.pollOnce();
    expect(controller.isStopChatsRetryOffered).toBe(true);

    deps.postResponses.push({ status: 202, body: null });
    controller.runStopChatsRetry();
    await settle();
    expect(deps.postCalls[1].body).toEqual({ stop_chats: true });
  });

  it("routes a storage change to configure, and turning backups off to disable", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);

    deps.postResponses.push({ status: 202, body: null });
    controller.startStorageChange("API_KEY", "RESTIC_REPOSITORY=s3:example\n");
    await settle();
    expect(deps.postCalls[0].url).toBe(
      "/api/v1/workspaces/agent-11/backup-service/configure",
    );
    expect(deps.postCalls[0].body).toEqual({
      backup_provider: "API_KEY",
      api_key_env: "RESTIC_REPOSITORY=s3:example\n",
    });

    deps.postResponses.push({ status: 202, body: null });
    controller.startStorageChange("NONE", "");
    await settle();
    expect(deps.postCalls[1].url).toBe(
      "/api/v1/workspaces/agent-11/backup-service/disable",
    );
    expect(deps.postCalls[1].body).toEqual({});
  });
});

describe("DestroyedWorkspacesModel.deleteBackup", () => {
  it("reads the list again instead of showing an error for a backup already gone", async () => {
    const deps = new FakeDeps();
    const model = new DestroyedWorkspacesModel(deps);
    deps.postResponses.push({
      status: 404,
      body: { error: "This backup is no longer here." },
    });
    deps.getResponses.push({ retention_days: 30, rows: [] });

    await model.deleteBackup("agent-21");

    expect(model.deleteFailure).toBeNull();
    expect(deps.getUrls).toEqual(["/ui/api/destroyed-workspaces"]);
    expect(model.deletingAgentIds.has("agent-21")).toBe(false);
  });

  it("remembers which backup a failed delete was for, and keeps the restart remedy for a lapsed login", async () => {
    const deps = new FakeDeps();
    const model = new DestroyedWorkspacesModel(deps);
    deps.postResponses.push(
      { status: 502, body: { error: "Could not delete the backup" } },
      { status: 401, body: { error: "Not authenticated" } },
    );

    await model.deleteBackup("agent-21");
    expect(model.deleteFailure?.agentId).toBe("agent-21");
    expect(model.deleteFailure?.notice.error.message).toBe(
      "Couldn't delete this backup. Try again. If this persists, report a bug.",
    );
    expect(model.deleteFailure?.notice.isRestartAppOffered).toBe(false);

    await model.deleteBackup("agent-21");
    expect(model.deleteFailure?.notice.error.message).toBe(
      "Imbue Studio needs to restart to reconnect this window.",
    );
    expect(model.deleteFailure?.notice.isRestartAppOffered).toBe(true);
  });
});

describe("browserLifecycleDeps postJson", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps a plain-text failure's text, and no body for a success", async () => {
    const respond = vi.fn(
      async () => new Response("Bad Gateway", { status: 502 }),
    );
    vi.stubGlobal("fetch", respond);
    const deps = browserLifecycleDeps(() => undefined);

    const failure = await deps.postJson("/x", {});
    respond.mockImplementation(
      async () => new Response('{"operation_id":"op-1"}', { status: 202 }),
    );
    const success = await deps.postJson("/x", {});

    expect(failure.status).toBe(502);
    expect(errorMessageFromBody(failure.body, "fallback")).toBe("Bad Gateway");
    expect(success).toEqual({ status: 202, body: null });
  });

  it("resolves status 0 when the request gets no response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const outcome = await browserLifecycleDeps(() => undefined).postJson(
      "/x",
      {},
    );

    expect(outcome).toEqual({ status: 0, body: null });
  });
});
