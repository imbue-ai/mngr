import m from "mithril";
import { splitAttrs } from "./attrs";

export type StatusBadgeVariant =
  "neutral" | "success" | "error" | "warn" | "info";

// Done / Failed / Info read as solid status fills with white text; neutral is
// a muted fill with secondary text; warn is the yellow caution surface with
// the warning foreground, since white on yellow is unreadable.
const VARIANTS: Record<StatusBadgeVariant, string> = {
  neutral: "bg-fill-subtle text-secondary",
  success: "bg-success text-white",
  error: "bg-important text-white",
  warn: "bg-[var(--c-warning-surface)] text-warning",
  info: "bg-info text-white",
};

export function statusBadgeClass(
  variant: StatusBadgeVariant,
  size: "sm" | "xs",
  extra: string,
): string {
  const typeRole = size === "sm" ? "type-label" : "type-helper";
  return (
    "inline-flex items-center px-2 py-0.5 rounded-md " +
    typeRole +
    " " +
    VARIANTS[variant] +
    " " +
    extra
  );
}

interface StatusBadgeAttrs extends m.Attributes {
  variant?: StatusBadgeVariant;
  size?: "sm" | "xs";
  extra?: string;
  title?: string;
}

/** Compact pill-shaped status indicator (StatusBadge.jinja). */
export function StatusBadge(): m.Component<StatusBadgeAttrs> {
  return {
    view(vnode) {
      const {
        variant = "neutral",
        size = "sm",
        extra = "",
        title,
      } = vnode.attrs;
      return m(
        "span",
        {
          class: statusBadgeClass(variant, size, extra),
          ...(title ? { title } : {}),
          ...splitAttrs(vnode.attrs, ["variant", "size", "extra", "title"]),
        },
        vnode.children,
      );
    },
  };
}
