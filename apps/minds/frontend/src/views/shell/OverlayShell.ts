// The one overlay. Every surface the shell floats over the page -- Minds
// settings, Accounts, the AI-keys dialog, the New machine stepper, the docked
// machine-options panel, the permission-request popup, the notification feed,
// Get help -- is this component with a different `placement` and a different
// card class. There is one backdrop, one card, one close X, and one raised
// titlebar strip, written once.
//
// Three placements, which is the whole of what those surfaces disagree about:
//
//   centered  a card in the middle of the window, for the surfaces the
//             titlebar has no icon for.
//   docked    hung from the machine tab strip's left edge, running wide -- the
//             options panel and the request popup, which is that panel's
//             window showing a request.
//   anchored  hung from the right-hand icon pair at one fixed width, so the
//             feed and Get help swap inside a box that does not move.
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
import {
  animateCardResize,
  holdSize,
  isContentPending,
  measureCardBox,
  releaseSize,
} from "./card-resize";

export type OverlayPlacement = "centered" | "docked" | "anchored";

/** Gutter the docked card keeps from either window edge. */
const DOCKED_MIN_GUTTER_PX = 24;

/** How far left of the tab strip the docked card's edge sits, so it reads as
 * hanging from the tabs rather than floating loose beside them. */
const DOCKED_OVERHANG_PX = 20;

/** Gutter the anchored panel keeps from the window's right edge. */
const ANCHORED_MIN_GUTTER_PX = 8;

/** The width BOTH anchored surfaces take, and the icon they both right-align
 * to. The feed and Get help are one window shown two ways -- you switch by
 * clicking the other icon, without clicking out -- so a switch must not move
 * or resize the box under the cursor. The bug button is the rightmost of the
 * pair, so aligning to it puts the panel's right edge under the strip's. */
export const ANCHORED_CARD_CLASS = "w-[400px] min-h-0";
const ANCHORED_ALIGN_ICON: TitlebarPopupId = "help";

const CARD_BASE =
  "pointer-events-auto relative flex flex-col bg-surface-primary text-primary " +
  "rounded-xl shadow-overlay overflow-hidden";

export interface OverlayShellAttrs {
  shell: ShellState;
  placement: OverlayPlacement;
  /** The titlebar icon this surface belongs to: drawn raised and filled, and
   * clicking it puts the surface away. Null for the surfaces the titlebar has
   * no icon for, which raise no strip at all. */
  selected: TitlebarPopupId | null;
  panelId: string;
  backdropId: string;
  cardClass: string;
  bodyClass: string;
  onDismiss: () => void;
  /** DOM id for the close X, where a caller's tests name it. */
  closeButtonId?: string;
  /** Overrides where a raised icon leads. The options panel takes this: within
   * the panel a tab switch is a param change that keeps the group and section
   * the other tabs were left on, not a fresh open. */
  onSelectIcon?: (id: TitlebarPopupId) => void;
  /** The request popup's box lifecycle: the card starts held at the options
   * panel's box (the window it takes over), then resizes into whatever its
   * content asks for as the request loads and the Adjust editor opens. Only
   * the popup sets this; the shell's one overlay slot flips it off when the
   * slot switches surface, which also releases any held size. */
  animatesBox?: boolean;
}

/** What a placement adds to the caller's own `cardClass`. Deliberately no
 * `max-w-*` on the docked placement: its two surfaces cap their width
 * differently (the options panel stops at 880px, the request popup fills), and
 * a bound here would win over theirs. */
