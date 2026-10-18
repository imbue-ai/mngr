// The drop offer a main window shows while a pulled-out window of its
// workspace is dragged over its surface (the pull-out-window spec, section
// 4.4): releasing there returns the window to the desktop where it was
// dropped. Painted over the workspace surface, under the titlebar.

import m from "mithril";

export const PopoutDropOverlay: m.Component = {
  view() {
    return m(
      "div#popout-drop-overlay",
      {
        class:
          "workspace-surface pointer-events-none z-[90] flex items-center justify-center " +
          "border-2 border-dashed border-accent bg-accent/10",
      },
      m(
        "div",
        { class: "rounded-md bg-surface-primary px-4 py-2 shadow-raised type-label text-primary" },
        "Drop to return this window to the desktop",
      ),
    );
  },
};
