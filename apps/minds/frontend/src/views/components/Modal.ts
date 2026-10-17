import m from "mithril";
import { Icon16 } from "./Icon";
import { splitAttrs } from "./attrs";

export type ModalSize = "sm" | "md" | "lg" | "xl";

// The card's ONLY max-w-* class, picked here rather than passed in cardExtra:
// two competing max-w-* utilities are decided by their order in the generated
// stylesheet (where .max-w-sm follows .max-w-md, so the narrower one wins),
// not by the caller's class string.
const MODAL_SIZES: Record<ModalSize, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-lg",
  xl: "max-w-xl",
};

interface ModalAttrs extends m.Attributes {
  isOpen: boolean;
  onClose?: () => void;
  size?: ModalSize;
  cardExtra?: string;
}

/**
 * In-DOM overlay modal: fixed dimming backdrop centering an inner card.
 * The SPA port of Modal.jinja, with visibility driven by `isOpen` instead of
 * a hidden-class toggle (there is no overlay-iframe layer anymore -- every
 * modal is a plain component). Clicking the backdrop (not the card) invokes
 * onClose when provided.
 *
 * The card is bounded by the window, not its content: `size` is a ceiling it
 * only reaches when there is room, it stops at what .modal-viewport leaves and
 * scrolls the rest, so a tall card keeps its title and close X on screen.
 *
 * The backdrop sits above the titlebar (z-110, over its z-100) so the dim
 * covers the whole window rather than stopping at the 38px strip; style.css
 * gives .modal-viewport the matching no-drag so clicks up there still land.
 */
export function Modal(): m.Component<ModalAttrs> {
  return {
    view(vnode) {
      const { isOpen, onClose, size = "sm", cardExtra = "" } = vnode.attrs;
      if (!isOpen) {
        return null;
      }
      return m(
        "div",
        {
          class:
            "modal-viewport fixed inset-0 z-[110] flex items-center justify-center bg-surface-overlay",
          onclick: (event: MouseEvent) => {
            if (onClose !== undefined && event.target === event.currentTarget) {
              onClose();
            }
          },
          ...splitAttrs(vnode.attrs, [
            "isOpen",
            "onClose",
            "size",
            "cardExtra",
          ]),
        },
        m(
          "div",
          {
            class:
              "bg-surface-primary rounded-lg shadow-overlay border border-default w-full mx-4 overflow-hidden " +
              MODAL_SIZES[size] +
              " flex max-h-full flex-col " +
              cardExtra,
          },
          // The padding rides the scroller, not the card, so overflowing content
          // runs to the card's own bottom edge and is clipped there.
          m("div", { class: "min-h-0 overflow-y-auto p-6" }, vnode.children),
        ),
      );
    },
  };
}

interface DialogCloseButtonAttrs extends m.Attributes {
  onClose: () => void;
}

/**
 * Absolute-positioned X button at the top right of a modal dialog
 * (DialogCloseButton.jinja): the shared Icon16 close glyph at 20px in a
 * 32px hit area.
 *
 * Opaque, because the body scrolls under it.
 */
export function DialogCloseButton(): m.Component<DialogCloseButtonAttrs> {
  return {
    view(vnode) {
      return m(
        "button",
        {
          type: "button",
          "aria-label": "Close",
          "data-tooltip": "Close",
          onclick: vnode.attrs.onClose,
          class:
            "absolute top-3 right-3 z-10 inline-flex items-center justify-center w-8 h-8 rounded-md bg-surface-primary text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
          ...splitAttrs(vnode.attrs, ["onClose"]),
        },
        m(Icon16, { name: "close", size: "lg" }),
      );
    },
  };
}
