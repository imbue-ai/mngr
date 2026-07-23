// Workspace settings page: rename, color picker, account association,
// sharing servers list, the backup section, and the danger-zone destroy flow.
//
// Color-picker model: 10 unlabeled palette swatches + an always-visible hex
// input. The hex input is the source of truth: selecting a swatch fills the
// input, typing a valid hex selects the matching swatch (if any). Save is
// implicit -- a valid hex saves on blur/Enter, a swatch click saves
// immediately; no Save button. SSE drives the re-paint of the chrome after
// each save; an optimistic host preview shortcuts the local window's paint.
import m from "mithril";

import type { WorkspaceSettingsBootExtras, WorkspaceSettingsBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";
import { AssociatePrompt } from "./AssociatePrompt";
import { Icon } from "./Icon";

// Shared hex normalization from workspace_accent.js (a shell script loaded
// by every ChromeShell document), mirroring Python's
// normalize_workspace_color. Null when the script is absent (never in
// practice) or the value is invalid.
declare global {
  interface Window {
    mindsAccent?: { normalizeHex(value: string): string | null };
  }
}

function normalizeHex(value: string): string | null {
  if (window.mindsAccent === undefined) return null;
  return window.mindsAccent.normalizeHex(value);
}

interface BackupsEntry {
  is_backing_up?: boolean;
  is_configured?: boolean;
  is_verification_enabled?: boolean;
  snapshots?: Array<{ time: string }>;
  snapshots_error?: string;
  check_state?: "OK" | "PROBLEMS" | "OFFLINE" | "DISABLED";
  check_detail?: string;
  problems?: string[];
  installed_version?: string;
  minimum_version?: string;
  update_target_version?: string;
}

interface BackupOperation {
  status?: string;
  is_done?: boolean;
  error?: string;
  blocked_chats?: string[];
}

const PROBLEM_LABELS: Record<string, string> = {
  NOT_CONFIGURED: "Backups are not configured for this workspace.",
  CODE_OUTDATED: "The backup service code is outdated.",
  ENV_MISSING: "The backup credentials file is missing on the workspace.",
  ENV_MISMATCH: "The workspace's backup credentials don't match the expected configuration.",
  SERVICE_NOT_RUNNING: "The backup service is not running.",
  UNVERIFIABLE: "The backup service could not be verified.",
};

const OPERATION_POLL_INTERVAL_MS = 2000;

class WorkspaceSettingsController {
  readonly extras: WorkspaceSettingsBootExtras;

  // -- Rename --
  renameValue: string;
  renameError: string | null = null;
  isRenameInFlight = false;

  // -- Color picker --
  hexValue: string;
  lastSavedHex: string;
  colorError: string | null = null;
  isColorSaving = false;

  // -- Account --
  isDisassociateInFlight = false;

  // -- Backups --
  backupStatusLine = "Loading backup status...";
  backupVersionsLine: string | null = null;
  backupProblems: string[] = [];
  isVerificationEnabled = true;
  isVerificationButtonShown = false;
  verificationSpinnerText: string | null = null;
  isUpdateButtonShown = false;
  isStopChatsButtonShown = false;
  isOperationRunning = false;
  isOperationCancellable = false;
  operationProgressLine: string | null = null;
  backupError: string | null = null;
  isConfigureFormShown = false;
  configureProvider: "IMBUE_CLOUD" | "API_KEY" | "NONE";
  configureApiKeyEnv = "";

  // -- Danger zone --
  isDestroyDialogShown = false;
  isDestroyInFlight = false;
  destroyError: string | null = null;

  private operationPollTimer: ReturnType<typeof setTimeout> | null = null;
  private logStream: { close(): void } | null = null;

  constructor(extras: WorkspaceSettingsBootExtras) {
    this.extras = extras;
    this.renameValue = extras.ws_name;
    this.hexValue = extras.current_color;
    this.lastSavedHex = extras.current_color.toLowerCase();
    this.configureProvider = extras.has_account ? "IMBUE_CLOUD" : "API_KEY";
  }

  stop(): void {
    if (this.operationPollTimer !== null) {
      clearTimeout(this.operationPollTimer);
      this.operationPollTimer = null;
    }
    if (this.logStream !== null) {
      this.logStream.close();
      this.logStream = null;
    }
  }

  private workspaceUrl(): string {
    return `/api/v1/workspaces/${encodeURIComponent(this.extras.agent_id)}`;
  }

  // -- Rename -------------------------------------------------------------

  async saveRename(): Promise<void> {
    const newName = this.renameValue.trim();
    this.renameError = null;
    if (newName === "") {
      this.renameError = "A workspace name is required.";
      m.redraw();
      return;
    }
    this.isRenameInFlight = true;
    m.redraw();
    try {
      const response = await fetch(`${this.workspaceUrl()}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      if (response.ok) {
        // Reload so every surface (title, menu) repaints with the new name.
        window.location.reload();
        return;
      }
      const data = (await response.json().catch(() => ({}))) as { error?: string; message?: string; detail?: string };
      this.isRenameInFlight = false;
      this.renameError = data.error ?? data.message ?? data.detail ?? `Rename failed (HTTP ${response.status})`;
    } catch {
      this.isRenameInFlight = false;
      this.renameError = "Rename failed (network error)";
    }
    m.redraw();
  }

  // -- Color picker --------------------------------------------------------

  private previewChromeAccent(hex: string): void {
    // Optimistic local repaint through the host adapter (Electron IPC;
    // browser mode's SSE path updates the bar a tick later).
    getHost().previewWorkspaceAccent(this.extras.agent_id, hex);
  }

  applyHexInput(value: string): void {
    this.hexValue = value;
    const normalized = normalizeHex(value);
    // Mid-typing invalidity is not an error yet; blur decides.
    this.colorError = null;
    if (normalized !== null) m.redraw();
  }

  commitHexInput(): void {
    const normalized = normalizeHex(this.hexValue);
    if (normalized === null) {
      this.colorError = "That hex value is not valid. Use #rrggbb or #rgb.";
      this.hexValue = this.lastSavedHex;
      m.redraw();
      return;
    }
    this.hexValue = normalized;
    void this.saveColor(normalized);
  }

  pickSwatch(hex: string): void {
    const normalized = normalizeHex(hex);
    if (normalized === null) return;
    this.colorError = null;
    this.hexValue = normalized;
    void this.saveColor(normalized);
  }

  // The normalized hex currently reflected by the controls (selection state).
  get selectedHex(): string {
    return normalizeHex(this.hexValue) ?? this.lastSavedHex;
  }

  private async saveColor(normalized: string): Promise<void> {
    // Idempotency: skip the POST when the value is already saved.
    if (normalized === this.lastSavedHex) return;
    this.previewChromeAccent(normalized);
    this.isColorSaving = true;
    m.redraw();
    try {
      const response = await fetch(this.workspaceUrl(), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ color: normalized }),
      });
      const body = (await response.json().catch(() => ({}))) as { error?: string };
      this.isColorSaving = false;
      if (response.ok) {
        this.lastSavedHex = normalized;
        this.hexValue = normalized;
        this.colorError = null;
        m.redraw();
        return;
      }
      const errorKind = body.error ?? "unknown";
      if (errorKind === "invalid_hex") this.colorError = "That hex value is not valid. Use #rrggbb or #rgb.";
      else if (errorKind === "not_primary") this.colorError = "This agent isn't a primary workspace; color can't be set.";
      else if (errorKind === "stale_provider") this.colorError = "This workspace is currently unreachable; try again later.";
      else if (errorKind === "host_unreachable") this.colorError = "Could not reach the workspace host. Try again in a moment.";
      else this.colorError = `Save failed (HTTP ${response.status}).`;
      // Revert the input + the optimistic chrome paint to the last saved
      // value so the picker stays consistent with persisted state.
      this.hexValue = this.lastSavedHex;
      this.previewChromeAccent(this.lastSavedHex);
    } catch (saveError) {
      this.isColorSaving = false;
      this.colorError = `Network error saving color: ${saveError instanceof Error ? saveError.message : String(saveError)}`;
      this.hexValue = this.lastSavedHex;
      this.previewChromeAccent(this.lastSavedHex);
    }
    m.redraw();
  }

  // -- Account -------------------------------------------------------------

  async disassociate(): Promise<void> {
    this.isDisassociateInFlight = true;
    m.redraw();
    try {
      await fetch(this.workspaceUrl(), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: null }),
      });
      window.location.reload();
    } catch (disassociateError) {
      window.alert(`Failed: ${disassociateError instanceof Error ? disassociateError.message : String(disassociateError)}`);
      this.isDisassociateInFlight = false;
      m.redraw();
    }
  }

  // -- Sharing -------------------------------------------------------------

  openSharing(event: Event, service: string): void {
    // The sharing editor opens as a centered overlay modal when a modal
    // surface exists; the links' hrefs stay as the standalone fallback.
    if (window.minds === undefined && window.__mindsOpenModal === undefined) return;
    event.preventDefault();
    getHost().openModal({ kind: "sharing", agentId: this.extras.agent_id, serviceName: service });
  }

  // -- Backups -------------------------------------------------------------

  private renderBackupsEntry(entry: BackupsEntry): void {
    this.backupStatusLine = this.snapshotText(entry);
    this.isVerificationEnabled = entry.is_verification_enabled === true;
    this.isVerificationButtonShown = true;
    this.backupProblems = [];
    this.backupVersionsLine = null;
    this.isUpdateButtonShown = false;

    if (entry.check_state === "DISABLED") {
      this.backupStatusLine += " Backup service verification is disabled for this workspace.";
      // The update is an idempotent converge and does not depend on
      // verification, so it stays available.
      this.isUpdateButtonShown = true;
      return;
    }
    if (entry.check_state === "OFFLINE") {
      this.backupStatusLine += " The workspace is offline; its backup service will be verified when it is back.";
      return;
    }
    const versionParts: string[] = [];
    if (entry.installed_version !== undefined && entry.installed_version !== "") {
      versionParts.push(`Installed backup service: ${entry.installed_version}`);
    }
    if (entry.minimum_version !== undefined && entry.minimum_version !== "") {
      versionParts.push(`minimum required: ${entry.minimum_version}`);
    }
    if (
      entry.update_target_version !== undefined &&
      entry.update_target_version !== "" &&
      entry.update_target_version !== entry.minimum_version
    ) {
      versionParts.push(`update installs: ${entry.update_target_version}`);
    }
    if (versionParts.length > 0) this.backupVersionsLine = versionParts.join(" / ");
    // The update is an idempotent converge, so the button is always offered
    // for a reachable workspace -- even at the target version it usefully
    // resets a wedged backup service.
    this.isUpdateButtonShown = true;
    if (entry.check_state === "PROBLEMS") {
      this.backupProblems = (entry.problems ?? []).map((problem) => PROBLEM_LABELS[problem] ?? problem);
      if (entry.check_detail !== undefined && entry.check_detail !== "") this.backupProblems.push(entry.check_detail);
    } else if (entry.check_state === "OK") {
      this.backupStatusLine += " The backup service is up to date.";
    }
  }

  private snapshotText(entry: BackupsEntry): string {
    if (entry.is_backing_up === true) return "Backing up now...";
    let latest: string | null = null;
    for (const snapshot of entry.snapshots ?? []) {
      if (latest === null || Date.parse(snapshot.time) > Date.parse(latest)) latest = snapshot.time;
    }
    if (latest !== null) return `Last backup: ${new Date(latest).toLocaleString()}`;
    if (entry.is_configured !== true) return "Backups are not configured.";
    if (entry.snapshots_error !== undefined && entry.snapshots_error !== "") return "Backup status unknown.";
    return "No successful backup yet.";
  }

  async refreshBackupHealth(): Promise<void> {
    try {
      const response = await fetch(`${this.workspaceUrl()}/backups`);
      const entry = response.ok ? ((await response.json()) as BackupsEntry) : null;
      if (entry === null) {
        this.backupStatusLine = "Backup status unavailable for this workspace.";
      } else {
        this.renderBackupsEntry(entry);
        window.mindsBackupHealth?.ingestEntry(entry);
      }
    } catch {
      this.backupStatusLine = "Could not load backup status.";
    }
    m.redraw();
  }

  async toggleVerification(): Promise<void> {
    this.backupError = null;
    const targetEnabled = !this.isVerificationEnabled;
    this.isVerificationButtonShown = false;
    this.verificationSpinnerText = targetEnabled ? "Enabling..." : "Disabling...";
    m.redraw();
    try {
      const response = await fetch(`${this.workspaceUrl()}/backup-service/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: targetEnabled }),
      });
      if (!response.ok) {
        this.verificationSpinnerText = null;
        this.isVerificationButtonShown = true;
        this.backupError = `Could not update the verification setting (HTTP ${response.status}).`;
        m.redraw();
        return;
      }
      // Re-fetching also re-runs the (possibly slow) service check; the
      // spinner keeps showing what we're doing until the fresh state lands.
      const entryResponse = await fetch(`${this.workspaceUrl()}/backups`);
      const entry = entryResponse.ok ? ((await entryResponse.json()) as BackupsEntry) : null;
      this.verificationSpinnerText = null;
      if (entry !== null) {
        this.renderBackupsEntry(entry);
        window.mindsBackupHealth?.ingestEntry(entry);
      } else {
        this.isVerificationButtonShown = true;
      }
    } catch {
      this.verificationSpinnerText = null;
      this.isVerificationButtonShown = true;
      this.backupError = "Could not update the verification setting (network error).";
    }
    m.redraw();
  }

  // Tracked operation driving (update + configure/disable share the poller).
  // Cancel only affects a still-waiting backup *update* (the cancel route
  // 404s for configure operations, which have no waiting phase).
  private setOperationRunning(isRunning: boolean, isCancellable: boolean): void {
    this.isOperationRunning = isRunning;
    this.isOperationCancellable = isRunning && isCancellable;
    if (!isRunning) this.operationProgressLine = null;
  }

  private streamOperationLogs(): void {
    const source = new EventSource(
      `/api/v1/workspaces/operations/backup/${encodeURIComponent(this.extras.agent_id)}/logs`,
    );
    this.logStream = source;
    source.onmessage = (event) => {
      try {
        const frame = JSON.parse(event.data as string) as { log?: string; done?: boolean };
        if (frame.log !== undefined && frame.log !== "") {
          this.operationProgressLine = frame.log;
          m.redraw();
        }
        if (frame.done === true) source.close();
      } catch {
        // Keepalive frames etc.
      }
    };
    source.onerror = () => source.close();
  }

  private pollOperation(): void {
    void (async () => {
      try {
        const response = await fetch(`/api/v1/workspaces/operations/backup/${encodeURIComponent(this.extras.agent_id)}`);
        const operation = response.ok ? ((await response.json()) as BackupOperation) : null;
        if (operation === null) {
          this.setOperationRunning(false, false);
          m.redraw();
          return;
        }
        if (operation.status === "RUNNING") {
          this.operationPollTimer = setTimeout(() => this.pollOperation(), OPERATION_POLL_INTERVAL_MS);
          return;
        }
        this.setOperationRunning(false, false);
        this.isStopChatsButtonShown = false;
        if (operation.is_done === true) {
          void this.refreshBackupHealth();
          return;
        }
        if (operation.blocked_chats !== undefined && operation.blocked_chats.length > 0) {
          this.backupError =
            `Chats are running in this workspace (${operation.blocked_chats.join(", ")}). ` +
            "Stop them before updating the backup service; they resume on your next message.";
          this.isStopChatsButtonShown = true;
          m.redraw();
          return;
        }
        this.backupError = operation.error !== undefined && operation.error !== "" ? operation.error : "The backup operation failed.";
        m.redraw();
      } catch {
        // A transient fetch failure must not end the Working state while the
        // backend operation is still running -- keep polling.
        this.operationPollTimer = setTimeout(() => this.pollOperation(), OPERATION_POLL_INTERVAL_MS);
      }
    })();
  }

  async startOperation(url: string, body: Record<string, unknown>, isCancellable: boolean): Promise<void> {
    this.backupError = null;
    this.setOperationRunning(true, isCancellable);
    m.redraw();
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (response.status === 202) {
        this.streamOperationLogs();
        this.pollOperation();
        return;
      }
      const data = (await response.json().catch(() => ({}))) as { error?: string; message?: string };
      this.setOperationRunning(false, false);
      this.backupError = data.error ?? data.message ?? `Request failed (HTTP ${response.status})`;
    } catch {
      this.setOperationRunning(false, false);
      this.backupError = "Request failed (network error).";
    }
    m.redraw();
  }

  startUpdate(stopChats: boolean): void {
    if (stopChats) this.isStopChatsButtonShown = false;
    void this.startOperation(`${this.workspaceUrl()}/backup-service/update`, { stop_chats: stopChats }, true);
  }

  cancelUpdate(): void {
    void fetch(`${this.workspaceUrl()}/backup-service/update/cancel`, { method: "POST" }).catch(() => undefined);
  }

  submitConfigure(): void {
    if (this.configureProvider === "NONE") {
      void this.startOperation(`${this.workspaceUrl()}/backup-service/disable`, {}, false);
      return;
    }
    void this.startOperation(
      `${this.workspaceUrl()}/backup-service/configure`,
      { backup_provider: this.configureProvider, api_key_env: this.configureApiKeyEnv },
      false,
    );
  }

  // -- Danger zone ----------------------------------------------------------

  async confirmDestroy(): Promise<void> {
    this.isDestroyInFlight = true;
    m.redraw();
    try {
      // Fire-and-redirect: the detached destroy subprocess survives a
      // settings-page navigation, and the landing page renders a
      // "Destroying..." marker on the row from on-disk state.
      const response = await fetch(`${this.workspaceUrl()}/destroy`, { method: "POST" });
      if (response.ok) {
        window.location.href = "/";
        return;
      }
      this.isDestroyInFlight = false;
      this.destroyError = `Could not start destroy (HTTP ${response.status})`;
      this.isDestroyDialogShown = false;
    } catch {
      this.isDestroyInFlight = false;
      this.destroyError = "Could not start destroy (network error)";
      this.isDestroyDialogShown = false;
    }
    m.redraw();
  }
}

