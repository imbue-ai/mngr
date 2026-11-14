// The Machines-list row rules, extracted from LandingPage's row renderer so
// they are a testable pure decision: Start is offered only for a
// shutdown-capable machine that is STOPPED and whose stop is the owner's to
// end (its stop kind is owner-startable), Stop only for one that is RUNNING,
// and neither during transitions or when the liveness is unknown.
//
// A dead discovery consumer makes every row's data stale at once. Nothing is
// arriving to correct it, so the row stops claiming to know a state and stops
// offering actions computed from one -- an action taken on a frozen reading
// is worse than no action offered.

import type { UiWorkspaceEntry } from "../../channel/messages";
import type { RemoteTileState } from "../../generated/ui";
import type { RefusalNotice, RequestOutcome, UserFacingError } from "../../models/errors";
import {
  NO_RESPONSE_SENTENCE,
  asSentence,
  errorCodeFromBody,
  errorFromResponse,
  errorMessageFromBody,
  fallbackErrorSentence,
  plainErrorText,
  providerReasonFromBody,
  responseDetails,
} from "../../models/errors";
import { reconnectError } from "../../models/reconnect";
import type {
  DiscoveryHealth,
  EnvironmentCondition,
  RecoveryFailureCause,
  RecoveryKind,
  WorkspaceHealth,
} from "../../models/health";
import type { MindLiveness } from "../../models/create";
import { MIND_LIVENESS_LABELS } from "../../models/create";

export interface MindControls {
  isStartShown: boolean;
  isStopShown: boolean;
}

/** The stop kinds whose stop is the owner's to end. "" is a running machine, a
 * stop recorded before the connector had kinds, or a kind not yet read; a kind
 * this build does not know ("unknown") is a hold: shown, not actionable. */
export function isOwnerStartableStopKind(stopKind: string): boolean {
  return stopKind === "" || stopKind === "owner" || stopKind === "idle";
}

/** The sentence a held machine's surfaces show; the connector answers a
 * refused start with the same words, so the two never disagree. */
export const MAINTENANCE_MESSAGE = "This workspace is undergoing maintenance and will be back shortly.";

/** The sentence a retired machine's surfaces show: it never runs again, and
 * its data lives in its backups (or in an archive support hands out). The
 * connector answers a refused start with the same words. */
export const RETIRED_MESSAGE =
  "This workspace has been retired and cannot be started again. " +
  "*Download its data from its backups, or contact support if it has none.*";

/** The sentence a suspended account's surfaces show, in Imbue Cloud's own words. */
export const SUSPENDED_MESSAGE =
  "This account is suspended. *If you believe this is a mistake, contact support@imbue.com.*";

export interface LivenessBadge {
  label: string;
  /** Why the row cannot be started or opened, for a hold; "" otherwise. */
  tooltip: string;
}

/** What the liveness badge says: the lifecycle label, except that a machine an
 * operator is holding says so instead of "Stopped" (the badge is the one
 * place the row explains why Start is missing), with why on its tooltip. */
export function livenessBadgeFor(liveness: string, stopKind: string): LivenessBadge {
  if (isMaintenanceHold(liveness, stopKind))
    return { label: "Maintenance", tooltip: plainErrorText(MAINTENANCE_MESSAGE) };
  if (isRetiredHold(liveness, stopKind)) {
    return {
      label: "Retired",
      tooltip:
        "This workspace has been retired, so it can't be started again. " +
        "Its backups are still available from the Backups button.",
    };
  }
  if (isSuspensionHold(liveness, stopKind))
    return { label: "Suspended", tooltip: plainErrorText(SUSPENDED_MESSAGE) };
  if (isHeldStop(liveness, stopKind)) {
    return {
      label: "Unavailable",
      tooltip: "Imbue Cloud is holding this workspace stopped, so it can't be started from here.",
    };
  }
  return { label: MIND_LIVENESS_LABELS[liveness] ?? "Status unknown", tooltip: "" };
}

/** Whether a machine is stopped, or on its way there, under an operator's
 * maintenance hold: the badge and the notice band name it. */
export function isMaintenanceHold(liveness: string, stopKind: string): boolean {
  return stopKind === "maintenance" && (liveness === "STOPPED" || liveness === "STOPPING");
}

