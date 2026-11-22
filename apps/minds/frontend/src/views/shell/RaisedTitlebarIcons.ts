// The bar's flyout buttons -- the account and workspace switchers, the
// notification bell, the bug-report button, and the Settings gear -- and the
// raised copy of them that every flyout draws over the dimmed bar while it is
// open.
//
// Every flyout raises the same strip, so any button is one click from any
// other -- no clicking out first -- and the strip reads as one strip wherever
// you are.
//
// The copies are drawn at the real buttons' own measured window rects (the
// Titlebar hides them by visibility while a flyout is open, which keeps their
// boxes and so their rects true), so the raised strip lands exactly on the
// bar it stands in for. A button with no rect is one the bar is not showing
// -- the workspace button on a hub page other than Home -- and is skipped.

import m from "mithril";
import { Badge } from "../components/Badge";
import { Icon16 } from "../components/Icon";
import type { IconName } from "../components/icons";
import type { ShellState } from "./shell-state";
import { isTitlebarPopupRoutePath } from "./classify";
import type { TitlebarStatus } from "./titlebar-status";
import { titlebarStatusPillClass, titlebarStatusPillContent } from "./titlebar-status";
import { titlebarStatusFor } from "./workspace-notices";
import {
  ACCOUNT_BUTTON_ID,
  TRAIL_BUTTON_INSET,
  WORKSPACE_BUTTON_ID,
  accountButtonContent,
  barStatusWorkspaceAgentId,
  barWorkspaceAnyId,
  trailWorkspaceFor,
  workspaceButtonContent,
} from "./trail";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { warmPermissionsOverview } from "../../models/permissionsPrefetch";

/** Which of the bar's flyouts a surface is. */
export type TitlebarPopupId = "account" | "workspace" | "notifications" | "help" | "settings";

export const SETTINGS_BUTTON_ID = "settings-btn";
export const SHARE_BUTTON_ID = "share-btn";

export interface TitlebarPopupIcon {
  id: TitlebarPopupId;
  /** DOM id of the real titlebar button this copy stands in for. */
  buttonId: string;
  /** The glyph of an icon button; null for a trail button, which draws its own content. */
  icon: IconName | null;
  label: string;
}

/** The five buttons, left to right as the bar shows them. The one place the
 * registration lives: the bar's buttons and the raised strip both resolve
 * through it. */
export const TITLEBAR_POPUP_ICONS: readonly TitlebarPopupIcon[] = [
  { id: "account", buttonId: ACCOUNT_BUTTON_ID, icon: null, label: "Switch account" },
  { id: "workspace", buttonId: WORKSPACE_BUTTON_ID, icon: null, label: "Switch workspace" },
  { id: "notifications", buttonId: "notifications-toggle", icon: "bell", label: "Notifications" },
  { id: "help", buttonId: "help-toggle", icon: "bug", label: "Report a bug" },
  { id: "settings", buttonId: SETTINGS_BUTTON_ID, icon: "settings", label: "Settings" },
];

