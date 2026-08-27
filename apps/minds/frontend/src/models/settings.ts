// Settings page model: loads the app-level settings payload from
// /ui/api/settings, tracks the active section + revoke dialog, and performs
// the page's writes (legacy POST routes for revokes/connectors/master
// password; the If-Match-guarded /ui/api endpoints for the error-reporting
// toggle and the notification prefs).
//
// It also holds the release-channel state behind the Updates panel
// (updateState, peekedChannels, pendingChannelSwitch). That comes from the
// Electron main process over the electronBridge IPC, not from the settings
// payload, and is null in the browser build.
//
// The response interfaces are hand-written mirrors of the pydantic models in
// ui_api_settings.py (the generated schema currently covers only channel
// frames; see the tranche report).

import m from "mithril";
import { electronBridge } from "../electron-bridge";
import type { PeekedChannel, UpdateChannel, UpdateState, UpdateStatus } from "../electron-bridge";
import type { NotificationPrefs, NotificationStyle } from "./notificationsUi";
import {
  DEFAULT_NOTIFICATION_PREFS,
  applyNotificationPrefs,
  postNotificationPrefsWrite,
} from "./notificationsUi";

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
export function addAccountBlockedReason(
  service: ServicePermissionOverview,
): string | null {
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
  /** Optional only for version skew: an already-running backend from before
   * this field existed (a not-yet-restarted process, a stale cached
   * Electron bundle talking to it) can still serve this SPA. Absent reads
   * as the defaults (enabled + both) and writes then surface the server's
   * error. CLEANUP: make this required (and drop the
   * DEFAULT_NOTIFICATION_PREFS fallbacks reading it) once no such backend
   * can still be running. */
  notification_prefs?: NotificationPrefs;
  version: string;
}

export type SettingsSection =
  | "connectors"
  | "file-sharing"
  | "workspace-delegation"
  | "notifications"
  | "error-reporting"
  | "updates"
  | "backups";

export const SETTINGS_SECTIONS: {
  name: SettingsSection;
  label: string;
  group: "Permissions" | "Other";
}[] = [
  { name: "connectors", label: "Connectors", group: "Permissions" },
  { name: "file-sharing", label: "Local files", group: "Permissions" },
  { name: "workspace-delegation", label: "Machines", group: "Permissions" },
  { name: "notifications", label: "Notifications", group: "Other" },
  { name: "error-reporting", label: "Error reporting", group: "Other" },
  { name: "updates", label: "Updates", group: "Other" },
  { name: "backups", label: "Master password", group: "Other" },
];

/**
 * Slowest to fastest, which is the order the list is rendered in.
 *
 * Blurbs say what a channel *is*, never how often it ships: a cadence printed
 * in the UI is a promise the release process has not made.
 *
 */
export const CHANNEL_COPY: {
  name: UpdateChannel;
  label: string;
  blurb: string;
}[] = [
  {
    name: "stable",
    label: "Stable",
    blurb: "Ready for everyday use.",
  },
  {
    name: "beta",
    label: "Beta",
    blurb: "Test new features early.",
  },
  {
    name: "alpha",
    label: "Alpha",
    blurb: "Internal development builds.",
  },
];

// electronBridge.onUpdateStatus has no unregister, so the preload callback is
// registered ONCE at module scope and forwards to whichever model last loaded;
// SettingsPage is re-created on every visit to the page.
let activeUpdateStatusForwarder: ((status: UpdateStatus) => void) | null = null;
let isUpdateStatusRegistered = false;

