// Providers-panel state: one section per account (plus the local profile)
// with its connections, and the device providers. The pure row decisions the
// panel and the add-connection modal make live here so they are testable.

import type { UiProvidersMessage } from "../channel/messages";
import type {
  ConnectionPanelStatus,
  ProviderPanelStatus,
  UiAccountProvidersEntry,
  UiConnectionEntry,
  UiProviderEntry,
} from "../generated/ui";
import type { ErrorDialogRequest } from "./errorDialog";
import { titledError } from "./errorDialog";
import type { ErrorAction, RequestOutcome, UserFacingError } from "./errors";
import { asSentence, errorCodeFromBody, errorFromResponse, errorFromSentenceResponse } from "./errors";

/** The owner the local profile's section reports (never synced). */
export const LOCAL_PROFILE_USER_ID = "local";

export interface ConnectionOwnerOption {
  userId: string;
  label: string;
}

export interface StatusChip {
  label: string;
  tone: "ok" | "warn" | "error" | "muted";
}

/** The removal route's code for a connection something still depends on; its sentence says what. */
const CONNECTION_IN_USE_CODE = "connection_in_use";
/** The removal route's code for a connection a newer version of the app wrote. */
const CONNECTION_TOO_NEW_CODE = "connection_too_new";

/**
 * The dialog for a connection that could not be removed: the route's own
 * sentence when the connection is still in use (workspaces on it, a create or
 * credential change under way), ours with `updateActions` when a newer version
 * of the app wrote it. Anything else gets the fallback naming the connection.
 * The response is the details.
 */
export function connectionRemovalDialog(
  alias: string,
  outcome: RequestOutcome,
  updateActions: readonly ErrorAction[],
): ErrorDialogRequest {
  const whatFailed = `Couldn't remove ${alias}`;
  switch (errorCodeFromBody(outcome.body)) {
    case CONNECTION_IN_USE_CODE:
      return titledError(
        whatFailed,
        errorFromSentenceResponse(whatFailed, outcome),
      );
    case CONNECTION_TOO_NEW_CODE:
      return {
        ...titledError(
          whatFailed,
          errorFromResponse(
            whatFailed,
            outcome,
            {
              plainReason:
                `A newer version of Imbue Studio added ${alias}, so this version can't remove it. ` +
                "*Update Imbue Studio, then try again.*",
            },
          ),
        ),
        actions: updateActions,
      };
    default:
      return titledError(
        whatFailed,
        errorFromResponse(whatFailed, outcome),
      );
  }
}

/** A connection as the compute select offers it, so the creation summary cannot name it differently. */
export function connectionLaunchLabel(alias: string, backendLabel: string): string {
  return `${alias} (${backendLabel})`;
}

/** What a provider instance row's status chip says (a device provider, or an account's imbue_cloud). */
export function providerStatusChipFor(status: ProviderPanelStatus): StatusChip {
  switch (status) {
    case "OK":
      return { label: "OK", tone: "ok" };
    case "ERROR":
      return { label: "Error", tone: "error" };
    case "UNLISTED":
      return { label: "Starting…", tone: "muted" };
  }
}

/** What a connection row's status chip says. */
export function connectionStatusChipFor(
  status: ConnectionPanelStatus,
): StatusChip {
  switch (status) {
    case "OK":
      return { label: "OK", tone: "ok" };
    case "ERROR":
      return { label: "Error", tone: "error" };
    case "UNLISTED":
      return { label: "Starting…", tone: "muted" };
    case "LOCKED":
      return { label: "Locked", tone: "warn" };
    case "NO_CREDENTIALS":
      return { label: "No credentials", tone: "warn" };
    case "READ_ONLY":
      return { label: "Needs a newer app", tone: "warn" };
    case "PROFILE_MISSING":
      return { label: "Set up on this computer", tone: "warn" };
  }
}

/** What a row says under its name line, and the raw error behind See details. */
export interface RowProblem extends UserFacingError {
  /** True for a check that failed; false for a setup step the user takes,
   * which the notice's own Check again confirms. */
  isFailedCheck: boolean;
}

const MAY_BE_MISSING_SENTENCE = "Workspaces on it may be missing from the list.";

function rawErrorText(entry: Pick<UiProviderEntry, "error_type" | "error_message">): string {
  if (!entry.error_message) return "";
  return entry.error_type ? `${entry.error_type}: ${entry.error_message}` : entry.error_message;
}

/** A failed check's row text: the provider's own reason, which is often all
 * the user needs to fix it, what it means for the list, and the fix the
 * provider named. */
function failedCheckMessage(
  entry: Pick<UiProviderEntry, "error_message" | "error_reason" | "remediation">,
  subject: "account" | "connection" | "provider",
): string {
  const reason = (entry.error_reason || entry.error_message || "").trim();
  const reasonSentence = reason === "" ? `Couldn't check this ${subject}.` : asSentence(reason);
  const fix = entry.remediation ? ` To fix it, *${entry.remediation}*.` : "";
  return `${reasonSentence} ${MAY_BE_MISSING_SENTENCE}${fix}`;
}

/**
 * What a provider instance row says when its last check failed, or null when
 * it has nothing to say. `subject` names the row the way its section does: an
 * account's Imbue Cloud row is the account, a row under this computer's own
 * providers is a provider.
 */
export function providerRowProblemFor(
  entry: Pick<UiProviderEntry, "status" | "error_type" | "error_message" | "error_reason" | "remediation">,
  subject: "account" | "provider",
): RowProblem | null {
  if (entry.status !== "ERROR") return null;
  return {
    message: failedCheckMessage(entry, subject),
    details: rawErrorText(entry),
    isFailedCheck: true,
  };
}

