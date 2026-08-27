// Lifecycle-surface models (tranche T4): backup history + tracked backup
// operations, the destroy detail driver, recently-destroyed rows, and the
// recovery driver.
//
// This is the consolidation of three overlapping legacy modules
// (backup_operation_ui.js, backup_table.js, workspace_backup_history.js /
// workspace_backups.js): ONE operation controller drives the tracked
// per-workspace backup operation wherever it is observed from, and the
// history model is a plain paginated fetch. Status/log polling stays on the
// existing /api/v1 operation resources; the transient op-log streams remain
// SSE by design (they die with the operation).
//
// Everything network- or timer-shaped is injected so vitest drives the state
// machines synchronously; pages construct with the browser defaults.

import type { EnvironmentCondition } from "./health";

export const BACKUP_HISTORY_PAGE_SIZE = 15;

const OPERATION_POLL_INTERVAL_MS = 2000;
const DESTROY_POLL_INTERVAL_MS = 1000;
// LifecycleDeps.getJson maps both non-2xx and network failure to null, so a
// permanently broken status endpoint (e.g. a 404) would otherwise keep the
// "Working..." state alive forever. Bound the run of consecutive nulls
// (~5 minutes at the 2s operation cadence) and then declare the poll lost.
export const MAX_CONSECUTIVE_POLL_FAILURES = 150;

export interface BackupSnapshot {
  snapshot_id: string;
  time: string;
  tags?: string[];
}

