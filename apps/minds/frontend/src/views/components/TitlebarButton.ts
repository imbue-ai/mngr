import m from "mithril";
import { splitAttrs } from "./attrs";

export type TitlebarButtonVariant = "nav" | "crumb" | "control";
export type TitlebarButtonTone = "default" | "muted" | "danger";

const VARIANTS: Record<TitlebarButtonVariant, string> = {
  nav: "p-1.5 rounded-md",
  crumb: "px-1.5 py-1 rounded-md",
  control: "w-9 h-[38px] rounded-none",
};

const TONES: Record<TitlebarButtonTone, string> = {
  default: "text-primary",
  muted: "text-secondary hover:text-primary",
  danger: "text-primary titlebar-btn-danger",
};

const BASE =
  "inline-flex items-center justify-center cursor-pointer hover:bg-fill-hover active:bg-fill-active focus-visible:outline-2 focus-visible:outline-accent";

export function titlebarButtonClass(
  variant: TitlebarButtonVariant,
  tone: TitlebarButtonTone,
  extra: string,
): string {
  return BASE + " " + VARIANTS[variant] + " " + TONES[tone] + " " + extra;
}

interface TitlebarButtonAttrs extends m.Attributes {
  variant?: TitlebarButtonVariant;
  tone?: TitlebarButtonTone;
  extra?: string;
}

/**
 * Title-bar window-control / nav button (TitlebarButton.jinja). Its colors are
 * plain design tokens re-based per-workspace by the .titlebar-surface scope in
 * style.css, so the same button reads correctly on any workspace color.
 */
export function TitlebarButton(): m.Component<TitlebarButtonAttrs> {
  return {
    view(vnode) {
      const { variant = "nav", tone = "default", extra = "" } = vnode.attrs;
      return m(
        "button",
        {
          type: "button",
          class: titlebarButtonClass(variant, tone, extra),
          ...splitAttrs(vnode.attrs, ["variant", "tone", "extra"]),
        },
        vnode.children,
      );
    },
  };
}
