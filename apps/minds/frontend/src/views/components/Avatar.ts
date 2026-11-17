import m from "mithril";
import { monogramColor, monogramLetters } from "../../models/monogram";
import { splitAttrs } from "./attrs";

export type AvatarSize = 14 | 16 | 20;

interface AvatarAttrs extends m.Attributes {
  /** The picture, or null for a monogram of the label. */
  src: string | null;
  /** Who it is: the monogram's letters and colour, and the image's alt. */
  label: string;
  size?: AvatarSize;
  extra?: string;
}

const OWN_KEYS = ["src", "label", "size", "extra"] as const;

/** A small round face: the picture when there is one, else a monogram (specs/home-screen-redesign, 4.8). */
export function Avatar(): m.Component<AvatarAttrs> {
  return {
    view(vnode) {
      const { src, label, size = 16, extra = "" } = vnode.attrs;
      const dimension = `width: ${size}px; height: ${size}px;`;
      const passthrough = splitAttrs(vnode.attrs, OWN_KEYS);
      if (src !== null && src !== "") {
        return m("img", {
          src,
          alt: "",
          "data-avatar-for": label,
          class: "shrink-0 rounded-full object-cover " + extra,
          style: dimension,
          ...passthrough,
        });
      }
      return m(
        "span",
        {
          "aria-hidden": "true",
          "data-avatar-for": label,
          class:
            "shrink-0 inline-flex items-center justify-center rounded-full text-white font-semibold leading-none " +
            extra,
          style: `${dimension} background-color: ${monogramColor(label)}; font-size: ${Math.round(size * 0.5)}px;`,
          ...passthrough,
        },
        monogramLetters(label).slice(0, 1),
      );
    },
  };
}
