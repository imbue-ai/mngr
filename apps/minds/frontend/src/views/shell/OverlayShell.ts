// The one overlay. Every surface the shell floats over the page -- Settings,
// the AI-keys dialog, the New workspace stepper, the permission-request popup,
// the notification feed, Get help -- is this component with a different
// `placement` and a different card class. There is one backdrop, one card,
// and one raised titlebar strip, written once.
//
// Two placements, which is the whole of what those surfaces disagree about:
//
//   centered  a card in the middle of the window, with a close X, for the
//             surfaces the titlebar has no button for.
//   anchored  a flyout hung from one of the bar's right-hand buttons and
//             joined to it, with no close X: it is put away by its own
//             button, by Escape, or by a click on the dimmed window.
//
// A placement that needs the titlebar and cannot find it (a cold-start deep
// link, or vitest's node environment) falls back to centered rather than
// drawing at a guessed rect.

import m from "mithril";
import type { ShellState } from "./shell-state";
import type { TitlebarPopupId } from "./RaisedTitlebarIcons";
import {
  RaisedTitlebarIcons,
  openTitlebarPopup,
  titlebarAnchors,
} from "./RaisedTitlebarIcons";
import { OverlayBackdrop } from "./OverlayBackdrop";
import { DialogCloseButton } from "../components/Modal";
import type { CardBox } from "./card-resize";
import { animateCardResize, measureCardBox } from "./card-resize";

export type OverlayPlacement = "centered" | "anchored";

/** Gutter an anchored flyout keeps from the window's right edge. */
const ANCHORED_MIN_GUTTER_PX = 8;

/** The width the feed and Get help both take. They are one window shown two
 * ways -- you switch by clicking the other button, without clicking out -- so
 * a switch must not move or resize the box under the cursor: both right-align
 * to the bug button, the rightmost of the pair. */
export const ANCHORED_CARD_CLASS = "w-[400px] min-h-0";

/** Marks the card's body, which is its scroll container when it has one. */
const OVERLAY_BODY_ATTR = "data-overlay-body";

const CARD_BASE =
  "pointer-events-auto relative flex flex-col bg-surface-primary text-primary " +
  "rounded-xl shadow-overlay overflow-hidden";

export interface OverlayShellAttrs {
  shell: ShellState;
  placement: OverlayPlacement;
  /** The bar button this surface belongs to: drawn raised and filled, and
   * clicking it puts the surface away. Null for the surfaces the bar has no
   * button for, which raise no strip at all. */
  selected: TitlebarPopupId | null;
  /** The bar button an anchored flyout right-aligns to; its own (`selected`) unless it shares its box with
   * another flyout. */
  alignTo?: TitlebarPopupId;
  panelId: string;
  backdropId: string;
  cardClass: string;
  bodyClass: string;
  onDismiss: () => void;
  /** DOM id for the close X, where a caller's tests name it. */
  closeButtonId?: string;
  /** What the close X says, for a surface that names itself ("Close sharing").
   * Absent leaves the shared control's plain "Close". */
  closeLabel?: string;
  /** The request popup's box lifecycle: the card resizes into whatever its
   * content asks for as the request loads and the Adjust editor opens. Only
   * the popup sets this; the shell's one overlay slot flips it off when the
   * slot switches surface, which also releases any held size. */
  animatesBox?: boolean;
}

/** What a placement adds to the caller's own `cardClass`. */
const PLACEMENT_BOUNDS: Record<OverlayPlacement, string> = {
  centered: " max-w-full max-h-[calc(100%-64px)] border border-subtle",
  anchored: " max-w-[calc(100%-16px)]",
};

