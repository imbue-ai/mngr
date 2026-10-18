import m from "mithril";
import { splitAttrs } from "./attrs";

export type NoticeVariant = "info" | "warn" | "success" | "error";

const NOTICE_VARIANTS: Record<NoticeVariant, string> = {
  info: "bg-[var(--c-info-surface)] text-info",
  warn: "bg-[var(--c-warning-surface)] text-warning",
  success: "bg-[var(--c-success-surface)] text-success",
  error: "bg-[var(--c-important-surface)] text-important",
};

/** Just the variant's colour tokens, for surfaces that need the severity
 * palette without the boxed-notice layout (the shell's full-width band). */
export function noticeVariantClass(variant: NoticeVariant): string {
  return NOTICE_VARIANTS[variant];
}

export function noticeClass(variant: NoticeVariant): string {
  // wrap-anywhere, because what lands in a notice is usually an error verbatim
  // from somewhere else: URLs, socket paths, command lines. Such a token has no
  // break opportunity in it, so without this it sizes the box past its
  // container and spills out of the coloured surface.
  return "px-3 py-2 rounded-md type-body my-2 wrap-anywhere " + noticeVariantClass(variant);
}

interface NoticeAttrs extends m.Attributes {
  variant?: NoticeVariant;
  extra?: string;
}

/** Info / warn / success / error notice box (Notice.jinja). */
export function Notice(): m.Component<NoticeAttrs> {
  return {
    view(vnode) {
      const { variant = "info", extra = "" } = vnode.attrs;
      return m(
        "div",
        {
          class: noticeClass(variant) + (extra ? " " + extra : ""),
          ...splitAttrs(vnode.attrs, ["variant", "extra"]),
        },
        vnode.children,
      );
    },
  };
}
