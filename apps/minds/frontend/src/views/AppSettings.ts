// The app-level ("Minds") settings sections, shared verbatim between the
// centered settings modal (the Electron overlay surface / browser ModalHost
// iframe) and the full-page browser-mode fallback. Two columns: a left nav
// that switches between subsections and a right pane showing the active one.
//
// Sections (grouped Permissions / Other):
//   - Connectors: predefined-service grants held across all active workspaces.
//   - Local files: file-sharing (WebDAV) grants per workspace.
//   - Workspaces: cross-workspace-management (delegation) grants.
//   - Error reporting: the per-machine error-reporting toggles.
//   - Master password: the sync master-password change flow (rewraps each
//     signed-in account's sync key; workspace backup repositories untouched).
//
// All are per-machine / app-level (not account-scoped); the app, not any
// single workspace, owns permissions.
import m from "mithril";

import type { SettingsBootExtras, SettingsBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";
import { DialogCloseButton } from "./DialogCloseButton";

type SectionName = "connectors" | "file-sharing" | "workspace-delegation" | "error-reporting" | "backups";

const SECTION_NAMES: SectionName[] = [
  "connectors",
  "file-sharing",
  "workspace-delegation",
  "error-reporting",
  "backups",
];

interface RevokeRequest {
  url: string;
  body: Record<string, string>;
}

interface PendingRevoke {
  title: string;
  body: string;
  request: RevokeRequest;
  error: string | null;
  isInFlight: boolean;
}

interface PasswordChangeResultEntry {
  account: string;
  is_ok: boolean;
  error?: string;
}

class AppSettingsController {
  readonly extras: SettingsBootExtras;
  activeSection: SectionName = "connectors";
  pendingRevoke: PendingRevoke | null = null;
  isReportEnabled: boolean;
  isLogsEnabled: boolean;
  // Master-password change flow.
  newPassword = "";
  newPasswordConfirm = "";
  isChangeInFlight = false;
  changeError: string | null = null;
  changeResultLines: string[] | null = null;

  constructor(extras: SettingsBootExtras) {
    this.extras = extras;
    this.isReportEnabled = extras.report_unexpected_errors;
    this.isLogsEnabled = extras.include_error_logs;
    // Restore the active section from the URL hash (survives the reload a
    // revoke triggers; harmless in the modal iframe). Falls back to
    // Connectors.
    const initialSection = window.location.hash.replace(/^#/, "");
    if ((SECTION_NAMES as string[]).includes(initialSection)) {
      this.activeSection = initialSection as SectionName;
    }
  }

  selectSection(name: SectionName): void {
    this.activeSection = name;
    // Remember the active section in the URL hash so a revoke's reload (or a
    // manual refresh of the full page) restores the same tab.
    try {
      history.replaceState(null, "", `#${name}`);
    } catch {
      // Sandboxed contexts may reject; the tab still switches.
    }
    m.redraw();
  }

  openRevokeDialog(title: string, body: string, request: RevokeRequest): void {
    this.pendingRevoke = { title, body, request, error: null, isInFlight: false };
    m.redraw();
  }

  closeRevokeDialog(): void {
    this.pendingRevoke = null;
    m.redraw();
  }

  async confirmRevoke(): Promise<void> {
    const pending = this.pendingRevoke;
    if (pending === null || pending.isInFlight) return;
    pending.isInFlight = true;
    pending.error = null;
    m.redraw();
    try {
      const response = await fetch(pending.request.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pending.request.body),
      });
      if (response.ok) {
        // Reload re-renders the page with a fresh island reflecting the
        // revocation (works in both the full page and the modal iframe).
        window.location.reload();
        return;
      }
      pending.isInFlight = false;
      pending.error = `Could not revoke (HTTP ${response.status})`;
    } catch {
      pending.isInFlight = false;
      pending.error = "Could not revoke (network error)";
    }
    m.redraw();
  }

  setReportEnabled(enabled: boolean): void {
    this.isReportEnabled = enabled;
    const payload: Record<string, boolean> = { report_unexpected_errors: enabled };
    if (!enabled) {
      this.isLogsEnabled = false;
      payload.include_logs = false;
    }
    void this.saveErrorReporting(payload);
    m.redraw();
  }

  setLogsEnabled(enabled: boolean): void {
    this.isLogsEnabled = enabled;
    void this.saveErrorReporting({ include_logs: enabled });
    m.redraw();
  }

  private async saveErrorReporting(payload: Record<string, boolean>): Promise<void> {
    try {
      await fetch("/_chrome/error-reporting", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      // Best-effort persistence, matching the pre-component behavior; the
      // toggle state re-seeds from the server on the next page load.
    }
  }

  // Synchronous POST: the server rewraps each signed-in account's sync key
  // (and pushes/clears the synced secrets), then answers with per-account
  // results. Workspace backup repositories are never touched.
  async changeMasterPassword(): Promise<void> {
    this.changeError = null;
    this.changeResultLines = null;
    if (this.newPassword !== this.newPasswordConfirm) {
      this.changeError = "The two passwords do not match.";
      m.redraw();
      return;
    }
    this.isChangeInFlight = true;
    m.redraw();
    try {
      const response = await fetch("/_chrome/backup-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          new_password: this.newPassword,
          new_password_confirm: this.newPasswordConfirm,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        ok?: boolean;
        results?: PasswordChangeResultEntry[];
      };
      this.isChangeInFlight = false;
      if (response.status !== 200) {
        this.changeError = data.error !== undefined && data.error !== "" ? data.error : `The change failed (HTTP ${response.status}).`;
        m.redraw();
        return;
      }
      this.newPassword = "";
      this.newPasswordConfirm = "";
      this.changeResultLines = buildChangeResultLines(data.results, data.ok === true);
    } catch {
      this.isChangeInFlight = false;
      this.changeError = "The change failed (network error).";
    }
    m.redraw();
  }
}

function buildChangeResultLines(results: PasswordChangeResultEntry[] | undefined, isAllOk: boolean): string[] {
  if (results === undefined || results.length === 0) {
    return ["The master password change failed."];
  }
  const lines = results.map((entry) =>
    entry.is_ok ? `${entry.account}: updated` : `${entry.account}: FAILED - ${entry.error ?? "unknown error"}`,
  );
  lines.push(isAllOk ? "Master password updated for every account." : "Re-run the change to retry the failed accounts.");
  return lines;
}

// -- Section views ----------------------------------------------------------

const NOTICE_WARN = "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-warning-surface)] text-warning";
const NOTICE_INFO = "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info";

function navButton(controller: AppSettingsController, name: SectionName, label: string): m.Children {
  const isActive = controller.activeSection === name;
  return m(
    "button",
    {
      type: "button",
      "data-settings-nav": name,
      class:
        "flex items-center h-8 px-2 rounded-md cursor-pointer type-body text-left border-0 " +
        "hover:text-primary hover:bg-fill-hover " +
        (isActive ? "bg-fill-hover text-primary" : "text-secondary"),
      onclick: () => controller.selectSection(name),
    },
    label,
  );
}

function workspaceDot(color: string): m.Children {
  return m("span", { class: "w-2.5 h-2.5 rounded-full shrink-0", style: { backgroundColor: color } });
}

function connectorsSection(controller: AppSettingsController): m.Children {
  const { extras } = controller;
  let bodyContent: m.Children;
  if (extras.permissions_unavailable) {
    bodyContent = m("div", { class: NOTICE_WARN }, "Connectors can't be loaded right now. Try again in a moment.");
  } else if (extras.services_overview.length > 0) {
    bodyContent = m(
      "div",
      { class: "flex flex-col gap-8" },
      extras.services_overview.map((service) => {
        const wsCount = service.workspace_grants.length;
        return m("div", { key: service.service_name }, [
          m("div", { class: "flex items-center justify-between gap-3 mb-3" }, [
            m("h3", { class: "type-heading text-primary" }, [
              service.display_name,
              m(
                "span",
                { class: "type-helper text-tertiary font-normal" },
                ` · ${wsCount} workspace${wsCount === 1 ? "" : "s"}`,
              ),
            ]),
            m(
              "button",
              {
                type: "button",
                class: buttonClasses("ghost"),
                onclick: () =>
                  controller.openRevokeDialog(
                    `Remove all ${service.display_name} authorizations?`,
                    `This removes ${service.display_name} permissions from every workspace. ` +
                      "Agents can request them again later.",
                    { url: "/settings/permissions/revoke-all", body: { service_name: service.service_name } },
                  ),
              },
              "Revoke all",
            ),
          ]),
          m(
            "div",
            { class: "grid grid-cols-2 gap-3" },
            service.workspace_grants.map((grant) =>
              m("div", { key: grant.workspace_agent_id, class: "minds-card p-4 flex flex-col gap-3" }, [
                m("div", { class: "flex items-center gap-2" }, [
                  workspaceDot(grant.color),
                  m("span", { class: "type-body font-semibold text-primary truncate" }, grant.workspace_name),
                ]),
                m("div", [
                  m("p", { class: "type-helper text-tertiary mb-1" }, "Allowed"),
                  m(
                    "div",
                    { class: "flex flex-wrap gap-1" },
                    grant.permissions.map((permission) =>
                      m(
                        "code",
                        {
                          class: "code-pill",
                          "data-tooltip": permission.description !== "" ? permission.description : undefined,
                        },
                        permission.label,
                      ),
                    ),
                  ),
                ]),
                m("div", { class: "mt-auto" }, [
                  m(
                    "button",
                    {
                      type: "button",
                      class: buttonClasses("secondary"),
                      onclick: () =>
                        controller.openRevokeDialog(
                          `Revoke ${service.display_name} access?`,
                          `This removes ${grant.workspace_name}'s ${service.display_name} permissions. ` +
                            "The agent can request them again later.",
                          {
                            url: "/settings/permissions/revoke",
                            body: {
                              workspace_agent_id: grant.workspace_agent_id,
                              service_name: service.service_name,
                            },
                          },
                        ),
                    },
                    "Revoke",
                  ),
                ]),
              ]),
            ),
          ),
        ]);
      }),
    );
  } else {
    bodyContent = m("div", { class: NOTICE_INFO }, "No connectors have been added yet.");
  }
  return m("section", { "data-settings-panel": "connectors" }, [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Connectors"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Third-party services your agents have connected to. To connect a new one, just ask an agent in a " +
        "workspace to use it. Revoking here removes access -- your saved sign-in is kept, so agents can " +
        "reconnect later.",
    ),
    bodyContent,
  ]);
}

