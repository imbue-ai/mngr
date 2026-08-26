// The workspace-options overlay: the Permissions / Share machine / Machine
// settings panel docked under the titlebar's icon-tabs, a faithful port of the
// legacy WorkspaceOptionsShell.jinja docked presentation.
//
// The panel HANGS from the titlebar's #ws-tab-strip: it measures that strip's
// window rect (the SPA analogue of chrome.js measuring it and packing the rect
// into the legacy modal iframe's URL) and draws its OWN tab strip at that exact
// spot, with the selected tab filled with the card's surface and square-
// bottomed so it reads as physically joined to the panel below it. The card's
// top sits at the strip's bottom and its left edge under the strip, so the whole
// thing reads as hanging from the tabs; it stops widening at 880px and never
// comes closer than a 24px gutter to either window edge. The Titlebar hides its
// own icon-tab buttons while this is up (isWorkspaceOverlayPath) so the two
// never ghost through each other.
//
// A cold-start deep link renders before the titlebar exists, so with no anchor
// yet it falls back to a plain centered card (also the legacy no-anchor shape)
// until the first measure lands.

import m from "mithril";
import type { OptionsTab, SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import type { PermissionsModel } from "../../../models/workspacePermissions";
import { OverlayBackdrop } from "../../shell/OverlayBackdrop";
import { DialogCloseButton } from "../../components/Modal";
import { Icon16 } from "../../components/Icon";
import { OptionsPanel } from "./OptionsPanel";
import { defaultFetchJson } from "../../../models/workspaceOptions";
import { warmPermissionsOverview } from "../../../models/permissionsPrefetch";

interface StripAnchor {
  x: number;
  y: number;
  height: number;
}

export interface WorkspaceOptionsOverlayAttrs {
  /** The machine this panel is open on, so dismissing never has to re-read a
   * route that may belong to a modal floating over the panel. */
  agentId: string;
  model: WorkspaceOptionsModel;
  permissions: PermissionsModel;
  tab: OptionsTab;
  group: SettingsGroup;
  /** ?section for the Permissions left nav, or null for "whatever is first". */
  section: string | null;
  onSelectTab: (tab: OptionsTab) => void;
  onSelectGroup: (group: SettingsGroup) => void;
  onSelectSection: (section: string) => void;
  /** Open a waiting request on its own page over this pane. */
  onReviewRequest: (requestId: string) => void;
}

/** The docked strip's tabs, in order. Exported so the tab registration (this
 * strip, the ?tab parse, and the titlebar's icon-tabs) is asserted in one
 * place. */
export const DOCKED_TABS: { id: OptionsTab; icon: string; label: string }[] = [
  { id: "permissions", icon: "key", label: "Permissions" },
  { id: "settings", icon: "settings", label: "Machine settings" },
  { id: "share", icon: "user-plus", label: "Share machine" },
];

/** Gutter kept clear on both sides of the card at small window sizes. */
const MIN_GUTTER_PX = 24;

/** How far left of the titlebar tab strip the card's edge sits, so it reads as
 * hanging from the tabs rather than floating loose beside them. */
const STRIP_OVERHANG_PX = 20;

// A fixed-height card, so a long pane (twenty share entries) scrolls inside it
// rather than growing the card off-screen; capped to the window (max-h-full).
const CARD_CLASS =
  "pointer-events-auto relative flex flex-col h-[660px] max-h-full overflow-hidden " +
  "rounded-xl bg-surface-primary text-primary shadow-overlay";

/** The #ws-tab-strip window rect, or null when the titlebar is not mounted yet
 * (a cold-start deep link's first paint). The strip keeps its box while the
 * Titlebar hides its buttons by visibility, so the rect stays true once open. */
function readStripAnchor(): StripAnchor | null {
  const strip = document.getElementById("ws-tab-strip");
  if (strip === null) return null;
  const rect = strip.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { x: rect.left, y: rect.top, height: rect.height };
}

export function WorkspaceOptionsOverlay(): m.Component<WorkspaceOptionsOverlayAttrs> {
  // The titlebar persists across the open, so on the common in-workspace open
  // it is already mounted and measurable before the first paint (no centered
  // flash); oncreate/onupdate re-measure for a cold-start deep link and for a
  // late-loading workspace name that shifts the strip.
  let anchor: StripAnchor | null = readStripAnchor();

  function remeasure(): void {
    const next = readStripAnchor();
    if (next === null) return;
    if (anchor === null || anchor.x !== next.x || anchor.y !== next.y || anchor.height !== next.height) {
      anchor = next;
      m.redraw();
    }
  }

  function dismiss(agentId: string): void {
    // Back to the bare workspace surface (kept mounted behind the overlay),
    // mirroring shell.closeWorkspaceOverlay without needing a shell handle.
    m.route.set(`/workspace/${agentId}`);
  }

  /** The docked tab strip: icon-only, standing in for the titlebar icon-tabs it
   * covers. The selected tab is filled with the card surface (square-bottomed,
   * joined to the panel); an unselected one self-themes on the workspace accent
   * via titlebar-surface + the inherited --titlebar-bg, exactly as the real
   * titlebar buttons do. */
  function renderStrip(
    currentTab: OptionsTab,
    onSelectTab: (tab: OptionsTab) => void,
    positionStyle: string,
    agentId: string,
  ): m.Child {
    return m(
      "div",
      {
        role: "tablist",
        "aria-label": "Workspace options",
        class: "pointer-events-auto absolute z-10 flex items-center gap-1",
        style: positionStyle,
      },
      DOCKED_TABS.map((entry) => {
        const isSelected = entry.id === currentTab;
        return m(
          "button",
          {
            type: "button",
            role: "tab",
            "data-wsopt-tab": entry.id,
            "aria-selected": isSelected ? "true" : "false",
            "aria-label": entry.label,
            "data-tooltip": entry.label,
            // Same warm the titlebar key does: switching to Permissions from
            // Share or Settings reads the same overview, and pointing at the
            // tab is the same head start.
            onpointerenter: () => {
              if (entry.id === "permissions") warmPermissionsOverview(agentId, defaultFetchJson);
            },
            class:
              "inline-flex items-center justify-center p-1.5 rounded-md focus-visible:outline-2 focus-visible:outline-accent " +
              (isSelected
                ? "bg-surface-primary rounded-b-none text-primary"
                : "titlebar-surface cursor-pointer text-secondary hover:bg-fill-hover active:bg-fill-active hover:text-primary"),
            onclick: () => onSelectTab(entry.id),
          },
          m(Icon16, { name: entry.icon }),
        );
      }),
    );
  }

  function renderCard(attrs: WorkspaceOptionsOverlayAttrs): m.Child {
    const { agentId, model, permissions, tab, group, section, onSelectGroup, onSelectSection, onReviewRequest } =
      attrs;
    return m("div#ws-options-panel", { class: CARD_CLASS + " w-full max-w-[880px]" }, [
      m(DialogCloseButton, { id: "ws-options-close", onClose: () => dismiss(agentId) }),
      // A column, not a scroller: the pane inside decides what stays pinned
      // (its title + nav) and what scrolls, so a long pane never drags the
      // title off the top with it.
      m(
        "div",
        { class: "flex-1 min-h-0 flex flex-col px-6 py-4" },
        m(OptionsPanel, {
          model,
          permissions,
          tab,
          group,
          section,
          onSelectGroup,
          onSelectSection,
          onReviewRequest,
        }),
      ),
    ]);
  }

  return {
    oncreate() {
      remeasure();
    },
    onupdate() {
      remeasure();
    },
    view(vnode) {
      const { agentId, tab, onSelectTab } = vnode.attrs;
      const gutterPx = anchor === null ? MIN_GUTTER_PX : Math.max(MIN_GUTTER_PX, Math.round(anchor.x - STRIP_OVERHANG_PX));
      const region =
        anchor !== null
          ? m(
              "div",
              {
                class: "fixed left-0 right-0 bottom-3 flex items-start justify-start pointer-events-none",
                style: `top: ${anchor.y + anchor.height}px; padding-left: ${gutterPx}px; padding-right: ${MIN_GUTTER_PX}px`,
              },
              [
                renderStrip(
                  tab,
                  onSelectTab,
                  `left: ${anchor.x}px; top: -${anchor.height}px; height: ${anchor.height}px`,
                  agentId,
                ),
                renderCard(vnode.attrs),
              ],
            )
          : m(
              "div",
              { class: "fixed inset-0 flex items-center justify-center p-4 pointer-events-none" },
              renderCard(vnode.attrs),
            );

      return m(
        OverlayBackdrop,
        { backdropId: "ws-options-backdrop", fullWindow: true, onDismiss: () => dismiss(agentId) },
        region,
      );
    },
  };
}
