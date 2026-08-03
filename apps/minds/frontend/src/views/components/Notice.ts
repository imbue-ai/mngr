import m from "mithril";
import { splitAttrs } from "./attrs";

export type NoticeVariant = "info" | "warn" | "success" | "error";

const NOTICE_VARIANTS: Record<NoticeVariant, string> = {
  info: "bg-[var(--c-info-surface)] text-info",
  warn: "bg-[var(--c-warning-surface)] text-warning",
  success: "bg-[var(--c-success-surface)] text-success",
  error: "bg-[var(--c-important-surface)] text-important",
};

export function noticeClass(variant: NoticeVariant): string {
  return "px-3 py-2 rounded-md type-body my-2 " + NOTICE_VARIANTS[variant];
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