function fileSharingSection(controller: AppSettingsController): m.Children {
  const { extras } = controller;
  let bodyContent: m.Children;
  if (extras.permissions_unavailable) {
    bodyContent = m("div", { class: NOTICE_WARN }, "File sharing can't be loaded right now. Try again in a moment.");
  } else if (extras.file_sharing_grants.length > 0) {
    bodyContent = m(
      "div",
      { class: "flex flex-col gap-3" },
      extras.file_sharing_grants.map((grant) =>
        m("div", { key: grant.workspace_agent_id, class: "minds-card p-4 flex flex-col gap-3" }, [
          m("div", { class: "flex items-center justify-between gap-3" }, [
            m("div", { class: "flex items-center gap-2 min-w-0" }, [
              workspaceDot(grant.color),
              m("span", { class: "type-body font-semibold text-primary truncate" }, grant.workspace_name),
            ]),
            m(
              "button",
              {
                type: "button",
                class: buttonClasses("secondary"),
                onclick: () =>
                  controller.openRevokeDialog(
                    "Revoke file sharing?",
                    `This removes ${grant.workspace_name}'s shared file access. ` +
                      "The agent can request it again later.",
                    {
                      url: "/settings/permissions/file-sharing/revoke",
                      body: { workspace_agent_id: grant.workspace_agent_id },
                    },
                  ),
              },
              "Revoke",
            ),
          ]),
          m("div", [
            m("p", { class: "type-helper text-tertiary mb-1" }, "Allowed"),
            m(
              "div",
              { class: "flex flex-col gap-1" },
              grant.paths.map((shared) =>
                m("div", { class: "flex items-center justify-between gap-3" }, [
                  m("code", { class: "code-pill truncate min-w-0", "data-tooltip": shared.path }, shared.path),
                  m("span", { class: "type-helper text-secondary shrink-0" }, shared.access_label),
                ]),
              ),
            ),
          ]),
        ]),
      ),
    );
  } else {
    bodyContent = m("div", { class: NOTICE_INFO }, "No files are being shared with agents yet.");
  }
  return m("section", { "data-settings-panel": "file-sharing" }, [
    m("div", { class: "flex items-center justify-between gap-3 mb-2" }, [
      m("h2", { class: "type-heading-lg text-primary" }, "Local files"),
      extras.file_sharing_grants.length > 0 && !extras.permissions_unavailable
        ? m(
            "button",
            {
              type: "button",
              class: buttonClasses("ghost"),
              onclick: () =>
                controller.openRevokeDialog(
                  "Remove all file sharing?",
                  "This removes shared file access from every workspace. Agents can request it again later.",
                  { url: "/settings/permissions/file-sharing/revoke-all", body: {} },
                ),
            },
            "Revoke all",
          )
        : null,
    ]),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Files and folders on this machine that your agents can read or write. To share a new location, ask " +
        "an agent in a workspace to access it or to write data in there. Revoking removes access; agents " +
        "can ask again later.",
    ),
    bodyContent,
  ]);
}

