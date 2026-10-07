// Where one service's requests from a workspace leave from, drawn the same way
// wherever it can be set.
//
// A render function rather than a component: the route, and whether the editor
// is open, belong to whoever renders it, so there is nothing for an instance
// to hold.

import m from "mithril";
import type { DesktopEgressMode, UiWorkspaceDesktop } from "../../generated/ui";
import {
  HOP_PHRASE_SEPARATOR,
  addDesktopEgressHop,
  addableDesktopEgressHops,
  canMoveDesktopEgressHopDown,
  canMoveDesktopEgressHopUp,
  canRemoveDesktopEgressHop,
  desktopEgressHopLabel,
  desktopEgressHopPhrases,
  desktopEgressMode,
  desktopEgressModeSentence,
  isDesktopEgressHopShownById,
  moveDesktopEgressHopDown,
  moveDesktopEgressHopUp,
  removeDesktopEgressHop,
  switchedDesktopEgressRoute,
  thisComputerDeviceId,
} from "../../models/desktopEgressRoute";
import { Button } from "./Button";
import { SETTING_RAIL_CLASS, SETTING_SELECT_CLASS } from "./FolderSyncSetting";
import { Icon16 } from "./Icon";
import { renderPermissionSwitch } from "./PermissionSwitch";
import { StatusBadge } from "./StatusBadge";

export const DESKTOP_EGRESS_LABEL = "Proxy through my desktop";

const TEXT_LINK_CLASS =
  "type-helper text-accent hover:underline cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed";

const HOP_MOVE_CLASS =
  "p-1 rounded-md text-secondary cursor-pointer hover:bg-fill-hover " +
  "disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent";

const DEVICE_ID_CLASS = "font-mono";

const THIS_COMPUTER_UNKNOWN_TITLE =
  "This computer's id is not known, so requests cannot be sent through it.";

type HopMoveDirection = "up" | "down";

const HOP_MOVE_ATTR: Record<HopMoveDirection, string> = {
  up: "data-desktop-egress-move-up",
  down: "data-desktop-egress-move-down",
};

// A move reorders the hop's row in the DOM, which drops focus in a browser,
// and the button that made it can be disabled at the new position. The next
// draw hands focus back to the moved hop so the keyboard can keep moving it.
let hopMoveAwaitingFocus: { hop: string; direction: HopMoveDirection } | null =
  null;

function restoreHopMoveFocus(row: Element, hop: string): void {
  if (hopMoveAwaitingFocus?.hop !== hop) return;
  const { direction } = hopMoveAwaitingFocus;
  hopMoveAwaitingFocus = null;
  const buttonFor = (wanted: HopMoveDirection): HTMLButtonElement | null =>
    row.querySelector<HTMLButtonElement>(`[${HOP_MOVE_ATTR[wanted]}]`);
  const same = buttonFor(direction);
  const target =
    same !== null && !same.disabled
      ? same
      : buttonFor(direction === "up" ? "down" : "up");
  target?.focus();
}

export interface DesktopEgressRouteEditorAttrs {
  /** The route the editor shows and changes: a draft apart from the route the
   * switch shows, until it is saved. */
  route: readonly string[];
  onChange: (route: string[]) => void;
  /** Close the editor without saving. */
  onClose: () => void;
  onSave: () => void;
  isSaveDisabled?: boolean;
}

export interface DesktopEgressRouteSettingAttrs {
  /** The service, which keys the controls' data attributes. */
  serviceName: string;
  /** The route the switch and the summary show. */
  route: readonly string[];
  desktops: readonly UiWorkspaceDesktop[];
  /** A write of this route is in flight. */
  isBusy?: boolean;
  /** Something else is in flight, so the controls sit out (with a reason on
   * hover). */
  lockedTitle?: string | null;
  /** The switch was flipped; carries the route that flip stands for. */
  onSwitch: (route: string[]) => void;
  onAdjust: () => void;
  /** The open editor, or null while only the summary shows. */
  editor: DesktopEgressRouteEditorAttrs | null;
}