const PLACEMENT_BOUNDS: Record<OverlayPlacement, string> = {
  centered: " max-w-full max-h-[calc(100%-64px)] border border-subtle",
  docked: " max-h-full",
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
  /** The size the window is being held at while the request loads, or null
   * when the card is sizing itself. */
  let held: CardBox | null = null;
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
    resize = animateCardResize(card, previous, boxes.content);
    if (resize !== null)
      resize.addEventListener("finish", () => (resize = null));
  }

  /** Advance the box lifecycle for this render, from oncreate and onupdate. */
  function stepBoxAnimation(attrs: OverlayShellAttrs): void {
    const card = document.getElementById(attrs.panelId);
    if (attrs.animatesBox !== true) {
      // The slot switched to a surface that sizes itself: the card node
      // SURVIVES that switch (that is the slot's point), so any sizing the
      // popup left on it -- a held width/height, a mid-flight animation --
      // must come off or the new surface draws at the popup's size.
      if (wasBoxAnimated) {
        resize?.cancel();
        resize = null;
        if (held !== null && card !== null) releaseSize(card);
        held = null;
        lastBox = null;
      }
      wasBoxAnimated = false;
      return;
    }
    const isOpening = !wasBoxAnimated;
    wasBoxAnimated = true;
    if (card === null) return;
    if (isOpening) {
      // Opened over the options panel (a "Waiting on you" row), the popup
      // takes that window over, so it starts at the panel's box and shrinks
      // into its own -- the panel is still mounted behind, so it is there to
      // measure. Opened from anywhere else there is no window to take over,
      // and the popup simply appears at its size.
      const panel = document.getElementById("ws-options-panel");
      const openedFrom = panel === null ? null : measureCardBox(panel);
      // Nothing to shrink INTO yet while the request is still loading: sizing
      // to the spinner would shrink past the answer and grow back into it.
      // The window holds the size it was opened at until the request lands,
      // and then makes that trip once.
      if (openedFrom !== null && isContentPending(card)) {
        holdSize(card, openedFrom);
        held = openedFrom;
        return;
      }
      resizeInto(card, openedFrom);
      return;
    }
    if (held !== null) {
      if (isContentPending(card)) return;
      const openedFrom = held;
      held = null;
      releaseSize(card);
      resizeInto(card, openedFrom);
      return;
    }
    resizeInto(card, lastBox);
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
    const { shell, selected, onSelectIcon } = attrs;
    return [
      m(
        `div#${attrs.panelId}`,
        {
          class: CARD_BASE + " " + attrs.cardClass + extraCardClass,
          style: cardStyle,
        },
        [
          m(DialogCloseButton, {
            id: attrs.closeButtonId,
            onClose: attrs.onDismiss,
          }),
          m("div", { class: attrs.bodyClass }, children),
        ],
      ),
      selected === null
        ? null
        : m(RaisedTitlebarIcons, {
            anchors,
            selected,
            onDismiss: attrs.onDismiss,
            onSelect:
              onSelectIcon ?? ((id) => openTitlebarPopup(shell, id)),
            unresolvedCount: shell.stores.notifications.unresolvedCount,
            hasWorkspaceRequestDot:
              shell.stores.notifications.hasUnresolvedForWorkspace(
                shell.displayedWorkspaceAgentId(),
              ),
            agentId: shell.displayedWorkspaceAgentId(),
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
      held = null;
    },
    view(vnode) {
      const attrs = vnode.attrs;
      const children = vnode.children;
      // The strip's left edge is the key tab's, which is what a docked card
      // hangs from; the anchored pair shares one row, so either rect gives it.
      const anchor =
        attrs.placement === "docked"
          ? anchors.stripRect()
          : attrs.placement === "anchored"
            ? anchors.rectOf(ANCHORED_ALIGN_ICON)
            : null;

      let region: m.Children;
      if (attrs.placement === "centered" || anchor === null) {
        region = centered(attrs, children);
      } else if (attrs.placement === "docked") {
        const gutterPx = Math.max(
          DOCKED_MIN_GUTTER_PX,
          Math.round(anchor.x - DOCKED_OVERHANG_PX),
        );
        region = m(
          "div",
          {
            class:
              "fixed left-0 right-0 bottom-3 flex items-start justify-start pointer-events-none",
            style: `top: ${anchor.y + anchor.height}px; padding-left: ${gutterPx}px; padding-right: ${DOCKED_MIN_GUTTER_PX}px`,
          },
          cardAndStrip(attrs, children, PLACEMENT_BOUNDS.docked, undefined),
        );
      } else {
        const rightPx = Math.max(
          ANCHORED_MIN_GUTTER_PX,
          Math.round(window.innerWidth - (anchor.x + anchor.width)),
        );
        // Square the top-right corner only when the icon standing at it is the
        // selected one -- then panel and icon join into one shape with a tab.
        // Under the other icon of the pair that corner has an unselected,
        // titlebar-coloured button above it and nothing to join, so it stays
        // rounded like the rest of the card.
        const corner =
          attrs.selected === ANCHORED_ALIGN_ICON ? " rounded-tr-none" : "";
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
