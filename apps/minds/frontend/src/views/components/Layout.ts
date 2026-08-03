import m from "mithril";
import { splitAttrs } from "./attrs";

interface PageContainerAttrs extends m.Attributes {
  extra?: string;
}

/** Centered max-w-[720px] page-body wrapper (PageContainer.jinja). */
export function PageContainer(): m.Component<PageContainerAttrs> {
  return {
    view(vnode) {
      const { extra = "" } = vnode.attrs;
      return m(
        "div",
        {
          class: "max-w-[720px] mx-auto px-6 py-12 " + extra,
          ...splitAttrs(vnode.attrs, ["extra"]),
        },
        vnode.children,
      );
    },
  };
}

interface PageNarrowContainerAttrs extends m.Attributes {
  padding?: "default" | "form";
  maxWidth?: string;
}

/**
 * Narrow, vertically-centered page column (the body half of
 * PageNarrowContainer.jinja -- the shell chrome around it is the SPA Shell's
 * job now). min-h-full fills the scroll card's height so a short column
 * centers and a tall one scrolls.
 */
export function PageNarrowContainer(): m.Component<PageNarrowContainerAttrs> {
  return {
    view(vnode) {
      const { padding = "default", maxWidth = "max-w-[420px]" } = vnode.attrs;
      const paddingClass = padding === "default" ? "p-8" : "p-6";
      return m(
        "div",
        {
          class: "min-h-full flex items-center justify-center p-4",
          ...splitAttrs(vnode.attrs, ["padding", "maxWidth"]),
        },
        m(
          "div",
          { class: paddingClass + " " + maxWidth + " w-full" },
          vnode.children,
        ),
      );
    },
  };
}

interface SectionHeaderAttrs extends m.Attributes {
  divider?: boolean;
  extra?: string;
}

/** Small section header for settings-style pages (SectionHeader.jinja). */
export function SectionHeader(): m.Component<SectionHeaderAttrs> {
  return {
    view(vnode) {
      const { divider = false, extra = "" } = vnode.attrs;
      const dividerClass = divider ? "mt-8 pt-4 border-t border-default" : "";
      return m(
        "h2",
        {
          class: "type-label text-secondary mb-3 " + dividerClass + " " + extra,
          ...splitAttrs(vnode.attrs, ["divider", "extra"]),
        },
        vnode.children,
      );
    },
  };
}

interface CopyFieldAttrs extends m.Attributes {
  value: string;
  extra?: string;
}

/**
 * Read-only monospace value in a subtle-fill box (CopyField.jinja); the
 * input selects itself on click so the value is easy to copy. Pass trailing
 * controls (e.g. a Copy button) as children.
 */
export function CopyField(): m.Component<CopyFieldAttrs> {
  return {
    view(vnode) {
      const { value, extra = "" } = vnode.attrs;
      return m(
        "div",
        {
          class:
            "flex gap-2 items-center bg-fill-subtle border border-default rounded-md px-3 py-2 " +
            extra,
        },
        [
          m("input", {
            type: "text",
            readonly: true,
            value,
            onclick: (event: Event) =>
              (event.target as HTMLInputElement).select(),
            class:
              "flex-1 bg-transparent border-0 type-body text-primary font-mono outline-none",
            ...splitAttrs(vnode.attrs, ["value", "extra"]),
          }),
          vnode.children,
        ],
      );
    },
  };
}