export interface EventSourceLike {
  close(): void;
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export interface LifecycleDeps {
  /** GET returning parsed JSON, or null on any failure/non-2xx. */
  getJson(url: string): Promise<unknown | null>;
  /** POST returning {status, json} (json null when unparseable). */
  postJson(url: string, body: unknown): Promise<{ status: number; json: unknown | null }>;
  /** DELETE returning the HTTP status. */
  deleteResource(url: string): Promise<number>;
  openEventSource(url: string): EventSourceLike;
  schedule(callback: () => void, delayMs: number): void;
  redraw(): void;
}

export function formatRelativeAgo(iso: string, nowMs: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} ${minutes === 1 ? "min" : "mins"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} ${days === 1 ? "day" : "days"} ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} ${months === 1 ? "month" : "months"} ago`;
  const years = Math.floor(months / 12);
  return `${years} ${years === 1 ? "year" : "years"} ago`;
}

/** The "Restored from <time>" row label, or null when the snapshot is not a restore result. */
export function restoredFromLabel(tags: readonly string[] | undefined): string | null {
  const allTags = tags ?? [];
  if (!allTags.includes("restored")) return null;
  const lineage = allTags.find((tag) => tag.startsWith("restored-from:"));
  if (lineage === undefined) return "Restored";
  return `Restored from ${new Date(lineage.slice("restored-from:".length)).toLocaleString()}`;
}

// The worker words these failures distinctively (see backup_update.py); the
// failure-specific retry buttons key on that wording. Keep in sync.
export function isSafetySnapshotFailure(message: string | null): boolean {
  return (message ?? "").includes("pre-restore safety snapshot failed");
}

export function isChatGateFailure(message: string | null): boolean {
  const text = message ?? "";
  return text.includes("cannot determine running chats") || text.includes("Could not probe the machine");
}

export interface RestoreOptions {
  stopChats?: boolean;
  updateAfter?: boolean;
  skipSafetySnapshot?: boolean;
  skipChatGate?: boolean;
}

interface OperationStatusPayload {
  status?: string;
  kind?: string;
  is_done?: boolean;
  is_cancellable?: boolean;
  error?: string | null;
  warning?: string | null;
  blocked_chats?: string[];
  snapshot_id?: string | null;
}

const OPERATION_SUCCESS_MESSAGES: Record<string, string> = {
  backup_restore:
    "The restore completed successfully. A safety backup of your previous state was saved first.",
  backup_update: "The backup software update completed successfully.",
  backup_configure: "Your backup settings were updated.",
};

const OPERATION_CANCELLED_MESSAGES: Record<string, string> = {
  backup_restore: "Restore cancelled. Nothing was changed.",
  backup_update: "Update cancelled. Nothing was changed.",
};

const OPERATION_RUNNING_LABELS: Record<string, string> = {
  backup_update: "Updating backup software...",
  backup_configure: "Changing backup settings...",
};

/**
 * Drives the ONE tracked backup operation a workspace can run (update /
 * configure / restore): dispatch, log stream, status polling, cancel, and
 * the failure-specific retries. A restore reports on its snapshot row
 * (isRestore + restoringSnapshotId); everything else reports on the strip.
 */
export class BackupOperationController {
  readonly agentId: string;
  isRunning = false;
  isRestore = false;
  restoringSnapshotId: string | null = null;
  isCancellable = false;
  runningLabel = "";
  progressLine: string | null = null;
  logLines: string[] = [];
  errorMessage: string | null = null;
  warningMessage: string | null = null;
  successMessage: string | null = null;
  cancelledMessage: string | null = null;
  isStopChatsRetryOffered = false;
  isSkipSafetyRetryOffered = false;
  isForceRetryOffered = false;
  /** Page hook: refresh data after a DONE operation (e.g. reload the table). */
  onSuccess: (() => void) | null = null;
  /** Page hook: disable page actions while an operation runs. */
  onRunningChange: ((isRunning: boolean) => void) | null = null;

  private readonly deps: LifecycleDeps;
  private logSource: EventSourceLike | null = null;
  private consecutivePollFailures = 0;
  private pendingSuccessMessage: string | null = null;
  private retryWithStopChats: (() => void) | null = null;
  private retrySkipSafety: (() => void) | null = null;
  private retryForce: (() => void) | null = null;
  private isStopped = false;

  constructor(agentId: string, deps: LifecycleDeps) {
    this.agentId = agentId;
    this.deps = deps;
  }

  /** Tear down the log stream when the page unmounts. */
  stop(): void {
    this.isStopped = true;
    this.closeLogSource();
  }

  successMessageFor(kind: string): string {
    return OPERATION_SUCCESS_MESSAGES[kind] ?? "The operation completed successfully.";
  }

  startRestore(snapshot: BackupSnapshot, timeText: string, options: RestoreOptions): void {
    const body = {
      stop_chats: options.stopChats === true,
      update_after: options.updateAfter !== false,
      skip_safety_snapshot: options.skipSafetySnapshot === true,
      skip_chat_gate: options.skipChatGate === true,
    };
    this.dispatch(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backups/${encodeURIComponent(snapshot.snapshot_id)}/restore`,
      body,
      {
        isRestore: true,
        snapshotId: snapshot.snapshot_id,
        isCancellable: true,
        successMessage: timeText
          ? `Machine restored to the backup from ${timeText}. A safety backup of your previous state was saved first.`
          : OPERATION_SUCCESS_MESSAGES.backup_restore,
        retryWithStopChats: () => this.startRestore(snapshot, timeText, { ...options, stopChats: true }),
        retrySkipSafety: () => this.startRestore(snapshot, timeText, { ...options, skipSafetySnapshot: true }),
        retryForce: () => this.startRestore(snapshot, timeText, { ...options, skipChatGate: true }),
      },
    );
  }

  runStopChatsRetry(): void {
    this.isStopChatsRetryOffered = false;
    this.retryWithStopChats?.();
  }

  runSkipSafetyRetry(): void {
    this.isSkipSafetyRetryOffered = false;
    this.retrySkipSafety?.();
  }

  runForceRetry(): void {
    this.isForceRetryOffered = false;
    this.retryForce?.();
  }

  /** Attach to an operation started elsewhere (another window / a reload). */
  async reattach(): Promise<void> {
    if (this.isRunning || this.isStopped) return;
    const payload = (await this.deps.getJson(this.operationUrl())) as OperationStatusPayload | null;
    if (this.isRunning || this.isStopped) return;
    if (payload === null || payload.status !== "RUNNING") return;
    // This page did not dispatch the running operation: no retry closures,
    // generic success wording.
    this.pendingSuccessMessage = null;
    this.retryWithStopChats = null;
    this.retrySkipSafety = null;
    this.retryForce = null;
    this.isRestore = payload.kind === "backup_restore";
    this.restoringSnapshotId = payload.snapshot_id ?? null;
    this.setRunning(true, payload.is_cancellable === true, OPERATION_RUNNING_LABELS[payload.kind ?? ""] ?? "Working...");
    this.consecutivePollFailures = 0;
    this.streamLogs();
    this.pollSoon();
  }

  async requestCancel(): Promise<void> {
    const result = await this.deps.postJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/update/cancel`,
      {},
    );
    if (result.status >= 400) {
      const detail = result.json as { error?: string; message?: string } | null;
      this.showError(detail?.error ?? detail?.message ?? "Could not cancel the operation.");
    }
    this.deps.redraw();
  }

  dispatch(
    url: string,
    body: unknown,
    opts: {
      isRestore?: boolean;
      snapshotId?: string;
      isCancellable?: boolean;
      label?: string;
      successMessage?: string;
      retryWithStopChats?: () => void;
      retrySkipSafety?: () => void;
      retryForce?: () => void;
    },
  ): void {
    this.clearTerminalNotices();
    this.isStopChatsRetryOffered = false;
    this.isSkipSafetyRetryOffered = false;
    this.isForceRetryOffered = false;
    this.isRestore = opts.isRestore === true;
    this.restoringSnapshotId = opts.snapshotId ?? null;
    this.pendingSuccessMessage = opts.successMessage ?? null;
    this.retryWithStopChats = opts.retryWithStopChats ?? null;
    this.retrySkipSafety = opts.retrySkipSafety ?? null;
    this.retryForce = opts.retryForce ?? null;
    this.setRunning(true, opts.isCancellable === true, opts.label ?? "Working...");
    this.consecutivePollFailures = 0;
    void this.deps.postJson(url, body).then((result) => {
      if (result.status === 202) {
        this.streamLogs();
        this.pollSoon();
        return;
      }
      this.setRunning(false, false, "");
      const detail = result.json as { error?: string; message?: string } | null;
      this.showError(detail?.error ?? detail?.message ?? `Request failed (HTTP ${result.status})`);
      this.deps.redraw();
    });
  }

  private operationUrl(): string {
    return `/api/v1/workspaces/operations/backup/${encodeURIComponent(this.agentId)}`;
  }

  private pollSoon(): void {
    this.deps.schedule(() => void this.pollOnce(), OPERATION_POLL_INTERVAL_MS);
  }

  /** One status-poll tick; reschedules itself while the operation runs. */
  async pollOnce(): Promise<void> {
    if (this.isStopped) return;
    const payload = (await this.deps.getJson(this.operationUrl())) as OperationStatusPayload | null;
    if (this.isStopped) return;
    if (payload === null) {
      // Transient fetch failure: keep polling rather than ending the
      // working state under a still-running backend operation -- but only
      // for a bounded run, or a permanent failure pins "Working..." forever.
      this.consecutivePollFailures += 1;
      if (this.consecutivePollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        this.setRunning(false, false, "");
        this.showError(
          "Lost contact with the backup operation; its status could not be read for several minutes. " +
            "Reload this page to check whether it finished.",
        );
        this.deps.redraw();
        return;
      }
      this.pollSoon();
      return;
    }
    this.consecutivePollFailures = 0;
    if (payload.status === "RUNNING") {
      this.isCancellable = payload.is_cancellable === true;
      this.deps.redraw();
      this.pollSoon();
      return;
    }
    this.setRunning(false, false, "");
    if (payload.status === "CANCELLED") {
      this.cancelledMessage =
        OPERATION_CANCELLED_MESSAGES[payload.kind ?? ""] ?? "The operation was cancelled. Nothing was changed.";
      this.deps.redraw();
      return;
    }
    if (payload.is_done === true) {
      this.successMessage =
        this.pendingSuccessMessage ?? this.successMessageFor(payload.kind ?? "");
      this.warningMessage = payload.warning ?? null;
      this.onSuccess?.();
      this.deps.redraw();
      return;
    }
    if (payload.blocked_chats !== undefined && payload.blocked_chats.length > 0) {
      this.showError(
        `Chats are running in this machine (${payload.blocked_chats.join(", ")}). ` +
          "Stop them before continuing; they resume on your next message.",
      );
      this.isStopChatsRetryOffered = this.retryWithStopChats !== null;
      this.deps.redraw();
      return;
    }
    this.showError(payload.error ?? "The backup operation failed.");
    this.isSkipSafetyRetryOffered = this.retrySkipSafety !== null && isSafetySnapshotFailure(payload.error ?? null);
    this.isForceRetryOffered = this.retryForce !== null && isChatGateFailure(payload.error ?? null);
    this.deps.redraw();
  }

  private streamLogs(): void {
    this.logLines = [];
    this.progressLine = null;
    this.closeLogSource();
    const source = this.deps.openEventSource(
      `/api/v1/workspaces/operations/backup/${encodeURIComponent(this.agentId)}/logs`,
    );
    this.logSource = source;
    source.onmessage = (event) => {
      let frame: { log?: string; done?: boolean };
      try {
        frame = JSON.parse(event.data) as { log?: string; done?: boolean };
      } catch {
        return; // keepalive frames etc.
      }
      if (frame.log !== undefined) {
        this.logLines.push(frame.log);
        if (!this.isRestore) this.progressLine = frame.log;
        this.deps.redraw();
      }
      if (frame.done === true) this.closeLogSource();
    };
    source.onerror = () => this.closeLogSource();
  }

  private closeLogSource(): void {
    this.logSource?.close();
    this.logSource = null;
  }

  private setRunning(isRunning: boolean, isCancellable: boolean, label: string): void {
    this.isRunning = isRunning;
    this.isCancellable = isRunning && isCancellable;
    this.runningLabel = label;
    if (!isRunning) {
      this.isRestore = false;
      this.restoringSnapshotId = null;
      this.progressLine = null;
    }
    this.onRunningChange?.(isRunning);
    this.deps.redraw();
  }

  private clearTerminalNotices(): void {
    this.errorMessage = null;
    this.warningMessage = null;
    this.successMessage = null;
    this.cancelledMessage = null;
  }

  private showError(message: string): void {
    this.clearTerminalNotices();
    this.errorMessage = message;
  }
}

interface BackupsListingPayload {
  is_configured?: boolean;
  snapshots?: BackupSnapshot[];
  snapshots_total?: number;
  snapshots_error?: string | null;
}

/** The paginated snapshot-history table (newest first, server-side paging). */
export class BackupHistoryModel {
  readonly agentId: string;
  offset = 0;
  total = 0;
  snapshots: BackupSnapshot[] = [];
  /** Human status line replacing the table (loading / empty / error states). */
  statusMessage: string | null = "Loading backup history...";
  /** The backup-check verdict gating Restore (OFFLINE disables it). */
  checkState: string | null = null;

  private readonly deps: LifecycleDeps;

  constructor(agentId: string, deps: LifecycleDeps) {
    this.agentId = agentId;
    this.deps = deps;
  }

  get rangeText(): string {
    const first = this.offset + 1;
    const last = this.offset + this.snapshots.length;
    return `Showing ${first}-${last} of ${this.total} backups`;
  }

  get isPaginationShown(): boolean {
    return this.total > BACKUP_HISTORY_PAGE_SIZE;
  }

  get canGoNewer(): boolean {
    return this.offset > 0;
  }

  get canGoOlder(): boolean {
    return this.offset + this.snapshots.length < this.total;
  }

  isRestoreDisabledByCheck(): boolean {
    return this.checkState === "OFFLINE";
  }

  async loadPage(): Promise<void> {
    this.statusMessage = "Loading backup history...";
    this.deps.redraw();
    const url =
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backups` +
      `?limit=${BACKUP_HISTORY_PAGE_SIZE}&offset=${this.offset}`;
    const payload = (await this.deps.getJson(url)) as BackupsListingPayload | null;
    if (payload === null) {
      this.statusMessage = "Could not load backup history.";
      this.deps.redraw();
      return;
    }
    if (payload.is_configured !== true) {
      this.statusMessage = "Backups are turned off for this machine.";
      this.deps.redraw();
      return;
    }
    if (payload.snapshots_error) {
      this.statusMessage = "Couldn't load your backup history right now.";
      this.deps.redraw();
      return;
    }
    this.snapshots = payload.snapshots ?? [];
    // Fall back to the returned rows if the count is absent (older backend)
    // so a present page never collapses to the empty state.
    this.total = typeof payload.snapshots_total === "number" ? payload.snapshots_total : this.offset + this.snapshots.length;
    this.statusMessage = this.total === 0 ? "No backups yet. The first backup runs within the hour." : null;
    this.deps.redraw();
  }

  async fetchCheckState(): Promise<void> {
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-check`,
    )) as { check_state?: string } | null;
    if (payload?.check_state !== undefined) {
      this.checkState = payload.check_state;
      this.deps.redraw();
    }
  }

  goNewer(): void {
    this.offset = Math.max(0, this.offset - BACKUP_HISTORY_PAGE_SIZE);
    void this.loadPage();
  }

  goOlder(): void {
    this.offset += BACKUP_HISTORY_PAGE_SIZE;
    void this.loadPage();
  }
}

export interface DestroyedWorkspaceRow {
  agent_id: string;
  display_name: string;
  account_label: string;
  destroyed_at_display: string;
  days_left_display: string;
  has_backup: boolean;
  can_download: boolean;
  is_locked: boolean;
  can_delete: boolean;
  delete_hint: string;
}

interface DestroyedWorkspacesPayload {
  retention_days: number;
  rows: DestroyedWorkspaceRow[];
}

/** The recently-destroyed page: rows + retention header + delete/download actions. */
export class DestroyedWorkspacesModel {
  rows: DestroyedWorkspaceRow[] = [];
  retentionDays = 0;
  isLoaded = false;
  errorMessage: string | null = null;
  /** The row whose Remove has been armed (inline confirm), if any. */
  armedDeleteAgentId: string | null = null;
  downloadingAgentIds = new Set<string>();
  deletingAgentIds = new Set<string>();

  private readonly deps: LifecycleDeps;

  constructor(deps: LifecycleDeps) {
    this.deps = deps;
  }

  async load(): Promise<void> {
    const payload = (await this.deps.getJson("/ui/api/destroyed-workspaces")) as DestroyedWorkspacesPayload | null;
    if (payload === null) {
      this.errorMessage = "Could not load recently destroyed machines.";
      this.isLoaded = true;
      this.deps.redraw();
      return;
    }
    this.rows = payload.rows;
    this.retentionDays = payload.retention_days;
    this.isLoaded = true;
    this.deps.redraw();
  }

  async deleteBackup(agentId: string): Promise<void> {
    this.armedDeleteAgentId = null;
    this.deletingAgentIds.add(agentId);
    this.deps.redraw();
    const result = await this.deps.postJson(
      `/ui/api/destroyed-workspaces/${encodeURIComponent(agentId)}/delete-backup`,
      {},
    );
    this.deletingAgentIds.delete(agentId);
    if (result.status >= 400) {
      const detail = result.json as { error?: string } | null;
      this.errorMessage = detail?.error ?? "Could not delete the backup; see the logs and try again.";
      this.deps.redraw();
      return;
    }
    await this.load();
  }
}

export type DestroyStatus = "running" | "failed" | "done";

/** The destroy detail page driver: status poll + log tail + retry/dismiss. */
export class DestroyingModel {
  readonly agentId: string;
  status: DestroyStatus = "running";
  logText = "";
  /** User-visible reason the last retry dispatch was refused, if any. */
  retryErrorMessage: string | null = null;
  /** Set once the destroy reports done; the page routes home. */
  onDone: (() => void) | null = null;

  private readonly deps: LifecycleDeps;
  private source: EventSourceLike | null = null;
  private consecutivePollFailures = 0;
  private isStopped = false;

  constructor(agentId: string, deps: LifecycleDeps) {
    this.agentId = agentId;
    this.deps = deps;
  }

  start(): void {
    this.isStopped = false;
    this.consecutivePollFailures = 0;
    this.openLogSource();
    void this.pollOnce();
  }

  stop(): void {
    this.isStopped = true;
    this.closeSource();
  }

  async retry(): Promise<void> {
    this.retryErrorMessage = null;
    const result = await this.deps.postJson(`/api/v1/workspaces/${encodeURIComponent(this.agentId)}/destroy`, {});
    // Status 0 is the browser deps' network-failure sentinel.
    if (result.status >= 400 || result.status === 0) {
      const detail = result.json as { error?: string; message?: string } | null;
      this.retryErrorMessage =
        detail?.error ??
        detail?.message ??
        (result.status === 0
          ? "Could not restart the destroy: the app server is unreachable."
          : `Could not restart the destroy (HTTP ${result.status}).`);
      this.deps.redraw();
      return;
    }
    this.logText = "";
    this.status = "running";
    this.deps.redraw();
    this.start();
  }

  async dismiss(): Promise<void> {
    await this.deps.deleteResource(`/api/v1/workspaces/operations/destroy/${encodeURIComponent(this.agentId)}`);
    this.onDone?.();
  }

  async pollOnce(): Promise<void> {
    if (this.isStopped) return;
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/operations/destroy/${encodeURIComponent(this.agentId)}`,
    )) as { status?: string } | null;
    if (this.isStopped) return;
    if (payload?.status !== undefined) {
      this.consecutivePollFailures = 0;
      this.applyStatus(payload.status.toLowerCase());
    } else {
      // Unreadable status: tolerate a bounded run of failures, then mark
      // the destroy failed rather than showing "running" forever.
      this.consecutivePollFailures += 1;
      if (this.consecutivePollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        this.applyStatus("failed");
        return;
      }
    }
    if (this.status === "running") {
      this.deps.schedule(() => void this.pollOnce(), DESTROY_POLL_INTERVAL_MS);
    }
  }

  applyStatus(status: string): void {
    if (this.isStopped && status !== "done") return;
    if (status !== "running" && status !== "failed" && status !== "done") return;
    if (status === this.status) return;
    this.status = status;
    this.deps.redraw();
    if (status === "done") {
      this.closeSource();
      this.isStopped = true;
      this.onDone?.();
    } else if (status === "failed") {
      this.closeSource();
    }
  }

  private openLogSource(): void {
    this.closeSource();
    const source = this.deps.openEventSource(
      `/api/v1/workspaces/operations/destroy/${encodeURIComponent(this.agentId)}/logs`,
    );
    this.source = source;
    source.onmessage = (event) => {
      let frame: { log?: string; done?: boolean; status?: string };
      try {
        frame = JSON.parse(event.data) as { log?: string; done?: boolean; status?: string };
      } catch {
        return;
      }
      if (frame.log !== undefined) {
        this.logText += frame.log;
        this.deps.redraw();
      }
      if (frame.done === true) {
        this.closeSource();
        if (frame.status !== undefined) this.applyStatus(frame.status.toLowerCase());
      }
    };
    source.onerror = () => this.closeSource();
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }
}

