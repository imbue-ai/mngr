// The titlebar's five popup icons -- Permissions / Machine settings / Share
// machine, the notification bell, and the bug-report button -- and the raised
// copy of all five that every one of their surfaces draws over the dimmed
// titlebar while it is open.
//
// Every one of those surfaces (the docked options panel, the request popup,
// the bell's feed, Get help) raises the same strip, so any of the five is one
// click from any other -- no clicking out first -- and the strip reads as one
// strip wherever you are.
//
// The copies are drawn at the real buttons' own measured window rects (the
// Titlebar hides them by visibility while a popup is open, which keeps their
// boxes and so their rects true), so the raised strip lands exactly on the
// titlebar it stands in for. An icon with no rect is one the titlebar is not
// showing -- the three machine tabs on a hub page -- and is skipped.

import m from "mithril";
import { Badge } from "../components/Badge";
import { Icon16 } from "../components/Icon";
import type { ShellState } from "./shell-state";
import { isTitlebarPopupRoutePath } from "./classify";
import type { OptionsTab } from "../../models/workspaceOptions";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { warmPermissionsOverview } from "../../models/permissionsPrefetch";

/** Which of the titlebar's popup icons a surface belongs to. The three
 * machine tabs keep their `OptionsTab` names, so the ?tab parse, the
 * titlebar's highlight and this strip all speak one vocabulary. */
export type TitlebarPopupId = OptionsTab | "notifications" | "help";

export interface TitlebarPopupIcon {
  id: TitlebarPopupId;
  /** DOM id of the real titlebar button this copy stands in for. */
  buttonId: string;
  icon: string;
  label: string;
}

/** The five icons, left to right as the titlebar shows them. The one place
 * the registration lives: the titlebar's buttons, the raised strip and the
 * ?tab parse all resolve through it. */
export const TITLEBAR_POPUP_ICONS: readonly TitlebarPopupIcon[] = [
  {
    id: "permissions",
    buttonId: "ws-tab-permissions",
    icon: "key",
    label: "Permissions",
  },
  {
    id: "settings",
    buttonId: "ws-tab-settings",
    icon: "settings",
    label: "Machine settings",
  },
  {
    id: "share",
    buttonId: "ws-tab-share",
    icon: "user-plus",
    label: "Share machine",
  },
  {
    id: "notifications",
    buttonId: "notifications-toggle",
    icon: "bell",
    label: "Notifications",
  },
  { id: "help", buttonId: "help-toggle", icon: "bug", label: "Report a bug" },
];

export interface AnchorRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A titlebar element's window rect by DOM id, or null when none is mounted to
 * hang from (or there is no DOM, as under vitest's node environment). A
 * zero-size rect is an element the titlebar is not showing -- the machine tabs
 * outside a workspace, which sit in a `hidden` crumb -- and reads as absent. */
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
  /** The `#ws-tab-strip` rect, which the docked panels hang their card from. */
  stripRect(): AnchorRect | null;
  /** Re-read every rect, redrawing only if one actually moved. */
  remeasure(): void;
}

/** Tracks the window rects of the titlebar's popup icons, re-measured from the
 * host component's create/update (mithril's redraw hooks). Per-instance
 * closure state rather than a module cell: each surface mounts its own, so a
 * remount always starts from a fresh measure of the titlebar as it is now. */
