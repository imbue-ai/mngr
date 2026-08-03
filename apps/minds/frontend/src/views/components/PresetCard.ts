import m from "mithril";
import { splitAttrs } from "./attrs";

// Selectable "where to run" card (PresetCard.jinja). The edge is an outline
// (not a border): outlines paint outside the box and never take up layout
// space, so growing 1px -> 2px between states never shifts the card.
const BASE =
  "flex flex-col gap-2 p-4 text-left cursor-pointer rounded-lg bg-surface-primary outline-1 outline-dashed outline-strong transition-all duration-150 ease-out hover:-translate-y-px hover:shadow-raised active:translate-y-0 active:scale-[0.99] aria-checked:outline-2 aria-checked:outline-solid aria-checked:outline-accent";

interface PresetCardAttrs extends m.Attributes {
  preset: string;
  selected?: boolean;
  extra?: string;
}

/**
 * Radio-style selectable card; selection is driven by aria-checked, exactly
 * like the ColorSwatch picker, so callers only toggle the attribute.
 */
export function PresetCard(): m.Component<PresetCardAttrs> {
  return {
    view(vnode) {
      const { preset, selected = false, extra = "" } = vnode.attrs;
      return m(
        "button",
        {
          type: "button",
          role: "radio",
          class: BASE + " " + extra,
          "data-preset": preset,
          "aria-checked": selected ? "true" : "false",
          ...splitAttrs(vnode.attrs, ["preset", "selected", "extra"]),
        },
        vnode.children,
      );
    },
  };
}
