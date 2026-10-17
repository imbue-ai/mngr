import { describe, expect, it } from "vitest";
import type { EventSourceLike, LifecycleDeps } from "./backups";
import { ConnectionOperationWatcher, connectionOperationStatusUrl } from "./connectionOperation";
import type { CloudAccountOption } from "./create";

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
  sources: FakeEventSource[] = [];
  scheduled: Array<() => void> = [];
  redrawCount = 0;

  async getJson(url: string): Promise<unknown | null> {
    this.getUrls.push(url);
    return this.getResponses.length > 0 ? this.getResponses.shift()! : null;
  }

  async postJson(): Promise<{ status: number; json: unknown | null }> {
    throw new Error("not used");
  }

  async deleteResource(): Promise<number> {
    throw new Error("not used");
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

  async runScheduled(): Promise<void> {
    const pending = this.scheduled.splice(0);
    for (const callback of pending) callback();
    await Promise.resolve();
    await Promise.resolve();
  }
}

const ACCOUNT: CloudAccountOption = {
  name: "aws_" + "a".repeat(32),
  alias: "team aws",
  backend: "aws",
  region: "us-west-2",
  owner_user_id: "user-1",
  is_region_pinned: true,
};

interface Recorded {
  logs: string[];
  done: CloudAccountOption[];
  failed: Record<string, unknown>[];
}

function watch(deps: FakeDeps): { watcher: ConnectionOperationWatcher; recorded: Recorded } {
  const recorded: Recorded = { logs: [], done: [], failed: [] };
  const watcher = new ConnectionOperationWatcher(deps, ACCOUNT.name, {
    onLog: (line) => recorded.logs.push(line),
    onDone: (account) => recorded.done.push(account),
    onFailed: (failure) => recorded.failed.push(failure),
  });
  return { watcher, recorded };
}

describe("ConnectionOperationWatcher", () => {
  it("streams the prepare's output, keeps polling while running, and hands over the account when done", async () => {
    const deps = new FakeDeps();
    deps.getResponses = [{ status: "RUNNING", is_done: false }, { status: "DONE", is_done: true, account: ACCOUNT }];
    const { watcher, recorded } = watch(deps);

    watcher.start();
    await deps.runScheduled();
    expect(deps.getUrls).toEqual([connectionOperationStatusUrl(ACCOUNT.name)]);
    const [source] = deps.sources;
    source.emit({ log: "Created security group mngr-aws" });
    source.emit({ log: "Prepared AWS security group" });
    expect(recorded.logs).toEqual(["Created security group mngr-aws", "Prepared AWS security group"]);
    expect(deps.redrawCount).toBeGreaterThan(0);

    // Still running: the next poll was scheduled, not fired.
    expect(recorded.done).toEqual([]);
    expect(deps.scheduled.length).toBe(1);
    await deps.runScheduled();

    expect(recorded.done).toEqual([ACCOUNT]);
    expect(recorded.failed).toEqual([]);
    expect(source.isClosed).toBe(true);
    expect(deps.scheduled.length).toBe(0);
  });

  it("reports a failed operation with the error and code the synchronous route used to answer", async () => {
    const deps = new FakeDeps();
    deps.getResponses = [
      { status: "FAILED", is_done: false, error: "Account setup failed: bad key", code: "credentials_rejected" },
    ];
    const { watcher, recorded } = watch(deps);

    watcher.start();
    await deps.runScheduled();

    expect(recorded.failed).toEqual([{ error: "Account setup failed: bad key", code: "credentials_rejected" }]);
    expect(recorded.done).toEqual([]);
    expect(deps.sources[0].isClosed).toBe(true);
  });

  it("keeps polling through a transient read failure and stops cleanly", async () => {
    const deps = new FakeDeps();
    deps.getResponses = [null];
    const { watcher, recorded } = watch(deps);

    watcher.start();
    await deps.runScheduled();
    expect(deps.scheduled.length).toBe(1);

    watcher.stop();
    await deps.runScheduled();
    expect(deps.getUrls.length).toBe(1);
    expect(recorded.done).toEqual([]);
    expect(deps.sources[0].isClosed).toBe(true);
  });
});
