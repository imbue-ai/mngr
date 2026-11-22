// The shared scaffold under every Shell overlay (Settings, Get help, the
// notification feed, the request popup and the other centered cards): a dim
// click-away backdrop calling onDismiss. Callers render the card as children,
// so the backdrop geometry lives in one place.
//
// Escape is not handled here. Every one of these overlays is a surface the
// shell itself opens and closes, and it is the shell that knows which is on
// top -- see ShellState.handleEscape. A listener per overlay would order the
// key by mount order instead.

import m from "mithril";

export interface OverlayBackdropAttrs {
  onDismiss: () => void;
  backdropId: string;
  // Cover the whole window: dim over the titlebar too (z above its z-100)
  // and lay the card out from the top of the full height. The default stops
  // below the titlebar and centers its card there.
  fullWindow?: boolean;
}

export function OverlayBackdrop(): m.Component<OverlayBackdropAttrs> {
  return {
    view(vnode) {
      const positionClass = vnode.attrs.fullWindow
        ? "inset-0 z-[110]"
        : "left-0 right-0 top-(--titlebar-height) bottom-0 z-[90]";
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
