import m from "mithril";
import { joinClasses, splitAttrs } from "./attrs";

export type CardLayout = "block" | "row" | "row-spread";
export type CardPadding = "default" | "tight";

const LAYOUTS: Record<CardLayout, string> = {
  block: "",
  row: "flex items-center gap-1.5",
  "row-spread": "flex items-center justify-between gap-1.5",
};

const PADDINGS: Record<CardPadding, string> = {
  default: "p-4",
  tight: "px-4 py-2",
};

export function cardClass(
  layout: CardLayout,
  padding: CardPadding,
  interactive: boolean,
  tag: "div" | "a" | "button",
  extra: string,
): string {
  return joinClasses(
    "minds-card",
    LAYOUTS[layout],
    PADDINGS[padding],
    interactive && "cursor-pointer hover:border-strong hover:shadow-raised",
    tag === "a" && "no-underline text-inherit",
    extra,
  );
}

interface CardAttrs extends m.Attributes {
  layout?: CardLayout;
  padding?: CardPadding;
  interactive?: boolean;
  // The element to render ("div" | "a" | "button"). Named `as` rather than
  // `tag` because mithril's hyperscript treats any attrs object carrying a
  // non-null `tag` key as a child vnode, so a `tag` attr would silently make
  // m(Card, {tag: "a"}) drop its attributes.
  as?: "div" | "a" | "button";
  href?: string;
  extra?: string;
}

const OWN_KEYS = [
  "layout",
  "padding",
  "interactive",
  "as",
  "href",
  "extra",
] as const;

/**
 * Card surface (Card.jinja). The visual shell is the shared .minds-card CSS
 * class; `interactive` adds the hover lift for clickable cards. Use as="a"
 * (+ href) for click-anywhere navigation.
 */
export function Card(): m.Component<CardAttrs> {
  return {
    view(vnode) {
      const {
        layout = "block",
        padding = "default",
        interactive = false,
        as = "div",
        href = "",
        extra = "",
      } = vnode.attrs;
      const cls = cardClass(layout, padding, interactive, as, extra);
      const passthrough = splitAttrs(vnode.attrs, OWN_KEYS);
      if (as === "a") {
        return m("a", { class: cls, href, ...passthrough }, vnode.children);
      } else if (as === "button") {
        return m(
          "button",
          { class: cls, type: "button", ...passthrough },
          vnode.children,
        );
      } else {
        return m("div", { class: cls, ...passthrough }, vnode.children);
      }
    },
  };
}
