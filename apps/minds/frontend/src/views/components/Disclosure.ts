// A one-line toggle with a chevron that opens a detail underneath: the
// manifesto's points and the creation page's reading material use it, so a
// list of them reads as one idiom. Open state belongs to the caller (the page
// keeps a set of open ids), which keeps the component free of state and lets a
// redraw from anywhere preserve what the reader opened.

import m from "mithril";
import { Icon16 } from "./Icon";

interface DisclosureAttrs extends m.Attributes {
  isOpen: boolean;
  onToggle: () => void;
  /** The always-visible line the chevron sits in front of. */
  summary: m.Children;
}

export function Disclosure(): m.Component<DisclosureAttrs> {
  return {
    view(vnode) {
      const { isOpen, onToggle, summary } = vnode.attrs;
      return m("div", { class: "flex flex-col", "data-disclosure": isOpen ? "open" : "closed" }, [
        m(
          "button",
          {
            type: "button",
            class:
              "flex items-start gap-2 text-left cursor-pointer bg-transparent border-0 p-0 " +
              "text-primary hover:text-accent",
            "aria-expanded": isOpen ? "true" : "false",
            onclick: onToggle,
          },
          [
            m(
              "span",
              { class: "mt-0.5 shrink-0 text-tertiary", "aria-hidden": "true" },
              m(Icon16, { name: isOpen ? "chevron-down" : "chevron-right" }),
            ),
            m("span", { class: "leading-[1.5]" }, summary),
          ],
        ),
        isOpen ? m("div", { class: "ml-6 mt-1 text-secondary leading-[1.5]" }, vnode.children) : null,
      ]);
    },
  };
}
