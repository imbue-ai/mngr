import m from "mithril";
import { splitAttrs } from "./attrs";

interface LinkAttrs extends m.Attributes {
  href: string;
  weight?: "regular" | "medium";
  extra?: string;
}

/**
 * Inline text link (Link.jinja): text-accent with hover underline. For
 * click-toggles that look like links but are not navigations, prefer the
 * ghost-Button-as-link recipe in the styleguide.
 */
export function Link(): m.Component<LinkAttrs> {
  return {
    view(vnode) {
      const { href, weight = "regular", extra = "" } = vnode.attrs;
      const weightClass = weight === "medium" ? "font-semibold " : "";
      return m(
        "a",
        {
          href,
          class: "text-accent hover:underline " + weightClass + extra,
          ...splitAttrs(vnode.attrs, ["href", "weight", "extra"]),
        },
        vnode.children,
      );
    },
  };
}
