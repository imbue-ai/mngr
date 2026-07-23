// Compact pill-shaped status indicator (mirror of StatusBadge.jinja). Five
// variants cover the colors that recur across the landing-page row badges,
// the destroying detail page, and the accounts page. ``size="xs"`` is for
// inline-with-text callouts; ``size="sm"`` for badges in their own slot.
//
// Done / Failed / Info read as solid status fills with white text; neutral is
// a muted fill with secondary text; warn is the yellow caution surface with
// the (brown-orange) warning foreground, since white on yellow is unreadable.
import m from "mithril";

export interface StatusBadgeAttrs {
  variant?: "neutral" | "success" | "error" | "warn" | "info";
  size?: "sm" | "xs";
  extra?: string;
  title?: string;
}

const VARIANT_CLASSES: Record<string, string> = {
  neutral: "bg-fill-subtle text-secondary",
  success: "bg-success text-white",
  error: "bg-important text-white",
  warn: "bg-[var(--c-warning-surface)] text-warning",
  info: "bg-info text-white",
};

export function StatusBadge(): m.Component<StatusBadgeAttrs> {
  return {
    view(vnode) {
      const variant = vnode.attrs.variant ?? "neutral";
      // sm slot badges read as labels (14/semibold), xs inline tags as
      // helper (12/regular).
      const typeClass = (vnode.attrs.size ?? "sm") === "sm" ? "type-label" : "type-helper";
      const extra = vnode.attrs.extra !== undefined ? ` ${vnode.attrs.extra}` : "";
      return m(
        "span",
        {
          class: `inline-flex items-center px-2 py-0.5 rounded-md ${typeClass} ${VARIANT_CLASSES[variant]}${extra}`,
          title: vnode.attrs.title,
        },
        vnode.children,
      );
    },
  };
}