function renderHop(
  attrs: DesktopEgressRouteSettingAttrs,
  editor: DesktopEgressRouteEditorAttrs,
  index: number,
  isDisabled: boolean,
): m.Children {
  const { route } = editor;
  const hop = route[index];
  const label = desktopEgressHopLabel(hop, attrs.desktops);
  return m(
    "li",
    {
      key: hop,
      class: "flex items-center justify-between gap-3",
      "data-desktop-egress-hop": hop,
      onupdate: (vnode: m.VnodeDOM) => restoreHopMoveFocus(vnode.dom, hop),
    },
    [
      m(
        "span",
        {
          class: "type-body text-primary min-w-0 truncate",
          "data-desktop-egress-hop-label": hop,
        },
        [
          `${index + 1}. `,
          isDesktopEgressHopShownById(hop, attrs.desktops)
            ? m("span", { class: DEVICE_ID_CLASS }, label)
            : label,
        ],
      ),
      m("span", { class: "flex shrink-0 items-center gap-1" }, [
        m(
          "button",
          {
            type: "button",
            class: HOP_MOVE_CLASS,
            [HOP_MOVE_ATTR.up]: hop,
            "aria-label": `Try ${label} earlier`,
            disabled: isDisabled || !canMoveDesktopEgressHopUp(route, index),
            onclick: () => {
              hopMoveAwaitingFocus = { hop, direction: "up" };
              editor.onChange(moveDesktopEgressHopUp(route, index));
            },
          },
          m(Icon16, { name: "chevron-up", size: "sm" }),
        ),
        m(
          "button",
          {
            type: "button",
            class: HOP_MOVE_CLASS,
            [HOP_MOVE_ATTR.down]: hop,
            "aria-label": `Try ${label} later`,
            disabled: isDisabled || !canMoveDesktopEgressHopDown(route, index),
            onclick: () => {
              hopMoveAwaitingFocus = { hop, direction: "down" };
              editor.onChange(moveDesktopEgressHopDown(route, index));
            },
          },
          m(Icon16, { name: "chevron-down", size: "sm" }),
        ),
        m(
          "button",
          {
            type: "button",
            class: "ml-1 " + TEXT_LINK_CLASS,
            "data-desktop-egress-remove": hop,
            "aria-label": `Remove ${label}`,
            disabled: isDisabled || !canRemoveDesktopEgressHop(route),
            onclick: () =>
              editor.onChange(removeDesktopEgressHop(route, index)),
          },
          "Remove",
        ),
      ]),
    ],
  );
}

function renderAddHop(
  attrs: DesktopEgressRouteSettingAttrs,
  editor: DesktopEgressRouteEditorAttrs,
  isDisabled: boolean,
): m.Children {
  const addable = addableDesktopEgressHops(editor.route, attrs.desktops);
  if (addable.length === 0) return null;
  return m(
    "select",
    {
      class: "self-start " + SETTING_SELECT_CLASS,
      "data-desktop-egress-add": attrs.serviceName,
      "aria-label": "Add a place to try",
      // Always back on the prompt: the select is a menu of things to add, not
      // a value the route holds.
      value: "",
      disabled: isDisabled,
      onchange: (event: Event) => {
        const hop = (event.target as HTMLSelectElement).value;
        if (hop !== "") editor.onChange(addDesktopEgressHop(editor.route, hop));
      },
    },
    [
      m("option", { value: "", selected: true }, "Add…"),
      ...addable.map((hop) =>
        m("option", { value: hop }, desktopEgressHopLabel(hop, attrs.desktops)),
      ),
    ],
  );
}

function renderEditor(
  attrs: DesktopEgressRouteSettingAttrs,
  editor: DesktopEgressRouteEditorAttrs,
  isDisabled: boolean,
): m.Children {
  return m(
    "div",
    {
      class: SETTING_RAIL_CLASS,
      "data-desktop-egress-editor": attrs.serviceName,
    },
    [
      m(
        "p",
        { class: "type-helper text-secondary m-0" },
        "The workspace's machine tries these in order, from the top.",
      ),
      m(
        "ol",
        { class: "flex flex-col gap-1" },
        editor.route.map((_hop, index) =>
          renderHop(attrs, editor, index, isDisabled),
        ),
      ),
      renderAddHop(attrs, editor, isDisabled),
      m(
        "p",
        {
          class: "type-helper text-tertiary m-0",
          "data-desktop-egress-add-hint": attrs.serviceName,
        },
        "Another computer is added from Imbue Studio on that computer.",
      ),
      m("div", { class: "flex items-center gap-2" }, [
        m(
          Button,
          {
            variant: "primary",
            "data-desktop-egress-save": attrs.serviceName,
            disabled: isDisabled || editor.isSaveDisabled === true,
            onclick: editor.onSave,
          },
          "Save",
        ),
        m(
          Button,
          {
            variant: "secondary",
            "data-desktop-egress-cancel": attrs.serviceName,
            onclick: editor.onClose,
          },
          "Cancel",
        ),
      ]),
    ],
  );
}

