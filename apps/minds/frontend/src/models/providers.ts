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
  }
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
    const noun = entry.workspace_count === 1 ? "machine" : "machines";
    return {
      isEnabled: false,
      tooltip: `In use by ${entry.workspace_count} ${noun} — destroy them first.`,
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

  /** Every provider instance with an error verdict, for the panel's collapsed summary. */
  errorCount(): number {
    let count = this.deviceProviders.filter(
      (entry) => entry.status === "ERROR",
    ).length;
    for (const account of this.accounts) {
      if (account.imbue_cloud?.status === "ERROR") count += 1;
      count += account.connections.filter(
        (entry) => entry.status === "ERROR",
      ).length;
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
