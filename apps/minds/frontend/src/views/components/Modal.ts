import m from "mithril";
import { Icon16 } from "./Icon";
import { splitAttrs } from "./attrs";

interface ModalAttrs extends m.Attributes {
  isOpen: boolean;
  onClose?: () => void;
  cardExtra?: string;
}

/**
 * In-DOM overlay modal: fixed dimming backdrop centering an inner card.
 * The SPA port of Modal.jinja, with visibility driven by `isOpen` instead of
 * a hidden-class toggle (there is no overlay-iframe layer anymore -- every
 * modal is a plain component). Clicking the backdrop (not the card) invokes
 * onClose when provided.
 */
export function Modal(): m.Component<ModalAttrs> {
  return {
    view(vnode) {
      const { isOpen, onClose, cardExtra = "" } = vnode.attrs;
      if (!isOpen) {
        return null;
      }
      return m(
        "div",
        {
          class:
            "fixed inset-0 z-50 flex items-center justify-center bg-surface-overlay",
          onclick: (event: MouseEvent) => {
            if (onClose !== undefined && event.target === event.currentTarget) {
              onClose();
            }
          },
          ...splitAttrs(vnode.attrs, ["isOpen", "onClose", "cardExtra"]),
        },
        m(
          "div",
          {
            class:
              "bg-surface-primary rounded-lg shadow-overlay border border-default max-w-sm w-full mx-4 p-6 " +
              cardExtra,
          },
          vnode.children,
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
            "absolute top-3 right-3 z-10 inline-flex items-center justify-center w-8 h-8 rounded-md text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
          ...splitAttrs(vnode.attrs, ["onClose"]),
        },
        m(Icon16, { name: "close", size: "lg" }),
      );
    },
  };
}
