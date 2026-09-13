// The downloaded-update offer.
//
// Deliberately quiet. The update installs on the next restart whether or not
// this is ever touched, so it states a fact and offers a shortcut -- it does
// not demand an answer, and nothing behind it is blocked while it is up.
//
// Carries no position of its own: the shell floats it in the corner, and the
// styleguide renders it in the flow of the page. Keeping placement out of it is
// what lets it be looked at without an update actually existing.

import m from "mithril";
import { Button } from "../components/Button";
import { Icon16 } from "../components/Icon";

export interface UpdateReadyCardAttrs {
  version: string;
  onRestart: () => void;
  onDismiss: () => void;
}

export function UpdateReadyCard(): m.Component<UpdateReadyCardAttrs> {
  return {
    view(vnode) {
      const { version, onRestart, onDismiss } = vnode.attrs;
      return m(
        "div",
        {
          class:
            "flex items-center gap-4 rounded-lg pl-4 pr-3 py-3 " +
            "bg-surface-primary border border-subtle shadow-raised",
          role: "status",
        },
        [
          // Two lines, so the version does not have to share weight with the
          // instruction: what happened, then what it costs.
          m("div", { class: "flex flex-col gap-0.5 min-w-0" }, [
            m("span", { class: "type-label text-primary truncate" }, `Minds ${version} is ready`),
            m("span", { class: "type-helper text-tertiary" }, "Installs when you restart"),
          ]),
          m(Button, { variant: "primary", onclick: onRestart, extra: "shrink-0" }, "Restart now"),
          // The same shape as a dialog's close: a glyph in its own hit area,
          // rather than a bare character with no target to speak of.
          m(
            "button",
            {
              type: "button",
              "aria-label": "Dismiss",
              onclick: onDismiss,
              class:
                "shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-md " +
                "text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
            },
            m(Icon16, { name: "close" }),
          ),
        ],
      );
    },
  };
}
