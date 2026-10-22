// The Workspace settings pane: General / Account / Backup / Updates groups
// behind a left nav, rendered by the options panel's Settings tab
// (WorkspaceSettingsSections.jinja's successor; the old standalone
// /workspace/<id>/settings page now redirects into that tab).

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { Button } from "../../components/Button";
import { ColorSwatch } from "../../components/ColorSwatch";
import { Icon16 } from "../../components/Icon";
import { Collapsible } from "../../components/Collapsible";
import type { IconName } from "../../components/icons";
import { machineVerdict } from "../../components/MachineVerdict";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { CopyField, SectionHeader } from "../../components/Layout";
import { FormLabel, Select, TextInput } from "../../components/FormControls";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import type {
  SettingsGroup,
  WorkspaceMachineSizeData,
  WorkspaceOptionsModel,
} from "../../../models/workspaceOptions";
import {
  formatPendingMachineSize,
  isMachineSizePickedFromChoices,
  minimumMachineDiskGb,
  normalizeWorkspaceColorHex,
} from "../../../models/workspaceOptions";
import type {
  MachineSizeDimension,
  MachineSizeDraft,
} from "../../../models/machineSize";
import {
  formatMachineSizeNumbers,
  machineSizeDraftFor,
} from "../../../models/machineSize";
import { MachineSizeInput } from "../../components/MachineSizeInput";
import {
  RUNNING_CHATS_PAUSE_LINE,
  devOverridePrefill,
  isArmedByAutoUpdate,
  isRecreationRequired,
  isUpdateDispatchable,
  plainScheduleTargetRef,
  recordedDispatchFailure,
  scheduleSkipLine,
  scheduledLine,
  skippedVersionText,
  standingUpdateNotice,
  updateActivityNotice,
} from "../../../models/updates";
import { SettingsModel } from "../../../models/settings";
import {
  noBackupConfirmPrompt,
  skipConfirmPrompt,
  STOPPED_UPDATES_LINE,
  updateVersionRow,
} from "../../components/UpdateModal";
import { workspaceUpdateSettings } from "../settings/WorkspaceUpdateSettings";
import { BackupGroup } from "./BackupGroup";
import { Spinner } from "../../components/Spinner";
import { navEntryClass, splitPane } from "../../components/SplitPane";
import { workspacePageNoticeFor } from "../../shell/notice-band";

const GROUPS: { id: SettingsGroup; icon: IconName; label: string }[] = [
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
  pendingSchedule: {
    agentId: string;
    action: "schedule" | "cancel" | "skip";
    targetRef: string;
  } | null;
  /** The press held for the go-ahead-without-backups confirmation, and its machine. */
  noBackupConfirm: {
    agentId: string;
    action: "now" | "schedule";
    targetRef: string;
  } | null;
  /** The machine whose skip is waiting on its confirmation, or null. */
  skipConfirmAgentId: string | null;
  /** The last dispatch's refusal and the machine it was refused for, or null. */
  updateError: { agentId: string; message: string; detail: string } | null;
  /** The resize fields as typed, and the machine they were typed for; null until edited. */
  sizeDraft: { agentId: string; draft: MachineSizeDraft } | null;
  /** The app-wide update settings the Updates group mirrors (auto-update and
   * the window); one copy for the pane, since they are not per machine. */
  appSettings: SettingsModel;
}

/** The piece of update state only when it belongs to the machine being drawn:
 * a route change between machines preserves this component instance, so every
 * piece is machine-stamped. */