function ensureUpdateStatusRegistered(): void {
  if (isUpdateStatusRegistered) return;
  isUpdateStatusRegistered = true;
  electronBridge.onUpdateStatus((status) => activeUpdateStatusForwarder?.(status));
}

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

  // -- Release channels (desktop only) --
  updateState: UpdateState | null = null;
  peekedChannels: Record<string, PeekedChannel> = {};
  /** Set when a switch would park the user; cleared by confirm or cancel. */
  pendingChannelSwitch: { channel: UpdateChannel; targetVersion: string | null } | null = null;
  isUpdateBusy = false;
  updateError = "";
  errorReportingError = "";
  notificationPrefsError = "";
  /** Set after a failed openNotificationOsSettings() call (e.g. no known
   * settings command found on this Linux desktop environment), so the panel
   * can tell the reader the automatic open didn't work rather than leaving
   * the button looking like it silently did nothing. */
  notificationOsSettingsOpenFailed = false;
  masterPasswordError = "";
  masterPasswordResults: MasterPasswordResult[] | null = null;
  isMasterPasswordAllOk = false;
  isMasterPasswordBusy = false;

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;

  // The default wraps the global fetch in a plain call: passing `fetch`
  // itself would invoke it as `this.fetchImpl(...)` with the model as its
  // receiver, which browsers reject with "Illegal invocation".
  constructor(
    fetchImpl: FetchLike = (input, init) => fetch(input, init),
    redraw: () => void = m.redraw,
  ) {
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
      // Keep the app-wide applied prefs (which gate notification arrivals)
      // in step with what this window just learned; a no-op when absent.
      applyNotificationPrefs(this.overview.notification_prefs);
    } catch {
      this.isLoadFailed = true;
    }
    this.redraw();
  }

  /** The notification prefs to render: the loaded ones, else the defaults
   * (enabled + both) the backend also assumes before any write. */
  notificationPrefs(): NotificationPrefs {
    return this.overview?.notification_prefs ?? DEFAULT_NOTIFICATION_PREFS;
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
        // Merge onto the CURRENT overview, not the pre-await `overview`
        // snapshot: a concurrent write (e.g. setNotificationPrefs) may have
        // already landed its own field on `this.overview` while this request
        // was in flight, and spreading the stale snapshot would clobber it.
        const latest = this.overview;
        if (latest === null) return;
        this.overview = {
          ...latest,
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
      this.errorReportingError =
        "Could not update error reporting (network error).";
    }
    this.redraw();
  }

  /** Write the notification prefs through the If-Match-guarded endpoint,
   * mirroring `setReportUnexpectedErrors`: a 412 (another window changed them
   * first) rebases by reloading rather than clobbering, and a refusal leaves
   * the model state standing with a stated reason. The If-Match token is the
   * PREFS' own version (the write endpoint guards the prefs record). */
  async setNotificationPrefs(next: {
    is_enabled: boolean;
    style: NotificationStyle;
    is_os_hint_dismissed: boolean;
  }): Promise<void> {
    const overview = this.overview;
    if (overview === null) return;
    this.notificationPrefsError = "";
    const current = overview.notification_prefs ?? DEFAULT_NOTIFICATION_PREFS;
    try {
      const response = await postNotificationPrefsWrite(
        this.fetchImpl,
        current.version,
        next,
      );
      if (response.status === 412) {
        // Another window changed the prefs first: rebase on the newer state.
        await this.load();
        return;
      }
      if (response.ok) {
        const result = (await response.json()) as { version: string };
        const applied: NotificationPrefs = {
          ...current,
          ...next,
          version: result.version,
        };
        // Merge onto the CURRENT overview, not the pre-await `overview`
        // snapshot: a concurrent write (e.g. setReportUnexpectedErrors) may
        // have already landed its own field on `this.overview` while this
        // request was in flight, and spreading the stale snapshot would
        // clobber it.
        const latest = this.overview;
        if (latest === null) return;
        this.overview = { ...latest, notification_prefs: applied };
        applyNotificationPrefs(applied);
      } else {
        // Persisted nothing; the unchanged model state stands, and the
        // snapped-back control needs a stated reason.
        let message = `Could not update notifications (HTTP ${response.status}).`;
        try {
          const data = (await response.json()) as { error?: string };
          if (data.error) message = data.error;
        } catch {
          // Non-JSON error body: keep the status-based message.
        }
        this.notificationPrefsError = message;
      }
    } catch {
      // Network failure: nothing was persisted; say so rather than snapping
      // the control back silently.
      this.notificationPrefsError =
        "Could not update notifications (network error).";
    }
    this.redraw();
  }

  /** Open the OS's notification-settings pane, tracking whether it actually
   * worked (e.g. no known settings command found on this Linux desktop
   * environment) so the panel can say so rather than leaving the button
   * looking like it silently did nothing. */
  async openNotificationOsSettings(): Promise<void> {
    this.notificationOsSettingsOpenFailed = false;
    const opened = await electronBridge.openNotificationSettings();
    this.notificationOsSettingsOpenFailed = !opened;
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

  /** The nav entries this build can actually service. */
  get visibleSections(): typeof SETTINGS_SECTIONS {
    return SETTINGS_SECTIONS.filter(
      (section) => section.name !== "updates" || electronBridge.isDesktop,
    );
  }

  async loadUpdateState(): Promise<void> {
    // Registered before the first await: a main process that cannot describe
    // its updater must not also cost this session its status listener.
    activeUpdateStatusForwarder = (status) => this.receiveUpdateStatus(status);
    ensureUpdateStatusRegistered();
    try {
      this.updateState = await electronBridge.getUpdateState();
    } catch (error) {
      // A desktop build whose updater failed to describe itself is not the
      // browser build, and the panel says so rather than showing the browser
      // copy with no version, no channel, and no reason.
      this.updateError = error instanceof Error ? error.message : String(error);
      this.redraw();
      return;
    }
    this.redraw();
    if (this.updateState === null) return;
    // Peeked after the first paint: the panel states what every channel serves,
    // and a channel whose manifest does not resolve is not selectable.
    await this.refreshPeekedChannels();
  }

  /**
   * Re-read what every channel serves, without holding the panel.
   *
   * Never awaited under `isUpdateBusy`. The main process runs one updater task
   * at a time, so a peek issued after a check that found an update is queued
   * behind the download that check started -- minutes, for a build of this
   * size. Holding the button across that turns a finished check into a panel
   * that reads as still checking for the whole transfer.
   */
  private async refreshPeekedChannels(): Promise<void> {
    try {
      this.peekedChannels = await electronBridge.peekUpdateChannels();
    } catch (error) {
      this.updateError = error instanceof Error ? error.message : String(error);
    }
    this.redraw();
  }

  /**
   * Take the channel the status was produced for, not just the status.
   *
   * Main checks whatever `currentChannel()` says, so a pushed status names the
   * app's channel. Only the window that called `setUpdateChannel` learns about a
   * switch from its return value; another window with this panel open would go on
   * showing the channel it was on -- and since its radio is already checked,
   * clicking it fires no `onchange`, so there is no way to act on it from there
   * either.
   */
  receiveUpdateStatus(status: UpdateStatus): void {
    if (this.updateState === null) return;
    // `disabled` and a check that rejected outright carry no channel.
    const channel = status.channel ?? this.updateState.channel;
    // Statuses that are not a settled check (`checking`, `disabled`) carry no
    // time, and must not erase the one the last real check reported.
    const lastCheckedAt = status.lastCheckedAt ?? this.updateState.lastCheckedAt;
    this.updateState = { ...this.updateState, channel, status, lastCheckedAt };
    this.redraw();
  }

  /**
   * Start a channel switch, stopping to confirm when it would park the user.
   *
   * The cost has to be stated before it is paid: moving to a slower channel
   * means receiving nothing until that channel catches up, and there is no way
   * back down -- nothing in the data directory has a down-migration.
   *
   * A channel whose manifest could not be read has no version to compare, so it
   * cannot park and cannot be switched to either: the preference would stick and
   * every check from then on would fail against a feed that serves nothing.
   */
  async requestChannel(channel: UpdateChannel): Promise<void> {
    if (this.updateState === null || channel === this.updateState.channel) return;
    this.isUpdateBusy = true;
    this.updateError = "";
    this.redraw();
    try {
      this.peekedChannels = await electronBridge.peekUpdateChannels();
      const target = this.peekedChannels[channel];
      if (target !== undefined && target.version === null) {
        this.updateError = `The ${channel} channel is unavailable right now, so Minds stayed on ${this.updateState.channel}.`;
        return;
      }
      if (target !== undefined && target.wouldPark) {
        this.pendingChannelSwitch = { channel, targetVersion: target.version };
        return;
      }
      await this.applyChannel(channel);
    } catch (error) {
      this.updateError = error instanceof Error ? error.message : String(error);
    } finally {
      this.isUpdateBusy = false;
      this.redraw();
    }
  }

  async confirmChannelSwitch(): Promise<void> {
    const pending = this.pendingChannelSwitch;
    if (pending === null) return;
    this.pendingChannelSwitch = null;
    this.isUpdateBusy = true;
    this.redraw();
    try {
      await this.applyChannel(pending.channel);
    } catch (error) {
      this.updateError = error instanceof Error ? error.message : String(error);
    } finally {
      this.isUpdateBusy = false;
      this.redraw();
    }
  }

  cancelChannelSwitch(): void {
    this.pendingChannelSwitch = null;
    this.redraw();
  }

  private async applyChannel(channel: UpdateChannel): Promise<void> {
    const next = await electronBridge.setUpdateChannel(channel);
    if (next !== null) this.updateState = next;
  }

  /**
   * Restart into the staged update.
   *
   * No busy flag and no redraw: the app is quitting, so there is no later state
   * to render, and a spinner that never resolves is what a failed quit would
   * leave behind.
   */
  async installUpdateNow(): Promise<void> {
    await electronBridge.installUpdate();
  }

  async checkForUpdatesNow(): Promise<void> {
    this.isUpdateBusy = true;
    this.updateError = "";
    this.redraw();
    try {
      // `describe()` carries the time the check just settled, so there is
      // nothing to stamp here.
      const next = await electronBridge.checkForUpdates();
      if (next !== null) this.updateState = next;
    } catch (error) {
      this.updateError = error instanceof Error ? error.message : String(error);
    } finally {
      this.isUpdateBusy = false;
      this.redraw();
    }
    // Deliberately after the button is released and not awaited by the caller:
    // the check has already answered, and this only refines what each channel
    // is reported to serve.
    void this.refreshPeekedChannels();
  }
}
