import { describe, expect, it } from "vitest";
import { settle } from "../testing";
import {
  BACKUP_HISTORY_PAGE_SIZE,
  BackupHistoryModel,
  BackupOperationController,
  DestroyingModel,
  MAX_CONSECUTIVE_POLL_FAILURES,
  RecoveryModel,
  formatRelativeAgo,
  isChatGateFailure,
  isSafetySnapshotFailure,
  restoredFromLabel,
  type EventSourceLike,
  type LifecycleDeps,
} from "./backups";

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
  getUrls: string[] = [];
  postResponses: Array<{ status: number; json: unknown | null }> = [];
  postCalls: Array<{ url: string; body: unknown }> = [];
  deleteStatuses: number[] = [];
  deleteUrls: string[] = [];
  sources: FakeEventSource[] = [];
  scheduled: Array<() => void> = [];
  redrawCount = 0;

  async getJson(url: string): Promise<unknown | null> {
    this.getUrls.push(url);
    return this.getResponses.length > 0 ? this.getResponses.shift()! : null;
  }

  async postJson(url: string, body: unknown): Promise<{ status: number; json: unknown | null }> {
    this.postCalls.push({ url, body });
    return this.postResponses.length > 0 ? this.postResponses.shift()! : { status: 500, json: null };
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
    const withLineage = restoredFromLabel(["restored", "restored-from:2026-01-02T03:04:05Z"]);
    expect(withLineage).toContain("Restored from ");
  });
});

describe("failure classification", () => {
  it("keys the failure-specific retries on the worker's wording", () => {
    expect(isSafetySnapshotFailure("the pre-restore safety snapshot failed: disk full")).toBe(true);
    expect(isSafetySnapshotFailure("something else")).toBe(false);
    expect(isChatGateFailure("cannot determine running chats")).toBe(true);
    expect(isChatGateFailure("Could not probe the machine")).toBe(true);
    expect(isChatGateFailure(null)).toBe(false);
  });
});

describe("BackupHistoryModel", () => {
  it("walks the listing states: unconfigured, error, empty, and a real page", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);

    deps.getResponses.push({ is_configured: false });
    await model.loadPage();
    expect(model.statusMessage).toBe("Backups are turned off for this machine.");

    deps.getResponses.push({ is_configured: true, snapshots_error: "boom" });
    await model.loadPage();
    expect(model.statusMessage).toBe("Couldn't load your backup history right now.");

    deps.getResponses.push({ is_configured: true, snapshots: [], snapshots_total: 0 });
    await model.loadPage();
    expect(model.statusMessage).toBe("No backups yet. The first backup runs within the hour.");

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

  it("pages by the fixed page size and gates Restore on the OFFLINE verdict", async () => {
    const deps = new FakeDeps();
    const model = new BackupHistoryModel("agent-11", deps);
    deps.getResponses.push({ is_configured: true, snapshots: [], snapshots_total: 40 });
    model.goOlder();
    await settle();
    expect(model.offset).toBe(BACKUP_HISTORY_PAGE_SIZE);
    expect(deps.getUrls[0]).toContain(`offset=${BACKUP_HISTORY_PAGE_SIZE}`);

    deps.getResponses.push({ check_state: "OFFLINE" });
    await model.fetchCheckState();
    expect(model.isRestoreDisabledByCheck()).toBe(true);
  });
});

