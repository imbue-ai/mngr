// The app-level settings sections: left nav + the five panels + the shared
// revoke dialog. Port of templates/AppSettingsSections.jinja with the
// interactivity of static/app_settings.js folded into the SettingsModel.

import m from "mithril";
import type {
  PendingRevoke,
  ServiceAccountOverview,
  ServicePermissionOverview,
  SettingsModel,
  SettingsSection,
  WorkspaceDelegationGrant,
  WorkspaceFileSharingGrant,
} from "../../../models/settings";
import { SETTINGS_SECTIONS, addAccountBlockedReason } from "../../../models/settings";
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
function addAccountButton(model: SettingsModel, service: ServicePermissionOverview): m.Children {
  const isBusy = model.addAccountBusyService === service.service_name;
  const blockedReason = addAccountBlockedReason(service);
  return m(
    "span",
    { class: "shrink-0", ...(blockedReason === null ? {} : { title: blockedReason }) },
    m(
      Button,
      {
        variant: "ghost",
        size: "md",
        id: `add-account-${service.service_name}`,
        disabled: isBusy || blockedReason !== null,
        onclick: async () => {
          const errorMessage = await model.addConnectorAccount(service.service_name);
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
              ...SETTINGS_SECTIONS.filter(
                (section) => section.group === group,
              ).map((section) => navButton(model, section.name, section.label)),
            ]),
          ),
          content: [
            model.activeSection === "connectors" ? connectorsPanel(model) : null,
            model.activeSection === "file-sharing"
              ? fileSharingPanel(model)
              : null,
            model.activeSection === "workspace-delegation"
              ? delegationPanel(model)
              : null,
            model.activeSection === "error-reporting"
              ? errorReportingPanel(model)
              : null,
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
