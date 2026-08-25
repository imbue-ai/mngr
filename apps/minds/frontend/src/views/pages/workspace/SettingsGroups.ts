// The Machine settings pane: General / Account / Backup groups behind a left
// nav, rendered by the options panel's Settings tab
// (WorkspaceSettingsSections.jinja's successor; the old standalone
// /workspace/<id>/settings page now redirects into that tab).

import m from "mithril";
import { Button } from "../../components/Button";
import { ColorSwatch } from "../../components/ColorSwatch";
import { Icon16 } from "../../components/Icon";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { SectionHeader } from "../../components/Layout";
import { TextInput } from "../../components/FormControls";
import type { SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { normalizeWorkspaceColorHex } from "../../../models/workspaceOptions";
import { BackupGroupSlot } from "./BackupGroupSlot";
import { navEntryClass, splitPane } from "../../components/SplitPane";

const GROUPS: { id: SettingsGroup; icon: string; label: string }[] = [
  { id: "general", icon: "info", label: "General" },
  { id: "account", icon: "user", label: "Account" },
  { id: "backup", icon: "cloud", label: "Backup" },
];

export interface SettingsGroupsAttrs {
  model: WorkspaceOptionsModel;
  selectedGroup: SettingsGroup;
  onSelectGroup: (group: SettingsGroup) => void;
}

interface SettingsGroupsLocalState {
  nameDraft: string | null;
  colorDraft: string | null;
  isDestroyDialogOpen: boolean;
  isUnlinkDialogOpen: boolean;
}

export function SettingsGroups(): m.Component<SettingsGroupsAttrs> {
  const local: SettingsGroupsLocalState = {
    nameDraft: null,
    colorDraft: null,
    isDestroyDialogOpen: false,
    isUnlinkDialogOpen: false,
  };

  return {
    view(vnode) {
      const { model, selectedGroup, onSelectGroup } = vnode.attrs;
      const data = model.data;
      if (data === null) return null;

      return [
        splitPane({
          navLabel: "Settings groups",
          nav: m(
            "div",
            { class: "flex flex-col gap-0.5" },
            GROUPS.map((group) =>
              m(
                "button",
                {
                  type: "button",
                  "data-settings-group": group.id,
                  "aria-pressed": group.id === selectedGroup ? "true" : "false",
                  class: navEntryClass(group.id === selectedGroup),
                  onclick: () => onSelectGroup(group.id),
                },
                [m(Icon16, { name: group.icon, extra: "shrink-0" }), m("span", { class: "truncate" }, group.label)],
              ),
            ),
          ),
          content: [
            selectedGroup === "general" ? renderGeneralGroup(model, local) : null,
            selectedGroup === "account" ? renderAccountGroup(model, local) : null,
            selectedGroup === "backup" ? m(BackupGroupSlot, { agentId: data.agent_id }) : null,
          ],
          extra: "mt-8",
        }),
        // Fixed-position when open and nothing at all when closed, so they ride
        // beside the pane rather than as extra columns inside it.
        renderDestroyDialog(model, local),
        renderUnlinkDialog(model, local),
      ];
    },
  };
}

function renderGeneralGroup(model: WorkspaceOptionsModel, local: SettingsGroupsLocalState): m.Children {
  const data = model.data;
  if (data === null) return null;
  const nameValue = local.nameDraft ?? data.name;
  const colorValue = local.colorDraft ?? data.color;
  const normalizedDraft = normalizeWorkspaceColorHex(colorValue);
  const isCustomColor = normalizedDraft !== null && !Object.values(data.palette).includes(normalizedDraft);

  return m("div", [
    m(SectionHeader, "Name"),
    m("div", { id: "rename-section", class: "mb-8" }, [
      data.is_stale
        ? m(
            Notice,
            { variant: "warn" },
            "This workspace's machine is currently unreachable, so the workspace can't be renamed " +
              "right now. Try again once the machine reconnects.",
          )
        : null,
      m("div", { class: "flex items-center gap-2" }, [
        m(TextInput, {
          id: "workspace-name-input",
          name: "workspace_name",
          value: nameValue,
          maxlength: 200,
          spellcheck: "false",
          autocomplete: "off",
          "aria-label": "Workspace name",
          extra: "max-w-xs",
          disabled: data.is_stale,
          oninput: (event: InputEvent) => {
            local.nameDraft = (event.target as HTMLInputElement).value;
          },
        }),
        m(
          Button,
          {
            variant: "secondary",
            id: "rename-save-btn",
            disabled: data.is_stale || model.isRenameSaving,
            onclick: () => {
              void model.rename(nameValue).then((isRenamed) => {
                if (isRenamed) local.nameDraft = null;
              });
            },
          },
          "Save",
        ),
        model.isRenameSaving ? m("span", { class: "type-section text-secondary" }, "Saving...") : null,
      ]),
      model.renameErrorMessage
        ? m("p", { id: "rename-error", class: "type-body text-important mt-2", role: "alert" }, model.renameErrorMessage)
        : null,
    ]),

    m(SectionHeader, { extra: "flex items-center gap-2" }, [
      "Color ",
      model.isColorSaving
        ? m("span", { class: "type-section text-primary bg-surface-primary rounded-sm px-1.5 py-1" }, "Saving")
        : null,
    ]),
    m("div", { id: "color-section", class: "mb-8" }, [
      data.is_stale
        ? m(
            Notice,
            { variant: "warn" },
            "This machine is currently unreachable, so its color can't be changed right now. " +
              "Try again once the machine reconnects.",
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
          ...Object.entries(data.palette).map(([name, hexValue]) =>
            m(ColorSwatch, {
              hex: hexValue,
              name,
              size: "md",
              selected: hexValue === (normalizedDraft ?? data.color),
              disabled: data.is_stale || model.isColorSaving,
              onclick: () => {
                local.colorDraft = hexValue;
                void saveColorDraft(model, local, hexValue);
              },
            }),
          ),
          m("input", {
            id: "color-hex-input",
            type: "text",
            class:
              "color-hex-pill h-[34px] px-3 rounded-full type-body font-mono text-primary " +
              "placeholder:text-tertiary disabled:opacity-40" +
              (isCustomColor ? " is-selected" : ""),
            value: colorValue,
            placeholder: "#a1b2c3",
            maxlength: 9,
            spellcheck: "false",
            autocomplete: "off",
            "aria-label": "Workspace color hex",
            size: 8,
            disabled: data.is_stale || model.isColorSaving,
            oninput: (event: InputEvent) => {
              local.colorDraft = (event.target as HTMLInputElement).value;
            },
            onblur: () => void commitHexDraft(model, local),
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter") {
                event.preventDefault();
                (event.target as HTMLInputElement).blur();
              }
            },
          }),
        ],
      ),
      model.colorErrorMessage
        ? m("p", { id: "color-error", class: "type-body text-important mt-2", role: "alert" }, model.colorErrorMessage)
        : null,
    ]),

    m(SectionHeader, "ID"),
    m("p", { class: "type-body font-mono text-secondary mb-8 select-all break-all" }, data.agent_id),

    m(SectionHeader, "Danger zone"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Permanently destroy this machine and release any associated resources.",
    ),
    m(
      Button,
      {
        variant: "danger",
        id: "destroy-btn",
        onclick: () => {
          local.isDestroyDialogOpen = true;
        },
      },
      "Remove machine",
    ),
    model.destroyErrorMessage
      ? m("p", { id: "destroy-error", class: "type-body text-important mt-2" }, model.destroyErrorMessage)
      : null,
  ]);
}

