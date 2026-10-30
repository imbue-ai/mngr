// The Workspace settings pane: General / Account / Backup / Updates groups
// behind a left nav, rendered by the options panel's Settings tab
// (WorkspaceSettingsSections.jinja's successor; the old standalone
// /workspace/<id>/settings page now redirects into that tab).

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { Button } from "../../components/Button";
import { ColorSwatch } from "../../components/ColorSwatch";
import { Icon16 } from "../../components/Icon";
import type { IconName } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { CopyField, SectionHeader } from "../../components/Layout";
import { FormLabel, Select, TextInput } from "../../components/FormControls";
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
import { updateNavDotFor } from "../../../models/updateSurfaces";
import { shownUpdatePill } from "../../shell/workspace-notices";
import { BackupGroup } from "./BackupGroup";
import { UpdatesGroup } from "./UpdatesGroup";
import { Spinner } from "../../components/Spinner";
import { navEntryClass, splitPane } from "../../components/SplitPane";

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
  /** The resize fields as typed, and the machine they were typed for; null until edited. */
  sizeDraft: { agentId: string; draft: MachineSizeDraft } | null;
}

/** The piece of held state only when it belongs to the machine being drawn:
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
    sizeDraft: null,
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
                [
                  m(Icon16, { name: group.icon, extra: "shrink-0" }),
                  m("span", { class: "truncate" }, group.label),
                  group.id === "updates" ? updatesNavDot(data.agent_id) : null,
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
              ? m(UpdatesGroup, { agentId: data.agent_id })
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

/** The Updates entry's dot, in the titlebar pill's colour, while the pill shows. */
function updatesNavDot(agentId: string): m.Children {
  const dot = updateNavDotFor(shownUpdatePill(getAppContext().shell, agentId));
  if (dot === null) return null;
  return m("span", {
    id: "updates-nav-dot",
    class: `ml-auto shrink-0 size-1.5 rounded-full ${dot === "success" ? "bg-success" : "bg-warning"}`,
    "aria-hidden": "true",
  });
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

/**
 * The workspace now belongs to another window's account (every window shows one
 * account), so once an association change lands this window leaves for its
 * own account's hub rather than keep showing a workspace that is no longer its.
 */
function leaveForHubIfMoved(isMoved: boolean): void {
  if (isMoved) getAppContext().shell.routeToHub();
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
          {
            class: "text-primary underline",
            ...routeLinkAttrs(
              m.buildPathname("/accounts", getAppContext().shell.overlayQuery()),
            ),
          },
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
            onclick: () =>
              void model.setAccount(account.user_id).then(leaveForHubIfMoved),
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
              void model.setAccount(null).then((isMoved) => {
                local.isUnlinkDialogOpen = false;
                leaveForHubIfMoved(isMoved);
              });
            },
          },
          model.isAccountBusy ? "Unlinking..." : "Unlink",
        ),
      ]),
    ],
  );
}
