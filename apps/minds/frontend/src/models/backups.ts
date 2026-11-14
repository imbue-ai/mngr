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

import {
  NO_RESPONSE_SENTENCE,
  errorCodeFromBody,
  errorFromResponse,
  errorFromSentenceResponse,
  fallbackErrorSentence,
  operationKindFromBody,
  readResponseBody,
  responseDetails,
} from "./errors";
import type {
  RefusalNotice,
  RequestOutcome,
  ResponseBody,
  UserFacingError,
} from "./errors";
import type { EnvironmentCondition, RecoveryFailureCause, RecoveryKind } from "./health";
import { isReconnectNeeded, reconnectError } from "./reconnect";
import { destroyDispatchFailureFor } from "./workspaceOptions";
import type { DestroyRefusal } from "./workspaceOptions";

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
  /** POST returning its status, and the body when it failed (null when it
   * succeeded). */
  postJson(url: string, body: unknown): Promise<RequestOutcome>;
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
export function restoredFromLabel(
  tags: readonly string[] | undefined,
): string | null {
  const allTags = tags ?? [];
  if (!allTags.includes("restored")) return null;
  const lineage = allTags.find((tag) => tag.startsWith("restored-from:"));
  if (lineage === undefined) return "Restored";
  return `Restored from ${new Date(lineage.slice("restored-from:".length)).toLocaleString()}`;
}

export interface RestoreOptions {
  stopChats?: boolean;
  updateAfter?: boolean;
  skipSafetySnapshot?: boolean;
  skipChatGate?: boolean;
  /** An archive id from the listing's previous repositories; null restores from current backups. */
  repositoryId?: string | null;
}

/** Snapshots still readable at one of the workspace's previous backup destinations. */
export interface PreviousBackupRepository {
  archive_id: string;
  archived_at: string;
  snapshots?: BackupSnapshot[];
  snapshots_error?: string | null;
}

interface OperationStatusPayload {
  status?: string;
  kind?: string;
  is_done?: boolean;
  is_cancellable?: boolean;
  error?: string | null;
  failure_reason?: string | null;
  warning?: string | null;
  is_update_suggested?: boolean;
  services_down?: string[];
  service_config_error?: string | null;
  blocked_chats?: string[];
  snapshot_id?: string | null;
}

/** How a failed backup operation is told to the user, and what it offers. */
export interface OperationFailure {
  error: UserFacingError;
  /** Off for a sentence that explains itself and asks for no report. */
  isBugReportOffered: boolean;
  /** Whether running the same operation again is offered. */
  isRetryOffered: boolean;
  /** Whether stopping the chats and running it again is offered. */
  isStopChatsRetryOffered: boolean;
  /** Whether restarting Imbue Studio is the remedy. */
  isRestartAppOffered: boolean;
  /** Whether reloading the page, to read what is true now, is the remedy. */
  isReloadOffered: boolean;
}

const UPDATE_FAILED_SENTENCE = fallbackErrorSentence(
  "Couldn't update this workspace's backup software",
);
const CONFIGURE_FAILED_SENTENCE =
  "Imbue Studio couldn't set up backups for this workspace. *Press Save to try again.* " +
  "If this persists, report a bug.";
const CHAT_CHECK_FAILED_SENTENCE =
  "Imbue Studio couldn't check whether any chats are still running in this workspace.";
const RESTORE_INCOMPLETE_SENTENCE =
  "The restore didn't finish. Running it again picks up where it stopped.";
const RESTORE_UNREACHABLE_SENTENCE =
  "Imbue Studio couldn't reach this workspace to start the restore. " +
  "*Make sure the workspace is running, then try again.*";
const DISABLE_FAILED_SENTENCE =
  "Couldn't turn backups off for this workspace. *Press Save to try again.* If this persists, report a bug.";
const RESTIC_PASSWORD_SET_SENTENCE =
  "*Remove `RESTIC_PASSWORD` from these settings*; Imbue Studio sets each workspace's password itself.";
const STORAGE_QUOTA_SENTENCE =
  "Your Imbue Cloud plan has no room for this workspace's backups.";
const OPERATION_IN_PROGRESS_SENTENCE =
  "Another task is already running on this workspace. *Wait for it to finish, then try again.*";

function failure(
  message: string,
  details: string,
  options: {
    isBugReportOffered: boolean;
    isRetryOffered: boolean;
    isStopChatsRetryOffered?: boolean;
    isReloadOffered?: boolean;
  },
): OperationFailure {
  return {
    error: { message, details },
    isBugReportOffered: options.isBugReportOffered,
    isRetryOffered: options.isRetryOffered,
    isStopChatsRetryOffered: options.isStopChatsRetryOffered === true,
    isRestartAppOffered: false,
    isReloadOffered: options.isReloadOffered === true,
  };
}

/** A request refused because this window's login with Imbue Studio lapsed. */
function reconnectFailure(outcome: RequestOutcome): OperationFailure {
  return {
    error: reconnectError(outcome),
    isBugReportOffered: false,
    isRetryOffered: false,
    isStopChatsRetryOffered: false,
    isRestartAppOffered: true,
    isReloadOffered: false,
  };
}

/** The dispatch kind of "Set up backups" on the Backups page: a configure,
 * but one whose retry is the button beside it rather than Save. */
const BACKUP_SETUP_RETRY_KIND = "backup_setup_retry";

/** The dispatch kind of turning backups off: a configure operation on the
 * server, but its failure is worded as the disable it was. */
const BACKUP_DISABLE_KIND = "backup_disable";

/** A failure no reason was given for: the sentence for its kind of operation. */
function operationFallbackFailure(
  kind: string,
  details: string,
): OperationFailure {
  if (kind === "backup_update")
    return failure(UPDATE_FAILED_SENTENCE, details, {
      isBugReportOffered: true,
      isRetryOffered: true,
    });
  if (kind === "backup_configure")
    return failure(CONFIGURE_FAILED_SENTENCE, details, {
      isBugReportOffered: true,
      isRetryOffered: false,
    });
  if (kind === BACKUP_DISABLE_KIND)
    return failure(DISABLE_FAILED_SENTENCE, details, {
      isBugReportOffered: true,
      isRetryOffered: false,
    });
  if (kind === BACKUP_SETUP_RETRY_KIND)
    return failure(
      fallbackErrorSentence("Couldn't set up backups for this workspace"),
      details,
      { isBugReportOffered: true, isRetryOffered: true },
    );
  if (kind === "backup_restore")
    return failure(
      fallbackErrorSentence("Couldn't restore this backup"),
      details,
      { isBugReportOffered: true, isRetryOffered: true },
    );
  return failure(
    fallbackErrorSentence("The backup operation didn't finish", {
      isRetryable: false,
    }),
    details,
    { isBugReportOffered: true, isRetryOffered: false },
  );
}

/**
 * Word a failed backup operation from the reason the worker gave for it (see
 * OperationOrProgramFailureReason in workspace_operations.py); the worker's own text
 * goes behind See details. A reason this build does not know falls back to the
 * sentence for the kind of operation.
 */
