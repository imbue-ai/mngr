// Settings page model: loads the app-level settings payload from
// /ui/api/settings, tracks the active section + revoke dialog, and performs
// the page's writes (legacy POST routes for revokes/connectors/master
// password; the new If-Match-guarded /ui/api endpoint for the
// error-reporting toggle).
//
// The response interfaces are hand-written mirrors of the pydantic models in
// ui_api_settings.py (the generated schema currently covers only channel
// frames; see the tranche report).

import m from "mithril";

export interface GrantedPermission {
  label: string;
  description: string;
}

export interface WorkspaceServiceGrant {
  workspace_agent_id: string;
  workspace_name: string;
  color: string;
  permissions: GrantedPermission[];
}

export interface ServiceAccountOverview {
  account: string;
  label: string;
  is_connected: boolean;
  workspace_grants: WorkspaceServiceGrant[];
}

export interface ServicePermissionOverview {
  service_name: string;
  display_name: string;
  accounts: ServiceAccountOverview[];
  /** Whether latchkey can sign in to this service through a browser, which is
   * what "+ Add account" does. */
  is_browser_sign_in_supported: boolean;
}

/** Why "+ Add account" is unavailable for a service, or null when it works.
 *
 * The action is latchkey's browser sign-in, so a service without one (AWS,
 * Coolify, ...) has nothing for it to do; the dialog says so on hover instead
 * of failing after the click. */
export function addAccountBlockedReason(service: ServicePermissionOverview): string | null {
  if (service.is_browser_sign_in_supported) return null;
  return `${service.display_name} does not support signing in through a browser.`;
}

export interface SharedPath {
  path: string;
  access_label: string;
}

export interface WorkspaceFileSharingGrant {
  workspace_agent_id: string;
  workspace_name: string;
  color: string;
  paths: SharedPath[];
}

export interface WorkspaceDelegationVerb {
  verb_permission: string;
  label: string;
  description: string;
  is_all_workspaces: boolean;
  target_names: string[];
}

export interface WorkspaceDelegationGrant {
  workspace_agent_id: string;
  workspace_name: string;
  color: string;
  verbs: WorkspaceDelegationVerb[];
}

export interface SettingsOverview {
  services_overview: ServicePermissionOverview[];
  file_sharing_grants: WorkspaceFileSharingGrant[];
  workspace_delegation_grants: WorkspaceDelegationGrant[];
  permissions_unavailable: boolean;
  is_master_password_set: boolean;
  report_unexpected_errors: boolean;
  version: string;
}

export type SettingsSection =
  | "connectors"
  | "file-sharing"
  | "workspace-delegation"
  | "error-reporting"
  | "backups";

export const SETTINGS_SECTIONS: {
  name: SettingsSection;
  label: string;
  group: "Permissions" | "Other";
}[] = [
  { name: "connectors", label: "Connectors", group: "Permissions" },
  { name: "file-sharing", label: "Local files", group: "Permissions" },
  { name: "workspace-delegation", label: "Machines", group: "Permissions" },
  { name: "error-reporting", label: "Error reporting", group: "Other" },
  { name: "backups", label: "Master password", group: "Other" },
];

export interface PendingRevoke {
  title: string;
  body: string;
  confirmLabel: string;
  url: string;
  payload: Record<string, unknown>;
}

export interface MasterPasswordResult {
  account: string;
  is_ok: boolean;
  error?: string;
}

type FetchLike = typeof fetch;

export class SettingsModel {
  overview: SettingsOverview | null = null;
  isLoadFailed = false;
  activeSection: SettingsSection = "connectors";
  pendingRevoke: PendingRevoke | null = null;
  revokeError = "";
  isRevokeBusy = false;
  addAccountBusyService = "";
  errorReportingError = "";
  masterPasswordError = "";
  masterPasswordResults: MasterPasswordResult[] | null = null;
  isMasterPasswordAllOk = false;
  isMasterPasswordBusy = false;

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;

  // The default wraps the global fetch in a plain call: passing `fetch`
  // itself would invoke it as `this.fetchImpl(...)` with the model as its
  // receiver, which browsers reject with "Illegal invocation".
  constructor(fetchImpl: FetchLike = (input, init) => fetch(input, init), redraw: () => void = m.redraw) {
    this.fetchImpl = fetchImpl;
    this.redraw = redraw;
  }

