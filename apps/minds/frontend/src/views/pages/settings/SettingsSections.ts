// The app-level settings sections: left nav + the shared revoke dialog + one
// panel each for connectors, local files, machines, notifications, error
// reporting, updates, and the master password. Port of
// templates/AppSettingsSections.jinja with the interactivity of
// static/app_settings.js folded into the SettingsModel; the Updates panel is
// desktop-only and has no jinja original.

import m from "mithril";
import type { UpdateChannel, UpdateState } from "../../../electron-bridge";
import type {
  PendingRevoke,
  ServiceAccountOverview,
  ServicePermissionOverview,
  SettingsModel,
  SettingsSection,
  WorkspaceDelegationGrant,
  WorkspaceFileSharingGrant,
} from "../../../models/settings";
import {
  CHANNEL_COPY,
  addAccountBlockedReason,
} from "../../../models/settings";
import { formatRelativeAgo } from "../../../models/backups";
import type { NotificationStyle } from "../../../models/notificationsUi";
import {
  maybeRequestOsPermissionForStyle,
} from "../../../models/notificationsUi";
import { electronBridge } from "../../../electron-bridge";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { navEntryClass, splitPane } from "../../components/SplitPane";

interface SectionsAttrs {
  model: SettingsModel;
}

function navButton(
  model: SettingsModel,
  name: SettingsSection,
  label: string,
): m.Children {
  return m(
    "button",
    {
      type: "button",
      class: navEntryClass(model.activeSection === name),
      onclick: () => model.selectSection(name),
    },
    label,
  );
}

function colorDot(color: string): m.Children {
  return m("span", {
    class: "w-2.5 h-2.5 rounded-full shrink-0",
    style: `background-color: ${color}`,
  });
}

function accountSubsection(
  model: SettingsModel,
  service: ServicePermissionOverview,
  account: ServiceAccountOverview,
): m.Children {
  const workspaceCount = account.workspace_grants.length;
  const machineNoun = workspaceCount === 1 ? "machine" : "machines";
  return m("div", [
    m("div", { class: "flex items-center justify-between gap-3 mb-2" }, [
      m("p", { class: "type-body text-primary font-semibold truncate" }, [
        account.label,
        m(
          "span",
          { class: "type-helper text-tertiary font-normal" },
          ` · ${workspaceCount} ${machineNoun}${account.is_connected ? "" : " · not connected"}`,
        ),
      ]),
      m("div", { class: "flex items-center gap-2 shrink-0" }, [
        workspaceCount > 0
          ? m(
              Button,
              {
                variant: "ghost",
                size: "md",
                onclick: () =>
                  model.openRevoke({
                    title: `Remove all ${service.display_name} authorizations for ${account.label}?`,
                    body:
                      `This removes ${service.display_name} permissions for ${account.label} from every machine. ` +
                      "Agents can request them again later.",
                    confirmLabel: "Revoke",
                    url: "/settings/permissions/revoke-all",
                    payload: {
                      service_name: service.service_name,
                      account: account.account,
                    },
                  }),
              },
              "Revoke all",
            )
          : null,
        account.is_connected
          ? m(
              Button,
              {
                variant: "ghost",
                size: "md",
                onclick: () =>
                  model.openRevoke({
                    title: `Disconnect ${account.label}?`,
                    body:
                      `This signs ${account.label} out of ${service.display_name}. Your saved credentials and this ` +
                      "account's permissions are removed; agents can reconnect it later.",
                    confirmLabel: "Disconnect",
                    url: "/settings/connectors/disconnect-account",
                    payload: {
                      service_name: service.service_name,
                      account: account.account,
                    },
                  }),
              },
              "Disconnect",
            )
          : null,
      ]),
    ]),
    workspaceCount > 0
      ? m(
          "div",
          { class: "grid grid-cols-2 gap-3" },
          account.workspace_grants.map((grant) =>
            m("div", { class: "minds-card p-4 flex flex-col gap-3" }, [
              m("div", { class: "flex items-center gap-2" }, [
                colorDot(grant.color),
                m(
                  "span",
                  { class: "type-body font-semibold text-primary truncate" },
                  grant.workspace_name,
                ),
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
                        "data-tooltip": permission.description || undefined,
                      },
                      permission.label,
                    ),
                  ),
                ),
              ]),
              m(
                "div",
                { class: "mt-auto" },
                m(
                  Button,
                  {
                    variant: "secondary",
                    size: "md",
                    onclick: () =>
                      model.openRevoke({
                        title: `Revoke ${service.display_name} access?`,
                        body:
                          `This removes ${grant.workspace_name}'s ${service.display_name} permissions for ` +
                          `${account.label}. The agent can request them again later.`,
                        confirmLabel: "Revoke",
                        url: "/settings/permissions/revoke",
                        payload: {
                          workspace_agent_id: grant.workspace_agent_id,
                          service_name: service.service_name,
                          account: account.account,
                        },
                      }),
                  },
                  "Revoke",
                ),
              ),
            ]),
          ),
        )
      : m(
          "p",
          { class: "type-helper text-tertiary" },
          "Not allowed in any machine yet.",
        ),
  ]);
}