export function operationFailureFor(
  kind: string,
  reason: string | null,
  rawError: string,
): OperationFailure {
  const isRestore = kind === "backup_restore";
  switch (reason) {
    case "CHAT_GATE_UNDETERMINED":
      // A restore offers "Force restore" for this instead.
      return failure(CHAT_CHECK_FAILED_SENTENCE, rawError, {
        isBugReportOffered: false,
        isRetryOffered: !isRestore,
      });
    case "CHAT_RECHECK_FAILED":
      return failure(CHAT_CHECK_FAILED_SENTENCE, rawError, {
        isBugReportOffered: false,
        isRetryOffered: true,
      });
    case "CHAT_STOP_FAILED":
      return failure(
        "Imbue Studio couldn't stop the chats in this workspace.",
        rawError,
        {
          isBugReportOffered: false,
          isRetryOffered: false,
          isStopChatsRetryOffered: true,
        },
      );
    case "BACKUP_TICK_TIMEOUT":
      return failure(
        "A backup of this workspace is still running. Try again in a few minutes.",
        rawError,
        { isBugReportOffered: false, isRetryOffered: true },
      );
    case "WORKSPACE_PROBE_FAILED":
      return failure(
        isRestore
          ? RESTORE_UNREACHABLE_SENTENCE
          : "Imbue Studio couldn't reach this workspace to update its backup software. " +
              "*Make sure the workspace is running, then try again.*",
        rawError,
        { isBugReportOffered: false, isRetryOffered: true },
      );
    case "SAFETY_SNAPSHOT_FAILED":
      return failure(
        "Imbue Studio couldn't save a copy of this workspace's current state before restoring.",
        rawError,
        { isBugReportOffered: true, isRetryOffered: false },
      );
    case "RESTORE_INCOMPLETE":
      return failure(RESTORE_INCOMPLETE_SENTENCE, rawError, {
        isBugReportOffered: true,
        isRetryOffered: true,
      });
    case "RESTORE_INCOMPLETE_AFTER_SAFETY_BACKUP":
      return failure(
        `${RESTORE_INCOMPLETE_SENTENCE} A safety backup of the pre-restore state was taken first.`,
        rawError,
        { isBugReportOffered: true, isRetryOffered: true },
      );
    case "NO_RESULT":
      return isRestore
        ? failure(
            fallbackErrorSentence("The restore stopped before it finished"),
            rawError,
            { isBugReportOffered: true, isRetryOffered: true },
          )
        : operationFallbackFailure(kind, rawError);
    case "SNAPSHOT_UNRESTORABLE":
      return failure(
        "This backup doesn't contain a copy of your workspace, so it can't be restored. " +
          "*Pick a different one* -- nothing in your workspace was changed.",
        rawError,
        { isBugReportOffered: false, isRetryOffered: false },
      );
    case "SNAPSHOT_READ_FAILED":
      return failure(
        "Imbue Studio couldn't read this backup just now. Nothing in your workspace was changed. " +
          "Try again. If this persists, report a bug.",
        rawError,
        { isBugReportOffered: true, isRetryOffered: true },
      );
    case "CREDENTIALS_WRITE_FAILED":
      return failure(RESTORE_UNREACHABLE_SENTENCE, rawError, {
        isBugReportOffered: false,
        isRetryOffered: true,
      });
    case "UPDATE_STASH_KEPT":
      return failure(
        `${UPDATE_FAILED_SENTENCE} Some unsaved changes in this workspace couldn't be put back ` +
          "automatically. They are kept safe.",
        rawError,
        { isBugReportOffered: true, isRetryOffered: true },
      );
    case "BELOW_UPDATE_FLOOR":
      // The server's own sentence already says what to do.
      return failure(rawError, "", {
        isBugReportOffered: false,
        isRetryOffered: false,
      });
    case "RESTIC_PASSWORD_SET":
      return failure(RESTIC_PASSWORD_SET_SENTENCE, rawError, {
        isBugReportOffered: false,
        isRetryOffered: false,
      });
    case "STORAGE_QUOTA_EXCEEDED":
      return failure(STORAGE_QUOTA_SENTENCE, rawError, {
        isBugReportOffered: false,
        isRetryOffered: false,
      });
    case "DISABLE_FAILED":
      return failure(DISABLE_FAILED_SENTENCE, rawError, {
        isBugReportOffered: true,
        isRetryOffered: false,
      });
    default:
      return operationFallbackFailure(kind, rawError);
  }
}

/** A dispatch the route refused (or never answered): our sentence for the
 * cause its code or status names, else the sentence for the kind of operation.
 * The response always goes behind See details. */
export function dispatchRefusalFor(
  kind: string,
  status: number,
  body: ResponseBody,
): OperationFailure {
  const outcome: RequestOutcome = { status, body };
  const details = responseDetails(outcome);
  if (status === 0)
    return failure(NO_RESPONSE_SENTENCE, details, {
      isBugReportOffered: false,
      isRetryOffered: true,
    });
  if (isReconnectNeeded(outcome)) return reconnectFailure(outcome);
  const code = errorCodeFromBody(body);
  if (code === "BELOW_UPDATE_FLOOR") {
    // The route's sentence names the oldest version it still updates.
    const told = errorFromSentenceResponse(
      "Couldn't update this workspace's backup software",
      outcome,
      { isRetryable: false },
    );
    return failure(told.message, told.details, {
      isBugReportOffered: false,
      isRetryOffered: false,
    });
  }
  if (code === "BACKUPS_NOT_CONFIGURED")
    return failure(
      "Backups are turned off for this workspace, so it can't be restored here. " +
        "*Reload the page to see its backup settings.*",
      details,
      {
        isBugReportOffered: false,
        isRetryOffered: false,
        isReloadOffered: true,
      },
    );
  if (code === "BACKUP_CHOICE_INVALID")
    return failure(
      "*Choose where your backups should be stored, then press Save.*",
      details,
      { isBugReportOffered: false, isRetryOffered: false },
    );
  if (code === "BACKUP_ACCOUNT_MISSING")
    return failure(
      "Backing up to Imbue Cloud needs this workspace to be linked to an account. " +
        "*Link it under Account, or choose a different place for its backups, then press Save.*",
      details,
      { isBugReportOffered: false, isRetryOffered: false },
    );
  if (code === "RESTIC_PASSWORD_SET")
    return failure(RESTIC_PASSWORD_SET_SENTENCE, details, {
      isBugReportOffered: false,
      isRetryOffered: false,
    });
  if (code === "WORKSPACE_MACHINE_NOT_FOUND")
    return failure(
      `Imbue Studio can't find this workspace's machine, so it can't ${
        kind === BACKUP_SETUP_RETRY_KIND
          ? "set up its backups"
          : "change where its backups go"
      }. *Make sure the workspace is running and try again.*`,
      details,
      { isBugReportOffered: false, isRetryOffered: false },
    );
  if (code === "NO_SETUP_TO_RETRY")
    return failure(
      "This workspace's backups may already be set up. *Reload the page to check.*",
      details,
      {
        isBugReportOffered: false,
        isRetryOffered: false,
        isReloadOffered: true,
      },
    );
  // Every other refusal of these routes that carries a 409 is one operation
  // losing to another already running on the workspace.
  if (status === 409)
    return failure(OPERATION_IN_PROGRESS_SENTENCE, details, {
      isBugReportOffered: false,
      isRetryOffered: false,
    });
  return operationFallbackFailure(kind, details);
}