// -- Section views ----------------------------------------------------------

const NOTICE_WARN = "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-warning-surface)] text-warning";
const NOTICE_INFO = "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info";
const PILL_INPUT_CLASSES =
  "h-[34px] px-3 rounded-full type-body text-primary placeholder:text-tertiary disabled:opacity-40 bg-surface-secondary";

function sectionHeader(label: m.Children, isDivided = false, extra = ""): m.Children {
  const divider = isDivided ? " mt-8 pt-4 border-t border-default" : "";
  return m("h2", { class: `type-label text-secondary mb-3${divider}${extra !== "" ? ` ${extra}` : ""}` }, label);
}

function renameSection(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  return [
    sectionHeader("Workspace Name"),
    m("div", { id: "rename-section", class: "mb-4" }, [
      extras.is_stale
        ? m(
            "div",
            { class: NOTICE_WARN },
            "This workspace is currently unreachable, so it can't be renamed right now. Try again once the " +
              "workspace reconnects.",
          )
        : null,
      m("div", { class: "flex items-center gap-2" }, [
        m("input", {
          id: "workspace-name-input",
          type: "text",
          class: PILL_INPUT_CLASSES,
          value: controller.renameValue,
          maxlength: 200,
          spellcheck: false,
          autocomplete: "off",
          "aria-label": "Workspace name",
          disabled: extras.is_stale,
          oninput: (event: Event) => {
            controller.renameValue = (event.target as HTMLInputElement).value;
          },
        }),
        m(
          "button",
          {
            type: "button",
            id: "rename-save-btn",
            class: buttonClasses("secondary"),
            disabled: extras.is_stale || controller.isRenameInFlight,
            onclick: () => void controller.saveRename(),
          },
          "Save",
        ),
        controller.isRenameInFlight ? m("span", { class: "type-section text-secondary" }, "Saving...") : null,
      ]),
      controller.renameError !== null
        ? m("p", { id: "rename-error", class: "type-body text-important mt-2", role: "alert" }, controller.renameError)
        : null,
    ]),
  ];
}