export function titlebarAnchors(): TitlebarAnchors {
  const rects = new Map<string, AnchorRect>();
  let strip: AnchorRect | null = null;

  function measure(): boolean {
    let hasChanged = false;
    for (const entry of TITLEBAR_POPUP_ICONS) {
      const next = readAnchorRect(entry.buttonId);
      if (next === null) continue;
      if (!isSameRect(rects.get(entry.buttonId) ?? null, next)) {
        rects.set(entry.buttonId, next);
        hasChanged = true;
      }
    }
    const nextStrip = readAnchorRect("ws-tab-strip");
    if (nextStrip !== null && !isSameRect(strip, nextStrip)) {
      strip = nextStrip;
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
    stripRect: () => strip,
    remeasure() {
      if (measure()) m.redraw();
    },
  };
}

export interface RaisedTitlebarIconsAttrs {
  anchors: TitlebarAnchors;
  /** The icon whose surface is up: filled with the card's own surface and
   * square-bottomed, so it reads as joined to the panel below it. */
  selected: TitlebarPopupId;
  /** Put the open surface away -- the selected icon's own click, like the
   * titlebar button it stands in for. */
  onDismiss: () => void;
  /** Go to one of the other four surfaces, leaving this one. */
  onSelect: (id: TitlebarPopupId) => void;
  /** The bell's unresolved count; 0 draws no badge. */
  unresolvedCount: number;
  /** Whether the displayed machine is waiting on the user: the key icon
   * carries the same red dot the titlebar's own key tab does, so the cue
   * survives whichever surface is open over it. */
  hasWorkspaceRequestDot: boolean;
  /** Machine the three tabs belong to, for the Permissions hover warm; null
   * on a hub page, where those tabs are not shown at all. */
  agentId: string | null;
}

/** The raised copy of the titlebar's popup icons, drawn over the dimmed real
 * ones at their measured rects. A plain component so every surface -- the
 * docked options panel, the request popup, and the two anchored popovers --
 * draws the identical strip from the identical registry.
 *
 * Emit it AFTER the panel it belongs to: the panel's `shadow-overlay` halo
 * would otherwise bleed a few px onto the bottom of these opaque buttons and
 * read as a slightly darker shade right at the seam. */
export function RaisedTitlebarIcons(): m.Component<RaisedTitlebarIconsAttrs> {
  return {
    view(vnode) {
      const {
        anchors,
        selected,
        onDismiss,
        onSelect,
        unresolvedCount,
        hasWorkspaceRequestDot,
        agentId,
      } = vnode.attrs;
      return m(
        "div",
        {
          id: "raised-titlebar-icons",
          role: "tablist",
          "aria-label": "Titlebar panels",
          class: "contents",
        },
        TITLEBAR_POPUP_ICONS.map((entry) => {
          const rect = anchors.rectOf(entry.id);
          if (rect === null) return null;
          const isSelected = entry.id === selected;
          return m(
            "button",
            {
              type: "button",
              role: "tab",
              id: `${entry.buttonId}-raised`,
              "data-titlebar-popup": entry.id,
              "aria-selected": isSelected ? "true" : "false",
              "aria-label": isSelected ? `Close ${entry.label}` : entry.label,
              "data-tooltip": isSelected ? "Close" : entry.label,
              // Same warm the titlebar key does: arriving at Permissions from
              // any of the other four reads the same overview, and pointing at
              // the tab is the same head start.
              onpointerenter: () => {
                if (entry.id === "permissions" && agentId !== null)
                  warmPermissionsOverview(agentId, defaultFetchJson);
              },
              class:
                "pointer-events-auto fixed inline-flex items-center justify-center p-1.5 rounded-md " +
                "cursor-pointer focus-visible:outline-2 focus-visible:outline-accent " +
                (isSelected
                  ? "bg-surface-primary rounded-b-none text-primary"
                  : "titlebar-surface text-secondary hover:bg-fill-hover active:bg-fill-active hover:text-primary"),
              style: `left: ${rect.x}px; top: ${rect.y}px; width: ${rect.width}px; height: ${rect.height}px`,
              // The selected icon is the surface you are on, so it puts the
              // surface away; the other four are different surfaces, so they
              // go there.
              onclick: () => (isSelected ? onDismiss() : onSelect(entry.id)),
            },
            [
              m(Icon16, { name: entry.icon }),
              entry.id === "notifications" && unresolvedCount > 0
                ? m(
                    "span",
                    {
                      class:
                        "pointer-events-none absolute -top-1 -right-1 flex",
                    },
                    m(Badge, {
                      id: "notifications-badge-raised",
                      count: unresolvedCount,
                    }),
                  )
                : null,
              // The same waiting-on-you dot the titlebar's own key tab
              // carries, at the same corner (see that tab's positioning
              // comment in Titlebar.ts).
              entry.id === "permissions" && hasWorkspaceRequestDot
                ? m(
                    "span",
                    {
                      class:
                        "pointer-events-none absolute top-0.5 right-0.5 flex",
                    },
                    m(Badge, { id: "permissions-badge-raised" }),
                  )
                : null,
            ],
          );
        }),
      );
    },
  };
}

/** Go from whichever titlebar popup is open to the one `id` names, putting the
 * open one away -- the strip's click on an icon that is not the one you are
 * on. The one place that transition is written, so every surface leaves for
 * every other surface the same way: a switch REPLACES the open popup's route
 * entry (openHelp and switchToNotifications each do their own replacing), so
 * the surface being left is never one Back away under the new one. */
export function openTitlebarPopup(
  shell: ShellState,
  id: TitlebarPopupId,
): void {
  if (id === "notifications") {
    shell.switchToNotifications();
    return;
  }
  // The feed (local state, not a route) is deliberately NOT closed here: the
  // navigation below closes it when it lands (handleRouteChanged), and until
  // then the feed keeps the Shell's one overlay slot occupied -- so the slot
  // hands its backdrop and strip straight to the arriving surface instead of
  // rendering empty for the frames in between, which read as a flash.
  if (id === "help") {
    shell.openHelp();
    return;
  }
  const path = shell.currentRoutePath();
  const rememberedPanelRoute = shell.panelRouteBehindOverlay;
  if (path === "/inbox" && rememberedPanelRoute !== null) {
    // The request popup took the options panel's window over (it hangs from
    // the same key and resized out of the panel's box), so a tab click hands
    // that window back on the asked tab -- with the group and section the
    // other tabs were left on, which a fresh ?tab= open would lose.
    const [panelPath, query = ""] = rememberedPanelRoute.split("?");
    const params = new URLSearchParams(query);
    params.set("tab", id);
    m.route.set(`${panelPath}?${params.toString()}`, undefined, {
      replace: true,
    });
    return;
  }
  const agentId = shell.displayedWorkspaceAgentId();
  if (agentId === null) return;
  m.route.set(
    `/workspace/${agentId}/options`,
    { tab: id },
    isTitlebarPopupRoutePath(path) ? { replace: true } : undefined,
  );
}