/** The service-level "+ Add account" action.
 *
 * It runs latchkey's browser sign-in, so for a service that has none there is
 * nothing to click: the button is disabled and says why on hover. The title
 * sits on a wrapper because a disabled button gets no mouse events, and so no
 * tooltip, in Chromium. */
function addAccountButton(
  model: SettingsModel,
  service: ServicePermissionOverview,
): m.Children {
  const isBusy = model.addAccountBusyService === service.service_name;
  const blockedReason = addAccountBlockedReason(service);
  return m(
    "span",
    {
      class: "shrink-0",
      ...(blockedReason === null ? {} : { title: blockedReason }),
    },
    m(
      Button,
      {
        variant: "ghost",
        size: "md",
        id: `add-account-${service.service_name}`,
        disabled: isBusy || blockedReason !== null,
        onclick: async () => {
          const errorMessage = await model.addConnectorAccount(
            service.service_name,
          );
          if (errorMessage !== null) window.alert(errorMessage);
        },
      },
      isBusy ? "Signing in..." : "+ Add account",
    ),
  );
}

function connectorsPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Connectors"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Third-party services your agents have connected to. To connect a new one, just ask an agent in a machine " +
        "to use it. Revoking here removes access -- your saved sign-in is kept, so agents can reconnect later.",
    ),
    overview.permissions_unavailable
      ? m(
          Notice,
          { variant: "warn" },
          "Connectors can't be loaded right now. Try again in a moment.",
        )
      : overview.services_overview.length > 0
        ? m(
            "div",
            { class: "flex flex-col gap-12" },
            overview.services_overview.map((service) =>
              m("div", [
                m(
                  "div",
                  { class: "flex items-center justify-between gap-3 mb-3" },
                  [
                    m(
                      "h3",
                      { class: "type-heading text-primary" },
                      service.display_name,
                    ),
                    addAccountButton(model, service),
                  ],
                ),
                m(
                  "div",
                  { class: "flex flex-col gap-6" },
                  service.accounts.map((account) =>
                    accountSubsection(model, service, account),
                  ),
                ),
              ]),
            ),
          )
        : m(Notice, { variant: "info" }, "No connectors have been added yet."),
  ]);
}

function fileSharingPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const grants: WorkspaceFileSharingGrant[] = overview.file_sharing_grants;
  return m("section", [
    m("div", { class: "flex items-center justify-between gap-3 mb-2" }, [
      m("h2", { class: "type-heading-lg text-primary" }, "Local files"),
      grants.length > 0
        ? m(
            Button,
            {
              variant: "ghost",
              size: "md",
              onclick: () =>
                model.openRevoke({
                  title: "Remove all file sharing?",
                  body: "This removes shared file access from every machine. Agents can request it again later.",
                  confirmLabel: "Revoke",
                  url: "/settings/permissions/file-sharing/revoke-all",
                  payload: {},
                }),
            },
            "Revoke all",
          )
        : null,
    ]),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Files and folders on this computer that your agents can read or write. To share a new location, ask an " +
        "agent in a machine to access it or to write data in there. Revoking removes access; agents can ask again later.",
    ),
    overview.permissions_unavailable
      ? m(
          Notice,
          { variant: "warn" },
          "File sharing can't be loaded right now. Try again in a moment.",
        )
      : grants.length > 0
        ? m(
            "div",
            { class: "flex flex-col gap-3" },
            grants.map((grant) =>
              m("div", { class: "minds-card p-4 flex flex-col gap-3" }, [
                m("div", { class: "flex items-center justify-between gap-3" }, [
                  m("div", { class: "flex items-center gap-2 min-w-0" }, [
                    colorDot(grant.color),
                    m(
                      "span",
                      {
                        class: "type-body font-semibold text-primary truncate",
                      },
                      grant.workspace_name,
                    ),
                  ]),
                  m(
                    Button,
                    {
                      variant: "secondary",
                      size: "md",
                      onclick: () =>
                        model.openRevoke({
                          title: "Revoke file sharing?",
                          body: `This removes ${grant.workspace_name}'s shared file access. The agent can request it again later.`,
                          confirmLabel: "Revoke",
                          url: "/settings/permissions/file-sharing/revoke",
                          payload: {
                            workspace_agent_id: grant.workspace_agent_id,
                          },
                        }),
                    },
                    "Revoke",
                  ),
                ]),
                m("div", [
                  m(
                    "p",
                    { class: "type-helper text-tertiary mb-1" },
                    "Allowed",
                  ),
                  m(
                    "div",
                    { class: "flex flex-col gap-1" },
                    grant.paths.map((shared) =>
                      m(
                        "div",
                        { class: "flex items-center justify-between gap-3" },
                        [
                          m(
                            "code",
                            {
                              class: "code-pill truncate min-w-0",
                              "data-tooltip": shared.path,
                            },
                            shared.path,
                          ),
                          m(
                            "span",
                            { class: "type-helper text-secondary shrink-0" },
                            shared.access_label,
                          ),
                        ],
                      ),
                    ),
                  ),
                ]),
              ]),
            ),
          )
        : m(
            Notice,
            { variant: "info" },
            "No files are being shared with agents yet.",
          ),
  ]);
}

function delegationPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const grants: WorkspaceDelegationGrant[] =
    overview.workspace_delegation_grants;
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Machines"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Access you've granted agents in one machine to manage other machines (listing, creating, destroying, SSH, " +
        "health checks, and more), grouped by the machine being managed. To grant more, ask an agent to perform " +
        "the operation on the target machine. Revoking removes it; agents can ask again later.",
    ),
    overview.permissions_unavailable
      ? m(
          Notice,
          { variant: "warn" },
          "Machine delegation can't be loaded right now. Try again in a moment.",
        )
      : grants.length > 0
        ? m(
            "div",
            { class: "flex flex-col gap-8" },
            grants.map((grant) =>
              m("div", [
                m(
                  "h3",
                  {
                    class:
                      "type-heading text-primary flex items-center gap-2 mb-1",
                  },
                  [
                    colorDot(grant.color),
                    m("span", { class: "truncate" }, grant.workspace_name),
                  ],
                ),
                m("p", { class: "type-helper text-tertiary mb-2" }, "Allowed"),
                m(
                  "div",
                  { class: "flex flex-col" },
                  grant.verbs.map((verb) =>
                    m(
                      "div",
                      {
                        class:
                          "flex items-center justify-between gap-3 py-1.5 border-b border-subtle",
                      },
                      [
                        m(
                          "div",
                          {
                            class: "flex items-center gap-2 min-w-0 flex-wrap",
                          },
                          [
                            m(
                              "code",
                              {
                                class: "code-pill",
                                "data-tooltip": verb.description,
                              },
                              verb.label,
                            ),
                            m(
                              "span",
                              { class: "type-helper text-tertiary" },
                              "on:",
                            ),
                            m(
                              "span",
                              { class: "type-body text-secondary truncate" },
                              verb.is_all_workspaces
                                ? "All machines"
                                : verb.target_names.join(", "),
                            ),
                          ],
                        ),
                        m(
                          Button,
                          {
                            variant: "ghost",
                            size: "md",
                            extra: "shrink-0",
                            onclick: () =>
                              model.openRevoke({
                                title: `Revoke ${verb.label} access?`,
                                body:
                                  `This removes ${grant.workspace_name}'s ${verb.label} access to other machines. ` +
                                  "The agent can request it again later.",
                                confirmLabel: "Revoke",
                                url: "/settings/permissions/workspace/revoke",
                                payload: {
                                  workspace_agent_id: grant.workspace_agent_id,
                                  verb: verb.verb_permission,
                                },
                              }),
                          },
                          "Revoke",
                        ),
                      ],
                    ),
                  ),
                ),
              ]),
            ),
          )
        : m(
            Notice,
            { variant: "info" },
            "No machine management has been delegated to agents yet.",
          ),
  ]);
}

/** The three delivery styles, with the words a reader picks between. */
const NOTIFICATION_STYLE_OPTIONS: {
  value: NotificationStyle;
  label: string;
  description: string;
}[] = [
  {
    value: "cards",
    label: "In-app cards",
    description: "Floating cards in the corner of the window.",
  },
  {
    value: "os",
    label: "System notifications",
    description:
      "Banners from your operating system, even when Minds is in the background.",
  },
  {
    value: "both",
    label: "Both",
    description: "Cards in the window plus system banners.",
  },
];

/** System banners are the OS's call to make, and it never tells app code
 * whether it is delivering them -- so this stands whenever OS delivery is
 * selected, as the one place to go when banners do not appear. Surfaces the
 * open itself failing (e.g. no known settings command on this Linux desktop
 * environment) rather than leaving the button looking like it silently did
 * nothing. */
