import m from "mithril";
import { monogramColor, monogramLetters } from "../../models/monogram";
import { splitAttrs } from "./attrs";

interface MonogramTileAttrs extends m.Attributes {
  /** What the tile stands in for: its letters and its colour come from this. */
  name: string;
  /** The size, shape, shadow and font size: a 64px app icon, a 40px invite thumbnail. */
  extra: string;
}

const OWN_KEYS = ["name", "extra"] as const;

/** The tile an app with no usable icon wears (specs/home-screen-redesign, 4.8): its monogram on a colour of its name. */
export function MonogramTile(): m.Component<MonogramTileAttrs> {
  return {
    view(vnode) {
      const { name, extra } = vnode.attrs;
      const letters = monogramLetters(name);
      return m(
        "span",
        {
          "aria-hidden": "true",
          "data-monogram": letters,
          class:
            "inline-flex items-center justify-center text-white font-semibold " +
            extra,
          style: `background-color: ${monogramColor(name)};`,
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        letters,
      );
    },
  };
}
