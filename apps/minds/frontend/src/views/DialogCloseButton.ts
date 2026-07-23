// Absolute-positioned X button at the top right of a modal dialog (mirror of
// DialogCloseButton.jinja). The glyph is the shared 16px ``close`` icon.
// Carries ``data-tooltip`` so it shows a "Close" tooltip on the overlay
// surface (via /_static/tooltip_triggers.js) like the other overlay controls.
import m from "mithril";

import { Icon } from "./Icon";

export interface DialogCloseButtonAttrs {
  onclick: () => void;
  id?: string;
}

export function DialogCloseButton(): m.Component<DialogCloseButtonAttrs> {
  return {
    view(vnode) {
      return m(
        "button",
        {
          type: "button",
          "aria-label": "Close",
          "data-tooltip": "Close",
          id: vnode.attrs.id,
          onclick: vnode.attrs.onclick,
          class:
            "absolute top-3 right-3 z-10 inline-flex items-center justify-center w-8 h-8 rounded-md " +
            "text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
        },
        m(Icon, { name: "close" }),
      );
    },
  };
}