export interface RecoveryInfo {
  agent_id: string;
  workspace_name: string;
  health: string;
  health_error: string;
  /** Whether an in-flight restart skips the stop step. A full stop+start bounce
   * is only ever dispatched by the user's own click, so `false` is what makes
   * "Restarting" an honest claim rather than a guess. Null outside a restart. */
  is_restart_start_only: boolean | null;
  ssh_command: string;
  is_host_offline: boolean;
  device_environment: EnvironmentCondition;
  is_backend_unreachable: boolean;
  provider_label: string;
  unreachable_reason: string;
  is_device_cannot_connect: boolean;
  device_error_detail: string;
}

interface RestartStatusPayload {
  status?: string;
  is_done?: boolean;
  error?: string | null;
}

/**
 * The recovery card's driver: follow the machine's recovery state, dispatch a
 * manual restart, and follow that restart (status + log stream) to a terminal
 * state. Never navigates on its own -- the surfaces offer links.
 *
 * The state is re-read for as long as a card is mounted, not sampled once when
 * it opened. A recovery episode is exactly when the app's picture of a machine
 * is changing: discovery re-observes the host, a provider error lands or
 * clears, something else restarts it. A card that had frozen its first reading
 * would keep describing a condition that had already been superseded, and go on
 * offering the action that suited it.
 */