  async load(): Promise<void> {
    this.isLoadFailed = false;
    try {
      const response = await this.fetchImpl("/ui/api/settings", {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.overview = (await response.json()) as SettingsOverview;
    } catch {
      this.isLoadFailed = true;
    }
    this.redraw();
  }

  selectSection(name: SettingsSection): void {
    this.activeSection = name;
  }

  openRevoke(pending: PendingRevoke): void {
    this.pendingRevoke = pending;
    this.revokeError = "";
    this.isRevokeBusy = false;
  }

  closeRevoke(): void {
    this.pendingRevoke = null;
  }

  /** Confirm the pending revoke against its legacy POST route, then reload. */
  async confirmRevoke(): Promise<void> {
    const pending = this.pendingRevoke;
    if (pending === null || this.isRevokeBusy) return;
    this.isRevokeBusy = true;
    try {
      const response = await this.fetchImpl(pending.url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pending.payload),
      });
      if (!response.ok) {
        this.revokeError = `Could not revoke (HTTP ${response.status})`;
        this.isRevokeBusy = false;
        this.redraw();
        return;
      }
      this.pendingRevoke = null;
      this.isRevokeBusy = false;
      await this.load();
    } catch {
      this.revokeError = "Could not revoke (network error)";
      this.isRevokeBusy = false;
      this.redraw();
    }
  }

  /** Run the blocking connector sign-in flow for a service, then reload. */
  async addConnectorAccount(serviceName: string): Promise<string | null> {
    this.addAccountBusyService = serviceName;
    this.redraw();
    try {
      const response = await this.fetchImpl(
        "/settings/connectors/add-account",
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ service_name: serviceName }),
        },
      );
      if (!response.ok) {
        let message = `Could not add the account (HTTP ${response.status}).`;
        try {
          const data = (await response.json()) as { error?: string };
          if (data.error) message = data.error;
        } catch {
          // Non-JSON error body: keep the status-based message.
        }
        return message;
      }
      await this.load();
      return null;
    } catch {
      return "Could not add the account (network error).";
    } finally {
      this.addAccountBusyService = "";
      this.redraw();
    }
  }

  /** Flip the error-reporting flag through the If-Match-guarded endpoint.

  A 412 (another window changed it first) rebases by reloading the payload
  rather than clobbering; the checkbox then reflects the newer state. */
  async setReportUnexpectedErrors(isEnabled: boolean): Promise<void> {
    const overview = this.overview;
    if (overview === null) return;
    this.errorReportingError = "";
    try {
      const response = await this.fetchImpl(
        "/ui/api/settings/error-reporting",
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "If-Match": overview.version,
          },
          body: JSON.stringify({ report_unexpected_errors: isEnabled }),
        },
      );
      if (response.status === 412) {
        // Another window changed the flag first: rebase on the newer state.
        await this.load();
        return;
      }
      if (response.ok) {
        const result = (await response.json()) as { version: string };
        this.overview = {
          ...overview,
          report_unexpected_errors: isEnabled,
          version: result.version,
        };
      } else {
        // Persisted nothing; the unchanged model state stands, and the
        // snapped-back checkbox needs a stated reason.
        let message = `Could not update error reporting (HTTP ${response.status}).`;
        try {
          const data = (await response.json()) as { error?: string };
          if (data.error) message = data.error;
        } catch {
          // Non-JSON error body: keep the status-based message.
        }
        this.errorReportingError = message;
      }
    } catch {
      // Network failure: nothing was persisted; say so rather than
      // snapping the checkbox back silently.
      this.errorReportingError = "Could not update error reporting (network error).";
    }
    this.redraw();
  }

  async changeMasterPassword(
    newPassword: string,
    confirmPassword: string,
  ): Promise<void> {
    this.masterPasswordError = "";
    this.masterPasswordResults = null;
    if (newPassword !== confirmPassword) {
      this.masterPasswordError = "The two passwords do not match.";
      this.redraw();
      return;
    }
    this.isMasterPasswordBusy = true;
    this.redraw();
    try {
      const response = await this.fetchImpl("/_chrome/backup-password", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          new_password: newPassword,
          new_password_confirm: confirmPassword,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        ok?: boolean;
        results?: MasterPasswordResult[];
      };
      if (response.status !== 200) {
        this.masterPasswordError =
          data.error ?? `The change failed (HTTP ${response.status}).`;
      } else {
        this.masterPasswordResults = data.results ?? [];
        this.isMasterPasswordAllOk = data.ok === true;
      }
    } catch {
      this.masterPasswordError = "The change failed (network error).";
    }
    this.isMasterPasswordBusy = false;
    this.redraw();
  }
}
