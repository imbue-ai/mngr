// The shared modal scaffold used by every Shell overlay (the workspace options
// panel and the app-level request popup / Minds settings / Accounts / Get help
// modals): a dim click-away backdrop calling onDismiss. Callers render the card
// or panel as children, so the backdrop geometry lives in one place.
//
// Escape is not handled here. Every one of these overlays is a surface the
// shell itself opens and closes, and it is the shell that knows which is on
// top -- see ShellState.handleEscape. A listener per overlay would order the
// key by mount order instead.

import m from "mithril";

export interface OverlayBackdropAttrs {
  onDismiss: () => void;
  backdropId: string;
  // App-level modals cover the whole window -- dim over the titlebar too and
  // hang from the top of the full height, like the legacy full-window
  // settings modal (z above the titlebar's z-100). The workspace options
  // overlay stays below the titlebar (default) so its titlebar tabs remain
  // clickable, and stays centered there -- it is a docked panel, not a card
  // reading as "hung from the top" the way the app-level modals do.
  fullWindow?: boolean;
}

export function OverlayBackdrop(): m.Component<OverlayBackdropAttrs> {
  return {
    view(vnode) {
      const positionClass = vnode.attrs.fullWindow
        ? "inset-0 z-[110]"
        : "left-0 right-0 top-[38px] bottom-0 z-[90]";
      const alignClass = vnode.attrs.fullWindow
        ? "items-start"
        : "items-center";
      return m(
        "div",
        {
          id: vnode.attrs.backdropId,
          class:
            "fixed " +
            positionClass +
            " bg-black/20 flex " +
            alignClass +
            " justify-center p-4",
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) vnode.attrs.onDismiss();
          },
        },
        vnode.children,
      );
    },
  };
}
