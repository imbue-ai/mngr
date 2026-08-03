import m from "mithril";
import { splitAttrs } from "./attrs";

export type SpinnerSize = "sm" | "md" | "lg";
export type SpinnerTone = "default" | "accent" | "inverse";

const DIMS: Record<SpinnerSize, string> = {
  sm: "w-3.5 h-3.5 border",
  md: "w-[18px] h-[18px] border-2",
  lg: "w-8 h-8 border-[3px]",
};

export function spinnerClass(
  size: SpinnerSize,
  tone: SpinnerTone,
  extra: string,
): string {
  const toneClass =
    tone === "accent"
      ? " spinner-accent"
      : tone === "inverse"
        ? " spinner-inverse"
        : "";
  return (
    "spinner" +
    toneClass +
    " inline-block align-middle " +
    DIMS[size] +
    " " +
    extra
  );
}

interface SpinnerAttrs extends m.Attributes {
  size?: SpinnerSize;
  tone?: SpinnerTone;
  extra?: string;
}

/** CSS-only spinner (Spinner.jinja); the .spinner recipes live in style.css. */
export function Spinner(): m.Component<SpinnerAttrs> {
  return {
    view(vnode) {
      const { size = "md", tone = "default", extra = "" } = vnode.attrs;
      return m("span", {
        class: spinnerClass(size, tone, extra),
        "aria-hidden": "true",
        ...splitAttrs(vnode.attrs, ["size", "tone", "extra"]),
      });
    },
  };
}
