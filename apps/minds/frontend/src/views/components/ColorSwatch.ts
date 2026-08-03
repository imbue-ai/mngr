import m from "mithril";
import { splitAttrs } from "./attrs";

const SIZES: Record<"md" | "sm", string> = {
  md: "w-[34px] h-[34px]",
  sm: "w-6 h-6",
};

interface ColorSwatchAttrs extends m.Attributes {
  hex: string;
  name?: string;
  selected?: boolean;
  size?: "md" | "sm";
  disabled?: boolean;
}

/**
 * A single workspace-color swatch (ColorSwatch.jinja): a circular radio
 * button rendering one palette (or custom) color. Owns the markup contract
 * the pickers depend on: class color-swatch (rim + ring styles in
 * style.css), role=radio + aria-checked, data-color, aria-label.
 */
export function ColorSwatch(): m.Component<ColorSwatchAttrs> {
  return {
    view(vnode) {
      const {
        hex,
        name = "",
        selected = false,
        size = "md",
        disabled = false,
      } = vnode.attrs;
      return m("button", {
        type: "button",
        role: "radio",
        class:
          "color-swatch " +
          SIZES[size] +
          " rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-40",
        "aria-label": name,
        "aria-checked": selected ? "true" : "false",
        "data-color": hex,
        style: { backgroundColor: hex },
        disabled,
        ...splitAttrs(vnode.attrs, [
          "hex",
          "name",
          "selected",
          "size",
          "disabled",
        ]),
      });
    },
  };
}