/** The hops in the order they are tried, with each device id drawn as an
 * identifier. `isMidSentence` says the hops continue a sentence rather than
 * open the line. */
export function renderDesktopEgressHops(
  route: readonly string[],
  desktops: readonly UiWorkspaceDesktop[],
  isMidSentence: boolean,
): m.Children {
  return desktopEgressHopPhrases(route, desktops, isMidSentence).map(
    (phrase, index) => [
      index === 0 ? null : HOP_PHRASE_SEPARATOR,
      phrase.isDeviceId
        ? m("span", { class: DEVICE_ID_CLASS }, phrase.text)
        : phrase.text,
    ],
  );
}

/** One line saying where the route sends requests. */
function renderSummary(
  attrs: DesktopEgressRouteSettingAttrs,
  mode: DesktopEgressMode,
): m.Children {
  return (
    desktopEgressModeSentence(mode) ??
    renderDesktopEgressHops(attrs.route, attrs.desktops, false)
  );
}

/** The switch, what the route it stands for does, and the way into the editor.
 *
 * The switch only knows two routes -- the workspace's own machine, and this
 * computer alone -- so a route it did not set still reads as on, since
 * requests do go somewhere other than the plain default, with a "Custom"
 * badge saying the switch alone does not describe it. Flipping it replaces
 * whatever the route was, which is why the summary spells a custom route out
 * before the user does. */
export function renderDesktopEgressRouteSetting(
  attrs: DesktopEgressRouteSettingAttrs,
): m.Children {
  const thisDeviceId = thisComputerDeviceId(attrs.desktops);
  const mode = desktopEgressMode(attrs.route, thisDeviceId);
  const isBusy = attrs.isBusy === true;
  const lockedTitle = attrs.lockedTitle ?? null;
  const isDisabled = isBusy || lockedTitle !== null;
  // Turning the switch on names this computer. Turning it off does not, so a
  // route that is already on can still be switched off.
  const cannotTurnOn = mode === "OFF" && thisDeviceId === null;
  // One IDREF: aria-describedby splits its value on whitespace.
  const summaryId = `desktop-egress-summary-${attrs.serviceName.replace(/[^\w-]/g, "-")}`;
  return m(
    "div",
    {
      class: "flex flex-col",
      "data-desktop-egress": attrs.serviceName,
      "data-desktop-egress-mode": mode,
    },
    [
      m("div", { class: "flex items-center justify-between gap-4" }, [
        m("div", { class: "min-w-0" }, [
          m(
            "p",
            { class: "type-body text-primary truncate" },
            DESKTOP_EGRESS_LABEL,
          ),
          m(
            "p",
            {
              id: summaryId,
              class: "type-helper text-tertiary mt-0.5",
              "data-desktop-egress-summary": attrs.serviceName,
            },
            renderSummary(attrs, mode),
          ),
        ]),
        renderPermissionSwitch({
          isOn: mode !== "OFF",
          isBusy,
          isDisabled: isDisabled || cannotTurnOn,
          title:
            lockedTitle ?? (cannotTurnOn ? THIS_COMPUTER_UNKNOWN_TITLE : null),
          // A custom route is also "on"; the name says the switch alone does
          // not describe it, and the summary spells it out.
          label:
            mode === "CUSTOM"
              ? `${DESKTOP_EGRESS_LABEL} (custom route)`
              : DESKTOP_EGRESS_LABEL,
          describedBy: summaryId,
          permission: "desktop-egress",
          badge:
            mode === "CUSTOM"
              ? m(
                  StatusBadge,
                  {
                    variant: "info",
                    size: "xs",
                    "data-desktop-egress-custom": attrs.serviceName,
                  },
                  "Custom",
                )
              : null,
          onFlip: (isOn) => {
            const switched = switchedDesktopEgressRoute(isOn, thisDeviceId);
            if (switched !== null) attrs.onSwitch(switched);
          },
        }),
      ]),
      attrs.editor === null
        ? m(
            "button",
            {
              type: "button",
              class: "mt-1 self-start " + TEXT_LINK_CLASS,
              "data-desktop-egress-adjust": attrs.serviceName,
              disabled: isDisabled,
              ...(lockedTitle === null ? {} : { title: lockedTitle }),
              onclick: attrs.onAdjust,
            },
            "Adjust",
          )
        : renderEditor(attrs, attrs.editor, isDisabled),
    ],
  );
}