function colorSection(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  const selectedHex = controller.selectedHex;
  const paletteValues = Object.values(extras.palette);
  return [
    // The saving badge centers on the title's vertical axis; leading-none +
    // a fixed badge height keeps the line box from growing when it appears.
    sectionHeader(
      [
        "Workspace Color ",
        controller.isColorSaving
          ? m("span", { class: "type-section text-primary bg-surface-primary rounded-sm px-1.5 py-1" }, "Saving")
          : null,
      ],
      false,
      "flex items-center gap-2",
    ),
    m("div", { id: "color-section", class: controller.isColorSaving ? "is-saving" : "" }, [
      extras.is_stale
        ? m(
            "div",
            { class: NOTICE_WARN },
            "This workspace is currently unreachable, so its color can't be changed right now. Try again once " +
              "the workspace reconnects.",
          )
        : null,
      m(
        "div",
        {
          role: "radiogroup",
          "aria-label": "Workspace color palette",
          class: "flex flex-wrap items-center gap-2 mb-3",
          id: "color-swatches",
        },
        [
          // No keys: the palette is static for the mount's lifetime, and a
          // key here would make keyed/unkeyed siblings of the swatches and
          // the hex input (which mithril rejects on redraw).
          ...Object.entries(extras.palette).map(([name, hexValue]) =>
            m("button", {
              type: "button",
              role: "radio",
              class:
                "color-swatch w-[34px] h-[34px] rounded-full focus:outline-none focus-visible:ring-2 " +
                "focus-visible:ring-accent/60 disabled:opacity-40",
              "aria-label": name,
              "aria-checked": hexValue === selectedHex ? "true" : "false",
              "data-color": hexValue,
              style: { backgroundColor: hexValue },
              disabled: extras.is_stale || controller.isColorSaving,
              onclick: (event: Event) => {
                // Drop focus from the hex input so its ring doesn't linger
                // after a palette chip is picked.
                (event.target as HTMLElement).blur();
                controller.pickSwatch(hexValue);
              },
            }),
          ),
          m("input", {
            id: "color-hex-input",
            type: "text",
            class:
              "color-hex-pill h-[34px] px-3 rounded-full type-body font-mono text-primary " +
              "placeholder:text-tertiary disabled:opacity-40" +
              (paletteValues.includes(selectedHex) ? "" : " is-selected"),
            value: controller.hexValue,
            placeholder: "#a1b2c3",
            maxlength: 9,
            spellcheck: false,
            autocomplete: "off",
            "aria-label": "Workspace color hex",
            size: 8,
            disabled: extras.is_stale || controller.isColorSaving,
            oninput: (event: Event) => controller.applyHexInput((event.target as HTMLInputElement).value),
            onblur: () => controller.commitHexInput(),
            onkeydown: (event: KeyboardEvent) => {
              // Enter applies the color through the shared blur path.
              if (event.key === "Enter") {
                event.preventDefault();
                (event.target as HTMLInputElement).blur();
              }
            },
          }),
        ],
      ),
      controller.colorError !== null
        ? m("p", { id: "color-error", class: "type-body text-important mt-2", role: "alert" }, controller.colorError)
        : null,
    ]),
  ];
}

