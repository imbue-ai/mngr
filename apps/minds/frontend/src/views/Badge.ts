// Notification badge (mirror of Badge.jinja): a solid ``important`` count
// pill (99+ cap) when ``count`` is set, an 8px dot otherwise. Carries no
// position of its own -- the caller places it. Visibility is likewise the
// caller's job; note the pill bakes in ``inline-flex``, so hide it with the
// native ``hidden`` attribute (display:none !important), never a ``hidden``
// class.
import m from "mithril";

export interface BadgeAttrs {
  count?: number;
  extra?: string;
}

const DOT_CLASSES = "inline-block align-middle w-2 h-2 rounded-full bg-important";
const PILL_CLASSES =
  "inline-flex items-center justify-center align-middle min-w-[16px] px-1 py-px rounded-full bg-important text-white type-badge whitespace-nowrap overflow-hidden";

export function Badge(): m.Component<BadgeAttrs> {
  return {
    view(vnode) {
      const extra = vnode.attrs.extra;
      const count = vnode.attrs.count;
      if (count === undefined) {
        return m("span", { class: extra !== undefined ? `${DOT_CLASSES} ${extra}` : DOT_CLASSES });
      }
      return m(
        "span",
        { class: extra !== undefined ? `${PILL_CLASSES} ${extra}` : PILL_CLASSES },
        count > 99 ? "99+" : String(count),
      );
    },
  };
}
