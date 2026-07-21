// The 16x16 icon set (mirror of Icon16.jinja): a viewBox="0 0 16 16" shell
// defaulting to fill="currentColor" around the glyph path data from
// icons.ts. Most glyphs are filled outlines; ``play`` carries its own stroke
// attributes. Sizes are sm (14px), md (16px, native, default), lg (20px).
//
// m.trust() here injects STATIC path data from icons.ts (the same trusted
// constants Icon16.jinja renders with `| safe`) -- never interpolated data.
import m from "mithril";

import { ICONS_16 } from "../icons";
import { MindsUIError } from "../mount";

const SIZE_CLASSES: Record<string, string> = {
  sm: "w-3.5 h-3.5",
  md: "w-4 h-4",
  lg: "w-5 h-5",
};

export interface IconAttrs {
  name: string;
  size?: "sm" | "md" | "lg";
  extra?: string;
}

export function Icon(): m.Component<IconAttrs> {
  return {
    view(vnode) {
      const glyph = ICONS_16[vnode.attrs.name];
      if (glyph === undefined) {
        throw new MindsUIError(`unknown 16px icon: ${vnode.attrs.name}`);
      }
      const sizeClass = SIZE_CLASSES[vnode.attrs.size ?? "md"];
      const extra = vnode.attrs.extra;
      return m(
        "svg",
        {
          class: extra !== undefined ? `${sizeClass} ${extra}` : sizeClass,
          viewBox: "0 0 16 16",
          fill: "currentColor",
          "aria-hidden": "true",
        },
        m.trust(glyph),
      );
    },
  };
}