async function saveColorDraft(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  normalizedHex: string,
): Promise<void> {
  // No optimistic accent preview here: pages have no ShellState handle, and
  // the save's channel round trip (mngr label -> resolver -> publisher ->
  // workspaces message) repaints the titlebar within a tick.
  const isSaved = await model.saveColor(normalizedHex, () => undefined);
  if (isSaved) local.colorDraft = null;
  else local.colorDraft = model.lastSavedColor;
}

async function commitHexDraft(model: WorkspaceOptionsModel, local: SettingsGroupsLocalState): Promise<void> {
  if (local.colorDraft === null) return;
  const normalized = normalizeWorkspaceColorHex(local.colorDraft);
  if (normalized === null) {
    model.colorErrorMessage = "That hex value is not valid. Use #rrggbb or #rgb.";
    local.colorDraft = null;
    m.redraw();
    return;
  }
  await saveColorDraft(model, local, normalized);
}

function renderAccountGroup(model: WorkspaceOptionsModel, local: SettingsGroupsLocalState): m.Children {
  const data = model.data;
  if (data === null) return null;

  return m("div", [
    m(SectionHeader, "Account"),
    m("div", { id: "account-section" }, [
      data.is_leased_imbue_cloud
        ? [
            data.current_account
              ? m("p", { class: "type-body text-primary mb-3" }, [
                  "Linked to ",
                  m("strong", data.current_account.email),
                  ".",
                ])
              : null,
            m(
              Notice,
              { variant: "info" },
              "This machine runs on a host leased from Imbue Cloud, so its account link is fixed and cannot be changed.",
            ),
            m(Button, { variant: "danger", id: "disassociate-btn", disabled: true }, "Unlink"),
          ]
        : data.current_account
          ? [
              m("p", { class: "type-body text-primary mb-3" }, [
                "Linked to ",
                m("strong", data.current_account.email),
                ".",
              ]),
              m(
                Notice,
                { variant: "warn" },
                "Unlinking stops all sharing for this machine and revokes its links. " +
                  "You will need to set up sharing again after linking it back.",
              ),
              m(
                Button,
                {
                  variant: "danger",
                  id: "disassociate-btn",
                  disabled: model.isAccountBusy,
                  onclick: () => {
                    local.isUnlinkDialogOpen = true;
                  },
                },
                model.isAccountBusy ? "Unlinking..." : "Unlink",
              ),
            ]
          : renderAssociatePrompt(model, data.accounts),
      model.accountErrorMessage
        ? m(
            "p",
            { id: "disassociate-error", class: "type-body text-important mt-2", role: "alert" },
            model.accountErrorMessage,
          )
        : null,
    ]),
  ]);
}