function workspaceDelegationSection(controller: AppSettingsController): m.Children {
  const { extras } = controller;
  let bodyContent: m.Children;
  if (extras.permissions_unavailable) {
    bodyContent = m(
      "div",
      { class: NOTICE_WARN },
      "Workspace delegation can't be loaded right now. Try again in a moment.",
    );
  } else if (extras.workspace_delegation_grants.length > 0) {
    bodyContent = m(
      "div",
      { class: "flex flex-col gap-8" },
      extras.workspace_delegation_grants.map((grant) =>
        m("div", { key: grant.workspace_agent_id }, [
          m("h3", { class: "type-heading text-primary flex items-center gap-2 mb-1" }, [
            workspaceDot(grant.color),
            m("span", { class: "truncate" }, grant.workspace_name),
          ]),
          m("p", { class: "type-helper text-tertiary mb-2" }, "Allowed"),
          m(
            "div",
            { class: "flex flex-col" },
            grant.verbs.map((verb) =>
              m(
                "div",
                {
                  key: verb.verb_permission,
                  class: "flex items-center justify-between gap-3 py-1.5 border-b border-subtle",
                },
                [
                  m("div", { class: "flex items-center gap-2 min-w-0 flex-wrap" }, [
                    m("code", { class: "code-pill", "data-tooltip": verb.description }, verb.label),
                    m("span", { class: "type-helper text-tertiary" }, "on:"),
                    m(
                      "span",
                      { class: "type-body text-secondary truncate" },
                      verb.is_all_workspaces ? "All workspaces" : verb.target_names.join(", "),
                    ),
                  ]),
                  m(
                    "button",
                    {
                      type: "button",
                      class: `${buttonClasses("ghost")} shrink-0`,
                      onclick: () =>
                        controller.openRevokeDialog(
                          `Revoke ${verb.label} access?`,
                          `This removes ${grant.workspace_name}'s ${verb.label} access to other workspaces. ` +
                            "The agent can request it again later.",
                          {
                            url: "/settings/permissions/workspace/revoke",
                            body: {
                              workspace_agent_id: grant.workspace_agent_id,
                              verb: verb.verb_permission,
                            },
                          },
                        ),
                    },
                    "Revoke",
                  ),
                ],
              ),
            ),
          ),
        ]),
      ),
    );
  } else {
    bodyContent = m("div", { class: NOTICE_INFO }, "No workspace management has been delegated to agents yet.");
  }
  return m("section", { "data-settings-panel": "workspace-delegation" }, [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Workspaces"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Access you've granted agents in one workspace to manage other workspaces (listing, creating, " +
        "destroying, SSH, health checks, and more), grouped by the workspace being managed. To grant more, " +
        "ask an agent to perform the operation on the target workspace. Revoking removes it; agents can ask " +
        "again later.",
    ),
    bodyContent,
  ]);
}