/** Whether a machine is stopped, or on its way there, as a retired one: the
 * badge and the notice band name it too, since the user's next step (the
 * backups) differs from waiting out a maintenance hold. */
export function isRetiredHold(liveness: string, stopKind: string): boolean {
  return stopKind === "retired" && (liveness === "STOPPED" || liveness === "STOPPING");
}

/** Whether a machine is stopped, or on its way there, because its owner's
 * account is suspended. */
export function isSuspensionHold(liveness: string, stopKind: string): boolean {
  return stopKind === "suspension" && (liveness === "STOPPED" || liveness === "STOPPING");
}

/** Whether a machine is stopped, or on its way there, under any stop its owner
 * cannot end. */
function isHeldStop(liveness: string, stopKind: string): boolean {
  return !isOwnerStartableStopKind(stopKind) && (liveness === "STOPPED" || liveness === "STOPPING");
}

/** Whether the app has any current reading of a machine's state at all. */
export function isMachineStateKnown(discoveryHealth: DiscoveryHealth): boolean {
  return discoveryHealth !== "blocked";
}

export function mindControlsFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown" | "stop_kind">,
  liveness: MindLiveness,
  discoveryHealth: DiscoveryHealth,
): MindControls {
  const isShutdownSupported = (entry.supports_shutdown ?? false) && isMachineStateKnown(discoveryHealth);
  return {
    isStartShown: isShutdownSupported && liveness === "STOPPED" && isOwnerStartableStopKind(entry.stop_kind ?? ""),
    isStopShown: isShutdownSupported && liveness === "RUNNING",
  };
}

/**
 * The error for a Start or Stop the app could not carry out.
 *
 * A start Imbue Cloud refused because it holds the workspace says why in Imbue
 * Cloud's own words (a suspension reads as maintenance on the wire, so the
 * row's stop kind tells the two apart). Anything else names what failed,
 * without claiming it changed nothing: the command may have got further than
 * its answer says (a start that timed out is still running).
 */
export function lifecycleFailureError(
  action: "start" | "stop",
  name: string,
  stopKind: string,
  outcome: RequestOutcome,
): LifecycleFailure {
  const reason = errorMessageFromBody(outcome.body, "");
  const details = responseDetails(outcome);
  // The connector answers with these sentences as plain text; ours carry the
  // copy markup, so the two are compared without it.
  if (action === "start" && reason.includes(plainErrorText(RETIRED_MESSAGE))) {
    return { error: { message: RETIRED_MESSAGE, details }, isHold: true };
  }
  if (action === "start" && reason.includes(plainErrorText(MAINTENANCE_MESSAGE))) {
    const message = stopKind === "suspension" ? SUSPENDED_MESSAGE : MAINTENANCE_MESSAGE;
    return { error: { message, details }, isHold: true };
  }
  // The workspace's provider is what failed: its own reason says what to do.
  const providerReason = providerReasonFromBody(outcome.body);
  return {
    error: errorFromResponse(
      `Couldn't ${action} "${name}"`,
      outcome,
      {
        plainReason:
          providerReason === null ? null : `Couldn't ${action} "${name}". ${asSentence(providerReason)}`,
      },
    ),
    isHold: false,
  };
}

export interface LifecycleFailure {
  error: UserFacingError;
  /** Imbue Cloud is holding the workspace: the sentence explains it in full. */
  isHold: boolean;
}

/** A refused remove-from-list, and whether updating Imbue Studio is its remedy. */
export interface RemoveRecordRefusal extends RefusalNotice {
  isUpdateAppOffered: boolean;
}

/** How a refused or unanswered remove-from-list is told; restarting Imbue
 * Studio is the remedy when the window's own login lapsed, and updating it when
 * a newer version set the workspace up. */