function renderAssociatePrompt(
  model: WorkspaceOptionsModel,
  accounts: { user_id: string; email: string }[],
): m.Children {
  if (accounts.length === 0) {
    return m("div", [
      m(
        "p",
        { class: "type-body text-secondary mb-3" },
        "Link this machine to an account to enable sharing and cloud backups.",
      ),
      m(
        "p",
        { class: "type-body text-secondary" },
        m("a", { class: "text-primary underline", ...routeLinkAttrs("/accounts") }, "Sign in or create an account"),
      ),
    ]);
  }
  return m("div", [
    m("p", { class: "type-body text-secondary mb-3" }, "Link this machine to one of your accounts:"),
    m(
      "div",
      { class: "flex flex-col gap-2" },
      accounts.map((account) =>
        m(
          Button,
          {
            variant: "secondary",
            disabled: model.isAccountBusy,
            onclick: () => void model.setAccount(account.user_id),
          },
          `Link to ${account.email}`,
        ),
      ),
    ),
  ]);
}

function renderDestroyDialog(model: WorkspaceOptionsModel, local: SettingsGroupsLocalState): m.Children {
  const data = model.data;
  if (data === null) return null;
  return m(
    Modal,
    {
      isOpen: local.isDestroyDialogOpen,
      onClose: () => {
        local.isDestroyDialogOpen = false;
      },
    },
    [
      m("h2", { class: "type-heading-lg text-primary mb-3" }, "Remove machine?"),
      m("p", { class: "type-body text-primary mb-4" }, [
        "This will permanently destroy ",
        m("strong", data.name),
        " and all its data. This action cannot be undone. Backups are kept for 30 days after destruction, then deleted.",
      ]),
      m("div", { class: "flex justify-end gap-3" }, [
        m(
          Button,
          {
            variant: "secondary",
            id: "destroy-cancel-btn",
            onclick: () => {
              local.isDestroyDialogOpen = false;
            },
          },
          "Cancel",
        ),
        m(
          Button,
          {
            variant: "danger",
            id: "destroy-confirm-btn",
            disabled: model.isDestroyPending,
            onclick: () => {
              void model.destroy().then((isDestroyStarted) => {
                local.isDestroyDialogOpen = false;
                if (isDestroyStarted) m.route.set(`/destroying/${model.agentId}`);
              });
            },
          },
          model.isDestroyPending ? "Removing..." : "Remove",
        ),
      ]),
    ],
  );
}

function renderUnlinkDialog(model: WorkspaceOptionsModel, local: SettingsGroupsLocalState): m.Children {
  const data = model.data;
  if (data === null) return null;
  return m(
    Modal,
    {
      isOpen: local.isUnlinkDialogOpen,
      onClose: () => {
        local.isUnlinkDialogOpen = false;
      },
    },
    [
      m("h2", { class: "type-heading-lg text-primary mb-3" }, "Unlink this machine?"),
      m("p", { class: "type-body text-primary mb-4" }, [
        "This stops all sharing for ",
        m("strong", data.name),
        " and revokes its links. You will need to set up sharing again after linking it back.",
      ]),
      m("div", { class: "flex justify-end gap-3" }, [
        m(
          Button,
          {
            variant: "secondary",
            id: "unlink-cancel-btn",
            onclick: () => {
              local.isUnlinkDialogOpen = false;
            },
          },
          "Cancel",
        ),
        m(
          Button,
          {
            variant: "danger",
            id: "unlink-confirm-btn",
            disabled: model.isAccountBusy,
            onclick: () => {
              void model.setAccount(null).then(() => {
                local.isUnlinkDialogOpen = false;
              });
            },
          },
          model.isAccountBusy ? "Unlinking..." : "Unlink",
        ),
      ]),
    ],
  );
}