function errorReportingSection(controller: AppSettingsController): m.Children {
  return m("section", { "data-settings-panel": "error-reporting" }, [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Error reporting"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Applies to this device, regardless of which account is signed in.",
    ),
    m("label", { class: "flex items-start justify-between gap-3 py-3 border-b border-subtle cursor-pointer" }, [
      m("span", [
        m("span", { class: "type-body text-primary font-semibold" }, "Report unexpected errors"),
        m("span", { class: "block type-helper text-tertiary" }, "Send a report to Imbue when something goes wrong."),
      ]),
      m("input", {
        type: "checkbox",
        id: "report-errors-toggle",
        class: "mt-1 shrink-0",
        checked: controller.isReportEnabled,
        onchange: (event: Event) => controller.setReportEnabled((event.target as HTMLInputElement).checked),
      }),
    ]),
    controller.isReportEnabled
      ? m(
          "label",
          {
            id: "include-logs-row",
            class: "flex items-start justify-between gap-3 py-3 border-b border-subtle cursor-pointer",
          },
          [
            m("span", [
              m("span", { class: "type-body text-primary font-semibold" }, "Include logs"),
              m(
                "span",
                { class: "block type-helper text-tertiary" },
                "Attach recent log files to help diagnose the problem.",
              ),
            ]),
            m("input", {
              type: "checkbox",
              id: "include-logs-toggle",
              class: "mt-1 shrink-0",
              checked: controller.isLogsEnabled,
              onchange: (event: Event) => controller.setLogsEnabled((event.target as HTMLInputElement).checked),
            }),
          ],
        )
      : null,
  ]);
}

