// The Machine settings pane: General / Account / Backup / Updates groups
// behind a left nav, rendered by the options panel's Settings tab
// (WorkspaceSettingsSections.jinja's successor; the old standalone
// /workspace/<id>/settings page now redirects into that tab).

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { Button } from "../../components/Button";
import { ColorSwatch } from "../../components/ColorSwatch";
import { Icon16 } from "../../components/Icon";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { SectionHeader } from "../../components/Layout";
import { TextInput } from "../../components/FormControls";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import type { SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { normalizeWorkspaceColorHex } from "../../../models/workspaceOptions";
import {
  devOverridePrefill,
  isRecreationRequired,
  isUpdateDispatchable,
  standingUpdateNotice,
  updateActivityNotice,
} from "../../../models/updates";
import { noBackupConfirmPrompt, scheduledLine, updateVersionRow } from "../../components/UpdateModal";
import { BackupGroupSlot } from "./BackupGroupSlot";
import { Spinner } from "../../components/Spinner";
import { navEntryClass, splitPane } from "../../components/SplitPane";
import { workspacePageNoticeFor } from "../../shell/notice-band";

const GROUPS: { id: SettingsGroup; icon: string; label: string }[] = [
  { id: "general", icon: "info", label: "General" },
  { id: "account", icon: "user", label: "Account" },
  { id: "backup", icon: "cloud", label: "Backup" },
  { id: "updates", icon: "restart", label: "Updates" },
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
  /** The specific-version field's text and the machine it was typed for; null
   * until edited so the dev-build prefill shows through. */
  overrideDraft: { agentId: string; value: string } | null;
  isOverrideOpen: boolean;
  /** The in-flight dispatch: its machine and target ref ("" for the plain
   * "Update now"), which each button's spinner keys off. */
  pendingDispatch: { agentId: string; targetRef: string } | null;
  /** The in-flight schedule write, its machine, and its target ref ("" for the default). */
  pendingSchedule: { agentId: string; action: "schedule" | "cancel"; targetRef: string } | null;
  /** The press held for the go-ahead-without-backups confirmation, and its machine. */
  noBackupConfirm: { agentId: string; action: "now" | "schedule"; targetRef: string } | null;
  /** The last dispatch's refusal and the machine it was refused for, or null. */
  updateError: { agentId: string; message: string } | null;
}

/** The piece of update state only when it belongs to the machine being drawn:
 * a route change between machines preserves this component instance, so every
 * piece is machine-stamped. */
function forMachine<T extends { agentId: string }>(held: T | null, agentId: string): T | null {
  return held?.agentId === agentId ? held : null;
}

export function SettingsGroups(): m.Component<SettingsGroupsAttrs> {
  const local: SettingsGroupsLocalState = {
    nameDraft: null,
    colorDraft: null,
    isDestroyDialogOpen: false,
    isUnlinkDialogOpen: false,
    overrideDraft: null,
    isOverrideOpen: false,
    pendingDispatch: null,
    pendingSchedule: null,
    noBackupConfirm: null,
    updateError: null,
  };
  // ?override=1 is the update modal's "Update to a different version…" deep
  // link. Watched per render because route changes preserve this instance, and
  // for the value CHANGING to "1" because the param stays in the URL across
  // group switches and must not re-open a field the user closed.
  let lastOverrideParam: string | null | undefined;
  function consumeOverrideParam(): void {
    const param = new URLSearchParams((m.route.get() ?? "").split("?")[1] ?? "").get("override");
    if (param === "1" && lastOverrideParam !== "1") local.isOverrideOpen = true;
    lastOverrideParam = param;
  }

  return {
    view(vnode) {
      consumeOverrideParam();
      const { model, selectedGroup, onSelectGroup } = vnode.attrs;
      const data = model.data;
      if (data === null) return null;

      return [
        outOfDateNotice(data.agent_id),
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
            selectedGroup === "updates" ? renderUpdatesGroup(data.agent_id, local) : null,
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

/** The machine's own out-of-date notice, reached through the band's decision
 * so the two cannot disagree. */
function outOfDateNotice(agentId: string): m.Children {
  const { stores, shell } = getAppContext();
  const payload = workspacePageNoticeFor(
    standingUpdateNotice(stores.updates.forAgent(agentId), stores.updates.isUpdating(agentId)),
  );
  if (payload === null) return null;
  return m(
    Notice,
    { variant: payload.variant, extra: "mb-4", id: "ws-settings-out-of-date" },
    m("div", { class: "flex items-center justify-between gap-3" }, [
      m("span", payload.message),
      payload.action !== null
        ? m(Button, { variant: "secondary", onclick: () => shell.openUpdateModal(agentId) }, payload.action.label)
        : null,
    ]),
  );
}

/** Why "Update now" is greyed out: a disabled button with nothing beside it
 * reads as a bug. */
function disabledUpdateReason(update: UiWorkspaceUpdate): m.Children {
  const reason =
    update.availability === "UP_TO_DATE"
      ? "This machine is up to date."
      : update.availability === "APP_BEHIND"
        ? "This machine is newer than this copy of Minds. Update the app to catch up."
        : isRecreationRequired(update)
          ? "This machine is too old to update in place. Create a new machine and ask its agent to migrate your work across."
          : "";
  return reason ? m("p", { class: "type-helper text-tertiary mt-2" }, reason) : null;
}

/** The Updates group: versions, the update action, and the collapsed
 * specific-version override. The override is a real feature (an up-to-date
 * machine can be moved to a branch or a newer release) but collapsed because
 * naming a ref is the rare path. */
function renderUpdatesGroup(agentId: string, local: SettingsGroupsLocalState): m.Children {
  const { stores, shell } = getAppContext();
  const updates = stores.updates;
  const update = updates.forAgent(agentId);
  const isUpdating = updates.isUpdating(agentId);
  const pending = forMachine(local.pendingDispatch, agentId);
  const pendingSchedule = forMachine(local.pendingSchedule, agentId);
  const isBusy = pending !== null || pendingSchedule !== null;
  const prefill = devOverridePrefill(update.supported_version ?? "");
  const draft = forMachine(local.overrideDraft, agentId);
  const overrideValue = draft?.value ?? prefill;

  /** A press of either update-now button; asks the same no-backups question the modal asks. */
  function requestDispatch(targetRef: string): void {
    local.updateError = null;
    if (updates.needsNoBackupConfirmation(agentId)) {
      local.noBackupConfirm = { agentId, action: "now", targetRef };
      return;
    }
    dispatch(targetRef);
  }

  /** A press of either schedule button; asks the same no-backups question the modal asks. */
  function requestSchedule(targetRef: string): void {
    local.updateError = null;
    if (updates.needsNoBackupConfirmation(agentId)) {
      local.noBackupConfirm = { agentId, action: "schedule", targetRef };
      return;
    }
    writeSchedule("schedule", targetRef);
  }

  function writeSchedule(action: "schedule" | "cancel", targetRef = ""): void {
    const inFlight = { agentId, action, targetRef };
    local.pendingSchedule = inFlight;
    local.updateError = null;
    const call =
      action === "schedule" ? updates.scheduleUpdate(agentId, targetRef) : updates.cancelSchedule(agentId);
    void call.then((result) => {
      if (local.pendingSchedule === inFlight) local.pendingSchedule = null;
      if (!result.isOk) local.updateError = { agentId, message: result.error };
      m.redraw();
    });
  }

  function dispatch(targetRef: string): void {
    const inFlight = { agentId, targetRef };
    local.pendingDispatch = inFlight;
    local.updateError = null;
    void updates.updateNow(agentId, targetRef).then((result) => {
      // Only if still the dispatch being waited on: a press on another machine
      // meanwhile owns the slot.
      if (local.pendingDispatch === inFlight) local.pendingDispatch = null;
      // Into the machine, as the modal's Update now does: an attended update is a conversation.
      if (result.isOk) shell.enterWorkspace(agentId);
      else local.updateError = { agentId, message: result.error };
      m.redraw();
    });
  }

  const activity = updateActivityNotice(update, isUpdating);
  const held = forMachine(local.noBackupConfirm, agentId);
  const errorMessage = forMachine(local.updateError, agentId)?.message ?? "";

  return m("div", { class: "max-w-md" }, [
    m(SectionHeader, "Version"),
    m("div", { class: "flex flex-col gap-1 mb-8" }, [
      updateVersionRow("This machine", update.current_version ?? ""),
      updateVersionRow("Supported by Minds", update.supported_version ?? ""),
    ]),
    m(SectionHeader, "Update"),
    m("div", { id: "ws-updates-group", class: "mb-3" }, [
      activity.message
        ? m(
            Notice,
            { variant: "info", extra: "mb-3" },
            m("span", { class: "inline-flex items-center gap-2" }, [
              activity.isWaiting ? m(Spinner, { size: "sm" }) : null,
              m("span", activity.message),
            ]),
          )
        : null,
      m(
        "p",
        { class: "type-helper text-secondary mb-3" },
        "Updating runs an agent inside this machine, which uses credits. Its chat is the record of what was done.",
      ),
      held !== null
        ? // The question replaces the controls so there is one way to answer it.
          noBackupConfirmPrompt({
            onConfirm: () => {
              local.noBackupConfirm = null;
              if (held.action === "schedule") writeSchedule("schedule", held.targetRef);
              else dispatch(held.targetRef);
            },
            onCancel: () => {
              local.noBackupConfirm = null;
            },
          })
        : [
          update.is_scheduled
            ? m("p", { class: "type-helper text-secondary mb-3" }, scheduledLine(update, updates.updateWindow))
            : null,
          // Schedule first, as in the modal: the update window is when nobody is in the machine.
          m("div", { class: "flex items-center gap-2" }, [
            update.is_scheduled
              ? m(
                  Button,
                  {
                    variant: "secondary",
                    id: "ws-update-cancel-schedule-btn",
                    disabled: isBusy,
                    onclick: () => writeSchedule("cancel"),
                  },
                  pendingSchedule?.action === "cancel" ? m(Spinner, { size: "sm" }) : "Cancel schedule",
                )
              : m(
                  Button,
                  {
                    variant: "primary",
                    id: "ws-update-schedule-btn",
                    disabled: isBusy || isUpdating || !isUpdateDispatchable(update),
                    onclick: () => requestSchedule(""),
                  },
                  pendingSchedule?.action === "schedule" && pendingSchedule.targetRef === ""
                    ? m(Spinner, { size: "sm" })
                    : "Schedule update",
                ),
            m(
              Button,
              {
                variant: "secondary",
                id: "ws-update-now-btn",
                disabled: isBusy || isUpdating || !isUpdateDispatchable(update),
                onclick: () => requestDispatch(""),
              },
              pending?.targetRef === "" ? m(Spinner, { size: "sm" }) : "Update now",
            ),
          ]),
          disabledUpdateReason(update),
          // A named version is applied by the same in-place run, so a machine
          // too old for one is too old for the other.
          isRecreationRequired(update)
            ? null
            : m("div", { class: "mt-4" }, [
            m(
              "button",
              {
                type: "button",
                id: "update-override-toggle",
                class: "inline-flex items-center gap-1 type-helper text-secondary hover:text-primary cursor-pointer",
                "aria-expanded": local.isOverrideOpen ? "true" : "false",
                onclick: () => (local.isOverrideOpen = !local.isOverrideOpen),
              },
              [
                m(Icon16, { name: local.isOverrideOpen ? "chevron-down" : "chevron-right", size: "sm" }),
                m("span", "Update to a specific version"),
              ],
            ),
            local.isOverrideOpen
              ? m("div", { class: "flex flex-col gap-2 mt-2" }, [
                  m("div", { class: "flex items-center gap-2" }, [
                    m(TextInput, {
                      id: "update-override-input",
                      name: "update_override_ref",
                      value: overrideValue,
                      placeholder: "minds-v0.4.2, main, or a git ref",
                      spellcheck: "false",
                      autocomplete: "off",
                      "aria-label": "Version to update to",
                      extra: "flex-1",
                      oninput: (event: InputEvent) => {
                        local.overrideDraft = { agentId, value: (event.target as HTMLInputElement).value };
                      },
                    }),
                    m(
                      Button,
                      {
                        variant: "secondary",
                        id: "update-override-btn",
                        disabled: isBusy || isUpdating || overrideValue.trim() === "",
                        onclick: () => requestDispatch(overrideValue.trim()),
                      },
                      pending !== null && pending.targetRef !== ""
                        ? m(Spinner, { size: "sm" })
                        : "Update to this version",
                    ),
                    // The same confirmation the press above is; scheduling only
                    // changes when the run happens.
                    m(
                      Button,
                      {
                        variant: "secondary",
                        id: "update-override-schedule-btn",
                        disabled: isBusy || isUpdating || overrideValue.trim() === "",
                        onclick: () => requestSchedule(overrideValue.trim()),
                      },
                      pendingSchedule?.action === "schedule" && pendingSchedule.targetRef !== ""
                        ? m(Spinner, { size: "sm" })
                        : "Schedule",
                    ),
                  ]),
                  prefill && draft === null
                    ? m(
                        "p",
                        { class: "type-helper text-tertiary" },
                        "Prefilled with the template ref this build of Minds runs from.",
                      )
                    : null,
                  m(
                    "p",
                    { class: "type-helper text-tertiary" },
                    "Works on an up-to-date machine too. A version newer than this Minds app, a branch, or a bare " +
                      "ref is allowed and applied without further confirmation: it may not be a tested release, " +
                      "parts of this machine may stop working until the app catches up, and the update agent " +
                      'offers a rollback afterwards. On a branch, this machine may afterwards read as "version unknown".',
                  ),
                ])
              : null,
          ]),
          ],
      errorMessage
        ? m("p", { class: "type-helper text-important mt-3", role: "alert" }, errorMessage)
        : null,
    ]),
  ]);
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