function forMachine<T extends { agentId: string }>(
  held: T | null,
  agentId: string,
): T | null {
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
    skipConfirmAgentId: null,
    updateError: null,
    sizeDraft: null,
    appSettings: new SettingsModel(),
  };
  // ?override=1 is the update modal's "Update to a different version…" deep
  // link. Watched per render because route changes preserve this instance, and
  // for the value CHANGING to "1" because the param stays in the URL across
  // group switches and must not re-open a field the user closed.
  let lastOverrideParam: string | null | undefined;
  // The app-wide values can change on another surface (app Settings, the
  // auto-update offer) while this pane stays mounted; the updates frame says
  // when, and the pane's copy is read again.
  let lastSeenAppUpdateSettings: string | undefined;
  function reloadAppSettingsWhenChangedElsewhere(): void {
    const updates = getAppContext().stores.updates;
    if (!updates.hasReceivedUpdates) return;
    const seen = `${String(updates.isAutoUpdateEnabled)}|${updates.updateWindow}`;
    if (lastSeenAppUpdateSettings !== undefined && seen !== lastSeenAppUpdateSettings) {
      void local.appSettings.load();
    }
    lastSeenAppUpdateSettings = seen;
  }
  function consumeOverrideParam(): void {
    const param = new URLSearchParams(
      (m.route.get() ?? "").split("?")[1] ?? "",
    ).get("override");
    if (param === "1" && lastOverrideParam !== "1") local.isOverrideOpen = true;
    lastOverrideParam = param;
  }

  return {
    oninit() {
      void local.appSettings.load();
    },
    view(vnode) {
      consumeOverrideParam();
      reloadAppSettingsWhenChangedElsewhere();
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
                [
                  m(Icon16, { name: group.icon, extra: "shrink-0" }),
                  m("span", { class: "truncate" }, group.label),
                ],
              ),
            ),
          ),
          content: [
            selectedGroup === "general"
              ? renderGeneralGroup(model, local)
              : null,
            selectedGroup === "account"
              ? renderAccountGroup(model, local)
              : null,
            selectedGroup === "backup"
              ? m(BackupGroup, { agentId: data.agent_id })
              : null,
            selectedGroup === "updates"
              ? renderUpdatesGroup(data.agent_id, local)
              : null,
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
    standingUpdateNotice(
      stores.updates.forAgent(agentId),
      stores.updates.isUpdating(agentId),
      stores.updates.isAutoUpdateEnabled,
    ),
  );
  if (payload === null) return null;
  return m(
    Notice,
    { variant: payload.variant, extra: "mb-4", id: "ws-settings-out-of-date" },
    m("div", { class: "flex items-center justify-between gap-3" }, [
      m("span", payload.message),
      payload.actions.map((action) =>
        m(
          Button,
          {
            variant: "secondary",
            onclick: () =>
              action.kind === "sign-in-for-update"
                ? shell.signInForUpdateOrExplain(agentId)
                : shell.openUpdateModal(agentId),
          },
          action.label,
        ),
      ),
    ]),
  );
}

/** Why "Update now" is greyed out: a disabled button with nothing beside it
 * reads as a bug. */
function disabledUpdateReason(update: UiWorkspaceUpdate): m.Children {
  const reason =
    update.availability === "UP_TO_DATE"
      ? "This workspace is up to date."
      : update.availability === "APP_BEHIND"
        ? "This workspace is newer than this copy of Imbue Studio. Update the app to catch up."
        : isRecreationRequired(update)
          ? "This workspace is too old to update in place. Create a new workspace and ask its agent to migrate your work across."
          : (update.is_decision_pending ?? false)
            ? "The last update is waiting for your decision in its chat."
            : "";
  return reason
    ? m("p", { class: "type-helper text-tertiary mt-2" }, reason)
    : null;
}

/** The Updates group: versions, the update action, and the collapsed
 * specific-version override. The override is a real feature (an up-to-date
 * machine can be moved to a branch or a newer release) but collapsed because
 * naming a ref is the rare path. */
