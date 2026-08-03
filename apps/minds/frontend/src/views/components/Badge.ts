import m from "mithril";
import { splitAttrs } from "./attrs";

interface BadgeAttrs extends m.Attributes {
  count?: number | null;
}

/** Format a badge count with the 99+ cap the titlebar uses. */
export function formatBadgeCount(count: number): string {
  return count > 99 ? "99+" : String(count);
}

/**
 * Notification badge (Badge.jinja). Two shapes chosen by whether `count` is
 * provided: a solid important pill with the number (grows for wider numbers,
 * 99+ cap), or the bare 8px dot for presence-without-a-number. Carries no
 * position of its own -- the caller places it.
 */
export function Badge(): m.Component<BadgeAttrs> {
  return {
    view(vnode) {
      const { count } = vnode.attrs;
      const passthrough = splitAttrs(vnode.attrs, ["count"]);
      if (count === undefined || count === null) {
        return m("span", {
          class: "inline-block align-middle w-2 h-2 rounded-full bg-important",
          ...passthrough,
        });
      }
      return m(
        "span",
        {
          class:
            "inline-flex items-center justify-center align-middle min-w-[16px] px-1 py-px rounded-full bg-important text-white type-badge whitespace-nowrap overflow-hidden",
          ...passthrough,
        },
        formatBadgeCount(count),
      );
    },
  };
}