export function removeRecordFailureFor(outcome: RequestOutcome): RemoveRecordRefusal {
  const details = responseDetails(outcome);
  const explained = (message: string, isUpdateAppOffered = false): RemoveRecordRefusal => ({
    error: { message, details },
    isBugReportOffered: false,
    isRestartAppOffered: false,
    isUpdateAppOffered,
  });
  if (outcome.status === 0) return explained(NO_RESPONSE_SENTENCE);
  // This route refuses a lapsed window login with a 403, not the 401 `isReconnectNeeded` reads.
  if (outcome.status === 403)
    return {
      error: reconnectError(outcome),
      isBugReportOffered: false,
      isRestartAppOffered: true,
      isUpdateAppOffered: false,
    };
  const code = errorCodeFromBody(outcome.body);
  if (code === "LEASE_ACTIVE")
    return explained(
      "This workspace's machine is still held in Imbue Cloud, so it can't just be taken off the list. " +
        "*Remove the workspace instead.*",
    );
  if (code === "RECORD_TOO_NEW")
    return explained(
      "This workspace was set up by a newer version of Imbue Studio. *Update Imbue Studio to take it off the list.*",
      true,
    );
  return {
    error: { message: fallbackErrorSentence("Couldn't take this workspace off the list"), details },
    isBugReportOffered: true,
    isRestartAppOffered: false,
    isUpdateAppOffered: false,
  };
}

/** The question a stop or restart asks first, or null for none. An update
 * that is preparing leaves the live machine untouched, so it changes nothing
 * here; only the apply is rewriting the machine, and stopping under it is
 * what leaves one half-updated -- so that is the one case the question names. */
export function lifecycleConfirmation(action: "stop" | "restart", name: string, isApplying: boolean): string | null {
  if (action === "restart") {
    return isApplying
      ? `Restart "${name}"? An update is being applied to it right now, and restarting can leave it half-updated. ` +
          "Restart anyway?"
      : null;
  }
  const consequence = "Its agents will stop and its services become inaccessible. Data is preserved and you can start it again.";
  return isApplying
    ? `Stop "${name}"? An update is being applied to it right now, and stopping can leave it half-updated. ` +
        `${consequence} Stop anyway?`
    : `Stop "${name}"? ${consequence}`;
}

/** What clicking a machines-list row should do, as a testable pure decision.
 * "blocked" is a row whose click could go nowhere, so the row is not
 * clickable: a cloud machine this device holds no SSH key for (nothing on the
 * far side would answer; its chip says so), or a stop an operator holds (no
 * Start is offered; the badge names the hold). */
export type RowClickAction = "enter" | "recover" | "recover-start" | "blocked";

// ``liveness`` is a plain string (only equality against the lifecycle names
// matters) so callers without a MindLivenessTracker (e.g. CreateTemplatePage)
// can pass the entry's raw liveness field directly.
export function rowClickActionFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown" | "key_state" | "stop_kind">,
  liveness: string,
  isHealthy: boolean,
): RowClickAction {
  // A missing key outranks health: the machine reads unreachable from here
  // precisely because this device cannot connect to it, and recovery could
  // not change that.
  if ((entry.key_state ?? "") !== "") return "blocked";
  // A stop an operator holds is not the owner's to end, so a click has
  // nowhere useful to go: no Start is offered, and the badge names the hold.
  const isStoppedOnPurposeByOthers =
    (liveness === "STOPPED" || liveness === "STOPPING") && !isOwnerStartableStopKind(entry.stop_kind ?? "");
  if (isStoppedOnPurposeByOthers) return "blocked";
  if (!isHealthy) return "recover";
  if ((entry.supports_shutdown ?? false) && liveness === "STOPPED") {
    // A stopped container cannot be entered: go straight to Recovery, which
    // dispatches the idempotent start (with progress + logs) for every
    // backend -- its stop/start budgets are shared with the Start/Stop
    // buttons, so even a cloud restore taking minutes reports honestly.
    return "recover-start";
  }
  return "enter";
}