function renderUpdatesGroup(
  agentId: string,
  local: SettingsGroupsLocalState,
): m.Children {
  const { stores, shell } = getAppContext();
  const updates = stores.updates;
  const update = updates.forAgent(agentId);
  const isAutoUpdateEnabled = updates.isAutoUpdateEnabled;
  const isUpdating = updates.isUpdating(agentId);
  // A run under way supersedes the schedule, and a skip or cancel beside it would not stop it.
  const isScheduled = (update.is_scheduled ?? false) && !isUpdating;
  const isSkipOffered =
    isScheduled && isArmedByAutoUpdate(update, isAutoUpdateEnabled);
  const pendingSchedule = forMachine(local.pendingSchedule, agentId);
  // A question about a schedule that has since gone is dropped, so it cannot come back unasked.
  // Not while its skip is out: the skip cancels the schedule before it answers.
  if (
    local.skipConfirmAgentId === agentId &&
    pendingSchedule?.action !== "skip" &&
    !isSkipOffered
  ) {
    local.skipConfirmAgentId = null;
  }
  if (updates.isQuietWhileStopped(agentId)) {
    if (forMachine(local.noBackupConfirm, agentId) !== null)
      local.noBackupConfirm = null;
    return m("div", { class: "max-w-md" }, [
      m(SectionHeader, "Update"),
      m("p", { id: "ws-updates-stopped", class: "type-helper text-secondary" }, STOPPED_UPDATES_LINE),
      appUpdateSettingsSection(local),
    ]);
  }
  const pending = forMachine(local.pendingDispatch, agentId);
  const isBusy = pending !== null || pendingSchedule !== null;
  const prefill = devOverridePrefill(update.supported_version ?? "");
  const draft = forMachine(local.overrideDraft, agentId);
  const overrideValue = draft?.value ?? prefill;
  const skipLine = scheduleSkipLine(update);
  const isDecisionPending = update.is_decision_pending ?? false;
  const needsSignIn = (update.needs_sign_in ?? false) && !isUpdating;
  const chatAgentId = update.chat_agent_id ?? "";
  // A pending decision is answered in the run's chat, not by another run.
  const isDispatchBlocked =
    isBusy || isUpdating || isDecisionPending || !isUpdateDispatchable(update);
  const plainScheduleTarget = plainScheduleTargetRef(update);
  // A named version is not gated on availability, as the specific-version field is not.
  const isPlainScheduleBlocked =
    plainScheduleTarget === "" ? isDispatchBlocked : isBusy || isUpdating || isDecisionPending;

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

  function writeSchedule(
    action: "schedule" | "cancel" | "skip",
    targetRef = "",
  ): void {
    const inFlight = { agentId, action, targetRef };
    local.pendingSchedule = inFlight;
    local.updateError = null;
    const call =
      action === "schedule"
        ? updates.scheduleUpdate(agentId, targetRef)
        : action === "cancel"
          ? updates.cancelSchedule(agentId)
          : updates.skipUpdate(agentId);
    void call.then((result) => {
      if (local.pendingSchedule === inFlight) local.pendingSchedule = null;
      if (action === "skip" && local.skipConfirmAgentId === agentId)
        local.skipConfirmAgentId = null;
      if (!result.isOk)
        local.updateError = {
          agentId,
          message: result.error,
          detail: result.detail,
        };
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
      else
        local.updateError = {
          agentId,
          message: result.error,
          detail: result.detail,
        };
      m.redraw();
    });
  }

  const activity = updateActivityNotice(update, isUpdating);
  const held = forMachine(local.noBackupConfirm, agentId);
  const shownError =
    forMachine(local.updateError, agentId) ??
    recordedDispatchFailure(update, isUpdating);
  const errorMessage = shownError.message;

  return m("div", { class: "max-w-md" }, [
    m(SectionHeader, "Version"),
    m("div", { class: "flex flex-col gap-1 mb-8" }, [
      updateVersionRow("This workspace", update.current_version ?? ""),
      updateVersionRow(
        "Supported by Imbue Studio",
        update.supported_version ?? "",
      ),
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
        "Updating runs an agent inside this workspace, which uses credits. Its chat is the record of what was done. " +
          RUNNING_CHATS_PAUSE_LINE,
      ),
      held !== null
        ? // The question replaces the controls so there is one way to answer it.
          noBackupConfirmPrompt({
            onConfirm: () => {
              local.noBackupConfirm = null;
              if (held.action === "schedule")
                writeSchedule("schedule", held.targetRef);
              else dispatch(held.targetRef);
            },
            onCancel: () => {
              local.noBackupConfirm = null;
            },
          })
        : local.skipConfirmAgentId === agentId
          ? skipConfirmPrompt(update, isAutoUpdateEnabled, {
              isSkipping: pendingSchedule?.action === "skip",
              onConfirm: () => writeSchedule("skip"),
              onCancel: () => {
                local.skipConfirmAgentId = null;
              },
            })
          : [
            isScheduled
              ? m("div", { class: "flex flex-col gap-1 mb-3" }, [
                  m(
                    "p",
                    { class: "type-helper text-secondary" },
                    scheduledLine(
                      update,
                      updates.updateWindow,
                      isAutoUpdateEnabled,
                    ),
                  ),
                  skipLine
                    ? m("p", { class: "type-helper text-tertiary" }, skipLine)
                    : null,
                ])
              : null,
            // A press the workspace just refused for want of a sign-in already says so in its own words.
            needsSignIn && forMachine(local.updateError, agentId) === null
              ? m(
                  "p",
                  { class: "type-helper text-secondary mb-3" },
                  "No AI account is signed in to this workspace, so its update can't start.",
                )
              : null,
            // Schedule first, as in the modal: the update window is when nobody is in the machine.
            m("div", { class: "flex items-center gap-2 flex-wrap" }, [
              needsSignIn
                ? m(
                    Button,
                    {
                      variant: "primary",
                      id: "ws-update-sign-in-btn",
                      disabled: isBusy,
                      // Entering the workspace leaves this pane, so a refusal is explained in the modal.
                      onclick: () => shell.signInForUpdateOrExplain(agentId),
                    },
                    "Sign in",
                  )
                : null,
              plainScheduleTarget === null
                ? null
                : m(
                    Button,
                    {
                      variant: "primary",
                      id: "ws-update-schedule-btn",
                      disabled: isPlainScheduleBlocked,
                      onclick: () => requestSchedule(plainScheduleTarget),
                    },
                    pendingSchedule?.action === "schedule" &&
                      pendingSchedule.targetRef === plainScheduleTarget
                      ? m(Spinner, { size: "sm" })
                      : "Schedule update",
                  ),
              isSkipOffered
                ? m(
                    Button,
                    {
                      variant: "secondary",
                      id: "ws-update-skip-btn",
                      disabled: isBusy,
                      onclick: () => {
                        local.skipConfirmAgentId = agentId;
                      },
                    },
                    `Skip ${skippedVersionText(update)}`,
                  )
                : isScheduled
                  ? m(
                      Button,
                      {
                        variant: "secondary",
                        id: "ws-update-cancel-schedule-btn",
                        disabled: isBusy,
                        onclick: () => writeSchedule("cancel"),
                      },
                      pendingSchedule?.action === "cancel"
                        ? m(Spinner, { size: "sm" })
                        : "Cancel schedule",
                    )
                  : null,
              m(
                Button,
                {
                  variant: "secondary",
                  id: "ws-update-now-btn",
                  disabled: isDispatchBlocked,
                  onclick: () => requestDispatch(""),
                },
                pending?.targetRef === ""
                  ? m(Spinner, { size: "sm" })
                  : "Update now",
              ),
              isDecisionPending && chatAgentId
                ? m(
                    Button,
                    {
                      variant: "primary",
                      id: "ws-update-open-chat-btn",
                      onclick: () =>
                        shell.openWorkspaceChat(agentId, chatAgentId),
                    },
                    "Open chat",
                  )
                : null,
            ]),
            disabledUpdateReason(update),
            // A named version is applied by the same in-place run, so a machine
            // too old for one is too old for the other.
            isRecreationRequired(update)
              ? null
              : m(
                  Collapsible,
                  {
                    id: "update-override",
                    isOpen: local.isOverrideOpen,
                    onToggle: () =>
                      (local.isOverrideOpen = !local.isOverrideOpen),
                    extra: "mt-4",
                    triggerExtra:
                      "inline-flex items-center gap-1 type-helper text-secondary hover:text-primary",
                    panelExtra: "flex flex-col gap-2 mt-2",
                    summary: m("span", "Update to a specific version"),
                  },
                  [
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
                          local.overrideDraft = {
                            agentId,
                            value: (event.target as HTMLInputElement).value,
                          };
                        },
                      }),
                      m(
                        Button,
                        {
                          variant: "secondary",
                          id: "update-override-btn",
                          disabled:
                            isBusy ||
                            isUpdating ||
                            isDecisionPending ||
                            overrideValue.trim() === "",
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
                          disabled:
                            isBusy ||
                            isUpdating ||
                            isDecisionPending ||
                            overrideValue.trim() === "",
                          onclick: () => requestSchedule(overrideValue.trim()),
                        },
                        pendingSchedule?.action === "schedule" &&
                          pendingSchedule.targetRef !== "" &&
                          pendingSchedule.targetRef === overrideValue.trim()
                          ? m(Spinner, { size: "sm" })
                          : "Schedule",
                      ),
                    ]),
                    prefill && draft === null
                      ? m(
                          "p",
                          { class: "type-helper text-tertiary" },
                          "Prefilled with the template ref this build of Imbue Studio runs from.",
                        )
                      : null,
                    m(
                      "p",
                      { class: "type-helper text-tertiary" },
                      "Works on an up-to-date workspace too. A version newer than this Imbue Studio app, a branch, or a bare " +
                        "ref is allowed and applied without further confirmation: it may not be a tested release, " +
                        "parts of this workspace may stop working until the app catches up, and the update agent " +
                        'offers a rollback afterwards. On a branch, this workspace may afterwards read as "version unknown".',
                    ),
                  ],
                ),
          ],
      errorMessage
        ? m(
            "div",
            { class: "type-helper text-important mt-3", role: "alert" },
            [errorMessage, machineVerdict(shownError.detail)],
          )
        : null,
    ]),
    appUpdateSettingsSection(local),
  ]);
}