function masterPasswordSection(controller: AppSettingsController): m.Children {
  const { extras } = controller;
  return m("section", { "data-settings-panel": "backups" }, [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Master password"),
    m("p", { class: "type-body text-secondary mb-6" }, [
      "Protects the synced copy of your workspaces' access keys and backup credentials (initially empty -- " +
        "nothing secret syncs until one is set). You'll type it once on each new device to unlock your " +
        "workspaces there.",
      extras.is_master_password_set
        ? " A master password is currently set."
        : " No master password is set yet, so only workspace names and metadata sync.",
    ]),
    m("div", { class: "flex flex-col gap-2 max-w-md" }, [
      m("input", {
        type: "password",
        id: "backup-new-password",
        autocomplete: "new-password",
        placeholder: "new master password (empty disables secret sync)",
        class: "h-[34px] px-3 rounded-full type-body bg-surface-secondary text-primary",
        value: controller.newPassword,
        oninput: (event: Event) => {
          controller.newPassword = (event.target as HTMLInputElement).value;
        },
      }),
      m("input", {
        type: "password",
        id: "backup-new-password-confirm",
        autocomplete: "new-password",
        placeholder: "repeat the new master password",
        class: "h-[34px] px-3 rounded-full type-body bg-surface-secondary text-primary",
        value: controller.newPasswordConfirm,
        oninput: (event: Event) => {
          controller.newPasswordConfirm = (event.target as HTMLInputElement).value;
        },
      }),
      m("div", { class: "flex items-center gap-2" }, [
        m(
          "button",
          {
            type: "button",
            id: "backup-change-password-btn",
            class: buttonClasses("secondary"),
            disabled: controller.isChangeInFlight,
            onclick: () => void controller.changeMasterPassword(),
          },
          "Change master password",
        ),
        controller.isChangeInFlight
          ? m("span", { class: "type-section text-secondary" }, "Updating accounts...")
          : null,
      ]),
      controller.changeError !== null
        ? m("p", { id: "backup-change-error", class: "type-body text-important", role: "alert" }, controller.changeError)
        : null,
      controller.changeResultLines !== null
        ? m(
            "ul",
            {
              id: "backup-change-results",
              class: "type-helper text-secondary list-disc pl-4",
              "aria-live": "polite",
            },
            controller.changeResultLines.map((line) => m("li", line)),
          )
        : null,
    ]),
  ]);
}