export class RecoveryModel {
  readonly workspaceAnyId: string;
  info: RecoveryInfo | null = null;
  loadError: string | null = null;
  isRestartRunning = false;
  /** The shape of a restart *this model dispatched*, or null when the restart
   * it is following was dispatched elsewhere and only attached to. A surface
   * describing a restart in flight needs the difference: its own click is known
   * from the click, while an unattended or other-window dispatch is only ever
   * as good as what the tracker publishes about it. */
  dispatchedRestartIsStartOnly: boolean | null = null;
  restartError: string | null = null;
  isRestartSucceeded = false;
  logLines: string[] = [];

  private readonly deps: LifecycleDeps;
  private logSource: EventSourceLike | null = null;
  private isStopped = false;

  constructor(workspaceAnyId: string, deps: LifecycleDeps) {
    this.workspaceAnyId = workspaceAnyId;
    this.deps = deps;
  }

  get agentId(): string | null {
    return this.info?.agent_id ?? null;
  }

  async load(): Promise<void> {
    const payload = await this.fetchInfo();
    // A card removed while its first read was in flight adopts nothing and arms
    // nothing. Leaving `info` null is also what keeps the surfaces' own
    // post-load work off a dead model -- the recovery page's ?intent=restart
    // dispatches on this promise, and dispatchRestart no-ops without an agent id.
    if (this.isStopped) return;
    // The poll is armed either way. A first read that fails is no more final
    // than any later one -- it is the local app's own endpoint, mid-episode --
    // and without the poll the surface would hold that error until the window
    // was reloaded, while the machine recovered behind it.
    if (payload === null) {
      this.loadError = "Could not load this machine's recovery state.";
    } else {
      this.applyInfo(payload);
    }
    this.pollInfoSoon();
    this.deps.redraw();
  }