/** A cancel the route refused (or never answered). */
export function cancelRefusalFor(outcome: RequestOutcome): OperationFailure {
  const details = responseDetails(outcome);
  if (outcome.status === 0)
    return failure(NO_RESPONSE_SENTENCE, details, {
      isBugReportOffered: false,
      isRetryOffered: false,
    });
  if (isReconnectNeeded(outcome)) return reconnectFailure(outcome);
  if (outcome.status === 409)
    return failure(
      "It's too late to cancel: this task has started making changes and will run to the end.",
      details,
      { isBugReportOffered: false, isRetryOffered: false },
    );
  // The route tracks no backup update or restore for this workspace: the task
  // ended before the cancel arrived.
  if (outcome.status === 404)
    return failure(
      "There's nothing to cancel: this task has already finished.",
      details,
      { isBugReportOffered: false, isRetryOffered: false },
    );
  // The task is still running, so its Cancel is still on offer.
  return failure(fallbackErrorSentence("Couldn't cancel this task"), details, {
    isBugReportOffered: true,
    isRetryOffered: false,
  });
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
  error: UserFacingError | null = null;
  /** Whether the error on show asks for a bug report. */
  isErrorReportOffered = false;
  /** Whether the error on show offers restarting Imbue Studio. */
  isRestartAppOffered = false;
  /** Whether the error on show offers reloading the page. */
  isReloadOffered = false;
  warningMessage: string | null = null;
  /** Whether "Update backup software" finishes what the warning says was left undone. */
  isUpdateSuggested = false;
  /** Programs a finished restore left not yet running again. */
  servicesDown: string[] = [];
  /** supervisorctl's output when a restore, finished or failed, found the
   * restored workspace's program settings unreadable. */
  serviceConfigError: string | null = null;
  successMessage: string | null = null;
  cancelledMessage: string | null = null;
  isRetryOffered = false;
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
  /** The kind of operation this page dispatched, for wording a failure the
   * status payload does not name the kind of. */
  private dispatchedKind = "";
  private retrySame: (() => void) | null = null;
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
    return (
      OPERATION_SUCCESS_MESSAGES[kind] ??
      "The operation completed successfully."
    );
  }

  startRestore(
    snapshot: BackupSnapshot,
    timeText: string,
    options: RestoreOptions,
  ): void {
    const body = {
      stop_chats: options.stopChats === true,
      update_after: options.updateAfter !== false,
      skip_safety_snapshot: options.skipSafetySnapshot === true,
      skip_chat_gate: options.skipChatGate === true,
      repository: options.repositoryId ?? null,
    };
    this.dispatch(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backups/${encodeURIComponent(snapshot.snapshot_id)}/restore`,
      body,
      {
        isRestore: true,
        snapshotId: snapshot.snapshot_id,
        isCancellable: true,
        successMessage: timeText
          ? `Workspace restored to the backup from ${timeText}. A safety backup of your previous state was saved first.`
          : OPERATION_SUCCESS_MESSAGES.backup_restore,
        kind: "backup_restore",
        retrySame: () => this.startRestore(snapshot, timeText, options),
        retryWithStopChats: () =>
          this.startRestore(snapshot, timeText, {
            ...options,
            stopChats: true,
          }),
        retrySkipSafety: () =>
          this.startRestore(snapshot, timeText, {
            ...options,
            skipSafetySnapshot: true,
          }),
        retryForce: () =>
          this.startRestore(snapshot, timeText, {
            ...options,
            skipChatGate: true,
          }),
      },
    );
  }

  /** The idempotent "Update backup software" converge. */
  startUpdate(options: { stopChats?: boolean } = {}): void {
    this.dispatch(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/update`,
      { stop_chats: options.stopChats === true },
      {
        isCancellable: true,
        label: OPERATION_RUNNING_LABELS.backup_update,
        successMessage: OPERATION_SUCCESS_MESSAGES.backup_update,
        kind: "backup_update",
        retrySame: () => this.startUpdate(options),
        retryWithStopChats: () => this.startUpdate({ stopChats: true }),
      },
    );
  }

  /** Run a new workspace's failed backup setup again, with the inputs it was created with. */
  startSetupRetry(): void {
    this.dispatch(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/setup/retry`,
      {},
      {
        label: "Setting up backups...",
        successMessage: OPERATION_SUCCESS_MESSAGES.backup_configure,
        kind: BACKUP_SETUP_RETRY_KIND,
        retrySame: () => this.startSetupRetry(),
      },
    );
  }

  /** Enable backups, move where they go, or (provider "NONE") turn them off. */
  startStorageChange(provider: string, apiKeyEnv: string): void {
    if (provider === "NONE") {
      this.dispatch(
        `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/disable`,
        {},
        {
          label: "Turning backups off...",
          successMessage: "Backups are now turned off for this workspace.",
          kind: BACKUP_DISABLE_KIND,
        },
      );
      return;
    }
    this.dispatch(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/configure`,
      { backup_provider: provider, api_key_env: apiKeyEnv },
      {
        label: OPERATION_RUNNING_LABELS.backup_configure,
        successMessage: OPERATION_SUCCESS_MESSAGES.backup_configure,
        kind: "backup_configure",
      },
    );
  }

  runRetry(): void {
    this.isRetryOffered = false;
    this.retrySame?.();
  }

  runStopChatsRetry(): void {
    this.isStopChatsRetryOffered = false;
    this.retryWithStopChats?.();
  }

  runSkipSafetyRetry(): void {
    this.isSkipSafetyRetryOffered = false;
    this.retrySkipSafety?.();
  }

  /** Leave a restore whose safety copy failed undone: nothing was restored,
   * so its failure, its output and the offer to restore without the copy go. */
  abandonSkipSafetyRetry(): void {
    this.isSkipSafetyRetryOffered = false;
    this.clearTerminalNotices();
    this.logLines = [];
    this.deps.redraw();
  }

  runForceRetry(): void {
    this.isForceRetryOffered = false;
    this.retryForce?.();
  }

  /** Attach to an operation started elsewhere (another window / a reload). */
  async reattach(): Promise<void> {
    if (this.isRunning || this.isStopped) return;
    const payload = (await this.deps.getJson(
      this.operationUrl(),
    )) as OperationStatusPayload | null;
    if (this.isRunning || this.isStopped) return;
    if (payload === null || payload.status !== "RUNNING") return;
    this.clearLastOutcome();
    // This page did not dispatch the running operation: no retry closures,
    // generic success wording.
    this.pendingSuccessMessage = null;
    this.dispatchedKind = payload.kind ?? "";
    this.retrySame = null;
    this.retryWithStopChats = null;
    this.retrySkipSafety = null;
    this.retryForce = null;
    this.isRestore = payload.kind === "backup_restore";
    this.restoringSnapshotId = payload.snapshot_id ?? null;
    this.setRunning(
      true,
      payload.is_cancellable === true,
      OPERATION_RUNNING_LABELS[payload.kind ?? ""] ?? "Working...",
    );
    this.consecutivePollFailures = 0;
    this.streamLogs();
    this.pollSoon();
  }

  async requestCancel(): Promise<void> {
    const result = await this.deps.postJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/update/cancel`,
      {},
    );
    if (result.status >= 400 || result.status === 0) {
      this.showFailure(cancelRefusalFor(result));
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
      kind: string;
      retrySame?: () => void;
      retryWithStopChats?: () => void;
      retrySkipSafety?: () => void;
      retryForce?: () => void;
    },
  ): void {
    this.clearLastOutcome();
    this.isRestore = opts.isRestore === true;
    this.restoringSnapshotId = opts.snapshotId ?? null;
    this.pendingSuccessMessage = opts.successMessage ?? null;
    this.dispatchedKind = opts.kind;
    this.retrySame = opts.retrySame ?? null;
    this.retryWithStopChats = opts.retryWithStopChats ?? null;
    this.retrySkipSafety = opts.retrySkipSafety ?? null;
    this.retryForce = opts.retryForce ?? null;
    this.setRunning(
      true,
      opts.isCancellable === true,
      opts.label ?? "Working...",
    );
    this.consecutivePollFailures = 0;
    void this.deps.postJson(url, body).then((result) => {
      if (result.status === 202) {
        this.streamLogs();
        this.pollSoon();
        return;
      }
      this.setRunning(false, false, "");
      this.showFailure(
        dispatchRefusalFor(opts.kind, result.status, result.body),
      );
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
    const payload = (await this.deps.getJson(
      this.operationUrl(),
    )) as OperationStatusPayload | null;
    if (this.isStopped) return;
    if (payload === null) {
      // Transient fetch failure: keep polling rather than ending the
      // working state under a still-running backend operation -- but only
      // for a bounded run, or a permanent failure pins "Working..." forever.
      this.consecutivePollFailures += 1;
      if (this.consecutivePollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        this.setRunning(false, false, "");
        this.showError(
          {
            message:
              "Lost contact with the backup operation; its status could not be read for several minutes. " +
              "*Reload this page to check whether it finished.*",
            details: "",
          },
          true,
        );
        this.isReloadOffered = true;
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
    // A cancel refused while the task ran is moot once the task has ended.
    this.clearTerminalNotices();
    if (payload.status === "CANCELLED") {
      this.cancelledMessage =
        OPERATION_CANCELLED_MESSAGES[payload.kind ?? ""] ??
        "The operation was cancelled. Nothing was changed.";
      this.deps.redraw();
      return;
    }
    if (payload.is_done === true) {
      this.successMessage =
        this.pendingSuccessMessage ??
        this.successMessageFor(payload.kind ?? "");
      this.warningMessage = payload.warning ?? null;
      this.isUpdateSuggested = payload.is_update_suggested === true;
      this.servicesDown = payload.services_down ?? [];
      this.serviceConfigError = payload.service_config_error ?? null;
      this.onSuccess?.();
      this.deps.redraw();
      return;
    }
    if (
      payload.blocked_chats !== undefined &&
      payload.blocked_chats.length > 0
    ) {
      this.showError(
        {
          message:
            `Chats are running in this workspace (${payload.blocked_chats.join(", ")}). ` +
            "Stop them before continuing; they resume on your next message.",
          details: "",
        },
        false,
      );
      this.isStopChatsRetryOffered = this.retryWithStopChats !== null;
      this.deps.redraw();
      return;
    }
    const reason = payload.failure_reason ?? null;
    this.showFailure(
      operationFailureFor(
        this.dispatchedKind || (payload.kind ?? ""),
        reason,
        payload.error ?? "",
      ),
    );
    this.isSkipSafetyRetryOffered =
      this.retrySkipSafety !== null && reason === "SAFETY_SNAPSHOT_FAILED";
    this.isForceRetryOffered =
      this.retryForce !== null &&
      (reason === "CHAT_GATE_UNDETERMINED" ||
        reason === "WORKSPACE_PROBE_FAILED");
    this.serviceConfigError = payload.service_config_error ?? null;
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

  private setRunning(
    isRunning: boolean,
    isCancellable: boolean,
    label: string,
  ): void {
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

  /** A new operation is starting: the last one's notices and retries go. */
  private clearLastOutcome(): void {
    this.clearTerminalNotices();
    this.isRetryOffered = false;
    this.isStopChatsRetryOffered = false;
    this.isSkipSafetyRetryOffered = false;
    this.isForceRetryOffered = false;
  }

  private clearTerminalNotices(): void {
    this.error = null;
    this.isErrorReportOffered = false;
    this.isRestartAppOffered = false;
    this.isReloadOffered = false;
    this.warningMessage = null;
    this.isUpdateSuggested = false;
    this.servicesDown = [];
    this.serviceConfigError = null;
    this.successMessage = null;
    this.cancelledMessage = null;
  }

  private showError(error: UserFacingError, isBugReportOffered: boolean): void {
    this.clearTerminalNotices();
    this.error = error;
    this.isErrorReportOffered = isBugReportOffered;
  }

  private showFailure(failure: OperationFailure): void {
    this.showError(failure.error, failure.isBugReportOffered);
    this.isRestartAppOffered = failure.isRestartAppOffered;
    this.isReloadOffered = failure.isReloadOffered;
    this.isRetryOffered = failure.isRetryOffered && this.retrySame !== null;
    this.isStopChatsRetryOffered =
      failure.isStopChatsRetryOffered && this.retryWithStopChats !== null;
  }
}

interface BackupSetupFailurePayload {
  kind: string;
  detail: string;
}

interface BackupsListingPayload {
  setup_failure?: BackupSetupFailurePayload | null;
  is_configured?: boolean;
  is_backing_up?: boolean;
  snapshots?: BackupSnapshot[];
  snapshots_total?: number;
  snapshots_error?: string | null;
  previous_repositories?: PreviousBackupRepository[];
}

/** The paginated snapshot-history table (newest first, server-side paging). */
export class BackupHistoryModel {
  readonly agentId: string;
  offset = 0;
  total = 0;
  snapshots: BackupSnapshot[] = [];
  /** Previous backup destinations whose snapshots are still readable (newest archive first). */
  previousRepositories: PreviousBackupRepository[] = [];
  /** Whether the workspace has current backups; a restore (from any storage) needs them. */
  isBackupsConfigured = false;
  /** Human status line replacing the table (loading / empty states). */
  statusMessage: string | null = "Loading backup history...";
  /** Why the listing could not be read, shown in place of the table. */
  loadError: UserFacingError | null = null;
  /** Why backups could not be set up after the workspace was created, while
   * they still are not. */
  setupFailure: BackupSetupFailureNotice | null = null;
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
    this.loadError = null;
    this.deps.redraw();
    // Previous destinations do not page and cost the server one restic
    // listing each, so only the first page asks for them; later pages keep
    // the ones already loaded.
    const isFirstPage = this.offset === 0;
    const url =
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backups` +
      `?limit=${BACKUP_HISTORY_PAGE_SIZE}&offset=${this.offset}` +
      (isFirstPage ? "&include_previous=1" : "");
    const payload = (await this.deps.getJson(
      url,
    )) as BackupsListingPayload | null;
    this.setupFailure = backupSetupFailureNotice(
      payload?.setup_failure ?? null,
    );
    if (payload === null) {
      this.statusMessage = null;
      this.loadError = backupHistoryLoadError("");
      this.deps.redraw();
      return;
    }
    // Previous destinations are independent of the current listing's state:
    // a workspace whose backups were turned off may still have readable history.
    if (isFirstPage) {
      this.previousRepositories = payload.previous_repositories ?? [];
    }
    this.isBackupsConfigured = payload.is_configured === true;
    if (payload.snapshots_error) {
      this.statusMessage = null;
      this.loadError = backupHistoryLoadError(payload.snapshots_error);
      this.deps.redraw();
      return;
    }
    if (payload.is_configured !== true) {
      this.statusMessage =
        this.setupFailure === null
          ? "Backups are turned off for this workspace."
          : null;
      this.deps.redraw();
      return;
    }
    this.snapshots = payload.snapshots ?? [];
    // Fall back to the returned rows if the count is absent (older backend)
    // so a present page never collapses to the empty state.
    this.total =
      typeof payload.snapshots_total === "number"
        ? payload.snapshots_total
        : this.offset + this.snapshots.length;
    this.statusMessage =
      this.total === 0
        ? "No backups yet. The first backup runs within the hour."
        : null;
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

/** How many recent backups the settings group lists before linking to the full history. */
export const BACKUP_SETTINGS_RECENT_LIMIT = 5;

/** Each backup-service problem in the words the person reading it needs.
 *
 * Every one of these points at the same idempotent converge, which is the only
 * action available for any of them -- so the label's job is to say what is
 * wrong, and that the one button fixes it.
 */
const BACKUP_PROBLEM_LABELS: Record<string, string> = {
  NOT_CONFIGURED:
    'Backups are turned off for this workspace. Use "Change storage location" to turn them on.',
  CODE_OUTDATED:
    'The backup software in this workspace is out of date. Click "Update backup software" to fix this.',
  ENV_MISSING:
    'This workspace has lost its backup storage settings. Click "Update backup software" to restore them.',
  ENV_MISMATCH:
    'This workspace is set up to back up somewhere different than expected. Click "Update backup software" to fix this.',
  SERVICE_NOT_RUNNING:
    'The backup software in this workspace is not running. Click "Update backup software" to restart it.',
  UNVERIFIABLE:
    "Imbue Studio couldn't finish checking this workspace's backups. " +
    'Try "Update backup software"; if that fails too, try again in a few minutes.',
  BACKUPS_STALE:
    'This workspace has not backed up recently even though it is running. Click "Update backup software" to fix this.',
};

/** How the Backups page tells a failed setup after create. */
export interface BackupSetupFailureNotice {
  error: UserFacingError;
  /** Off for a plan limit, which is no bug. */
  isBugReportOffered: boolean;
}

/** The Backups page's account of a failed setup after create: what is true
 * now, with the failure's own text behind See details. */
export function backupSetupFailureNotice(
  failure: BackupSetupFailurePayload | null,
): BackupSetupFailureNotice | null {
  if (failure === null) return null;
  const isQuota = failure.kind === "STORAGE_QUOTA";
  return {
    error: {
      message: isQuota
        ? STORAGE_QUOTA_SENTENCE
        : "Backups aren't set up for this workspace yet.",
      details: failure.detail,
    },
    isBugReportOffered: !isQuota,
  };
}

/** The listing of a workspace's backups could not be read: the route did not
 * answer, or restic failed (its message is the detail). */
export function backupHistoryLoadError(listingError: string): UserFacingError {
  return {
    message: fallbackErrorSentence(
      "Couldn't load this workspace's backup history",
    ),
    details: listingError,
  };
}

interface BackupCheckPayload {
  check_state?: string;
  problems?: string[];
  installed_version?: string | null;
  minimum_version?: string | null;
  update_target_version?: string | null;
  check_detail?: string;
  is_verification_enabled?: boolean;
}

/**
 * The Backup group in Workspace settings: the snapshot summary and the recent
 * rows (fast, restic runs on this machine) plus the backup-service verdict
 * (slow, it execs into the machine), loaded independently so the slow half
 * never gates the fast one.
 */
export class BackupSettingsModel {
  readonly agentId: string;
  isConfigured = false;
  isBackingUp = false;
  snapshots: BackupSnapshot[] = [];
  snapshotsTotal = 0;
  /** Whether the listing failed, which reads as "unknown" rather than "none". */
  isSnapshotsErrored = false;
  /** The listing's own reason when it failed; "" when the route did not answer. */
  snapshotsErrorDetail = "";
  isSnapshotsLoaded = false;
  check: BackupCheckPayload | null = null;
  isCheckLoading = true;
  isVerificationPending = false;
  /** A failed verification write, which has no operation strip of its own. */
  verificationError: UserFacingError | null = null;
  /** What the failed verification write asked for, so its Try again sends the same. */
  private failedVerificationTarget: boolean | null = null;

  private readonly deps: LifecycleDeps;
  private isStopped = false;

  constructor(agentId: string, deps: LifecycleDeps) {
    this.agentId = agentId;
    this.deps = deps;
  }

  /** Stop adopting responses; the group was closed while reads were in flight. */
  stop(): void {
    this.isStopped = true;
  }

  async load(): Promise<void> {
    await Promise.all([this.loadSnapshots(), this.loadCheck()]);
  }

  /** Read again what could not be read: the listing, and the service check
   * too when it reached no verdict. */
  async reloadFailedReads(): Promise<void> {
    await Promise.all([
      this.loadSnapshots(),
      this.isCheckVerdictMissing ? this.loadCheck() : Promise.resolve(),
    ]);
  }

  async loadSnapshots(): Promise<void> {
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backups?limit=${BACKUP_SETTINGS_RECENT_LIMIT}`,
    )) as BackupsListingPayload | null;
    if (this.isStopped) return;
    this.isSnapshotsLoaded = true;
    if (payload === null) {
      // The route itself did not answer, which says nothing about the backups.
      this.isSnapshotsErrored = true;
      this.snapshotsErrorDetail = "";
      this.deps.redraw();
      return;
    }
    this.isConfigured = payload.is_configured === true;
    this.isBackingUp = payload.is_backing_up === true;
    this.isSnapshotsErrored = Boolean(payload.snapshots_error);
    this.snapshotsErrorDetail = payload.snapshots_error ?? "";
    this.snapshots = payload.snapshots ?? [];
    this.snapshotsTotal =
      typeof payload.snapshots_total === "number"
        ? payload.snapshots_total
        : this.snapshots.length;
    this.deps.redraw();
  }

  async loadCheck(): Promise<void> {
    this.isCheckLoading = true;
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-check`,
    )) as BackupCheckPayload | null;
    if (this.isStopped) return;
    this.isCheckLoading = false;
    // A verdict that did not arrive leaves `check` null, which the group reads
    // as "no verdict" -- never as a clean bill of health.
    this.check = payload;
    this.deps.redraw();
  }

  /** What the workspace's backups are doing, or "" when `statusError` says it instead. */
  get statusLine(): string {
    if (!this.isSnapshotsLoaded) return "Loading backup status...";
    if (this.isBackingUp) return "Backing up now...";
    const newest = this.snapshots.length > 0 ? this.snapshots[0].time : null;
    if (newest !== null)
      return `Last backup: ${new Date(newest).toLocaleString()}`;
    if (this.isSnapshotsErrored) return "";
    if (!this.isConfigured) return "Backups are turned off for this workspace.";
    return "No successful backup yet.";
  }

  /** The status the listing could not establish, or null when it could. */
  get statusError(): UserFacingError | null {
    if (!this.isSnapshotsLoaded || this.isBackingUp || !this.isSnapshotsErrored)
      return null;
    if (this.snapshots.length > 0) return null;
    return {
      message: fallbackErrorSentence(
        "Imbue Studio couldn't check this workspace's backups",
      ),
      details: this.snapshotsErrorDetail,
    };
  }

  /** What the service check concluded, or "" when it reached no verdict. */
  get checkLine(): string {
    const state = this.check?.check_state;
    if (state === "DISABLED")
      return "Backup service verification is disabled for this workspace.";
    if (state === "OFFLINE")
      return "This workspace is offline; its backups will be checked when it is back online.";
    if (state === "OK") return "The backup service is up to date.";
    return "";
  }

  /** Whether the service check has finished without reaching a verdict: it
   * could not run, did not answer in time, or its route did not answer. Kept
   * apart from a clean result, which it must never read as. */
  get isCheckVerdictMissing(): boolean {
    if (this.isCheckLoading) return false;
    return this.check === null || this.check.check_state === "UNKNOWN";
  }

  /** Why the recent-backups table is empty, or null when there are rows to show. */
  get emptyHistoryMessage(): string | null {
    if (!this.isSnapshotsLoaded) return "Loading backup history...";
    // Before the configured check, and in the order `statusLine` uses: a route
    // that did not answer leaves `isConfigured` at its default, which is not a
    // reading of anything.
    if (this.isSnapshotsErrored) return backupHistoryLoadError("").message;
    if (!this.isConfigured) return BACKUP_PROBLEM_LABELS.NOT_CONFIGURED;
    if (this.snapshots.length > 0) return null;
    return this.isBackingUp
      ? "Backing up now... the first backup will appear shortly."
      : "No backups yet. The first backup runs within the hour.";
  }

  get isViewAllShown(): boolean {
    return this.snapshotsTotal > BACKUP_SETTINGS_RECENT_LIMIT;
  }

  /** The installed / required / would-install versions, or "" when none are known. */
  get versionLine(): string {
    const check = this.check;
    if (check === null) return "";
    const parts: string[] = [];
    if (check.installed_version)
      parts.push(`Installed backup service: ${check.installed_version}`);
    if (check.minimum_version)
      parts.push(`minimum required: ${check.minimum_version}`);
    if (
      check.update_target_version &&
      check.update_target_version !== check.minimum_version
    ) {
      parts.push(`update installs: ${check.update_target_version}`);
    }
    return parts.join(" / ");
  }

  /** Every problem the check found, in plain words. */
  get problemLines(): string[] {
    const check = this.check;
    if (check === null || check.check_state !== "PROBLEMS") return [];
    return (check.problems ?? []).map(
      (problem) => BACKUP_PROBLEM_LABELS[problem] ?? problem,
    );
  }

  /** Whether "Update backup software" fixes any of the problems the check
   * found; turning backups on (NOT_CONFIGURED) is the storage form's job. */
  get isProblemFixedByUpdate(): boolean {
    const check = this.check;
    if (check === null || check.check_state !== "PROBLEMS") return false;
    return (check.problems ?? []).some(
      (problem) => problem !== "NOT_CONFIGURED",
    );
  }

  /** The check's own account of what it ran into, for See details under the
   * problems; "" when it has none. */
  get problemDetail(): string {
    const check = this.check;
    if (check === null || check.check_state !== "PROBLEMS") return "";
    return check.check_detail ?? "";
  }

  /** Whether the update is offered at all.
   *
   * It is an idempotent converge, so it stays on offer even for a machine that
   * reports no problems -- resetting a wedged backup service is exactly what it
   * is for. The two exceptions are a machine that cannot be reached to run it,
   * and one the server would refuse anyway for being too old.
   */
  isUpdateOffered(isRecreationRequired: boolean): boolean {
    if (isRecreationRequired) return false;
    return this.check?.check_state !== "OFFLINE";
  }

  get isRestoreDisabledByCheck(): boolean {
    return this.check?.check_state === "OFFLINE";
  }

  /** Verification defaults to on, so an unread verdict must not read as off. */
  get isVerificationEnabled(): boolean {
    return this.check?.is_verification_enabled !== false;
  }

  async toggleVerification(): Promise<void> {
    await this.changeVerification(!this.isVerificationEnabled);
  }

  /** Send the verification change that failed again. */
  async retryVerificationChange(): Promise<void> {
    if (this.failedVerificationTarget === null) return;
    await this.changeVerification(this.failedVerificationTarget);
  }

  private async changeVerification(target: boolean): Promise<void> {
    this.isVerificationPending = true;
    this.verificationError = null;
    this.failedVerificationTarget = null;
    this.deps.redraw();
    const result = await this.deps.postJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/backup-service/verification`,
      { enabled: target },
    );
    if (this.isStopped) return;
    if (result.status >= 400 || result.status === 0) {
      this.isVerificationPending = false;
      this.verificationError = {
        message: "Could not change the verification setting. Try again.",
        details: responseDetails(result),
      };
      this.failedVerificationTarget = target;
      this.deps.redraw();
      return;
    }
    // Re-reading also re-runs the (slow) service check, so the pending flag
    // stays up until the fresh verdict lands rather than snapping back to a
    // stale one.
    await this.loadCheck();
    if (this.isStopped) return;
    this.isVerificationPending = false;
    this.deps.redraw();
  }
}

export interface DestroyedWorkspaceRow {
  agent_id: string;
  display_name: string;
  is_orphan: boolean;
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

/** A Remove that did not delete its backup: how it is told, and the row it
 * was for, so Try again sends the same delete. */
export interface DeleteBackupFailure {
  agentId: string;
  notice: RefusalNotice;
}

/** The recently-destroyed page: rows + retention header + delete/download actions. */
export class DestroyedWorkspacesModel {
  rows: DestroyedWorkspaceRow[] = [];
  retentionDays = 0;
  isLoaded = false;
  /** Why the rows could not be read, shown in their place. */
  loadError: UserFacingError | null = null;
  /** Why the last Remove could not delete its backup, if it could not. */
  deleteFailure: DeleteBackupFailure | null = null;
  /** The row whose Remove has been armed (inline confirm), if any. */
  armedDeleteAgentId: string | null = null;
  downloadingAgentIds = new Set<string>();
  deletingAgentIds = new Set<string>();

  private readonly deps: LifecycleDeps;

  constructor(deps: LifecycleDeps) {
    this.deps = deps;
  }

  async load(): Promise<void> {
    const payload = (await this.deps.getJson(
      "/ui/api/destroyed-workspaces",
    )) as DestroyedWorkspacesPayload | null;
    if (payload === null) {
      this.loadError = {
        message: "Could not load recently destroyed workspaces.",
        details: "",
      };
      this.isLoaded = true;
      this.deps.redraw();
      return;
    }
    this.loadError = null;
    this.rows = payload.rows;
    this.retentionDays = payload.retention_days;
    this.isLoaded = true;
    this.deps.redraw();
  }

  async deleteBackup(agentId: string): Promise<void> {
    this.armedDeleteAgentId = null;
    this.deleteFailure = null;
    this.deletingAgentIds.add(agentId);
    this.deps.redraw();
    const result = await this.deps.postJson(
      `/ui/api/destroyed-workspaces/${encodeURIComponent(agentId)}/delete-backup`,
      {},
    );
    this.deletingAgentIds.delete(agentId);
    // A 404 is the backup already gone: the list, read again, shows that.
    if (
      (result.status >= 400 || result.status === 0) &&
      result.status !== 404
    ) {
      this.deleteFailure = {
        agentId,
        notice: deleteBackupFailureFor(result),
      };
      this.deps.redraw();
      return;
    }
    await this.load();
  }
}

/** How a Remove on the recently-destroyed page that did not delete its backup
 * is told: the restart remedy for a lapsed window login, else a Try again. */
export function deleteBackupFailureFor(outcome: RequestOutcome): RefusalNotice {
  if (isReconnectNeeded(outcome))
    return {
      error: reconnectError(outcome),
      isBugReportOffered: false,
      isRestartAppOffered: true,
    };
  const whatFailed = "Couldn't delete this backup";
  return {
    error: {
      message:
        outcome.status === 0
          ? NO_RESPONSE_SENTENCE
          : fallbackErrorSentence(whatFailed),
      details: responseDetails(outcome),
    },
    isBugReportOffered: outcome.status !== 0,
    isRestartAppOffered: false,
  };
}

/** "confirming": the removal finished, and its host is not yet seen gone. */
export type DestroyStatus = "running" | "confirming" | "failed" | "done";

/** The destroy detail page driver: status poll + log tail + retry/dismiss. */
export class DestroyingModel {
  readonly agentId: string;
  status: DestroyStatus = "running";
  logText = "";
  /** Whether the log stream failed before it delivered anything. */
  isLogUnavailable = false;
  /** Why the last Retry could not start the removal again, if it was refused. */
  retryFailure: DestroyRefusal | null = null;
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
    this.retryFailure = null;
    const result = await this.deps.postJson(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/destroy`,
      {},
    );
    // Status 0 is the browser deps' network-failure sentinel.
    if (result.status >= 400 || result.status === 0) {
      this.retryFailure = destroyDispatchFailureFor(result);
      this.deps.redraw();
      return;
    }
    this.logText = "";
    this.isLogUnavailable = false;
    this.status = "running";
    this.deps.redraw();
    this.start();
  }

  async dismiss(): Promise<void> {
    await this.deps.deleteResource(
      `/api/v1/workspaces/operations/destroy/${encodeURIComponent(this.agentId)}`,
    );
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
    if (this.status === "running" || this.status === "confirming") {
      this.deps.schedule(() => void this.pollOnce(), DESTROY_POLL_INTERVAL_MS);
    }
  }

  applyStatus(status: string): void {
    if (this.isStopped && status !== "done") return;
    if (
      status !== "running" &&
      status !== "confirming" &&
      status !== "failed" &&
      status !== "done"
    )
      return;
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
        frame = JSON.parse(event.data) as {
          log?: string;
          done?: boolean;
          status?: string;
        };
      } catch {
        return;
      }
      if (frame.log !== undefined) {
        this.logText += frame.log;
        this.deps.redraw();
      }
      if (frame.done === true) {
        this.closeSource();
        if (frame.status !== undefined)
          this.applyStatus(frame.status.toLowerCase());
      }
    };
    source.onerror = () => {
      this.closeSource();
      if (this.logText === "") {
        this.isLogUnavailable = true;
        this.deps.redraw();
      }
    };
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
  /** Which recovery is in flight, or which one failed. A full stop+start
   * bounce is only ever dispatched by the user's own click, so "restart" is
   * what makes "Restarting" an honest claim rather than a guess. Null outside a
   * recovery. */
  recovery_kind: RecoveryKind | null;
  /** Which step of a failed recovery failed; null unless health is recovery_failed. */
  recovery_failure_cause?: RecoveryFailureCause | null;
  ssh_command: string;
  is_host_offline: boolean;
  device_environment: EnvironmentCondition;
  is_backend_unreachable: boolean;
  provider_label: string;
  unreachable_reason: string;
  /** The concise next step the provider named for its error ("start Docker"),
   * "" when it named none. */
  unreachable_remediation?: string;
  /** Whether the machine refused this device's connection key, rather than the
   * provider reporting an error of its own. */
  is_access_rejected?: boolean;
  is_device_cannot_connect: boolean;
  device_error_detail: string;
}

