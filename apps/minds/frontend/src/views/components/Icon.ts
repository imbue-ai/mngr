import m from "mithril";
import { ICONS_12, ICONS_16 } from "./icons";

export type Icon16Size = "sm" | "md" | "lg";

const ICON16_SIZES: Record<Icon16Size, string> = {
  sm: "w-3.5 h-3.5",
  md: "w-4 h-4",
  lg: "w-5 h-5",
};

interface Icon16Attrs extends m.Attributes {
  name: string;
  size?: Icon16Size;
  extra?: string;
}

/**
 * 16x16 icon from the shared set (Icon16.jinja). The shell defaults to
 * fill=currentColor so each glyph takes the parent's text color. The inner
 * markup comes verbatim from the trusted ICONS_16 map (static path data, not
 * user input), so m.trust is safe here.
 */
export function Icon16(): m.Component<Icon16Attrs> {
  return {
    view(vnode) {
      const { name, size = "md", extra = "" } = vnode.attrs;
      return m(
        "svg",
        {
          class: ICON16_SIZES[size] + " " + extra,
          viewBox: "0 0 16 16",
          fill: "currentColor",
          "aria-hidden": "true",
        },
        m.trust(ICONS_16[name] ?? ""),
      );
    },
  };
}

interface Icon12Attrs extends m.Attributes {
  name: "minimize" | "maximize" | "close";
  extra?: string;
}

/**
 * 12x12 title-bar window-control glyph (Icon12.jinja): minimize / maximize /
 * close, rendered in a stroke shell sized for TitlebarButton variant=control.
 */
export function Icon12(): m.Component<Icon12Attrs> {
  return {
    view(vnode) {
      const { name, extra = "" } = vnode.attrs;
      return m(
        "svg",
        {
          class: "w-3 h-3 " + extra,
          viewBox: "0 0 12 12",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "2",
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
          "aria-hidden": "true",
        },
        m.trust(ICONS_12[name] ?? ""),
      );
    },
  };
}