function revokeDialog(controller: AppSettingsController): m.Children {
  const pending = controller.pendingRevoke;
  if (pending === null) return null;
  return m(
    "div",
    {
      id: "revoke-dialog",
      class: "fixed inset-0 z-50 flex items-center justify-center bg-surface-overlay",
      onclick: (event: Event) => {
        if (event.target === event.currentTarget) controller.closeRevokeDialog();
      },
    },
    m(
      "div",
      { class: "bg-surface-primary rounded-lg shadow-overlay border border-default max-w-sm w-full mx-4 p-6 text-left" },
      [
        m("h2", { class: "type-heading text-primary mb-3" }, pending.title),
        m("p", { class: "type-body text-primary mb-4" }, pending.body),
        pending.error !== null
          ? m("p", { id: "revoke-error", class: "type-body text-important mb-3", role: "alert" }, pending.error)
          : null,
        m("div", { class: "flex justify-end gap-3" }, [
          m(
            "button",
            {
              type: "button",
              id: "revoke-cancel-btn",
              class: buttonClasses("secondary"),
              onclick: () => controller.closeRevokeDialog(),
            },
            "Cancel",
          ),
          m(
            "button",
            {
              type: "button",
              id: "revoke-confirm-btn",
              class: buttonClasses("danger"),
              disabled: pending.isInFlight,
              onclick: () => void controller.confirmRevoke(),
            },
            "Revoke",
          ),
        ]),
      ],
    ),
  );
}

interface AppSettingsSectionsAttrs {
  controller: AppSettingsController;
}

function AppSettingsSections(): m.Component<AppSettingsSectionsAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const active = controller.activeSection;
      return m("div", { class: "flex gap-8 items-start" }, [
        m("nav", { class: "w-44 shrink-0 flex flex-col gap-0.5", "aria-label": "Settings sections" }, [
          m("p", { class: "type-section text-tertiary px-2 mb-1" }, "Permissions"),
          navButton(controller, "connectors", "Connectors"),
          navButton(controller, "file-sharing", "Local files"),
          navButton(controller, "workspace-delegation", "Workspaces"),
          m("p", { class: "type-section text-tertiary px-2 mt-4 mb-1" }, "Other"),
          navButton(controller, "error-reporting", "Error reporting"),
          navButton(controller, "backups", "Master password"),
        ]),
        m("div", { class: "flex-1 min-w-0" }, [
          active === "connectors" ? connectorsSection(controller) : null,
          active === "file-sharing" ? fileSharingSection(controller) : null,
          active === "workspace-delegation" ? workspaceDelegationSection(controller) : null,
          active === "error-reporting" ? errorReportingSection(controller) : null,
          active === "backups" ? masterPasswordSection(controller) : null,
        ]),
        revokeDialog(controller),
      ]);
    },
  };
}

function readSettingsIsland(): SettingsBootExtras {
  const island = readBootState() as SettingsBootIsland;
  if (island.settings === undefined) {
    throw new MindsUIError("settings boot island is missing the settings slice");
  }
  return island.settings;
}

// Full-page variant (/settings): heading + sections + back link, inside the
// shell's PageContainer.
export function mountSettingsPage(target: Element | null): void {
  const el = requireElement(target, "settings page container");
  const controller = new AppSettingsController(readSettingsIsland());
  mountWithTeardown(el, {
    view: () => [
      m("h1", { class: "type-heading-lg text-primary mb-8" }, "Settings"),
      m(AppSettingsSections, { controller }),
      m("div", { class: "mt-8" }, [
        m("a", { href: "/", class: "text-accent hover:underline type-helper" }, "← Back to workspaces"),
      ]),
    ],
  });
}

// Centered-modal variant (/settings/modal, hosted in the shared overlay
// surface): dim backdrop + fixed-height card (85% of the window regardless
// of how much each section holds, so switching tabs never resizes it; the
// header is pinned and the sections scroll inside).
export function mountSettingsModal(target: Element | null): void {
  const el = requireElement(target, "settings modal container");
  const controller = new AppSettingsController(readSettingsIsland());
  const dismiss = (): void => getHost().closeModal();
  mountWithTeardown(el, {
    view: () =>
      m(
        "div",
        {
          id: "settings-modal-backdrop",
          class: "fixed inset-0 flex items-center justify-center bg-surface-overlay p-4",
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) dismiss();
          },
        },
        m(
          "div",
          {
            class:
              "relative bg-surface-primary rounded-lg shadow-overlay max-w-4xl w-full p-8 h-[85vh] " +
              "flex flex-col text-left",
          },
          [
            m(DialogCloseButton, { onclick: dismiss }),
            m("h1", { class: "type-heading-lg text-primary mb-8 shrink-0" }, "Settings"),
            m("div", { class: "flex-1 min-h-0 overflow-y-auto" }, m(AppSettingsSections, { controller })),
          ],
        ),
      ),
  });
}