interface RecoveryStatusPayload {
  status?: string;
  is_done?: boolean;
  error?: string | null;
  warning?: string | null;
}

/** The workspace operations the app runs one at a time (WorkspaceOperationKind). */
type WorkspaceOperationKind = "RECOVERY" | "BACKUP_UPDATE" | "BACKUP_CONFIGURE" | "BACKUP_RESTORE";

/** What each operation that can hold the workspace is doing, for the sentence
 * that refuses a restart while it runs. */
const OPERATION_IN_PROGRESS_PHRASES: Record<WorkspaceOperationKind, string> = {
  RECOVERY: "bringing it back",
  BACKUP_UPDATE: "updating its backup software",
  BACKUP_CONFIGURE: "changing its backup settings",
  BACKUP_RESTORE: "restoring a backup",
};

function isWorkspaceOperationKind(value: string): value is WorkspaceOperationKind {
  return Object.prototype.hasOwnProperty.call(OPERATION_IN_PROGRESS_PHRASES, value);
}

/** The error for a restart (or start) the app refused to begin. */
export function recoveryDispatchError(kind: RecoveryKind, outcome: RequestOutcome): UserFacingError {
  const operationKind = operationKindFromBody(outcome.body);
  if (outcome.status === 409 && operationKind !== null) {
    const doing = isWorkspaceOperationKind(operationKind)
      ? OPERATION_IN_PROGRESS_PHRASES[operationKind]
      : "with something else";
    return {
      message: `Imbue Studio is already working on this workspace (${doing}). Wait for that to finish, then try again.`,
      details: responseDetails(outcome),
    };
  }
  const whatFailed =
    kind === "start" ? "Imbue Studio couldn't start this workspace" : "Imbue Studio couldn't restart this workspace";
  return errorFromResponse(whatFailed, outcome);
}