/**
 * What a row's health badge says, or null for a machine with nothing to report.
 *
 * The badge states what was observed, not what the app is attempting. Three of
 * the four readings would otherwise overstate:
 *
 * A connection that failed on this device outranks every reading below it,
 * because it explains them rather than restating them: the machine reads
 * unhealthy *because* this device could not build a connection to it, and may
 * well be running fine and serving other devices. This is the same rank the
 * recovery card and the shell's notice band give it, and it has to be, or the
 * list keeps blaming a machine that the card behind it exonerates -- which is
 * the misdiagnosis the whole decomposition exists to end, surviving in the one
 * surface that never read the verdict.
 *
 * The "recovering" wire state covers two different things: the user's own full
 * bounce, and the start the app fires at a machine that stopped answering. Only
 * the first is a restart. The second is idempotent and no-ops against a host
 * that is already up, so calling it one tells the user their work was
 * interrupted when nothing happened to the machine -- it is reported as the
 * connection being re-established instead. `recoveryKind` is the same evidence
 * the recovery card picks its heading from, read here so the two surfaces
 * cannot describe one episode differently; its null (no recovery the tracker
 * can describe) takes the weaker reading, as the card does. (The caller
 * suppresses the badge for a machine whose liveness reads STOPPED or
 * transitional, which is where the card says "Bringing ... back online"
 * instead.)
 *
 * The last reading, "recovery_failed", is where the badge would overstate one
 * step later. When the dispatched start reported it booted no host, or the
 * recovery failed before it ran anything, the machine was never taken down and
 * brought back, so it reads as a machine that never answered. Otherwise the
 * failure is named after the recovery that ran: only the user's own restart
 * failed as a restart; the app's start failed to reconnect.
 */
export function healthBadgeLabelFor(
  health: WorkspaceHealth,
  isRecoveryANoOp: boolean,
  recoveryKind: RecoveryKind | null,
  isDeviceCannotConnect: boolean,
  recoveryFailureCause: RecoveryFailureCause | null = null,
): string | null {
  if (health === "healthy") return null;
  if (isDeviceCannotConnect) return "Can't connect from this device";
  if (health === "stuck") return "Not responding";
  if (health === "recovering") return recoveryKind === "restart" ? "Restarting..." : "Reconnecting...";
  const isNothingRun =
    recoveryFailureCause === "WORKSPACE_NOT_FOUND" || recoveryFailureCause === "WORKER_NOT_STARTED";
  if (isRecoveryANoOp || isNothingRun) return "Not responding";
  return recoveryKind === "restart" ? "Restart failed" : "Couldn't reconnect";
}

/** Whether this device's own network explains a workspace not answering, so
 * its row should not blame the workspace: the device is offline or on a network
 * that blocks the connection, and the workspace needs the network to be
 * reached. The page's notice names the device's condition instead. */
export function isOutageExplainedByDevice(
  environment: EnvironmentCondition,
  isWorkspaceNetworkDependent: boolean,
): boolean {
  return isWorkspaceNetworkDependent && (environment === "OFFLINE" || environment === "SSH_BLOCKED");
}

/** The location badge of a remote row: a cloud workspace is named by its
 * provider (it lives there, not on any device); an other-device machine by
 * the device that hosts it. */
export function remoteLocationBadgeFor(
  entry: Pick<UiWorkspaceEntry, "remote_kind" | "location">,
): string {
  const location = entry.location ?? "";
  return (entry.remote_kind ?? "") === "cloud" ? location : `on ${location}`;
}

export interface RemoteStateChip {
  label: string;
  /** Whether the chip reports a problem (rendered in the important tone). */
  isImportant: boolean;
  /** What the chip means, on hover. */
  tooltip: string;
  /** For a failure, what a click on the chip shows: our sentence, with the
   * server's detail behind See details. */
  error: UserFacingError | null;
}

const ACCESS_SETUP_FAILED_SENTENCE = "Setting up this computer's access to this workspace failed.";

/** The chip a remote row shows for its derived access state, or null for the plain state. */
export function remoteStateChipFor(
  tile: Pick<RemoteTileState, "state" | "detail">,
): RemoteStateChip | null {
  switch (tile.state) {
    case "connecting":
      return {
        label: "Connecting…",
        isImportant: false,
        tooltip: "Imbue Studio is setting up this computer's access to this workspace.",
        error: null,
      };
    case "unreachable":
      return {
        label: "Unreachable",
        isImportant: true,
        tooltip:
          "Imbue Studio can't reach this workspace from this computer. Its machine may have been released, " +
          "or this computer's access to it stopped working.",
        error: null,
      };
    case "error":
      return {
        label: "Can't connect from here",
        isImportant: true,
        tooltip: `${ACCESS_SETUP_FAILED_SENTENCE} Click to see details.`,
        error: {
          message: ACCESS_SETUP_FAILED_SENTENCE,
          details: tile.detail ?? "",
        },
      };
    case "locked":
      return {
        label: "Locked",
        isImportant: false,
        tooltip: "Enter your master password to reach this workspace from this computer.",
        error: null,
      };
    case "missing_connection":
      return {
        label: "Connection removed",
        isImportant: true,
        tooltip:
          "The connection this workspace runs on was removed from your account. Add it again, or remove this " +
          "workspace from the list.",
        error: null,
      };
    case "no_credentials":
      return {
        label: "No credentials here",
        isImportant: true,
        tooltip: "The credentials of the connection this workspace runs on aren't available on this computer.",
        error: null,
      };
    case "read_only":
      return {
        label: "Needs a newer app",
        isImportant: true,
        tooltip:
          "The connection this workspace runs on was added by a newer version of Imbue Studio. Update Imbue " +
          "Studio to reach this workspace from this computer.",
        error: null,
      };
    default:
      return null;
  }
}

