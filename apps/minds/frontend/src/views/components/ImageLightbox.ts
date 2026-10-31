// A full-screen viewer for an image a page renders inline, opened by clicking
// it: the image enlarged over a dimming backdrop, its title centered over it,
// and a close X. Closed by the backdrop, the X, or Escape -- the shell's
// in-document handler consumes the key only when one of its own surfaces
// takes it, so an open shell overlay closes first and the next press reaches
// this. The workspace's chat app has its own viewer (the template's
// lightbox.ts); this is the SPA's, for the images it draws itself.

import m from "mithril";
import { Icon16 } from "./Icon";

export interface ImageLightboxAttrs extends m.Attributes {
  imageUrl: string;
  title: string;
  onClose: () => void;
}

export const ImageLightbox: m.ClosureComponent<ImageLightboxAttrs> = () => {
  // The document listener outlives any one render, so it reads the latest
  // close handler rather than a mount-time capture.
  let onClose: () => void = () => undefined;
  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape") return;
    onClose();
    m.redraw();
  }
  function closeOnBackdrop(event: MouseEvent): void {
    if (event.target === event.currentTarget) onClose();
  }
  return {
    oncreate() {
      document.addEventListener("keydown", onKeydown);
    },
    onremove() {
      document.removeEventListener("keydown", onKeydown);
    },
    view(vnode) {
      onClose = vnode.attrs.onClose;
      const { imageUrl, title } = vnode.attrs;
      return m(
        "div",
        {
          class:
            "image-lightbox-overlay fixed inset-0 z-[110] flex flex-col bg-black/85",
          role: "dialog",
          "aria-modal": "true",
          "aria-label": title,
          onclick: closeOnBackdrop,
        },
        [
          m(
            "div",
            {
              class:
                "relative flex min-h-[44px] items-center justify-center px-4 py-2.5",
            },
            [
              m(
                "div",
                {
                  class:
                    "max-w-[60%] truncate text-center type-body font-medium text-white/90",
                },
                title,
              ),
              m(
                "button",
                {
                  type: "button",
                  "aria-label": "Close image viewer",
                  onclick: () => onClose(),
                  class:
                    "absolute top-1/2 right-3 inline-flex h-8 w-8 -translate-y-1/2 cursor-pointer items-center " +
                    "justify-center rounded-md text-white/80 hover:bg-white/10 hover:text-white",
                },
                m(Icon16, { name: "close", size: "lg" }),
              ),
            ],
          ),
          m(
            "div",
            {
              class:
                "flex min-h-0 flex-1 items-center justify-center px-8 pb-8",
              onclick: closeOnBackdrop,
            },
            m("img", {
              src: imageUrl,
              alt: title,
              class:
                "max-h-full max-w-full rounded-md object-contain shadow-overlay",
            }),
          ),
        ],
      );
    },
  };
};