/**
 * The sentence for a recovery that failed, by the step that failed and the
 * kind of recovery that ran. The app's own start is not called a restart: it
 * only starts the workspace, and may find it already running.
 */
export function recoveryFailureSentence(
  cause: RecoveryFailureCause | null,
  kind: RecoveryKind | null,
): string {
  const verb = kind === "restart" ? "restart" : "start";
  switch (cause) {
    case "PROVIDER_UNREACHABLE":
      return fallbackErrorSentence(
        `Imbue Studio couldn't reach the service that runs this workspace, so it couldn't ${verb} it`,
      );
    case "WORKSPACE_NOT_FOUND":
      return fallbackErrorSentence(
        `Imbue Studio lost track of this workspace, so it couldn't ${verb} it`,
      );
    case "LOST_AFTER_START":
      return fallbackErrorSentence(
        "Imbue Studio started this workspace, then lost track of it, so it can't tell whether it's back",
      );
    case "NOT_ANSWERING_AFTER_START":
      return fallbackErrorSentence(
        "Imbue Studio tried to bring this workspace back, but it still isn't answering",
      );
    case "STEP_FAILED":
    case "WORKER_NOT_STARTED":
    case "WORKER_CRASHED":
    case null:
      return fallbackErrorSentence(`Imbue Studio couldn't ${verb} this workspace`);
  }
}