function accountSection(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  let body: m.Children;
  if (extras.is_leased_imbue_cloud) {
    body = [
      extras.current_account_email !== ""
        ? m("p", ["Associated with: ", m("strong", extras.current_account_email)])
        : null,
      m(
        "div",
        { class: NOTICE_INFO },
        "This workspace runs on a host leased from Imbue Cloud, so its account association is fixed and " +
          "cannot be changed.",
      ),
      m("button", { type: "button", id: "disassociate-btn", class: buttonClasses("danger"), disabled: true }, "Disassociate"),
    ];
  } else if (extras.current_account_email !== "") {
    body = [
      m("p", ["Associated with: ", m("strong", extras.current_account_email)]),
      m(
        "div",
        { class: NOTICE_WARN },
        "Disassociating will remove all sharing (tunnels) for this workspace. You will need to set up " +
          "sharing again after re-associating.",
      ),
      m(
        "button",
        {
          type: "button",
          id: "disassociate-btn",
          class: buttonClasses("danger"),
          disabled: controller.isDisassociateInFlight,
          onclick: () => void controller.disassociate(),
        },
        "Disassociate",
      ),
      controller.isDisassociateInFlight ? m("span", { class: "text-secondary ml-2" }, "Disassociating...") : null,
    ];
  } else {
    body = m(AssociatePrompt, { agentId: extras.agent_id, accounts: extras.associate_accounts });
  }
  return [
    sectionHeader("Account", true),
    m(
      "div",
      {
        id: "account-section",
        style: controller.isDisassociateInFlight ? { opacity: "0.5", pointerEvents: "none" } : undefined,
      },
      body,
    ),
  ];
}

