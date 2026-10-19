/**
 * Watcher for a cloud-connection registration or credential update: the add
 * and update routes answer 202 with the connection's instance name as the
 * operation id, and the prepare's output streams through the operation's log
 * while the status route says how it ended. Mirrors the creating page's
 * watcher, over the injectable lifecycle deps the backups model uses so tests
 * can fake the poll and the event source.
 */
import type { CloudAccountOption } from "./create";
import type { EventSourceLike, LifecycleDeps } from "./backups";
import type { OperationProgress } from "./progress";
import { progressOf } from "./progress";

export const CONNECTION_OPERATION_POLL_MS = 1000;

export interface ConnectionOperationStatus {
  status?: string;
  is_done?: boolean;
  account?: CloudAccountOption | null;
  error?: string | null;
  code?: string | null;
  status_code?: number | null;
  progress?: OperationProgress | null;
}

export interface ConnectionOperationHooks {
  onLog(line: string): void;
  onDone(account: CloudAccountOption): void;
  /** The failure body as the synchronous route used to answer it: `error`, and `code` when one applies. */
  onFailed(failure: Record<string, unknown>): void;
  /** The operation's progress as the poll carried it, every poll that carries one. */
  onProgress(progress: OperationProgress): void;
}

export function connectionOperationStatusUrl(operationId: string): string {
  return `/api/v1/desktop/cloud-accounts/operations/${encodeURIComponent(operationId)}`;
}

export class ConnectionOperationWatcher {
  private readonly deps: LifecycleDeps;
  private readonly operationId: string;
  private readonly hooks: ConnectionOperationHooks;
  private source: EventSourceLike | null = null;
  private isStopped = false;

  constructor(deps: LifecycleDeps, operationId: string, hooks: ConnectionOperationHooks) {
    this.deps = deps;
    this.operationId = operationId;
    this.hooks = hooks;
  }

  start(): void {
    this.source = this.deps.openEventSource(`${connectionOperationStatusUrl(this.operationId)}/logs`);
    this.source.onmessage = (event) => {
      let frame: { log?: string; done?: boolean };
      try {
        frame = JSON.parse(event.data) as { log?: string; done?: boolean };
      } catch {
        return;
      }
      if (frame.log !== undefined) {
        this.hooks.onLog(frame.log);
        this.deps.redraw();
      }
      if (frame.done === true) this.closeSource();
    };
    this.source.onerror = () => this.closeSource();
    void this.pollOnce();
  }

  stop(): void {
    this.isStopped = true;
    this.closeSource();
  }

  /** Fold one status read in; ends the watch on a terminal status. */
  applyStatus(data: ConnectionOperationStatus | null): void {
    if (this.isStopped) return;
    if (data === null) {
      this.scheduleNextPoll();
      return;
    }
    const progress = progressOf(data);
    if (progress !== null) this.hooks.onProgress(progress);
    if (data.status === "DONE" && data.account) {
      this.stop();
      this.hooks.onDone(data.account);
    } else if (data.status === "FAILED") {
      this.stop();
      const failure: Record<string, unknown> = { error: data.error ?? "Could not set up the connection." };
      if (data.code) failure.code = data.code;
      this.hooks.onFailed(failure);
    } else {
      this.scheduleNextPoll();
    }
  }

  private scheduleNextPoll(): void {
    this.deps.schedule(() => void this.pollOnce(), CONNECTION_OPERATION_POLL_MS);
  }

  private async pollOnce(): Promise<void> {
    if (this.isStopped) return;
    const data = (await this.deps.getJson(connectionOperationStatusUrl(this.operationId))) as
      | ConnectionOperationStatus
      | null;
    this.applyStatus(data);
    this.deps.redraw();
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }
}