/** What the recovery card shows when it cannot read the workspace's state. */
export const RECOVERY_LOAD_ERROR: UserFacingError = {
  message: "Imbue Studio can't read this workspace's status right now.",
  details: "The workspace's recovery state could not be read from Imbue Studio.",
};

/** What a declined start says when the app reported no reason of its own. */
const RECOVERY_DECLINED_FALLBACK_MESSAGE =
  "The workspace could not be started right now. Nothing was changed.";

/**
 * The recovery card's driver: follow the machine's recovery state, dispatch a
 * recovery, and follow it (status + log stream) to a terminal state. Never
 * navigates on its own -- the surfaces offer links.
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
  loadError: UserFacingError | null = null;
  isRecoveryRunning = false;
  /** Which recovery *this model dispatched*, or null when the one it is
   * following was dispatched elsewhere and only attached to. A surface
   * describing a recovery in flight needs the difference: its own click is known
   * from the click, while an unattended or other-window dispatch is only ever
   * as good as what the tracker publishes about it. */
  dispatchedRecoveryKind: RecoveryKind | null = null;
  /** Which recovery this model is following: its own dispatch's kind, or the
   * tracker's word for one it attached to (null when the tracker gave none). */
  followedRecoveryKind: RecoveryKind | null = null;
  /** Why this card's own restart was refused or failed. */
  recoveryError: UserFacingError | null = null;
  /** Whether this card stopped following a restart whose status it could not
   * read; the workspace's own state above keeps updating. */
  isRecoveryFollowLost = false;
  /** The reason a start was refused before it changed anything (an operator
   * holds the machine): an outcome that is neither a success -- the machine is
   * not answering -- nor a failure of the machine or this device. */
  recoveryNotice: string | null = null;
  isRecoverySucceeded = false;
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
    // dispatches on this promise, and dispatchRecovery no-ops without an agent id.
    if (this.isStopped) return;
    // The poll is armed either way. A first read that fails is no more final
    // than any later one -- it is the local app's own endpoint, mid-episode --
    // and without the poll the surface would hold that error until the window
    // was reloaded, while the machine recovered behind it.
    if (payload === null) {
      this.loadError = RECOVERY_LOAD_ERROR;
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
   * Only ``info`` is written: the recovery fields belong to a recovery this
   * model is running and are owned by ``dispatchRecovery`` /
   * ``pollRecoveryOnce``, which follow the operation itself rather than the
   * tracker's summary of it.
   *
   * A recovery this model is not already running is attached to, so one
   * dispatched elsewhere -- the unattended start, or the same machine's card in
   * another window -- shows its progress and its logs here too. Attaching
   * clears the previous recovery's outcome, which describes a different run.
   * Readings taken before a recovery this model was following reported its
   * outcome never get here (``pollInfoOnce`` drops them), so an outcome is
   * only ever cleared by a recovery that started after it.
   *
   * The one recovery never attached to is the one this model has already given
   * up following (``pollRecoveryOnce``'s bound). The tracker goes on reporting
   * it as running -- that is the state whose status could not be read -- so
   * attaching would clear the lost-contact report and start the bound over,
   * every poll, for as long as the tracker says so. The tracker leaving
   * "recovering" ends that run, and the next one is a different recovery.
   */
  private applyInfo(payload: RecoveryInfo): void {
    this.info = payload;
    // A declined start's notice describes a machine that is not answering;
    // once it is (the operator's start landed), the notice is stale.
    if (payload.health === "healthy" && !payload.is_host_offline)
      this.recoveryNotice = null;
    if (payload.health !== "recovering") {
      this.isRecoveryFollowAbandoned = false;
      // The run it lost track of has ended; the heading now says where the
      // workspace ended up.
      this.isRecoveryFollowLost = false;
      return;
    }
    if (
      this.isRecoveryRunning ||
      this.isRecoveryFollowAbandoned ||
      this.isStopped
    )
      return;
    this.isRecoveryRunning = true;
    // Attached, not dispatched: this model has no first-hand account of what
    // was run, so a surface must take the tracker's word for which it was.
    this.dispatchedRecoveryKind = null;
    this.followedRecoveryKind = payload.recovery_kind;
    this.isRecoverySucceeded = false;
    this.recoveryError = null;
    this.isRecoveryFollowLost = false;
    this.recoveryNotice = null;
    this.logLines = [];
    this.consecutiveRecoveryPollFailures = 0;
    this.streamRecoveryLogs(payload.agent_id);
    this.pollRecoverySoon(payload.agent_id);
  }

  private pollInfoSoon(): void {
    this.deps.schedule(
      () => void this.pollInfoOnce(),
      OPERATION_POLL_INTERVAL_MS,
    );
  }

  async pollInfoOnce(): Promise<void> {
    if (this.isStopped) return;
    const outcomeCount = this.recoveryOutcomeCount;
    const payload = await this.fetchInfo();
    if (this.isStopped) return;
    // A failed read is not news about the machine. The last good reading stays
    // on screen -- replacing a described condition with a fetch error would
    // tell the user less than they already had -- and the poll keeps running,
    // unbounded on purpose: this loop lives only as long as the card is open,
    // and the endpoint it reads is the local app's own.
    //
    // A reading that was in flight while the recovery being followed reported
    // its outcome is dropped whole: the server moves the tracker out of
    // "recovering" before the operation reports done, so such a reading still
    // says "recovering" and would re-report a recovery that has finished.
    if (payload !== null && outcomeCount === this.recoveryOutcomeCount) {
      this.loadError = null;
      this.applyInfo(payload);
      this.deps.redraw();
    }
    this.pollInfoSoon();
  }

  private consecutiveRecoveryPollFailures = 0;
  /** How many recoveries this model was following have reported an outcome.
   * Stamped on each recovery-info read so a late one can be told apart from a
   * current one. */
  private recoveryOutcomeCount = 0;
  /** Whether the recovery the tracker is still reporting is one this model has
   * stopped following. Cleared by the tracker leaving "recovering", and by a
   * recovery dispatched from here. */
  private isRecoveryFollowAbandoned = false;

  /**
   * Dispatch a host recovery for this machine and follow it.
   *
   * ``kind`` of "start" skips the stop step, running only the idempotent ``mngr
   * start``: the "open this stopped machine" click-through, which has nothing
   * to bounce. The card's own Restart workspace button asks for "restart", since
   * it may be aimed at a running-but-wedged container that only a bounce fixes.
   */
  async dispatchRecovery(kind: RecoveryKind = "restart"): Promise<void> {
    const agentId = this.agentId;
    if (agentId === null || this.isRecoveryRunning) return;
    this.isRecoveryRunning = true;
    this.dispatchedRecoveryKind = kind;
    this.followedRecoveryKind = kind;
    this.recoveryError = null;
    this.isRecoveryFollowLost = false;
    this.recoveryNotice = null;
    this.isRecoverySucceeded = false;
    this.consecutiveRecoveryPollFailures = 0;
    this.isRecoveryFollowAbandoned = false;
    this.logLines = [];
    this.deps.redraw();
    const result = await this.deps.postJson(
      `/api/v1/workspaces/${encodeURIComponent(agentId)}/restart`,
      {
        scope: "host",
        // The body field keeps its wire name: agents inside workspaces post it.
        start_only: kind === "start",
      },
    );
    // The card may have gone away while the POST was in flight; a stopped model
    // must not open a log stream nothing will ever close.
    if (this.isStopped) return;
    // Status 0 is a POST the app's own server never answered: nothing was dispatched.
    if (result.status === 0 || result.status >= 400) {
      this.isRecoveryRunning = false;
      this.recoveryError = recoveryDispatchError(kind, result);
      this.deps.redraw();
      return;
    }
    this.streamRecoveryLogs(agentId);
    this.pollRecoverySoon(agentId);
  }

  private pollRecoverySoon(agentId: string): void {
    this.deps.schedule(
      () => void this.pollRecoveryOnce(agentId),
      OPERATION_POLL_INTERVAL_MS,
    );
  }

  async pollRecoveryOnce(agentId: string): Promise<void> {
    if (this.isStopped) return;
    const payload = (await this.deps.getJson(
      `/api/v1/workspaces/operations/restart/${encodeURIComponent(agentId)}`,
    )) as RecoveryStatusPayload | null;
    if (this.isStopped) return;
    if (payload === null) {
      // Transient fetch failure: keep polling for a bounded run, exactly like
      // the backup/lifecycle pollers -- a permanent failure must not pin the
      // card on its in-flight state forever.
      this.consecutiveRecoveryPollFailures += 1;
      if (
        this.consecutiveRecoveryPollFailures >= MAX_CONSECUTIVE_POLL_FAILURES
      ) {
        this.isRecoveryRunning = false;
        // The tracker still calls this recovery running, and will until it
        // ends. Marking it abandoned is what keeps the recovery-info poll from
        // re-attaching to it and starting this same bounded run over.
        this.isRecoveryFollowAbandoned = true;
        // No longer "reload to find out": the card's own state poll is a
        // separate loop, still running against the same origin, and it is what
        // reports where the machine ended up.
        this.isRecoveryFollowLost = true;
        this.deps.redraw();
        return;
      }
      this.pollRecoverySoon(agentId);
      return;
    }
    this.consecutiveRecoveryPollFailures = 0;
    if (payload.status === "RUNNING") {
      this.pollRecoverySoon(agentId);
      return;
    }
    this.isRecoveryRunning = false;
    this.recoveryOutcomeCount += 1;
    if (payload.status === "DECLINED") {
      // Refused before anything changed: the machine is no more answering
      // than before, so this is not a success the page may leave on.
      this.recoveryNotice =
        payload.warning ?? RECOVERY_DECLINED_FALLBACK_MESSAGE;
    } else if (payload.is_done === true) {
      this.isRecoverySucceeded = true;
    } else {
      // The card words this from the step that failed once the workspace's
      // state reports it; until then it has only the operation's own account.
      this.recoveryError = {
        message: recoveryFailureSentence(null, this.followedRecoveryKind),
        details: payload.error ?? "",
      };
    }
    this.deps.redraw();
  }

  private streamRecoveryLogs(agentId: string): void {
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
    async postJson(url: string, body: unknown): Promise<RequestOutcome> {
      try {
        const response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body ?? {}),
        });
        return {
          status: response.status,
          body: response.ok ? null : await readResponseBody(response),
        };
      } catch {
        return { status: 0, body: null };
      }
    },
    async deleteResource(url: string): Promise<number> {
      try {
        const response = await fetch(url, {
          method: "DELETE",
          credentials: "same-origin",
        });
        return response.status;
      } catch {
        return 0;
      }
    },
    openEventSource(url: string): EventSourceLike {
      // Adapter over the browser EventSource so the model-facing surface
      // stays the minimal string-data shape tests can fake.
      const source = new EventSource(url);
      const wrapper: EventSourceLike = {
        close: () => source.close(),
        onmessage: null,
        onerror: null,
      };
      source.onmessage = (event) =>
        wrapper.onmessage?.({ data: String(event.data) });
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
export async function downloadSnapshotExport(
  agentId: string,
  snapshotId: string,
  // An archive id exports from one of the workspace's previous backup
  // destinations; null exports from its current backups.
  repositoryId: string | null = null,
): Promise<boolean> {
  let response: Response;
  const query =
    repositoryId === null
      ? ""
      : `?repository=${encodeURIComponent(repositoryId)}`;
  try {
    response = await fetch(
      `/api/v1/workspaces/${encodeURIComponent(agentId)}/backups/${encodeURIComponent(snapshotId)}/export${query}`,
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