function notificationOsPermissionNotice(model: SettingsModel): m.Vnode {
  return m(
    Notice,
    { variant: "warn", extra: "flex flex-col gap-2" },
    m(
      "div",
      { class: "flex items-center justify-between gap-3" },
      [
        m(
          "span",
          {},
          "System banners come from your operating system. If they don't " +
            "appear, check its notification settings for minds.",
        ),
        m(
          Button,
          {
            variant: "secondary",
            size: "md",
            extra: "shrink-0",
            onclick: () => void model.openNotificationOsSettings(),
          },
          "Open System Settings",
        ),
      ],
    ),
    model.notificationOsSettingsOpenFailed
      ? m(
          "span",
          { class: "type-helper", role: "alert" },
          "Couldn't open System Settings automatically — check your OS's " +
            "notification settings for minds.",
        )
      : null,
  );
}

function notificationsPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const prefs = model.notificationPrefs();
  const write = (next: {
    is_enabled: boolean;
    style: NotificationStyle;
  }): void => {
    // An OS-reaching choice is the user gesture the browser permission
    // prompt needs (plain-browser mode only): must fire synchronously with
    // the click, not after the write's await below, or the browser may no
    // longer treat it as within the same user-activation window.
    if (next.is_enabled) maybeRequestOsPermissionForStyle(next.style);
    void (async () => {
      await model.setNotificationPrefs({
        ...next,
        is_os_hint_dismissed: prefs.is_os_hint_dismissed,
      });
    })();
  };
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Notifications"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Applies to this device, regardless of which account is signed in.",
    ),
    m(
      "label",
      {
        class:
          "flex items-start justify-between gap-3 py-3 border-b border-subtle cursor-pointer",
      },
      [
        m("span", [
          m(
            "span",
            { class: "type-body text-primary font-semibold" },
            "Notify me when a machine needs me",
          ),
          m(
            "span",
            { class: "block type-helper text-tertiary" },
            "When an agent asks for a permission, surface it beyond its machine's own chat. " +
              "The bell's feed and count always record it either way.",
          ),
        ]),
        m("input", {
          type: "checkbox",
          id: "notifications-enabled-toggle",
          class: "mt-1 shrink-0",
          checked: prefs.is_enabled,
          onchange: (event: Event) => {
            const target = event.target as HTMLInputElement;
            write({ is_enabled: target.checked, style: prefs.style });
          },
        }),
      ],
    ),
    prefs.is_enabled
      ? m(
          "div",
          {
            role: "radiogroup",
            "aria-label": "Notification style",
            class: "flex flex-col",
          },
          NOTIFICATION_STYLE_OPTIONS.map((option) => {
            const isSelected = prefs.style === option.value;
            return m(
              "button",
              {
                type: "button",
                role: "radio",
                id: `notification-style-${option.value}`,
                "aria-checked": isSelected ? "true" : "false",
                class:
                  "flex w-full items-start gap-3 py-3 text-left cursor-pointer border-b border-subtle",
                onclick: () => write({ is_enabled: true, style: option.value }),
              },
              [
                m(
                  "span",
                  {
                    class:
                      "mt-1 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border " +
                      (isSelected ? "border-accent" : "border-strong"),
                    "aria-hidden": "true",
                  },
                  isSelected
                    ? m("span", { class: "h-2 w-2 rounded-full bg-accent" })
                    : null,
                ),
                m("span", [
                  m(
                    "span",
                    { class: "block type-body text-primary font-semibold" },
                    option.label,
                  ),
                  m(
                    "span",
                    { class: "block type-helper text-tertiary" },
                    option.description,
                  ),
                ]),
              ],
            );
          }),
        )
      : null,
    electronBridge.isDesktop && prefs.is_enabled && prefs.style !== "cards"
      ? notificationOsPermissionNotice(model)
      : null,
    model.notificationPrefsError !== ""
      ? m(
          "p",
          { class: "type-body text-important mt-3", role: "alert" },
          model.notificationPrefsError,
        )
      : null,
  ]);
}

function errorReportingPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Error reporting"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Applies to this device, regardless of which account is signed in.",
    ),
    m(
      "label",
      {
        class:
          "flex items-start justify-between gap-3 py-3 border-b border-subtle cursor-pointer",
      },
      [
        m("span", [
          m(
            "span",
            { class: "type-body text-primary font-semibold" },
            "Report unexpected errors",
          ),
          m(
            "span",
            { class: "block type-helper text-tertiary" },
            "Automatically send a report to Imbue, along with recent logs, when something goes wrong -- so we can " +
              "find and fix problems quickly.",
          ),
        ]),
        m("input", {
          type: "checkbox",
          id: "report-errors-toggle",
          class: "mt-1 shrink-0",
          checked: overview.report_unexpected_errors,
          onchange: (event: Event) => {
            const target = event.target as HTMLInputElement;
            void model.setReportUnexpectedErrors(target.checked);
          },
        }),
      ],
    ),
    m(
      "p",
      { class: "type-helper text-tertiary mt-3 mb-2" },
      "Reports include diagnostic details about the error and your setup, which may identify you " +
        "(for example, your signed-in account email).",
    ),
    m(
      "p",
      { class: "type-helper text-tertiary" },
      "Imbue will never look into your machines without your consent.",
    ),
  ]);
}

