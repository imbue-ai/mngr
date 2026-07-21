// CSS-only spinner (mirror of Spinner.jinja; the ``.spinner`` recipes live in
// app.css). ``tone="accent"`` is the blue primary-action ring;
// ``tone="inverse"`` derives from currentColor for spinners inside
// solid-filled buttons.
import m from "mithril";

export interface SpinnerAttrs {
  size?: "sm" | "md" | "lg";
  tone?: "default" | "accent" | "inverse";
  extra?: string;
}

const DIMENSION_CLASSES: Record<string, string> = {
  sm: "w-3.5 h-3.5 border",
  md: "w-[18px] h-[18px] border-2",
  lg: "w-8 h-8 border-[3px]",
};

export function Spinner(): m.Component<SpinnerAttrs> {
  return {
    view(vnode) {
      const tone = vnode.attrs.tone ?? "default";
      const toneClass = tone === "accent" ? " spinner-accent" : tone === "inverse" ? " spinner-inverse" : "";
      const dimensions = DIMENSION_CLASSES[vnode.attrs.size ?? "md"];
      const extra = vnode.attrs.extra !== undefined ? ` ${vnode.attrs.extra}` : "";
      return m("span", {
        class: `spinner${toneClass} inline-block align-middle ${dimensions}${extra}`,
        "aria-hidden": "true",
      });
    },
  };
}