  stop(): void {
    this.isStopped = true;
    this.logSource?.close();
    this.logSource = null;
  }

  private fetchInfo(): Promise<RecoveryInfo | null> {
    return this.deps.getJson(
      `/ui/api/workspaces/${encodeURIComponent(this.workspaceAnyId)}/recovery-info`,
    ) as Promise<RecoveryInfo | null>;
  }

  /**
   * Adopt a reading of the machine's state.
   *
   * Only ``info`` is written: the restart fields belong to a restart this model
   * is running and are owned by ``dispatchRestart`` / ``pollRestartOnce``, which
   * follow the operation itself rather than the tracker's summary of it.
   *
   * A restart this model is not already running is attached to, so a restart
   * dispatched elsewhere -- the unattended one, or the same machine's card in
   * another window -- shows its progress and its logs here too. Attaching
   * clears the previous restart's outcome, which describes a different run.
   * Readings taken before a restart this model was following reported its
   * outcome never get here (``pollInfoOnce`` drops them), so an outcome is
   * only ever cleared by a restart that started after it.
   *
   * The one restart never attached to is the one this model has already given
   * up following (``pollRestartOnce``'s bound). The tracker goes on reporting
   * it as running -- that is the state whose status could not be read -- so
   * attaching would clear the lost-contact report and start the bound over,
   * every poll, for as long as the tracker says so. The tracker leaving
   * "restarting" ends that run, and the next one is a different restart.
   */
  private applyInfo(payload: RecoveryInfo): void {
    this.info = payload;
    if (payload.health !== "restarting") {
      this.isRestartFollowAbandoned = false;
      return;
    }
    if (this.isRestartRunning || this.isRestartFollowAbandoned || this.isStopped) return;
    this.isRestartRunning = true;
    // Attached, not dispatched: this model has no first-hand account of what
    // was run, so a surface must take the tracker's word for its shape.
    this.dispatchedRestartIsStartOnly = null;
    this.isRestartSucceeded = false;
    this.restartError = null;
    this.logLines = [];
    this.consecutiveRestartPollFailures = 0;
    this.streamRestartLogs(payload.agent_id);
    this.pollRestartSoon(payload.agent_id);
  }

