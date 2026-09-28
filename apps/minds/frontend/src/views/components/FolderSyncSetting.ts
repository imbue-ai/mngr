// The "keep a synchronized copy on the machine" setting of one shared folder,
// drawn the same way wherever a sync can be turned on: the Local files card in
// the Permissions tab, and the file-sharing approval dialog.
//
// A render function rather than a component: it holds no state and needs no
// lifecycle. Whoever renders it says whether the switch is on and hears the
// flips; the card wires those to the model's writes, the dialog to the choice
// it will submit with Approve.

import m from "mithril";
import type { FileSharingAccess, FolderSyncConflict } from "../../generated/ui";
import {
  FOLDER_SYNC_CONFLICTS,
  folderSyncConflictLabel,
} from "../../models/workspacePermissions";

/** The right-aligned control on a shared path's setting row.
 *
 * Carries its own disabled look, which a bare ``<select disabled>`` does not:
 * the element stops responding but goes on looking exactly as it did, so a
 * setting that is waiting on the workspace's machine reads as one you simply
 * failed to click. Same pair the buttons use (``BTN_BASE``), so every control
 * in the pane that is unavailable says so the same way. */
export const SETTING_SELECT_CLASS =
  "type-body text-primary bg-transparent border border-subtle rounded-md px-2 py-1 " +
  "disabled:opacity-40 disabled:cursor-not-allowed";

/** Everything the switch has to say, hung off a short rule under it. The rule
 * is what says "this belongs to the switch"; no indent to keep in step with
 * the switch's own width. */
const SYNC_RAIL_CLASS =
  "mt-2 pl-3.5 border-l-2 border-subtle flex flex-col gap-2";

export const SYNC_SETTING_LABEL = "Keep a synchronized copy on the machine";

/** A label on the left, its control right-aligned on the right. */
export function renderSettingRow(
  label: string,
  control: m.Children,
): m.Children {
  return m("div", { class: "flex items-center justify-between gap-4" }, [
    m("span", { class: "type-body text-primary min-w-0" }, label),
    m("div", { class: "shrink-0" }, control),
  ]);
}

export interface FolderSyncSettingAttrs {
  /** The shared path, which keys the controls' data attributes. */
  path: string;
  /** What agents may do with the path; it decides which way a sync travels
   * and so whether the clash question is asked at all. */
  access: FileSharingAccess;
  isOn: boolean;
  /** A flip of this switch is in flight. */
  isBusy?: boolean;
  /** Something else is in flight, so this control sits out (with a reason
   * on hover). */
  lockedTitle?: string | null;
  /** Why the folder cannot be synced; empty when it can. Greys the switch out
   * and replaces the explanation. */
  unavailableReason: string;
  conflict: FolderSyncConflict;
  onToggle: (enabled: boolean) => void;
  onConflictChange: (conflict: FolderSyncConflict) => void;
  /** What sits beside the switch: the card's status badge. */
  status?: m.Children;
  /** The first line of the rail, when the caller has something to say before
   * the explanation: the dialog says the agent asked for the sync. */
  note?: m.Children;
  /** Why syncing this folder overlaps another workspace's; shown only while
   * the sync is on, since it describes what two running syncs do to each
   * other. */
  overlapWarning?: string;
  /** Whatever the caller wants at the foot of the rail: the card's set-aside
   * copy, or a failed sync's reason and retry. */
  trailing?: m.Children;
}

/** What syncing will actually do, said in terms of the access granted rather
 * than as a second choice that could contradict it. The words the access
 * dropdown uses are the bold ones, so the two read as one sentence. */
function renderDirectionSentence(access: FileSharingAccess): m.Children {
  const clause: m.Children =
    access === "WRITE"
      ? [
          "Since agents on this machine may both ",
          m("strong", { class: "font-semibold" }, "read and write"),
          " the folder, Mind synchronizes changes between your computer and this machine in both directions.",
        ]
      : [
          "Since agents on this machine may only ",
          m("strong", { class: "font-semibold" }, "read"),
          " the folder, Mind synchronizes changes from your computer to this machine in one direction.",
        ];
  return m("p", { class: "type-helper text-secondary m-0" }, clause);
}

