// A placeholder block for a region whose real contents have not arrived.
//
// Use one wherever drawing the region from defaults would state something
// false: an empty list reads as "nothing here", an off switch reads as "off".
// A skeleton says only "not yet", which is the one thing that is true.

import m from "mithril";

export interface SkeletonAttrs {
  /** Tailwind sizing (and any other) classes for this block, e.g. "h-10 w-48". */
  extra?: string;
}

/**
 * One pulsing block. Hidden from the accessibility tree: its shape means
 * nothing read aloud, so the region around it carries the status instead.
 */
export function Skeleton(): m.Component<SkeletonAttrs> {
  return {
    view(vnode) {
      return m("div", {
        class: `animate-pulse rounded-md bg-fill-subtle ${vnode.attrs.extra ?? ""}`,
        "aria-hidden": "true",
      });
    },
  };
}

export interface SkeletonRegionAttrs {
  /** What is loading, announced in place of the blocks, e.g. "Loading permissions". */
  label: string;
  extra?: string;
}

/**
 * The wrapper a group of blocks stands in, which announces the wait once.
 * Readers of the accessibility tree get the label; everyone else gets shapes.
 */
export function SkeletonRegion(): m.Component<SkeletonRegionAttrs> {
  return {
    view(vnode) {
      return m(
        "div",
        {
          role: "status",
          "aria-busy": "true",
          "aria-label": vnode.attrs.label,
          class: vnode.attrs.extra ?? "",
        },
        vnode.children,
      );
    },
  };
}
