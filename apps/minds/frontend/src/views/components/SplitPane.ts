// The two-column pane: a list of sections on the left, the selected section's
// panel on the right, each column scrolling on its own so the list stays in
// view however far down the panel you read. Shared by the workspace options
// panes (Permissions, Share machine, Machine settings) and the app-level Minds
// settings modal.
//
// The pane deliberately does NOT scroll as a whole -- one scroller around both
// columns takes the nav down with the panel, which is the bug this exists to
// prevent. It expects to sit in a height-bounded flex column (the docked
// options card, or the app-overlay card's body), which is what makes
// `flex-1 min-h-0` resolve to a real height and the columns' overflow bite.
//
// A plain function rather than an m.Component, matching serviceMark and the
// other class builders here: there is no state or lifecycle to earn one, and a
// function inlines the caller's nav in real child position -- passed through a
// component's attrs it would sit outside the vnode tree that tests and
// `vnode.children` walks can see.
//
// Ported from the four hand-rolled copies of this layout (and, before them, the
// legacy WorkspaceShareSection / WorkspaceSettingsSections panel_scroll
// classes).

import m from "mithril";

// One nav width everywhere: both cards this lands in are 880px wide, so a
// second width would only make the same layout read as two different ones.
const NAV_CLASS = "shrink-0 w-52 overflow-y-auto min-h-0 p-1.5 -m-1.5";

// The content column's scrollbar sits out at the card's px-6 edge: the negative
// margins cancel the card padding, the padding restores the inset. Both cards
// pad px-6, so the one recipe lands correctly in either.
const CONTENT_CLASS =
  "flex-1 min-w-0 overflow-y-auto min-h-0 pt-1.5 pb-1.5 pl-1.5 pr-6 -mt-1.5 -mb-1.5 -ml-1.5 -mr-6";

const NAV_ENTRY_CLASS =
  "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left type-body cursor-pointer " +
  "transition-colors text-primary hover:bg-fill-hover";
const NAV_ENTRY_SELECTED_CLASS = NAV_ENTRY_CLASS + " bg-fill-hover font-semibold";

/** One entry in a pane's left nav. The selected one fills and bolds, so the
 * list reads as the tabs it is rather than as links. */
export function navEntryClass(isSelected: boolean): string {
  return isSelected ? NAV_ENTRY_SELECTED_CLASS : NAV_ENTRY_CLASS;
}

export interface SplitPaneOptions {
  /** aria-label for the <nav>: what this list of sections is. */
  navLabel: string;
  /** The nav's own contents. Vnodes rather than a modelled entry list: a flat
   * list, two groups split by a divider, and headed groups are then the same
   * pane with different children instead of three configuration flags. */
  nav: m.Children;
  /** The selected section's panel. */
  content: m.Children;
  /** Extra classes on the row (the options panes' mt-8 under the pane title).
   * Must be a complete literal -- Tailwind scans source text, so a class
   * assembled from parts renders unstyled. */
  extra?: string;
  /** Extra classes on the content column, same literal rule. Only a panel that
   * is itself a flex column needs this; the rest stay block containers, where
   * adjacent margins still collapse. */
  contentExtra?: string;
}

export function splitPane(options: SplitPaneOptions): m.Child {
  const { navLabel, nav, content, extra = "", contentExtra = "" } = options;
  return m("div", { class: "flex gap-8 flex-1 min-h-0 " + extra }, [
    m("nav", { class: NAV_CLASS, "aria-label": navLabel }, nav),
    m("div", { class: CONTENT_CLASS + " " + contentExtra }, content),
  ]);
}