/** The clash rule, asked exactly when the access makes it a real question:
 * only a two-way sync can have a clash, and only read-and-write access makes
 * a sync two-way. */
function renderConflictChoice(attrs: FolderSyncSettingAttrs): m.Children {
  if (attrs.access !== "WRITE") return null;
  return renderSettingRow(
    "On a clash, keep",
    m(
      "select",
      {
        class: SETTING_SELECT_CLASS,
        "data-conflict-path": attrs.path,
        value: attrs.conflict,
        onchange: (event: Event) =>
          attrs.onConflictChange(
            (event.target as HTMLSelectElement).value as FolderSyncConflict,
          ),
      },
      FOLDER_SYNC_CONFLICTS.map((option) =>
        m(
          "option",
          { value: option, selected: option === attrs.conflict },
          folderSyncConflictLabel(option),
        ),
      ),
    ),
  );
}

/** Keeping a copy on the machine: an extra thing a shared folder can have,
 * not an alternative to sharing it.
 *
 * A switch rather than the other arm of a radio, because it is additive: the
 * on-demand grant stays exactly as it was, and the copy is one more way the
 * same agents reach the same folder. Which way changes travel is not asked:
 * it says the same thing as the access, so it is stated instead.
 *
 * A folder that cannot be synced keeps the switch, greyed, with the reason
 * where the explanation would be. Hiding the option would leave the user
 * wondering whether this row is different or they misremembered; letting them
 * flip it and be refused says the same thing, later and in a banner. */
export function renderFolderSyncSetting(
  attrs: FolderSyncSettingAttrs,
): m.Children {
  const isBusy = attrs.isBusy === true;
  const isUnavailable = attrs.unavailableReason !== "";
  const lockedTitle = attrs.lockedTitle ?? null;
  return [
    m("div", { class: "flex items-center justify-between gap-4" }, [
      m(
        "span",
        {
          class:
            "type-body text-primary min-w-0 truncate " +
            (isUnavailable ? "opacity-60" : ""),
        },
        SYNC_SETTING_LABEL,
      ),
      m("span", { class: "flex shrink-0 items-center gap-2" }, [
        attrs.status ?? null,
        m("input", {
          type: "checkbox",
          role: "switch",
          class: isBusy
            ? "perm-switch-input shrink-0 is-busy"
            : "perm-switch-input shrink-0",
          checked: attrs.isOn,
          disabled: isBusy || isUnavailable || lockedTitle !== null,
          ...(lockedTitle === null ? {} : { title: lockedTitle }),
          "data-sync-path": attrs.path,
          "aria-label": `Keep a synchronized copy of ${attrs.path} on the machine`,
          onchange: (event: Event) =>
            attrs.onToggle((event.target as HTMLInputElement).checked),
        }),
      ]),
    ]),
    m("div", { class: SYNC_RAIL_CLASS, "data-sync-detail": attrs.path }, [
      attrs.note ?? null,
      isUnavailable
        ? m(
            "p",
            {
              class: "type-helper text-secondary m-0",
              "data-sync-unavailable": attrs.path,
            },
            attrs.unavailableReason,
          )
        : m(
            "p",
            { class: "type-helper text-secondary m-0" },
            "Mind will synchronize the folder when it's running, and agents can continue to access the " +
              "synchronized folder when Mind is not running or your computer is offline.",
          ),
      attrs.isOn ? renderDirectionSentence(attrs.access) : null,
      !attrs.isOn ||
      attrs.overlapWarning === undefined ||
      attrs.overlapWarning === ""
        ? null
        : m(
            "p",
            {
              class: "type-helper text-warning m-0",
              "data-sync-overlap": attrs.path,
            },
            attrs.overlapWarning,
          ),
      attrs.isOn ? renderConflictChoice(attrs) : null,
      attrs.trailing ?? null,
    ]),
  ];
}