export interface KeyStateChip {
  label: string;
  tooltip: string;
}

/** The chip a live cloud row shows when this device cannot open it, or null when it can. */
export function keyStateChipFor(keyState: string): KeyStateChip | null {
  switch (keyState) {
    case "locked":
      return {
        label: "Enter your master password to open",
        tooltip: "This workspace's access key is synced to your account; unlock it above to open the workspace here",
      };
    case "syncing":
      return {
        label: "Syncing access…",
        tooltip: "This workspace's access key has not reached this device yet; it arrives with the next sync",
      };
    case "unavailable":
      return {
        label: "No access from this device",
        tooltip: "This workspace's access key was never synced: set a master password on the device that created it",
      };
    case "rejected":
      return {
        label: "Access refused from this device",
        tooltip:
          "The machine refused this device's access key, so restarting will not help: open it from the device that created it, or re-sync its record here",
      };
    default:
      return null;
  }
}

export interface BackupsControl {
  isShown: boolean;
  isEnabled: boolean;
  tooltip: string;
}

const BACKUPS_HIDDEN: BackupsControl = {
  isShown: false,
  isEnabled: false,
  tooltip: "",
};

/**
 * Whether a row offers its Backups button, and whether it is usable.
 *
 * The backups page runs restic from this device, so it needs no live
 * workspace: a remote row offers it whenever this device can read the
 * credentials, and explains itself (disabled) when it cannot -- locked behind
 * the master password, or never synced here. A live row offers it while the
 * machine is STOPPED, when the workspace's own settings (the other way in) are
 * unreachable.
 */
export function backupsControlFor(
  entry: Pick<UiWorkspaceEntry, "is_remote" | "backup_access">,
  liveness: string,
): BackupsControl {
  if (entry.is_remote ?? false) {
    switch (entry.backup_access ?? "") {
      case "available":
        return { isShown: true, isEnabled: true, tooltip: "Backups" };
      case "locked":
        return {
          isShown: true,
          isEnabled: false,
          tooltip:
            "Unlock this account with your master password to access this workspace's backups",
        };
      case "unavailable":
        return {
          isShown: true,
          isEnabled: false,
          tooltip:
            "Backups aren't reachable from this device. Set a master password on the device that created " +
            "this workspace to access them from other devices.",
        };
      default:
        return BACKUPS_HIDDEN;
    }
  }
  return liveness === "STOPPED"
    ? { isShown: true, isEnabled: true, tooltip: "Backups" }
    : BACKUPS_HIDDEN;
}

/**
 * The record-only entries that get a remote tile. A workspace this device is
 * destroying (or failed to destroy) keeps an active record until the destroy
 * finishes and discovery catches up, so it would otherwise read as a remote
 * tile whose "Remove from this list" control drops its record mid-destroy. A
 * failed destroy whose host is gone is listed on its own row instead.
 */
export function remoteTileEntries<Entry extends Pick<UiWorkspaceEntry, "id" | "is_remote">>(
  entries: readonly Entry[],
  orphanedFailedDestroyAgentIds: readonly string[],
  isDestroying: (agentId: string) => boolean,
): Entry[] {
  return entries.filter(
    (entry) =>
      (entry.is_remote ?? false) &&
      !orphanedFailedDestroyAgentIds.includes(entry.id) &&
      !isDestroying(entry.id),
  );
}