export interface AnchorRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A titlebar element's window rect by DOM id, or null when none is mounted to
 * hang from (or there is no DOM, as under vitest's node environment). A
 * zero-size rect is an element the titlebar is not showing and reads as absent. */
export function readAnchorRect(elementId: string): AnchorRect | null {
  if (typeof document === "undefined") return null;
  const element = document.getElementById(elementId);
  if (element === null) return null;
  const rect = element.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
}

function isSameRect(a: AnchorRect | null, b: AnchorRect): boolean {
  return (
    a !== null &&
    a.x === b.x &&
    a.y === b.y &&
    a.width === b.width &&
    a.height === b.height
  );
}

export interface TitlebarAnchors {
  /** The rect of one popup icon's real button, or null when it is not shown. */
  rectOf(id: TitlebarPopupId): AnchorRect | null;
  /** The bar's Share button's rect, or null where the bar shows none. */
  shareRect(): AnchorRect | null;
  /** The titlebar status pill's rect, or null while there is no pill. */
  statusPillRect(): AnchorRect | null;
  /** Re-read every rect, redrawing only if one actually moved. */
  remeasure(): void;
}

/** Tracks the window rects of the titlebar's popup icons, re-measured from the
 * host component's create/update (mithril's redraw hooks). Per-instance
 * closure state rather than a module cell: each surface mounts its own, so a
 * remount always starts from a fresh measure of the titlebar as it is now. */
export function titlebarAnchors(): TitlebarAnchors {
  const rects = new Map<string, AnchorRect>();
  let share: AnchorRect | null = null;
  let statusPill: AnchorRect | null = null;

  function measure(): boolean {
    let hasChanged = false;
    for (const entry of TITLEBAR_POPUP_ICONS) {
      const next = readAnchorRect(entry.buttonId);
      const previous = rects.get(entry.buttonId) ?? null;
      if (next === null) {
        // The workspace button leaves the bar on a hub page; its copy leaves with it.
        if (previous !== null) {
          rects.delete(entry.buttonId);
          hasChanged = true;
        }
        continue;
      }
      if (!isSameRect(previous, next)) {
        rects.set(entry.buttonId, next);
        hasChanged = true;
      }
    }
    // Like the pill below, Share comes and goes with what the bar shows.
    const nextShare = readAnchorRect(SHARE_BUTTON_ID);
    if (nextShare === null ? share !== null : !isSameRect(share, nextShare)) {
      share = nextShare;
      hasChanged = true;
    }
    // Unlike the icons, the pill comes and goes with its status, so its absence is a reading too.
    const nextStatusPill = readAnchorRect("titlebar-status-pill");
    if (nextStatusPill === null ? statusPill !== null : !isSameRect(statusPill, nextStatusPill)) {
      statusPill = nextStatusPill;
      hasChanged = true;
    }
    return hasChanged;
  }

  measure();

  return {
    rectOf(id) {
      const entry = TITLEBAR_POPUP_ICONS.find((icon) => icon.id === id);
      if (entry === undefined) return null;
      return rects.get(entry.buttonId) ?? null;
    },
    shareRect: () => share,
    statusPillRect: () => statusPill,
    remeasure() {
      if (measure()) m.redraw();
    },
  };
}

export interface RaisedTitlebarIconsAttrs {
  shell: ShellState;
  anchors: TitlebarAnchors;
  /** The button whose flyout is up: filled with the card's own surface and
   * square-bottomed, so it reads as joined to the flyout below it. */
  selected: TitlebarPopupId;
  /** Put the open flyout away -- the selected button's own click, like the
   * bar button it stands in for. */
  onDismiss: () => void;
  /** Go to another button's flyout, leaving this one. */
  onSelect: (id: TitlebarPopupId) => void;
}

const RAISED_BASE =
  "pointer-events-auto fixed inline-flex items-center justify-center rounded-md " +
  "cursor-pointer focus-visible:outline-2 focus-visible:outline-accent ";
const RAISED_SELECTED = "bg-surface-primary rounded-b-none text-primary";
const RAISED_UNSELECTED = "titlebar-surface hover:bg-fill-hover active:bg-fill-active hover:text-primary";

/** The raised copy of the bar's flyout buttons, drawn over the dimmed real
 * ones at their measured rects, with live copies of Share and the status pill
 * beside them. A plain component so every flyout draws the identical strip
 * from the identical registry.
 *
 * Emit it AFTER the flyout it belongs to: the flyout's `shadow-overlay` halo
 * would otherwise bleed a few px onto the bottom of these opaque buttons and
 * read as a slightly darker shade right at the seam. */
export function RaisedTitlebarIcons(): m.Component<RaisedTitlebarIconsAttrs> {
  return {
    view(vnode) {
      const { shell, anchors, selected, onDismiss, onSelect } = vnode.attrs;
      // The same workspace the bar's own dot and pill speak for, so the copies never disagree with them.
      const agentId = barStatusWorkspaceAgentId(shell);
      const unresolvedCount = shell.stores.notifications.unresolvedCount;
      const hasWorkspaceRequestDot = shell.stores.requests.hasPendingForWorkspace(agentId);
      const status = titlebarStatusFor(shell, agentId);
      const statusPillRect = anchors.statusPillRect();
      const shareRect = anchors.shareRect();
      return m(
        "div",
        {
          id: "raised-titlebar-icons",
          role: "tablist",
          "aria-label": "Titlebar panels",
          class: "contents",
        },
        [
          ...TITLEBAR_POPUP_ICONS.map((entry) => {
            const rect = anchors.rectOf(entry.id);
            if (rect === null) return null;
            const isSelected = entry.id === selected;
            const isTrail = entry.icon === null;
            return m(
              "button",
              {
                type: "button",
                role: "tab",
                id: `${entry.buttonId}-raised`,
                "data-titlebar-popup": entry.id,
                "aria-selected": isSelected ? "true" : "false",
                "aria-label": isSelected ? `Close ${entry.label}` : entry.label,
                // No tooltip on an open menu's own button: it would sit over the menu hanging right under it.
                "data-tooltip": isSelected ? (isTrail ? undefined : "Close") : entry.label,
                // The same warm the bar's gear does: a pending request opens Settings on Permissions.
                onpointerenter: () => {
                  if (entry.id === "settings" && agentId !== null && hasWorkspaceRequestDot)
                    warmPermissionsOverview(agentId, defaultFetchJson);
                },
                class:
                  RAISED_BASE +
                  (isTrail ? `px-1.5 py-1 ${TRAIL_BUTTON_INSET} ` : "p-1.5 ") +
                  (isSelected
                    ? RAISED_SELECTED
                    : RAISED_UNSELECTED + (isTrail ? " text-primary" : " text-secondary")),
                style: `left: ${rect.x}px; top: ${rect.y}px; width: ${rect.width}px; height: ${rect.height}px`,
                // The selected button is the flyout you are on, so it puts the
                // flyout away; the others are different flyouts, so they go
                // there.
                onclick: () => (isSelected ? onDismiss() : onSelect(entry.id)),
              },
              [
                entry.id === "account" ? accountButtonContent(shell, isSelected, null) : null,
                entry.id === "workspace"
                  ? workspaceButtonContent(trailWorkspaceFor(shell, barWorkspaceAnyId(shell)), {
                      isOpen: isSelected,
                      // The selected copy is drawn on the card's surface, not the bar's colour.
                      isOnBar: !isSelected,
                      nameId: null,
                    })
                  : null,
                entry.icon !== null ? m(Icon16, { name: entry.icon }) : null,
                entry.id === "notifications" && unresolvedCount > 0
                  ? m(
                      "span",
                      { class: "pointer-events-none absolute -top-1 -right-1 flex" },
                      m(Badge, { id: "notifications-badge-raised", count: unresolvedCount }),
                    )
                  : null,
                // The same waiting-on-you dot the bar's own gear carries, at the same corner.
                entry.id === "settings" && hasWorkspaceRequestDot
                  ? m(
                      "span",
                      { class: "pointer-events-none absolute top-0.5 right-0.5 flex" },
                      m(Badge, { id: "permissions-badge-raised" }),
                    )
                  : null,
              ],
            );
          }),
          shareRect !== null ? raisedShareButton(shell, shareRect) : null,
          status !== null && statusPillRect !== null ? raisedStatusPill(status, statusPillRect) : null,
        ],
      );
    },
  };
}

/** What the bar's Share shows, for the real button and its raised copy alike. */
export function shareButtonContent(): m.Children {
  return [m(Icon16, { name: "share" }), m("span", { class: "type-label" }, "Share")];
}

/** The classes the bar's Share and its raised copy share: a ghost in the bar's own text colour. */
export const SHARE_BUTTON_CLASS =
  "inline-flex items-center justify-center gap-1.5 h-7 px-2.5 rounded-md cursor-pointer text-primary " +
  "hover:bg-fill-hover active:bg-fill-active focus-visible:outline-2 focus-visible:outline-accent";

/** Share's raised copy, at the real button's rect: live, like the buttons beside it. */
function raisedShareButton(shell: ShellState, rect: AnchorRect): m.Children {
  return m(
    "button",
    {
      type: "button",
      id: `${SHARE_BUTTON_ID}-raised`,
      "aria-label": "Share",
      class: `titlebar-surface pointer-events-auto fixed ${SHARE_BUTTON_CLASS}`,
      style: `left: ${rect.x}px; top: ${rect.y}px; width: ${rect.width}px; height: ${rect.height}px`,
      onclick: () => shell.pressShare(),
    },
    shareButtonContent(),
  );
}

/** The status pill's raised copy, at the real pill's rect. */
function raisedStatusPill(status: TitlebarStatus, rect: AnchorRect): m.Children {
  return m(
    "button",
    {
      id: "titlebar-status-pill-raised",
      "data-status": status.key,
      class: `titlebar-surface pointer-events-auto fixed ${titlebarStatusPillClass(status)}`,
      style: `left: ${rect.x}px; top: ${rect.y}px; height: ${rect.height}px`,
      type: "button",
      "data-tooltip": status.tooltip,
      onclick: () => status.onClick(),
    },
    titlebarStatusPillContent(status),
  );
}

/** Go from whichever flyout is open to the one `id` names, putting the open
 * one away -- the strip's click on a button that is not the one you are on.
 * The one place that transition is written, so every flyout leaves for every
 * other the same way: a switch REPLACES the open flyout's route entry (each
 * opener does its own replacing), so the flyout being left is never one Back
 * away under the new one. */
export function openTitlebarPopup(shell: ShellState, id: TitlebarPopupId): void {
  switch (id) {
    case "notifications":
      shell.switchToNotifications();
      return;
    case "account":
      shell.switchToAccountSwitcher();
      return;
    case "workspace":
      shell.switchToWorkspaceSwitcher();
      return;
    // The feed (local state, not a route) is deliberately NOT closed by the
    // two routed flyouts: the navigation closes it when it lands
    // (handleRouteChanged), and until then the feed keeps the Shell's overlay
    // slot occupied -- so the slot hands its backdrop and strip straight to
    // the arriving flyout instead of rendering empty for the frames in
    // between, which read as a flash.
    case "help":
      shell.openHelp();
      return;
    case "settings":
      shell.openSettingsFromBar();
      return;
  }
}

/** Whether a flyout is up, so the bar hides its own buttons under the strip's copies. */
export function isTitlebarFlyoutOpen(shell: ShellState, routePath: string): boolean {
  return (
    isTitlebarPopupRoutePath(routePath) ||
    shell.isNotificationsOpen ||
    shell.isAccountSwitcherOpen ||
    shell.isWorkspaceSwitcherOpen
  );
}