/**
 * What a connection row says under its name line, or null when it has
 * nothing to say: for a login this computer lacks, the setup step to take;
 * for any other failed check, the provider's reason and its fix. The raw
 * error, with its type, goes behind See details either way.
 */
export function connectionRowProblemFor(
  entry: Pick<UiConnectionEntry, "status" | "error_type" | "error_message" | "error_reason" | "remediation">,
): RowProblem | null {
  if (entry.status === "PROFILE_MISSING") {
    const fix = entry.remediation ? ` To use it here, *${entry.remediation}, then check again*.` : "";
    return {
      message: `This connection uses a login that is not set up on this computer.${fix}`,
      details: rawErrorText(entry),
      isFailedCheck: false,
    };
  }
  if (entry.status !== "ERROR") return null;
  return {
    message: failedCheckMessage(entry, "connection"),
    details: rawErrorText(entry),
    isFailedCheck: true,
  };
}

/** The providers panel's collapsed header: how many rows, and how many of
 * them need the user's attention. */
export function providersSummaryFor(rowCount: number, attentionCount: number): string {
  const rows = `${rowCount} ${rowCount === 1 ? "provider" : "providers"}`;
  if (attentionCount === 0) return rows;
  return `${rows}, ${attentionCount} ${attentionCount === 1 ? "needs" : "need"} attention`;
}

interface RemoveControl {
  isEnabled: boolean;
  tooltip: string;
}

/** Whether a connection's Remove button is usable, and why not when it is not. */
export function removeControlFor(
  entry: Pick<UiConnectionEntry, "workspace_count" | "status">,
): RemoveControl {
  if (entry.status === "READ_ONLY") {
    return {
      isEnabled: false,
      tooltip: "This connection was added by a newer version of the app.",
    };
  }
  if (entry.workspace_count > 0) {
    const isOne = entry.workspace_count === 1;
    return {
      isEnabled: false,
      tooltip: `In use by ${entry.workspace_count} ${isOne ? "workspace" : "workspaces"}. Remove ${isOne ? "it" : "them"} first.`,
    };
  }
  return { isEnabled: true, tooltip: "Remove this connection" };
}

interface EditControl {
  isEnabled: boolean;
  tooltip: string;
}

/**
 * Whether a connection's Edit button is usable, and why not when it is not.
 * An update replaces the stored credentials whole, so it needs them usable
 * here: a locked, scrubbed, or newer-format row has nothing to replace.
 */
export function editControlFor(
  entry: Pick<UiConnectionEntry, "status">,
): EditControl {
  switch (entry.status) {
    case "LOCKED":
      return {
        isEnabled: false,
        tooltip: "Enter your master password to edit this connection",
      };
    case "NO_CREDENTIALS":
      return {
        isEnabled: false,
        tooltip:
          "This connection's credentials are not available on this device.",
      };
    case "READ_ONLY":
      return {
        isEnabled: false,
        tooltip: "This connection was added by a newer version of the app.",
      };
    case "PROFILE_MISSING":
      return { isEnabled: true, tooltip: "Use another login or paste keys instead" };
    case "OK":
    case "ERROR":
    case "UNLISTED":
      return { isEnabled: true, tooltip: "Replace this connection's credentials" };
  }
}

/** The section heading for an account, or the local profile. */
export function accountSectionTitleFor(
  entry: Pick<UiAccountProvidersEntry, "user_id" | "email" | "state">,
): string {
  if (entry.user_id === LOCAL_PROFILE_USER_ID) return "This device only";
  const name = entry.email || `${entry.user_id.slice(0, 8)}…`;
  switch (entry.state) {
    case "SIGNED_IN":
      return name;
    case "LOCKED":
      return `${name} (locked)`;
    case "SIGNED_OUT":
      return `${name} (signed out)`;
  }
}

export class ProvidersStore {
  accounts: readonly UiAccountProvidersEntry[] = [];
  deviceProviders: readonly UiProviderEntry[] = [];
  lastEventAt: string | null = null;
  lastFullSnapshotAt: string | null = null;

  applyProvidersMessage(message: UiProvidersMessage): void {
    this.accounts = message.accounts;
    this.deviceProviders = message.device_providers;
    this.lastEventAt = message.last_event_at;
    this.lastFullSnapshotAt = message.last_full_snapshot_at;
  }

  /** The owners a new synced connection can be added under here, the local profile last. */
  connectionOwnerOptions(): ConnectionOwnerOption[] {
    const options: ConnectionOwnerOption[] = [];
    for (const account of this.accounts) {
      if (
        account.user_id === LOCAL_PROFILE_USER_ID ||
        !account.is_connection_owner_eligible
      )
        continue;
      options.push({
        userId: account.user_id,
        label: account.email || account.user_id,
      });
    }
    options.push({ userId: LOCAL_PROFILE_USER_ID, label: "This device only" });
    return options;
  }

  /** Every row whose chip asks something of the user (an error or warning
   * tone), for the panel's collapsed summary. */
  attentionCount(): number {
    const needsAttention = (chip: StatusChip): boolean => chip.tone === "error" || chip.tone === "warn";
    let count = this.deviceProviders.filter((entry) => needsAttention(providerStatusChipFor(entry.status))).length;
    for (const account of this.accounts) {
      if (account.imbue_cloud !== null && needsAttention(providerStatusChipFor(account.imbue_cloud.status))) count += 1;
      count += account.connections.filter((entry) => needsAttention(connectionStatusChipFor(entry.status))).length;
    }
    return count;
  }

  /** Every row the panel renders (account instances, connections, device providers). */
  rowCount(): number {
    let count = this.deviceProviders.length;
    for (const account of this.accounts) {
      count +=
        (account.imbue_cloud === null ? 0 : 1) + account.connections.length;
    }
    return count;
  }
}