function sharingSection(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  return [
    sectionHeader("Sharing", true),
    extras.servers.length > 0
      ? m(
          "div",
          { class: "flex flex-col gap-2" },
          extras.servers.map((server) =>
            m("div", { key: server, class: "minds-card p-4 flex items-center justify-between gap-1.5" }, [
              m("span", { class: "font-semibold" }, server),
              m(
                "a",
                {
                  href: `/sharing/${encodeURIComponent(extras.agent_id)}/${encodeURIComponent(server)}`,
                  class: buttonClasses("secondary"),
                  onclick: (event: Event) => controller.openSharing(event, server),
                },
                "Manage sharing",
              ),
            ]),
          ),
        )
      : m("p", { class: "text-secondary" }, "No servers discovered for this workspace."),
  ];
}

function backupSection(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  return [
    sectionHeader("Backups", true),
    m("div", { id: "backup-section" }, [
      // Verification comes first: the service-status breakdown below only
      // exists while verification is enabled.
      m("div", { class: "flex items-center gap-2 mb-2" }, [
        m("span", { class: "type-body text-primary" }, "Backup service verification"),
        controller.isVerificationButtonShown
          ? m(
              "button",
              {
                type: "button",
                id: "backup-verification-btn",
                class: buttonClasses("secondary"),
                onclick: () => void controller.toggleVerification(),
              },
              controller.isVerificationEnabled ? "Disable" : "Enable",
            )
          : null,
        controller.verificationSpinnerText !== null
          ? m("span", { class: "type-section text-secondary" }, controller.verificationSpinnerText)
          : null,
      ]),
      m("p", { id: "backup-status-line", class: "type-body text-secondary mb-2" }, controller.backupStatusLine),
      controller.backupVersionsLine !== null
        ? m("div", { id: "backup-versions", class: "type-helper text-tertiary mb-2" }, controller.backupVersionsLine)
        : null,
      controller.backupProblems.length > 0
        ? m(
            "ul",
            { id: "backup-problems", class: "type-body text-important mb-2 list-disc pl-4" },
            controller.backupProblems.map((problem) => m("li", problem)),
          )
        : null,
      m("div", { class: "flex items-center gap-2 mb-2" }, [
        controller.isUpdateButtonShown
          ? m(
              "button",
              {
                type: "button",
                id: "backup-update-btn",
                class: buttonClasses("secondary"),
                disabled: controller.isOperationRunning,
                onclick: () => controller.startUpdate(false),
              },
              "Update backup service",
            )
          : null,
        controller.isStopChatsButtonShown
          ? m(
              "button",
              {
                type: "button",
                id: "backup-stop-chats-btn",
                class: buttonClasses("secondary"),
                disabled: controller.isOperationRunning,
                onclick: () => controller.startUpdate(true),
              },
              "Stop all chats and retry",
            )
          : null,
        controller.isOperationRunning && controller.isOperationCancellable
          ? m(
              "button",
              { type: "button", id: "backup-cancel-btn", class: buttonClasses("secondary"), onclick: () => controller.cancelUpdate() },
              "Cancel",
            )
          : null,
        controller.isOperationRunning ? m("span", { class: "type-section text-secondary" }, "Working...") : null,
      ]),
      controller.operationProgressLine !== null
        ? m(
            "p",
            { id: "backup-op-progress", class: "type-helper text-tertiary mb-2", "aria-live": "polite" },
            controller.operationProgressLine,
          )
        : null,
      controller.backupError !== null
        ? m("p", { id: "backup-error", class: "type-body text-important mb-2", role: "alert" }, controller.backupError)
        : null,
      m("div", { id: "backup-configure", class: "mb-3" }, [
        m(
          "button",
          {
            type: "button",
            id: "backup-configure-toggle-btn",
            class: buttonClasses("secondary"),
            onclick: () => {
              controller.isConfigureFormShown = !controller.isConfigureFormShown;
              m.redraw();
            },
          },
          "Configure backups...",
        ),
        controller.isConfigureFormShown
          ? m("div", { id: "backup-configure-form", class: "mt-3 flex flex-col gap-2" }, [
              m("div", { class: "flex items-center gap-2" }, [
                m("label", { for: "backup-provider-select", class: "type-label text-primary" }, "Provider"),
                m(
                  "select",
                  {
                    id: "backup-provider-select",
                    class: "h-[34px] px-3 rounded-full type-body bg-surface-secondary text-primary",
                    value: controller.configureProvider,
                    onchange: (event: Event) => {
                      controller.configureProvider = (event.target as HTMLSelectElement)
                        .value as typeof controller.configureProvider;
                      m.redraw();
                    },
                  },
                  [
                    m(
                      "option",
                      { value: "IMBUE_CLOUD", disabled: !extras.has_account },
                      extras.has_account ? "Imbue Cloud" : "Imbue Cloud (requires an account)",
                    ),
                    m("option", { value: "API_KEY" }, "Bring my own (restic env)"),
                    m("option", { value: "NONE" }, "None (disable backups)"),
                  ],
                ),
              ]),
              controller.configureProvider === "API_KEY"
                ? m("div", { id: "backup-api-key-row" }, [
                    m(
                      "label",
                      { for: "backup-api-key-env-input", class: "type-label text-primary block mb-1.5" },
                      "Restic env (KEY=VALUE per line; no RESTIC_PASSWORD)",
                    ),
                    m("textarea", {
                      id: "backup-api-key-env-input",
                      rows: 4,
                      spellcheck: false,
                      class: "w-full px-3 py-2 rounded-md type-body font-mono bg-surface-secondary text-primary",
                      placeholder: "RESTIC_REPOSITORY=\nAWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=",
                      value: controller.configureApiKeyEnv,
                      oninput: (event: Event) => {
                        controller.configureApiKeyEnv = (event.target as HTMLTextAreaElement).value;
                      },
                    }),
                  ])
                : null,
              m("div", [
                m(
                  "button",
                  {
                    type: "button",
                    id: "backup-configure-submit-btn",
                    class: buttonClasses("secondary"),
                    disabled: controller.isOperationRunning,
                    onclick: () => controller.submitConfigure(),
                  },
                  "Apply",
                ),
              ]),
            ])
          : null,
      ]),
    ]),
  ];
}

