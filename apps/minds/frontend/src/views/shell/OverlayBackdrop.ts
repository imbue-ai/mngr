// The shared modal scaffold used by every Shell overlay (the workspace options
// panel, the app-level Minds settings / Accounts / Get help modals, and the
// Requests inbox drawer): a dim click-away backdrop plus a document-level
// Escape handler, both calling onDismiss. Callers render the card / panel /
// drawer as children, so the backdrop geometry and Esc wiring live in one place.

import m from "mithril";

export interface OverlayBackdropAttrs {
  onDismiss: () => void;
  backdropId: string;
  // App-level modals cover the whole window -- dim over the titlebar too and
  // centered in the full height, like the legacy full-window settings modal
  // (z above the titlebar's z-100). The workspace options overlay stays below
  // the titlebar (default) so its titlebar tabs remain clickable.
  fullWindow?: boolean;
  // "center" (default) centers a card; "start" lays a full-height panel flush
  // against the start edge (the inbox drawer) with no centering or padding.
  align?: "center" | "start";
}

export function OverlayBackdrop(): m.Component<OverlayBackdropAttrs> {
  let onKeyDown: ((event: KeyboardEvent) => void) | null = null;

  return {
    oncreate(vnode) {
      onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== "Escape") return;
        event.stopPropagation();
        vnode.attrs.onDismiss();
        m.redraw();
      };
      document.addEventListener("keydown", onKeyDown);
    },
    onremove() {
      if (onKeyDown !== null) document.removeEventListener("keydown", onKeyDown);
      onKeyDown = null;
    },
    view(vnode) {
      const positionClass = vnode.attrs.fullWindow
        ? "inset-0 z-[110]"
        : "left-0 right-0 top-[38px] bottom-0 z-[90]";
      // Centered card (default) vs a full-height panel flush to the start edge
      // (the inbox drawer): the latter drops the centering and the padding.
      const alignClass =
        vnode.attrs.align === "start" ? "flex justify-start" : "flex items-center justify-center p-4";
      return m(
        "div",
        {
          id: vnode.attrs.backdropId,
          class: "fixed " + positionClass + " bg-black/20 " + alignClass,
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) vnode.attrs.onDismiss();
          },
        },
        vnode.children,
      );
    },
  };
}