export function OverlayShell(): m.Component<OverlayShellAttrs> {
  // The titlebar persists across an open, so on the common in-app open it is
  // already mounted and measurable before the first paint (no centered flash);
  // the hooks re-measure for a cold-start deep link and for a late-loading
  // workspace name that shifts the strip.
  const anchors = titlebarAnchors();

  // -- The `animatesBox` lifecycle (the request popup's). One instance of this
  // component hosts every surface in turn (see Shell's overlay slot), so the
  // state tracks whether the surface CURRENTLY shown animates, and tears its
  // sizing down when the slot switches to one that does not.

  /** The card's last settled size, so a change (the request landing after its
   * spinner, the Adjust editor opening) grows out of what was on screen. */
  let lastBox: CardBox | null = null;
  /** The resize in flight, if any. Held so an interrupting change animates
   * from where the card visually IS rather than from where it was headed. */
  let resize: Animation | null = null;
  /** Whether the previous render's surface animated its box, so a flip of
   * `animatesBox` is seen as the popup opening in (or leaving) the slot. */
  let wasBoxAnimated = false;

  /** The card's box as drawn and as its content wants it -- the same box
   * unless a resize is mid-flight, which is cancelled here so the content
   * size can be measured (an animated element measures at its interpolated
   * size). `wasResizing` says whether that happened, since the drawn box is
   * then the only record of where the card visually was. */
  function boxesOf(
    card: HTMLElement,
  ): { drawn: CardBox; content: CardBox; wasResizing: boolean } | null {
    const drawn = measureCardBox(card);
    if (drawn === null) return null;
    if (resize === null) return { drawn, content: drawn, wasResizing: false };
    resize.cancel();
    resize = null;
    const content = measureCardBox(card);
    return content === null ? null : { drawn, content, wasResizing: true };
  }

  /** Take the card to whatever size its content now asks for, animating from
   * `from` (the previous size, or the surface the popup was opened over). */
  function resizeInto(card: HTMLElement, from: CardBox | null): void {
    const boxes = boxesOf(card);
    if (boxes === null) return;
    // A running resize was just cancelled to measure, and cancelling hands the
    // card straight to its content size -- so the trip has to be picked up from
    // where the card visually WAS, not from where the last one started. Without
    // this, any redraw landing inside the 200ms (the list load resolving, a
    // channel frame) ends the animation early and snaps the card to size:
    // `from` would be the size the cancelled run was already headed for, which
    // is no distance at all.
    const previous = boxes.wasResizing ? boxes.drawn : from;
    lastBox = boxes.content;
    if (previous === null) return;
    resize = animateCardResize(
      card,
      previous,
      boxes.content,
      card.querySelector<HTMLElement>(`:scope > [${OVERLAY_BODY_ATTR}]`),
    );
    if (resize !== null)
      resize.addEventListener("finish", () => (resize = null));
  }

  /** Advance the box lifecycle for this render, from oncreate and onupdate. */
  function stepBoxAnimation(attrs: OverlayShellAttrs): void {
    const card = document.getElementById(attrs.panelId);
    if (attrs.animatesBox !== true) {
      // The slot switched to a surface that sizes itself: the card node
      // SURVIVES that switch (that is the slot's point), so any sizing the
      // popup left on it (a mid-flight animation) must come off or the new
      // surface draws at the popup's size.
      if (wasBoxAnimated) {
        resize?.cancel();
        resize = null;
        lastBox = null;
      }
      wasBoxAnimated = false;
      return;
    }
    const isOpening = !wasBoxAnimated;
    wasBoxAnimated = true;
    if (card === null) return;
    // The popup appears at its size, and from then on grows out of what was on screen.
    resizeInto(card, isOpening ? null : lastBox);
  }

  /** The card, and the raised strip that belongs to it. Painted in that order:
   * the card's `shadow-overlay` halo would otherwise bleed onto the bottom of
   * the opaque raised buttons and read as a darker shade right at the seam. */
  function cardAndStrip(
    attrs: OverlayShellAttrs,
    children: m.Children,
    extraCardClass: string,
    cardStyle: string | undefined,
  ): m.Children {
    const { shell, selected } = attrs;
    return [
      m(
        `div#${attrs.panelId}`,
        {
          class: CARD_BASE + " " + attrs.cardClass + extraCardClass,
          style: cardStyle,
        },
        [
          // A flyout is put away by its own button; only a centered card needs an X.
          attrs.placement === "centered"
            ? m(DialogCloseButton, {
                id: attrs.closeButtonId,
                onClose: attrs.onDismiss,
                ...(attrs.closeLabel === undefined
                  ? {}
                  : {
                      "aria-label": attrs.closeLabel,
                      "data-tooltip": attrs.closeLabel,
                    }),
              })
            : null,
          m("div", { class: attrs.bodyClass, [OVERLAY_BODY_ATTR]: "" }, children),
        ],
      ),
      selected === null
        ? null
        : m(RaisedTitlebarIcons, {
            shell,
            anchors,
            selected,
            onDismiss: attrs.onDismiss,
            onSelect: (id) => openTitlebarPopup(shell, id),
          }),
    ];
  }

  function centered(attrs: OverlayShellAttrs, children: m.Children): m.Children {
    return m(
      "div",
      {
        class:
          "fixed inset-0 flex items-center justify-center p-4 pointer-events-none",
      },
      cardAndStrip(attrs, children, PLACEMENT_BOUNDS.centered, undefined),
    );
  }

  return {
    oncreate(vnode) {
      anchors.remeasure();
      stepBoxAnimation(vnode.attrs);
    },
    onupdate(vnode) {
      anchors.remeasure();
      stepBoxAnimation(vnode.attrs);
    },
    onremove() {
      if (resize !== null) resize.cancel();
      resize = null;
    },
    view(vnode) {
      const attrs = vnode.attrs;
      const children = vnode.children;
      const alignTo = attrs.alignTo ?? attrs.selected;
      const anchor = attrs.placement === "anchored" && alignTo !== null ? anchors.rectOf(alignTo) : null;

      let region: m.Children;
      if (anchor === null) {
        region = centered(attrs, children);
      } else {
        const rightPx = Math.max(
          ANCHORED_MIN_GUTTER_PX,
          Math.round(window.innerWidth - (anchor.x + anchor.width)),
        );
        // Square the top-right corner only when the button standing at it is
        // the selected one -- then flyout and button join into one shape with
        // a tab. Under the other button of a shared box that corner has an
        // unselected, bar-coloured button above it and nothing to join, so it
        // stays rounded like the rest of the card.
        const corner = attrs.selected === alignTo ? " rounded-tr-none" : "";
        region = m(
          "div",
          { class: "fixed inset-0 pointer-events-none" },
          cardAndStrip(
            attrs,
            children,
            PLACEMENT_BOUNDS.anchored + corner,
            `position: fixed; top: ${anchor.y + anchor.height}px; right: ${rightPx}px`,
          ),
        );
      }

      return m(
        OverlayBackdrop,
        {
          backdropId: attrs.backdropId,
          fullWindow: true,
          onDismiss: attrs.onDismiss,
        },
        region,
      );
    },
  };
}