  private pollInfoSoon(): void {
    this.deps.schedule(() => void this.pollInfoOnce(), OPERATION_POLL_INTERVAL_MS);
  }

  async pollInfoOnce(): Promise<void> {
    if (this.isStopped) return;
    const outcomeCount = this.restartOutcomeCount;
    const payload = await this.fetchInfo();
    if (this.isStopped) return;
    // A failed read is not news about the machine. The last good reading stays
    // on screen -- replacing a described condition with a fetch error would
    // tell the user less than they already had -- and the poll keeps running,
    // unbounded on purpose: this loop lives only as long as the card is open,
    // and the endpoint it reads is the local app's own.
    //
    // A reading that was in flight while the restart being followed reported
    // its outcome is dropped whole: the server moves the tracker out of
    // "restarting" before the operation reports done, so such a reading still
    // says "restarting" and would re-report a restart that has finished.
    if (payload !== null && outcomeCount === this.restartOutcomeCount) {
      this.loadError = null;
      this.applyInfo(payload);
      this.deps.redraw();
    }
    this.pollInfoSoon();
  }

  private consecutiveRestartPollFailures = 0;
  /** How many restarts this model was following have reported an outcome.
   * Stamped on each recovery-info read so a late one can be told apart from a
   * current one. */
  private restartOutcomeCount = 0;
  /** Whether the restart the tracker is still reporting is one this model has
   * stopped following. Cleared by the tracker leaving "restarting", and by a
   * restart dispatched from here. */
  private isRestartFollowAbandoned = false;