describe("BackupOperationController", () => {
  it("surfaces a non-202 dispatch as an error without entering the running state", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 409, json: { error: "already running" } });

    controller.dispatch("/x", {}, { label: "Working..." });
    await settle();

    expect(controller.isRunning).toBe(false);
    expect(controller.errorMessage).toBe("already running");
  });

  it("runs a restore to success: row-driven state, log stream, and the page refresh hook", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    let successCount = 0;
    controller.onSuccess = () => {
      successCount += 1;
    };
    deps.postResponses.push({ status: 202, json: null });

    controller.startRestore({ snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" }, "5 days ago", {});
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

    deps.getResponses.push({ status: "DONE", kind: "backup_restore", is_done: true });
    deps.runScheduled();
    await settle();
    expect(controller.isRunning).toBe(false);
    expect(controller.successMessage).toContain("Machine restored to the backup from 5 days ago");
    expect(successCount).toBe(1);
  });

  it("offers the stop-chats retry only when this page dispatched the operation", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, json: null });
    controller.startRestore({ snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" }, "", {});
    await settle();

    deps.getResponses.push({ status: "FAILED", blocked_chats: ["main"] });
    deps.runScheduled();
    await settle();

    expect(controller.errorMessage).toContain("Chats are running in this machine (main)");
    expect(controller.isStopChatsRetryOffered).toBe(true);

    // The retry re-dispatches the same restore with stop_chats flipped on.
    deps.postResponses.push({ status: 202, json: null });
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

    deps.getResponses.push({ status: "FAILED", error: "pre-restore safety snapshot failed" });
    deps.runScheduled();
    await settle();
    // No dispatch context: the safety-skip retry must NOT be offered.
    expect(controller.isSkipSafetyRetryOffered).toBe(false);
    expect(controller.errorMessage).toContain("safety snapshot failed");
  });

  it("gives up after a bounded run of unreadable status polls", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, json: null });
    controller.dispatch("/x", {}, { label: "Working..." });
    await settle();

    // Every poll answers null (FakeDeps with no queued responses): the
    // controller keeps polling until the bound, then declares the poll lost.
    for (let attempt = 0; attempt < MAX_CONSECUTIVE_POLL_FAILURES; attempt += 1) {
      expect(controller.isRunning).toBe(true);
      deps.runScheduled();
      await settle();
    }

    expect(controller.isRunning).toBe(false);
    expect(controller.errorMessage).toContain("Lost contact with the backup operation");
    expect(deps.scheduled).toHaveLength(0);
  });

  it("keeps polling when a null result is followed by a real payload", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, json: null });
    controller.dispatch("/x", {}, { label: "Working..." });
    await settle();

    // One null tick, then a real RUNNING payload: still running, no error.
    deps.runScheduled();
    await settle();
    deps.getResponses.push({ status: "RUNNING", is_cancellable: false });
    deps.runScheduled();
    await settle();

    expect(controller.isRunning).toBe(true);
    expect(controller.errorMessage).toBeNull();
  });

  it("reports a cancelled operation as a neutral outcome", async () => {
    const deps = new FakeDeps();
    const controller = new BackupOperationController("agent-11", deps);
    deps.postResponses.push({ status: 202, json: null });
    controller.dispatch("/x", {}, { label: "Updating...", isCancellable: true });
    await settle();

    deps.getResponses.push({ status: "CANCELLED", kind: "backup_update" });
    deps.runScheduled();
    await settle();

    expect(controller.cancelledMessage).toBe("Update cancelled. Nothing was changed.");
    expect(controller.errorMessage).toBeNull();
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

    for (let attempt = 0; attempt < MAX_CONSECUTIVE_POLL_FAILURES; attempt += 1) {
      deps.runScheduled();
      await settle();
    }

    expect(model.status).toBe("failed");
    expect(deps.scheduled).toHaveLength(0);
  });

  it("shows the failed state and lets a retry restart the flow", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.getResponses.push({ status: "FAILED" });
    model.start();
    await settle();
    expect(model.status).toBe("failed");

    deps.postResponses.push({ status: 202, json: null });
    deps.getResponses.push({ status: "RUNNING" });
    await model.retry();
    expect(model.status).toBe("running");
    expect(model.logText).toBe("");
    expect(model.retryErrorMessage).toBeNull();
  });

  it("surfaces a refused retry instead of silently staying failed", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.getResponses.push({ status: "FAILED" });
    model.start();
    await settle();

    deps.postResponses.push({ status: 409, json: { error: "another destroy is already running" } });
    await model.retry();
    expect(model.status).toBe("failed");
    expect(model.retryErrorMessage).toBe("another destroy is already running");

    // A later successful retry clears the message.
    deps.postResponses.push({ status: 202, json: null });
    deps.getResponses.push({ status: "RUNNING" });
    await model.retry();
    expect(model.retryErrorMessage).toBeNull();
    expect(model.status).toBe("running");
  });

  it("reports an unreachable server when the retry post cannot connect", async () => {
    const deps = new FakeDeps();
    const model = new DestroyingModel("agent-22", deps);
    deps.postResponses.push({ status: 0, json: null });

    await model.retry();

    expect(model.retryErrorMessage).toContain("unreachable");
  });
});

describe("RecoveryModel", () => {
  const info = {
    agent_id: "agent-33",
    workspace_name: "my-machine",
    health: "stuck",
    health_error: "",
    is_restart_start_only: null,
    ssh_command: "ssh -p 22 user@host",
    is_host_offline: false,
  };

  it("loads recovery info and dispatches a manual host restart to success", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("host-abc", deps);
    deps.getResponses.push(info);
    await model.load();
    expect(model.info?.workspace_name).toBe("my-machine");
    expect(model.agentId).toBe("agent-33");

    deps.postResponses.push({ status: 202, json: { operation_id: "agent-33", kind: "restart" } });
    await model.dispatchRestart();
    expect(model.isRestartRunning).toBe(true);
    expect(deps.postCalls[0].url).toContain("/api/v1/workspaces/agent-33/restart");
    expect(deps.postCalls[0].body).toEqual({ scope: "host" });

    deps.getResponses.push({ status: "DONE", is_done: true });
    deps.runScheduled();
    await settle();
    expect(model.isRestartRunning).toBe(false);
    expect(model.isRestartSucceeded).toBe(true);
  });

  it("surfaces a rejected restart dispatch and a failed restart operation", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.getResponses.push(info);
    await model.load();

    deps.postResponses.push({ status: 409, json: { error: "another operation is running" } });
    await model.dispatchRestart();
    expect(model.isRestartRunning).toBe(false);
    expect(model.restartError).toBe("another operation is running");

    deps.postResponses.push({ status: 202, json: null });
    await model.dispatchRestart();
    deps.getResponses.push({ status: "FAILED", is_done: false, error: "host did not come back" });
    deps.runScheduled();
    await settle();
    expect(model.restartError).toBe("host did not come back");
  });

  it("bounds consecutive failed restart-status polls like the sibling pollers", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.getResponses.push(info);
    await model.load();
    deps.postResponses.push({ status: 202, json: null });
    await model.dispatchRestart();

    // Every status poll answers null (no queued responses): the model keeps
    // polling until the bound, then surfaces a lost-contact error instead of
    // pinning "Restarting..." forever.
    for (let attempt = 0; attempt < MAX_CONSECUTIVE_POLL_FAILURES; attempt += 1) {
      expect(model.isRestartRunning).toBe(true);
      deps.runScheduled();
      await settle();
    }

    expect(model.isRestartRunning).toBe(false);
    expect(model.restartError).toContain("Lost contact with the restart");
    expect(deps.scheduled).toHaveLength(0);
  });

  it("reattaches to an in-flight restart when the page loads mid-restart", async () => {
    const deps = new FakeDeps();
    const model = new RecoveryModel("agent-33", deps);
    deps.getResponses.push({ ...info, health: "restarting" });
    await model.load();
    expect(model.isRestartRunning).toBe(true);
    expect(deps.sources).toHaveLength(1);
  });
});
