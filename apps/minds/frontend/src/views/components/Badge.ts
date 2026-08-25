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
 * Notification badge (Badge.jinja). Three shapes chosen by `count`: the bare
 * 8px dot for presence-without-a-number, a perfect 14px circle for a single
 * digit (1-9), or a pill that widens for two-or-more characters (10-99+) --
 * only the wider counts need the oval shape at all. Carries no position of
 * its own -- the caller places it.
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
      const label = formatBadgeCount(count);
      const isSingleDigit = label.length === 1;
      return m(
        "span",
        {
          class:
            "inline-flex items-center justify-center align-middle h-[14px] rounded-full bg-important " +
            "text-white type-badge whitespace-nowrap overflow-hidden " +
            (isSingleDigit ? "w-[14px]" : "min-w-[16px] px-1"),
          ...passthrough,
        },
        label,
      );
    },
  };
}
