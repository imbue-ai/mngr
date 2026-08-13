// Shared panel-scroll utility classes for the options panes. The docked options
// card is fixed-height, so each pane's nav and content column scrolls on its own
// (the title beside them stays put), with its scrollbar pulled out to the card's
// px-6 edge: the negative margins cancel the card padding, the padding restores
// the inset. Ported from the legacy WorkspaceShareSection /
// WorkspaceSettingsSections panel_scroll classes.

export const PANE_NAV_SCROLL = "overflow-y-auto min-h-0 p-1.5 -m-1.5";
export const PANE_CONTENT_SCROLL =
  "overflow-y-auto min-h-0 pt-1.5 pb-1.5 pl-1.5 pr-6 -mt-1.5 -mb-1.5 -ml-1.5 -mr-6";