/** Whole hours only: the setting means "while I am asleep". */
const HOUR_OPTIONS: number[] = Array.from({ length: 24 }, (_unused, hour) => hour);

function formatHour(hour: number): string {
  const suffix = hour < 12 ? "AM" : "PM";
  return `${hour % 12 || 12}:00 ${suffix}`;
}

function hourSelect(id: string, value: number, onchange: (hour: number) => void): m.Children {
  return m(
    "select",
    {
      id,
      class: "h-[34px] px-2 rounded-md type-body bg-fill-subtle text-primary",
      value: String(value),
      onchange: (event: Event) => onchange(Number((event.target as HTMLSelectElement).value)),
    },
    HOUR_OPTIONS.map((hour) => m("option", { value: String(hour) }, formatHour(hour))),
  );
}

/** The machine-updates half of the Updates panel: the window scheduled machine
 * updates run in. Drawn whatever build this is -- the app-updates half above it
 * is for installed desktop builds, but machines update the same way everywhere. */
function machineUpdatesSection(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const startHour = overview.update_window_start_hour;
  const endHour = overview.update_window_end_hour;
  return m("div", { class: "mt-8" }, [
    m("h3", { class: "type-heading text-primary mb-2" }, "Machine updates"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "When you schedule an update for a machine, Minds runs it inside this window. A machine that " +
        "isn't reachable or has agents working in it when the window comes is skipped and tried again " +
        "in the next one.",
    ),
    m("div", { class: "flex items-center gap-2 py-3 border-b border-subtle" }, [
      m("label", { class: "type-body text-primary", for: "update-window-start" }, "Between"),
      hourSelect("update-window-start", startHour, (hour) => void model.setUpdateWindow(hour, endHour)),
      m("label", { class: "type-body text-primary", for: "update-window-end" }, "and"),
      hourSelect("update-window-end", endHour, (hour) => void model.setUpdateWindow(startHour, hour)),
      m("span", { class: "type-helper text-tertiary" }, "local time"),
    ]),
    model.updateWindowError
      ? m("p", { class: "type-helper text-important mt-3", role: "alert" }, model.updateWindowError)
      : null,
  ]);
}

interface MasterPasswordState {
  newPassword: string;
  confirmPassword: string;
}

function masterPasswordPanel(
  model: SettingsModel,
  local: MasterPasswordState,
): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const statusSentence = overview.is_master_password_set
    ? " A master password is currently set."
    : " No master password is set yet, so only machine names and metadata sync.";
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Master password"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Protects the synced copy of your machines' access keys and backup credentials (initially empty -- nothing " +
        "secret syncs until one is set). You'll type it once on each new device to unlock your machines there." +
        statusSentence,
    ),
    m("div", { class: "flex flex-col gap-2 max-w-md" }, [
      m("input", {
        type: "password",
        id: "backup-new-password",
        autocomplete: "new-password",
        placeholder: "new master password (empty disables secret sync)",
        class:
          "h-[34px] px-3 rounded-full type-body bg-fill-subtle text-primary",
        value: local.newPassword,
        oninput: (event: Event) => {
          local.newPassword = (event.target as HTMLInputElement).value;
        },
      }),
      m("input", {
        type: "password",
        id: "backup-new-password-confirm",
        autocomplete: "new-password",
        placeholder: "repeat the new master password",
        class:
          "h-[34px] px-3 rounded-full type-body bg-fill-subtle text-primary",
        value: local.confirmPassword,
        oninput: (event: Event) => {
          local.confirmPassword = (event.target as HTMLInputElement).value;
        },
      }),
      m("div", { class: "flex items-center gap-2" }, [
        m(
          Button,
          {
            variant: "secondary",
            id: "backup-change-password-btn",
            disabled: model.isMasterPasswordBusy,
            onclick: async () => {
              await model.changeMasterPassword(
                local.newPassword,
                local.confirmPassword,
              );
              if (model.masterPasswordResults !== null) {
                local.newPassword = "";
                local.confirmPassword = "";
              }
            },
          },
          "Change master password",
        ),
        model.isMasterPasswordBusy
          ? m(
              "span",
              { class: "type-section text-secondary" },
              "Updating accounts...",
            )
          : null,
      ]),
      model.masterPasswordError !== ""
        ? m(
            "p",
            { class: "type-body text-important", role: "alert" },
            model.masterPasswordError,
          )
        : null,
      model.masterPasswordResults !== null
        ? m(
            "ul",
            {
              class: "type-helper text-secondary list-disc pl-4",
              "aria-live": "polite",
            },
            [
              ...(model.masterPasswordResults.length === 0
                ? [m("li", "The master password change failed.")]
                : model.masterPasswordResults.map((entry) =>
                    m(
                      "li",
                      entry.is_ok
                        ? `${entry.account}: updated`
                        : `${entry.account}: FAILED - ${entry.error ?? "unknown error"}`,
                    ),
                  )),
              model.masterPasswordResults.length > 0
                ? m(
                    "li",
                    model.isMasterPasswordAllOk
                      ? "Master password updated for every account."
                      : "Re-run the change to retry the failed accounts.",
                  )
                : null,
            ],
          )
        : null,
    ]),
  ]);
}

