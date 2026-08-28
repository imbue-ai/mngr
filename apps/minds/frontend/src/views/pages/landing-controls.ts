// The Machines-list row rules, extracted from LandingPage's row renderer so
// they are a testable pure decision: Start is offered only for a
// shutdown-capable machine that is STOPPED, Stop only for one that is
// RUNNING, and neither during transitions or when the liveness is unknown.
//
// A dead discovery consumer makes every row's data stale at once. Nothing is
// arriving to correct it, so the row stops claiming to know a state and stops
// offering actions computed from one -- an action taken on a frozen reading
// is worse than no action offered.

import type { UiWorkspaceEntry } from "../../channel/messages";
import type { DiscoveryHealth, RecoveryKind, WorkspaceHealth } from "../../models/health";
import type { MindLiveness } from "../../models/create";

export interface MindControls {
  isStartShown: boolean;
  isStopShown: boolean;
}

/** Whether the app has any current reading of a machine's state at all. */
export function isMachineStateKnown(discoveryHealth: DiscoveryHealth): boolean {
  return discoveryHealth !== "blocked";
}

export function mindControlsFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown">,
  liveness: MindLiveness,
  discoveryHealth: DiscoveryHealth,
): MindControls {
  const isShutdownSupported = (entry.supports_shutdown ?? false) && isMachineStateKnown(discoveryHealth);
  return {
    isStartShown: isShutdownSupported && liveness === "STOPPED",
    isStopShown: isShutdownSupported && liveness === "RUNNING",
  };
}

/** What clicking a machines-list row should do, as a testable pure decision. */
export type RowClickAction = "enter" | "recover" | "recover-start";

// ``liveness`` is a plain string (only the "STOPPED" comparison matters) so
// callers without a MindLivenessTracker (e.g. CreateTemplatePage) can pass
// the entry's raw liveness field directly.
export function rowClickActionFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown">,
  liveness: string,
  isHealthy: boolean,
): RowClickAction {
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
 * step later: when the dispatched start reported it booted no host, the machine
 * was up throughout -- it was never taken down and brought back, so a "Restart
 * failed" badge describes something the user did not experience. It reads as a
 * machine that never answered instead.
 */
export function healthBadgeLabelFor(
  health: WorkspaceHealth,
  isRecoveryANoOp: boolean,
  recoveryKind: RecoveryKind | null,
  isDeviceCannotConnect: boolean,
): string | null {
  if (health === "healthy") return null;
  if (isDeviceCannotConnect) return "Can't connect from this device";
  if (health === "stuck") return "Server not responding";
  if (health === "recovering") return recoveryKind === "restart" ? "Restarting..." : "Reconnecting...";
  return isRecoveryANoOp ? "Not responding" : "Restart failed";
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
  /** Whether clicking the chip should take the user to the Accounts page (the remedy for a signed-out provider). */
  isAccountsLink: boolean;
}

/** The chip a remote row shows for its derived access state, or null for the plain state. */
export function remoteStateChipFor(
  remoteState: string,
): RemoteStateChip | null {
  switch (remoteState) {
    case "signed_out":
      return { label: "Signed out", isImportant: true, isAccountsLink: true };
    case "connecting":
      return {
        label: "connecting…",
        isImportant: false,
        isAccountsLink: false,
      };
    case "unreachable":
      return { label: "unreachable", isImportant: true, isAccountsLink: false };
    case "error":
      return { label: "sync error", isImportant: true, isAccountsLink: false };
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
            "Unlock this account with your master password to access this machine's backups",
        };
      case "unavailable":
        return {
          isShown: true,
          isEnabled: false,
          tooltip:
            "Backups aren't reachable from this device. Set a master password on the device that created " +
            "this machine to access them from other devices.",
        };
      default:
        return BACKUPS_HIDDEN;
    }
  }
  return liveness === "STOPPED"
    ? { isShown: true, isEnabled: true, tooltip: "Backups" }
    : BACKUPS_HIDDEN;
}