  /**
   * Dispatch a host restart for this machine and follow it.
   *
   * ``isStartOnly`` skips the stop step, running only the idempotent ``mngr
   * start``: the "open this stopped machine" click-through, which has nothing
   * to bounce. The card's own Restart Machine button keeps the stop, since it
   * may be aimed at a running-but-wedged container that only a bounce fixes.
   */
  async dispatchRestart(isStartOnly = false): Promise<void> {
    const agentId = this.agentId;
    if (agentId === null || this.isRestartRunning) return;
    this.isRestartRunning = true;
    this.dispatchedRestartIsStartOnly = isStartOnly;
    this.restartError = null;
    this.isRestartSucceeded = false;
    this.consecutiveRestartPollFailures = 0;
    this.isRestartFollowAbandoned = false;
    this.logLines = [];
    this.deps.redraw();
    const result = await this.deps.postJson(`/api/v1/workspaces/${encodeURIComponent(agentId)}/restart`, {
      scope: "host",
      start_only: isStartOnly,
    });
    // The card may have gone away while the POST was in flight; a stopped model
    // must not open a log stream nothing will ever close.
    if (this.isStopped) return;
    if (result.status >= 400) {
      this.isRestartRunning = false;
      const detail = result.json as { error?: string } | null;
      this.restartError = detail?.error ?? `Could not start the restart (HTTP ${result.status}).`;
      this.deps.redraw();
      return;
    }
    this.streamRestartLogs(agentId);
    this.pollRestartSoon(agentId);
  }