function revokeDialog(model: SettingsModel): m.Children {
  const pending: PendingRevoke | null = model.pendingRevoke;
  return m(
    Modal,
    {
      isOpen: pending !== null,
      onClose: () => model.closeRevoke(),
      cardExtra: "text-left",
    },
    pending === null
      ? null
      : [
          m("h2", { class: "type-heading text-primary mb-3" }, pending.title),
          m("p", { class: "type-body text-primary mb-4" }, pending.body),
          model.revokeError !== ""
            ? m(
                "p",
                { class: "type-body text-important mb-3", role: "alert" },
                model.revokeError,
              )
            : null,
          m("div", { class: "flex justify-end gap-3" }, [
            m(
              Button,
              { variant: "secondary", onclick: () => model.closeRevoke() },
              "Cancel",
            ),
            m(
              Button,
              {
                variant: "danger",
                disabled: model.isRevokeBusy,
                onclick: () => void model.confirmRevoke(),
              },
              pending.confirmLabel,
            ),
          ]),
        ],
  );
}

/** The one notice above the channel list, or nothing when there is none to give. */
function updateStatusLine(model: SettingsModel): m.Children {
  const state = model.updateState;
  if (state === null) return null;
  const status = state.status;
  // `parked` gets no notice. Being ahead of your channel is temporary and
  // self-correcting -- the channel catches up -- and every channel already
  // prints what it serves, which is the same fact without the alarm. Warning
  // that you are "not receiving updates" reads as a fault when nothing is
  // wrong.
  if (status.type === "error") {
    return m(Notice, { variant: "warn" }, `Update check failed: ${status.message}`);
  }
  if (status.type === "update-downloaded") {
    return m(Notice, { variant: "info" }, `Minds ${status.version} is downloaded. Restart to install.`);
  }
  if (status.type === "update-available") {
    return m(Notice, { variant: "info" }, `Downloading ${status.feedVersion}...`);
  }
  if (status.type === "disabled") {
    return m(Notice, { variant: "info" }, "Updates are only available in installed builds.");
  }
  if (status.type === "parked" && status.feedVersion != null && status.currentVersion !== undefined) {
    // Stated, but not as an alarm: nothing is wrong, and the versions beside
    // each channel already imply it. Left unsaid, though, a channel switch
    // looks like it did nothing -- the panel redraws identically to up-to-date.
    return m(
      "p",
      { class: "type-helper text-tertiary" },
      `${channelLabel(status.channel)} is at ${status.feedVersion}, so you will stay on ` +
        `${status.currentVersion} until it catches up.`,
    );
  }
  return null;
}

/** A channel's display name, so a sentence never drops the bare `alpha` into prose. */
function channelLabel(channel: UpdateChannel | undefined): string {
  return CHANNEL_COPY.find((entry) => entry.name === channel)?.label ?? "That channel";
}

function channelSwitchDialog(model: SettingsModel): m.Children {
  const pending = model.pendingChannelSwitch;
  if (pending === null || model.updateState === null) return null;
  const label = channelLabel(pending.channel);
  // Read from the staged version rather than the status: the artifact is with
  // the OS installer, so it survives the later checks that overwrite the status
  // that announced it -- and this is the one moment the fact is being weighed.
  const stagedVersion = model.updateState.downloadedVersion;
  return m(
    Modal,
    { isOpen: true, onClose: () => model.cancelChannelSwitch() },
    [
      m("h3", { class: "type-heading-md text-primary mb-2" }, `Switch to ${label}?`),
      m(
        "p",
        { class: "type-body text-secondary mb-3" },
        `${label} is at ${pending.targetVersion}. You will stay on ` +
          `${model.updateState.currentVersion} until it catches up.`,
      ),
      // Says where the next restart lands, because switching to a slower
      // channel does not hold it back: a completed download is already with the
      // installer, so the restart moves forward, and the wait gets longer.
      stagedVersion != null
        ? m(
            "p",
            { class: "type-body text-secondary mb-3" },
            `Minds ${stagedVersion} is already downloaded and will still install when you ` +
              `restart -- you will stay on it until ${label} passes it.`,
          )
        : null,
      m("div", { class: "flex gap-2 justify-end" }, [
        m(Button, { variant: "secondary", onclick: () => model.cancelChannelSwitch() }, "Cancel"),
        m(Button, { onclick: () => void model.confirmChannelSwitch() }, "Switch"),
      ]),
    ],
  );
}

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * How long ago the last check ran.
 *
 * Relative while that is the useful answer: checks run often enough that this
 * almost always reads "just now" or a count of minutes. Past a day they have
 * stopped, and "3 days ago" is a worse answer than the time it happened.
 */