/** The app-wide update settings, which need nothing from the machine, so a stopped one shows them too. */
function appUpdateSettingsSection(local: SettingsGroupsLocalState): m.Children {
  return local.appSettings.overview !== null
    ? m("div", { class: "mt-8" }, [
        m(SectionHeader, "Update settings"),
        workspaceUpdateSettings(local.appSettings, "workspace"),
      ])
    : null;
}

function renderGeneralGroup(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
  const data = model.data;
  if (data === null) return null;
  const nameValue = local.nameDraft ?? data.name;
  const shownColor = model.pendingColor ?? data.color;
  const colorValue = local.colorDraft ?? shownColor;
  const normalizedDraft = normalizeWorkspaceColorHex(colorValue);
  const isCustomColor =
    normalizedDraft !== null &&
    !Object.values(data.palette).includes(normalizedDraft);

  return m("div", [
    m(SectionHeader, "Name"),
    m("div", { id: "rename-section", class: "mb-8" }, [
      data.is_stale
        ? m(
            Notice,
            { variant: "warn" },
            "This workspace is currently unreachable, so it can't be renamed " +
              "right now. Try again once the workspace reconnects.",
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
        model.isRenameSaving
          ? m("span", { class: "type-section text-secondary" }, "Saving...")
          : null,
      ]),
      model.renameErrorMessage
        ? m(
            "p",
            {
              id: "rename-error",
              class: "type-body text-important mt-2",
              role: "alert",
            },
            model.renameErrorMessage,
          )
        : null,
    ]),

    // A pick shows here at once, but the titlebar accent follows only once the save lands: its
    // channel round trip (mngr label -> resolver -> publisher -> workspaces message) repaints it.
    m(SectionHeader, "Color"),
    m("div", { id: "color-section", class: "mb-8" }, [
      data.is_stale
        ? m(
            Notice,
            { variant: "warn" },
            "This workspace is currently unreachable, so its color can't be changed right now. " +
              "Try again once the workspace reconnects.",
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
              selected: hexValue === (normalizedDraft ?? shownColor),
              disabled: data.is_stale,
              onclick: () => {
                local.colorDraft = null;
                void model.pickColor(hexValue);
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
            disabled: data.is_stale,
            oninput: (event: InputEvent) => {
              local.colorDraft = (event.target as HTMLInputElement).value;
            },
            onblur: () => commitHexDraft(model, local),
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
        ? m(
            "p",
            {
              id: "color-error",
              class: "type-body text-important mt-2",
              role: "alert",
            },
            model.colorErrorMessage,
          )
        : null,
    ]),

    renderMachineSizeSection(model, local),

    m(SectionHeader, "ID"),
    m(
      "p",
      { class: "type-body font-mono text-secondary mb-8 select-all break-all" },
      data.agent_id,
    ),

    data.ssh_command
      ? [
          m(SectionHeader, "Connect over SSH"),
          m("div", { id: "ssh-section", class: "mb-8" }, [
            m(
              "p",
              { class: "type-helper text-tertiary mb-2" },
              "For direct debugging, connect to the machine's host from a terminal:",
            ),
            m(CopyField, { value: data.ssh_command }),
          ]),
        ]
      : null,

    m(SectionHeader, "Danger zone"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Permanently destroy this workspace and release any associated resources.",
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
      "Remove workspace",
    ),
    model.destroyErrorMessage
      ? m(
          "p",
          { id: "destroy-error", class: "type-body text-important mt-2" },
          model.destroyErrorMessage,
        )
      : null,
  ]);
}

const SIZE_NOT_REPORTED = "This machine does not report its size.";
const SIZE_NOT_READ = "The machine's size could not be read.";

/** The machine's size: what its provider recorded, plus, for a resizable
 * machine, the fields and the action that change it (a local docker or lima
 * machine is restarted to apply the change; an Imbue Cloud machine's size is
 * saved for its next restart, which the status notice offers), or a passive
 * restart-to-apply note when a size is pending on a machine the page cannot
 * resize. Always drawn, so the page says where the size stands (being read,
 * not reported, or known) rather than leaving a gap. */
function renderMachineSizeSection(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
  return [
    m(SectionHeader, "Machine size"),
    m(
      "div",
      { id: "machine-size-section", class: "mb-8" },
      renderMachineSizeBody(model, local),
    ),
  ];
}

function renderMachineSizeBody(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
  const size = model.machineSize;
  if (size === null) {
    switch (model.machineSizeState) {
      case "loading":
        return m(
          "p",
          {
            id: "machine-size-loading",
            class: "type-body text-secondary flex items-center gap-2",
          },
          [m(Spinner, { size: "sm" }), "Reading the machine's size…"],
        );
      case "load_failed":
        return m(
          "p",
          { id: "machine-size-load-failed", class: "type-body text-secondary" },
          SIZE_NOT_READ,
        );
      default:
        return m(
          "p",
          { id: "machine-size-unavailable", class: "type-body text-secondary" },
          SIZE_NOT_REPORTED,
        );
    }
  }
  return [
    m(
      "p",
      { class: "type-body text-secondary" },
      formatMachineSizeNumbers(size) || SIZE_NOT_REPORTED,
    ),
    renderMachineSizeStatus(model, size),
    renderMachineSizeControls(model, local, size),
  ];
}

/** The size controls, a loading state while whether they are offered is
 * still being read, or nothing for a machine this page cannot resize. */
function renderMachineSizeControls(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  size: WorkspaceMachineSizeData,
): m.Children {
  if (size.is_resize_offer_pending === true || model.isResizeOfferLoading) {
    return m(
      "p",
      {
        id: "machine-size-offer-loading",
        class: "type-helper text-tertiary mt-2 flex items-center gap-2",
      },
      [
        m(Spinner, { size: "sm" }),
        "Checking whether this machine's size can be changed…",
      ],
    );
  }
  if (!size.is_resizable) return null;
  return isMachineSizePickedFromChoices(size)
    ? renderMachineResizeFromChoices(model, local, size)
    : renderMachineResize(model, local);
}

/** The one notice about a size in flux, in order of what matters most: the
 * restart under way (and so not to be asked for again), the size waiting for a
 * restart (with the restart itself when this page can ask for it: a running
 * Imbue Cloud machine whose size the page changes), or what the last change
 * did when nothing else on the page says it. */
function renderMachineSizeStatus(
  model: WorkspaceOptionsModel,
  size: WorkspaceMachineSizeData,
): m.Children {
  const pendingLabel = formatPendingMachineSize(size);
  if (model.isRestartInProgress) {
    return m(
      Notice,
      { variant: "info", id: "machine-size-restarting" },
      m("div", { class: "flex items-center gap-3" }, [
        m(Spinner, { size: "sm" }),
        m("span", machineRestartingText(model, size, pendingLabel)),
      ]),
    );
  }
  if (!pendingLabel) {
    return model.resizeNotice
      ? m(
          Notice,
          { variant: "info", id: "machine-resize-notice" },
          model.resizeNotice,
        )
      : null;
  }
  const isRestartOffered =
    size.is_resizable &&
    isMachineSizePickedFromChoices(size) &&
    size.is_running === true;
  if (!isRestartOffered) {
    return m(
      Notice,
      { variant: "info" },
      `A new size is pending (${pendingLabel}). Restart this workspace's machine to apply it.`,
    );
  }
  return m(Notice, { variant: "info", id: "machine-size-pending" }, [
    m("div", { class: "flex items-center justify-between gap-3 flex-wrap" }, [
      m(
        "span",
        `A new size is pending (${pendingLabel}). Restarting the machine applies it: the restart backs up ` +
          "the machine's disks first, so it can take a while.",
      ),
      m(
        Button,
        {
          variant: "secondary",
          id: "machine-size-restart-btn",
          disabled: model.isRestartBusy || model.isResizeBusy,
          onclick: () => {
            void model.restartMachineToApplySize();
          },
        },
        model.isRestartBusy ? m(Spinner, { size: "sm" }) : "Restart now",
      ),
    ]),
  ]);
}

/** What the running restart is doing: applying a new size when this page asked
 * for the restart or a size is pending, else the plain start or restart the
 * app reports (a stopped machine's start, the recovery card's restart), which
 * the section only waits out. */
function machineRestartingText(
  model: WorkspaceOptionsModel,
  size: WorkspaceMachineSizeData,
  pendingLabel: string,
): string {
  if (!model.isRestartDispatched && !pendingLabel) {
    return size.is_resizable
      ? "The machine is starting or restarting; its size can be changed once that finishes."
      : "The machine is starting or restarting.";
  }
  const applying = pendingLabel ? ` (${pendingLabel})` : "";
  return isMachineSizePickedFromChoices(size)
    ? `The machine is restarting to apply the new size${applying}. The restart backs up ` +
        "the machine's disks first, so it can take a few minutes."
    : `The machine is restarting to apply the new size${applying}.`;
}

/** The resize fields as typed for this machine, or its current size until they are edited. */
function currentSizeDraft(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  size: WorkspaceMachineSizeData,
): MachineSizeDraft {
  return (
    forMachine(local.sizeDraft, model.agentId)?.draft ??
    machineSizeDraftFor(size)
  );
}

/** One resize field, keyed to the machine it is typed for. */
function machineResizeField(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  size: WorkspaceMachineSizeData,
  dimension: MachineSizeDimension,
  label: string,
  bounds: { min?: string; max?: string; disabled?: boolean } = {},
): m.Children {
  const draft = currentSizeDraft(model, local, size);
  return m(MachineSizeInput, {
    dimension,
    label,
    value: draft[dimension],
    disabled:
      model.isResizeBusy ||
      model.isRestartInProgress ||
      bounds.disabled === true,
    min: bounds.min,
    max: bounds.max,
    oninput: (text: string) => {
      local.sizeDraft = {
        agentId: model.agentId,
        draft: { ...draft, [dimension]: text },
      };
    },
  });
}

/** The resize fields and the action that records the size and restarts the machine. */
function renderMachineResize(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
  const size = model.machineSize;
  if (size === null) return null;
  return m("div", { id: "machine-resize", class: "mt-3" }, [
    m(
      "p",
      { class: "type-helper text-tertiary mb-2" },
      size.is_disk_resizable
        ? "Change the CPUs, memory, or disk (the disk only grows), then apply. The machine restarts to pick the new size up."
        : "Change the CPUs or memory, then apply. The machine restarts to pick the new size up.",
    ),
    m("div", { class: "flex flex-wrap items-center gap-3" }, [
      machineResizeField(model, local, size, "cpu_count", "CPUs"),
      machineResizeField(model, local, size, "memory_gb", "GB RAM"),
      size.is_disk_resizable
        ? machineResizeField(model, local, size, "disk_gb", "GB disk")
        : null,
      m(
        Button,
        {
          variant: "secondary",
          id: "machine-resize-btn",
          disabled: model.isResizeBusy || model.isRestartInProgress,
          // The draft is kept on success: it is the size that was recorded,
          // which discovery reports back only on its next pass.
          onclick: () => {
            void model.resizeMachine(currentSizeDraft(model, local, size));
          },
        },
        model.isResizeBusy ? m(Spinner, { size: "sm" }) : "Apply and restart",
      ),
    ]),
    renderMachineResizeError(model),
  ]);
}

/** What the plan leaves for this machine: the RAM its other running machines
 * use against the plan's quota, and the same for disk. */
function machineSizePlanLine(size: WorkspaceMachineSizeData): string {
  const parts: string[] = [];
  if (
    size.plan_memory_limit_gb !== null &&
    size.plan_memory_limit_gb !== undefined
  ) {
    const others = size.other_machines_memory_gb ?? 0;
    parts.push(
      `Your plan allows ${size.plan_memory_limit_gb} GB of RAM across running machines` +
        (others > 0
          ? `, of which other running machines use ${others} GB.`
          : "."),
    );
  }
  if (size.max_disk_gb !== null && size.max_disk_gb !== undefined) {
    parts.push(
      size.max_disk_gb > minimumMachineDiskGb(size)
        ? `The disk can grow to ${size.max_disk_gb} GB.`
        : "The plan leaves no room to grow the disk.",
    );
  }
  return parts.join(" ");
}

/** The RAM field of a machine sized from choices: a select over the sizes
 * the plan allows, the largest marked when the plan (not the fleet) caps it,
 * keyed to the machine it is picked for. */
function machineMemoryChoiceField(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  size: WorkspaceMachineSizeData,
  isDisabled: boolean,
): m.Children {
  const draft = currentSizeDraft(model, local, size);
  const choices = size.memory_choices_gb ?? [];
  const largestChoice = choices[choices.length - 1];
  const memoryId = "machine-size-memory_gb";
  return m("div", { class: "flex items-center gap-2" }, [
    m(
      FormLabel,
      {
        target: memoryId,
        inline: true,
        extra: "type-helper text-secondary",
      },
      "GB RAM",
    ),
    m(
      Select,
      {
        id: memoryId,
        name: "memory_gb",
        width: "w-40",
        value: draft.memory_gb,
        disabled: isDisabled,
        onchange: (event: Event) => {
          local.sizeDraft = {
            agentId: model.agentId,
            draft: {
              ...draft,
              memory_gb: (event.target as HTMLSelectElement).value,
            },
          };
        },
      },
      choices.map((gb) =>
        m(
          "option",
          { value: String(gb), selected: String(gb) === draft.memory_gb },
          gb === largestChoice && size.is_memory_capped_by_plan
            ? `${gb} GB (plan limit)`
            : `${gb} GB`,
        ),
      ),
    ),
  ]);
}

/** The Imbue Cloud resize: RAM picked from the sizes the plan allows, a
 * grow-only disk, and the save that records the size for the next restart. */
function renderMachineResizeFromChoices(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
  size: WorkspaceMachineSizeData,
): m.Children {
  const isTransitioning = size.is_transitioning === true;
  const isDisabled =
    model.isResizeBusy ||
    model.isRestartBusy ||
    model.isRestartInProgress ||
    isTransitioning;
  return m("div", { id: "machine-resize", class: "mt-3" }, [
    m(
      "p",
      { class: "type-helper text-tertiary mb-2" },
      "Pick the RAM, and optionally grow the disk (it never shrinks), then save. CPUs scale with the RAM. " +
        "The new size takes effect the next time the machine restarts.",
    ),
    isTransitioning
      ? m(
          "p",
          {
            id: "machine-resize-wait",
            class: "type-helper text-secondary mb-2",
          },
          "The machine is starting or stopping; its size can be changed once that finishes.",
        )
      : null,
    m("div", { class: "flex flex-wrap items-center gap-3" }, [
      machineMemoryChoiceField(model, local, size, isDisabled),
      size.is_disk_resizable
        ? machineResizeField(model, local, size, "disk_gb", "GB disk", {
            min: String(minimumMachineDiskGb(size)),
            max:
              size.max_disk_gb === null || size.max_disk_gb === undefined
                ? undefined
                : String(size.max_disk_gb),
            disabled: isDisabled,
          })
        : null,
      m(
        Button,
        {
          variant: "secondary",
          id: "machine-resize-btn",
          disabled: isDisabled,
          onclick: () => {
            void model.resizeMachine(currentSizeDraft(model, local, size));
          },
        },
        model.isResizeBusy ? m(Spinner, { size: "sm" }) : "Save size",
      ),
    ]),
    m(
      "p",
      { id: "machine-size-plan-line", class: "type-helper text-tertiary mt-2" },
      machineSizePlanLine(size),
    ),
    renderMachineResizeError(model),
  ]);
}

/** The last resize's or restart's refusal, under either form. */
function renderMachineResizeError(model: WorkspaceOptionsModel): m.Children {
  return model.resizeErrorMessage
    ? m(
        "p",
        {
          id: "machine-resize-error",
          class: "type-helper text-important mt-2",
          role: "alert",
        },
        model.resizeErrorMessage,
      )
    : null;
}

function commitHexDraft(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): void {
  if (local.colorDraft === null) return;
  const normalized = normalizeWorkspaceColorHex(local.colorDraft);
  local.colorDraft = null;
  if (normalized === null) {
    model.colorErrorMessage =
      "That hex value is not valid. Use #rrggbb or #rgb.";
    m.redraw();
    return;
  }
  void model.pickColor(normalized);
}

function renderAccountGroup(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
  const data = model.data;
  if (data === null) return null;

  return m("div", [
    m(SectionHeader, "Account"),
    m("div", { id: "account-section" }, [
      data.is_account_owned_provider
        ? [
            data.current_account
              ? m("p", { class: "type-body text-primary mb-3" }, [
                  "Linked to ",
                  m("strong", data.current_account.email),
                  ".",
                ])
              : data.leased_owner_email
                ? m("p", { class: "type-body text-primary mb-3" }, [
                    "This workspace is owned by ",
                    m("strong", data.leased_owner_email),
                    ".",
                  ])
                : null,
            m(
              Notice,
              { variant: "info" },
              "This workspace runs on a provider owned by its account (Imbue Cloud, or one of the account's cloud " +
                "connections), so its account link is fixed and cannot be changed.",
            ),
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
                "Unlinking stops all sharing for this workspace and revokes its links. " +
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
            {
              id: "disassociate-error",
              class: "type-body text-important mt-2",
              role: "alert",
            },
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
        "Link this workspace to an account to enable sharing and cloud backups.",
      ),
      m(
        "p",
        { class: "type-body text-secondary" },
        m(
          "a",
          { class: "text-primary underline", ...routeLinkAttrs("/accounts") },
          "Sign in or create an account",
        ),
      ),
    ]);
  }
  return m("div", [
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Link this workspace to one of your accounts:",
    ),
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

function renderDestroyDialog(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
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
      m(
        "h2",
        { class: "type-heading-lg text-primary mb-3" },
        "Remove workspace?",
      ),
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
                if (isDestroyStarted)
                  m.route.set(`/destroying/${model.agentId}`);
              });
            },
          },
          model.isDestroyPending ? "Removing..." : "Remove",
        ),
      ]),
    ],
  );
}

function renderUnlinkDialog(
  model: WorkspaceOptionsModel,
  local: SettingsGroupsLocalState,
): m.Children {
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
      m(
        "h2",
        { class: "type-heading-lg text-primary mb-3" },
        "Unlink this workspace?",
      ),
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