  private pollRestartSoon(agentId: string): void {
    this.deps.schedule(() => void this.pollRestartOnce(agentId), OPERATION_POLL_INTERVAL_MS);
  }

  async pollRestartOnce(agentId: string): Promise<void> {
    if (this.isStopped) return;
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/operations/restart/${encodeURIComponent(agentId)}`,
    )) as RestartStatusPayload | null;
    if (this.isStopped) return;
    if (payload === null) {
      // Transient fetch failure: keep polling for a bounded run, exactly like
      // the backup/lifecycle pollers -- a permanent failure must not pin
      // "Restarting..." forever.
      this.consecutiveRestartPollFailures += 1;
      if (this.consecutiveRestartPollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        this.isRestartRunning = false;
        // The tracker still calls this restart running, and will until it ends.
        // Marking it abandoned is what keeps the recovery-info poll from
        // re-attaching to it and starting this same bounded run over.
        this.isRestartFollowAbandoned = true;
        // No longer "reload to find out": the card's own state poll is a
        // separate loop, still running against the same origin, and it is what
        // reports where the machine ended up.
        this.restartError =
          "Lost contact with the restart; its status could not be read for several minutes. " +
          "The machine's state above is still being checked.";
        this.deps.redraw();
        return;
      }
      this.pollRestartSoon(agentId);
      return;
    }
    this.consecutiveRestartPollFailures = 0;
    if (payload.status === "RUNNING") {
      this.pollRestartSoon(agentId);
      return;
    }
    this.isRestartRunning = false;
    this.restartOutcomeCount += 1;
    if (payload.is_done === true) {
      this.isRestartSucceeded = true;
    } else {
      this.restartError = payload.error ?? "The restart failed.";
    }
    this.deps.redraw();
  }

  private streamRestartLogs(agentId: string): void {
    this.logSource?.close();
    const source = this.deps.openEventSource(
      `/api/v1/workspaces/operations/restart/${encodeURIComponent(agentId)}/logs`,
    );
    this.logSource = source;
    source.onmessage = (event) => {
      let frame: { log?: string; done?: boolean };
      try {
        frame = JSON.parse(event.data) as { log?: string; done?: boolean };
      } catch {
        return;
      }
      if (frame.log !== undefined) {
        this.logLines.push(frame.log);
        this.deps.redraw();
      }
      if (frame.done === true) {
        source.close();
      }
    };
    source.onerror = () => source.close();
  }
}

/** Browser-default dependency wiring (pages use this; tests inject fakes). */
export function browserLifecycleDeps(redraw: () => void): LifecycleDeps {
  return {
    async getJson(url: string): Promise<unknown | null> {
      try {
        const response = await fetch(url, { credentials: "same-origin" });
        if (!response.ok) return null;
        return (await response.json()) as unknown;
      } catch {
        return null;
      }
    },
    async postJson(url: string, body: unknown): Promise<{ status: number; json: unknown | null }> {
      try {
        const response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body ?? {}),
        });
        let parsed: unknown | null = null;
        try {
          parsed = (await response.json()) as unknown;
        } catch {
          parsed = null;
        }
        return { status: response.status, json: parsed };
      } catch {
        return { status: 0, json: null };
      }
    },
    async deleteResource(url: string): Promise<number> {
      try {
        const response = await fetch(url, { method: "DELETE", credentials: "same-origin" });
        return response.status;
      } catch {
        return 0;
      }
    },
    openEventSource(url: string): EventSourceLike {
      // Adapter over the browser EventSource so the model-facing surface
      // stays the minimal string-data shape tests can fake.
      const source = new EventSource(url);
      const wrapper: EventSourceLike = { close: () => source.close(), onmessage: null, onerror: null };
      source.onmessage = (event) => wrapper.onmessage?.({ data: String(event.data) });
      source.onerror = (event) => wrapper.onerror?.(event);
      return wrapper;
    },
    schedule(callback: () => void, delayMs: number): void {
      setTimeout(callback, delayMs);
    },
    redraw,
  };
}

/** Download one snapshot export as a browser file-save (the route is POST-only). */
export async function downloadSnapshotExport(agentId: string, snapshotId: string): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(
      `/api/v1/workspaces/${encodeURIComponent(agentId)}/backups/${encodeURIComponent(snapshotId)}/export`,
      { method: "POST", credentials: "same-origin" },
    );
  } catch {
    return false;
  }
  if (!response.ok) return false;
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const filename = match ? match[1] : `${agentId}-backup.zip`;
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
  return true;
}