function formatChecked(iso: string): string {
  const nowMs = Date.now();
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  if (nowMs - then < DAY_MS) return formatRelativeAgo(iso, nowMs);
  return `at ${new Date(then).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
}

/**
 * The channels the panel lists: what this build can serve, plus whatever is in
 * effect.
 *
 * The channel in effect is included even when this build cannot serve it. A
 * preference written by a build with a manifest host survives into one without,
 * and `readChannel` resolves it against every known channel rather than against
 * this build's -- so leaving it out renders a list where nothing is selected
 * and the channel actually in use is named nowhere.
 */
function visibleChannels(state: UpdateState): typeof CHANNEL_COPY {
  return CHANNEL_COPY.filter(
    (channel) => state.available.includes(channel.name) || state.channel === channel.name,
  );
}

/** Held behind a disclosure, so selecting it takes a decision rather than a stray click. */
const INTERNAL_CHANNEL: UpdateChannel = "alpha";

function channelRow(
  model: SettingsModel,
  state: UpdateState,
  channel: (typeof CHANNEL_COPY)[number],
): m.Children {
  const peeked = model.peekedChannels[channel.name];
  // A build this app can offer but whose manifest does not resolve -- a
  // channel nobody has promoted to yet, or a feed host that is down. It is
  // not selectable: the preference would stick and updates would stop.
  const isUnavailable = peeked !== undefined && peeked.version === null;
  return m(
    "label",
    {
      class:
        "flex items-start justify-between gap-3 py-3 border-b border-subtle cursor-pointer",
    },
    [
      m("span", [
        // The version rides in the heading rather than on its own line: it is
        // what the channel *is* right now, and a second line repeated the word
        // "Currently" down the whole list.
        m(
          "span",
          { class: "type-body text-primary font-semibold" },
          peeked !== undefined && peeked.version !== null
            ? `${channel.label} (${peeked.version})`
            : channel.label,
        ),
        m("span", { class: "block type-helper text-tertiary" }, channel.blurb),
        isUnavailable
          ? m("span", { class: "block type-helper text-warning" }, "Unavailable right now.")
          : null,
      ]),
      m("input", {
        type: "radio",
        name: "update-channel",
        class: "mt-1 shrink-0",
        checked: state.channel === channel.name,
        disabled: model.isUpdateBusy || isUnavailable,
        onchange: () => void model.requestChannel(channel.name),
      }),
    ],
  );
}

/**
 * The internal channel, held behind a disclosure.
 *
 * It stays here even when it is the channel in effect, and the disclosure
 * opens itself instead. Moving the row into the list on selection would make
 * the group vanish under the cursor that just clicked it, and drop it out of
 * sight again on the way back -- a layout jump either way, for two end states
 * that were already legible.
 *
 * The rows render exactly as they do in the list above -- same width, same
 * alignment, no tint. The summary is the whole of the containment: anything
 * that set the row apart visually would also move it, and a channel that
 * renders differently depending on where it sits reads as a different thing.
 */
function internalChannelDisclosure(
  model: SettingsModel,
  state: UpdateState,
  concealed: typeof CHANNEL_COPY,
): m.Children {
  return m(
    "details",
    // Open when it holds the channel in effect, so what you are running is
    // never behind something you have to open. A manual toggle survives:
    // mithril skips an attribute whose value has not changed, so a redraw does
    // not touch the DOM the reader just changed.
    { open: concealed.some((channel) => channel.name === state.channel) },
    [
      m(
        "summary",
        { class: "type-helper text-tertiary py-3 cursor-pointer" },
        "Internal channels",
      ),
      ...concealed.map((channel) => channelRow(model, state, channel)),
    ],
  );
}

function updatesPanel(model: SettingsModel): m.Children {
  const state = model.updateState;
  if (!electronBridge.isDesktop) {
    // No app updater to describe outside the desktop build; the machine half stands alone.
    return m("section", [
      m("h2", { class: "type-heading-lg text-primary mb-2" }, "Updates"),
      machineUpdatesSection(model),
    ]);
  }
  if (state === null) {
    return m("section", [
      m("h2", { class: "type-heading-lg text-primary mb-2" }, "Updates"),
      // On a desktop build the state is either still being read or the read
      // failed. Saying "updates are managed by the desktop app" here would tell
      // a desktop user, on the surface built to manage them, that they are
      // somebody else's business -- and the menu bar's "Check for Updates..."
      // lands here from oninit, before the read has resolved.
      model.updateError !== ""
        ? m(Notice, { variant: "warn" }, `Could not read the update state: ${model.updateError}`)
        : m("p", { class: "type-body text-secondary" }, "Reading the update state..."),
      machineUpdatesSection(model),
    ]);
  }
  const visible = visibleChannels(state);
  const listed = visible.filter((channel) => channel.name !== INTERNAL_CHANNEL);
  const concealed = visible.filter((channel) => channel.name === INTERNAL_CHANNEL);
  return m("section", [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "Updates"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      `You are running Minds ${state.currentVersion}.`,
    ),
    updateStatusLine(model),
    ...listed.map((channel) => channelRow(model, state, channel)),
    concealed.length > 0 ? internalChannelDisclosure(model, state, concealed) : null,
    state.available.length === 1
      ? m(
          "p",
          { class: "type-helper text-tertiary mt-3" },
          "This build serves the stable channel only.",
        )
      : null,
    model.updateError !== ""
      ? m("p", { class: "type-body text-important mt-3", role: "alert" }, model.updateError)
      : null,
    m("div", { class: "mt-4 flex items-center gap-3" }, [
      // The floating card carries the same control, but it is dismissible and
      // does not come back for a version already dismissed -- so without this
      // there is no way in the app to install a download it has finished.
      state.downloadedVersion != null
        ? m(Button, { variant: "primary", onclick: () => void model.installUpdateNow() }, "Restart now")
        : null,
      // Disabled while the download it already started is running, because a
      // check queued behind that transfer would answer minutes later. The
      // status line above says what is happening, so the button does not
      // repeat it.
      m(
        Button,
        {
          variant: "secondary",
          disabled: model.isUpdateBusy || state.status.type === "update-available",
          onclick: () => void model.checkForUpdatesNow(),
        },
        model.isUpdateBusy ? "Checking..." : "Check now",
      ),
      // Most checks change nothing on screen -- up to date and parked both
      // redraw the same strings -- so without this the button is
      // indistinguishable from one that does nothing. Reported by the main
      // process, so the background checks it runs on its own count too.
      state.lastCheckedAt != null && !model.isUpdateBusy
        ? m("span", { class: "type-helper text-tertiary" }, `Checked ${formatChecked(state.lastCheckedAt)}.`)
        : null,
    ]),
    machineUpdatesSection(model),
    channelSwitchDialog(model),
  ]);
}

/** The grouped left nav + the active panel, in the same two-column pane every
 * machine's options tabs use, so the section list keeps its own scroller
 * instead of riding the card's and sliding off the top of it. */
export function SettingsSections(): m.Component<SectionsAttrs> {
  const masterPasswordState: MasterPasswordState = {
    newPassword: "",
    confirmPassword: "",
  };
  return {
    view(vnode) {
      const { model } = vnode.attrs;
      const groups: ("Permissions" | "Other")[] = ["Permissions", "Other"];
      return [
        splitPane({
          navLabel: "Settings sections",
          nav: m(
            "div",
            { class: "flex flex-col gap-0.5" },
            groups.flatMap((group, groupIdx) => [
              m(
                "p",
                {
                  class: `type-section text-tertiary px-2 mb-1${groupIdx > 0 ? " mt-4" : ""}`,
                },
                group,
              ),
              ...model.visibleSections
                .filter((section) => section.group === group)
                .map((section) => navButton(model, section.name, section.label)),
            ]),
          ),
          content: [
            model.activeSection === "connectors"
              ? connectorsPanel(model)
              : null,
            model.activeSection === "file-sharing"
              ? fileSharingPanel(model)
              : null,
            model.activeSection === "workspace-delegation"
              ? delegationPanel(model)
              : null,
            model.activeSection === "notifications"
              ? notificationsPanel(model)
              : null,
            model.activeSection === "error-reporting"
              ? errorReportingPanel(model)
              : null,
            model.activeSection === "updates" ? updatesPanel(model) : null,
            model.activeSection === "backups"
              ? masterPasswordPanel(model, masterPasswordState)
              : null,
          ],
        }),
        // Fixed-position when open and nothing at all when closed, so it rides
        // beside the pane rather than as a third column inside it.
        revokeDialog(model),
      ];
    },
  };
}