function dangerZone(controller: WorkspaceSettingsController): m.Children {
  const { extras } = controller;
  return [
    sectionHeader("Danger zone", true),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Permanently destroy this workspace and release any associated resources.",
    ),
    m(
      "button",
      {
        type: "button",
        id: "destroy-btn",
        class: buttonClasses("danger"),
        onclick: () => {
          controller.isDestroyDialogShown = true;
          m.redraw();
        },
      },
      "Destroy workspace",
    ),
    controller.destroyError !== null
      ? m("p", { id: "destroy-error", class: "type-body text-important mt-2" }, controller.destroyError)
      : null,
    controller.isDestroyDialogShown
      ? m(
          "div",
          {
            id: "destroy-dialog",
            class: "fixed inset-0 z-50 flex items-center justify-center bg-surface-overlay",
            onclick: (event: Event) => {
              if (event.target === event.currentTarget) {
                controller.isDestroyDialogShown = false;
                m.redraw();
              }
            },
          },
          m(
            "div",
            { class: "bg-surface-primary rounded-lg shadow-overlay border border-default max-w-sm w-full mx-4 p-6" },
            [
              m("h2", { class: "type-heading text-primary mb-3" }, "Destroy workspace?"),
              m("p", { class: "type-body text-primary mb-4" }, [
                "This will permanently destroy ",
                m("strong", extras.ws_name),
                " and all its data. This action cannot be undone.",
              ]),
              m("div", { class: "flex justify-end gap-3" }, [
                m(
                  "button",
                  {
                    type: "button",
                    id: "destroy-cancel-btn",
                    class: buttonClasses("secondary"),
                    onclick: () => {
                      controller.isDestroyDialogShown = false;
                      m.redraw();
                    },
                  },
                  "Cancel",
                ),
                m(
                  "button",
                  {
                    type: "button",
                    id: "destroy-confirm-btn",
                    class: buttonClasses("danger"),
                    disabled: controller.isDestroyInFlight,
                    onclick: () => void controller.confirmDestroy(),
                  },
                  "Destroy",
                ),
              ]),
            ],
          ),
        )
      : null,
  ];
}

interface WorkspaceSettingsPageAttrs {
  controller: WorkspaceSettingsController;
}

function WorkspaceSettingsPage(): m.Component<WorkspaceSettingsPageAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { extras } = controller;
      return m("div", { id: "workspace-settings" }, [
        m("h1", { class: "type-heading text-primary" }, extras.ws_name),
        m("p", { class: "type-helper text-tertiary mb-4" }, extras.agent_id),
        renameSection(controller),
        colorSection(controller),
        accountSection(controller),
        sharingSection(controller),
        backupSection(controller),
        dangerZone(controller),
      ]);
    },
  };
}

export function mountWorkspaceSettings(target: Element | null): void {
  const el = requireElement(target, "workspace settings container");
  const island = readBootState() as WorkspaceSettingsBootIsland;
  if (island.workspace_settings === undefined) {
    throw new MindsUIError("workspace-settings boot island is missing the workspace_settings slice");
  }
  const controller = new WorkspaceSettingsController(island.workspace_settings);
  void controller.refreshBackupHealth();
  mountWithTeardown(el, {
    view: () => m(WorkspaceSettingsPage, { controller }),
    // Timers and the SSE log stream must not outlive the component.
    onremove: () => controller.stop(),
  });
}
