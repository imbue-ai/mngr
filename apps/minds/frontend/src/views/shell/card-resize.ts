// Size morphing for the shell's overlay cards: a card that changes size grows
// or shrinks into the new one instead of jumping to it.
//
// The cards this animates are anchored by their top-left corner (the request
// popup and the options panel both hang from the titlebar), so animating the
// box alone reads as that window resizing -- no transform, so text is never
// scaled or blurred on the way.
//
// The animation is driven through the Web Animations API rather than a CSS
// transition because the size being animated TO is `auto`: the card is sized
// by its content, which a transition cannot interpolate towards. Passing
// measured pixel boxes sidesteps that, and the default `fill: "none"` leaves
// no inline size behind, so the card is back to content-sizing the moment the
// animation lands.

/** How long a card takes to settle into a new size. */
export const CARD_RESIZE_MS = 200;

/** Decelerating: quick to leave the old size, easy into the new one. */
const CARD_RESIZE_EASING = "cubic-bezier(0.2, 0, 0, 1)";

export interface CardBox {
  width: number;
  height: number;
}

/** Whether a size change is worth animating.
 *
 * A zero box means the card is not laid out (measured before paint, or hidden),
 * and animating from or to nothing would flatten the card for a frame. A change
 * under a pixel is not visible, and animating it would cost a redraw for
 * nothing -- this is what makes the every-redraw check below cheap.
 */
export function isResizeWorthAnimating(from: CardBox, to: CardBox): boolean {
  if (from.width <= 0 || from.height <= 0 || to.width <= 0 || to.height <= 0) return false;
  return Math.abs(from.width - to.width) >= 1 || Math.abs(from.height - to.height) >= 1;
}

/** The card's laid-out box, or null when it has none yet. */
export function measureCardBox(element: HTMLElement): CardBox | null {
  const rect = element.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { width: rect.width, height: rect.height };
}

/** Whether the card is still waiting on the content that decides its size.
 *
 * The Shell asks before sizing to what is on screen: a card showing a spinner
 * would have it shrink past the answer and grow back into it, which reads as
 * two windows rather than one resizing.
 */
export function isContentPending(card: HTMLElement): boolean {
  return card.querySelector("#request-popup-loading") !== null;
}

/** Hold the card at `box`, overriding its content sizing until released. */
export function holdSize(card: HTMLElement, box: CardBox): void {
  card.style.width = `${box.width}px`;
  card.style.height = `${box.height}px`;
}

/** Hand the card back to its own sizing. */
export function releaseSize(card: HTMLElement): void {
  card.style.removeProperty("width");
  card.style.removeProperty("height");
}

function isMotionUnwanted(): boolean {
  // Optional-chained: a non-browser host (unit tests stub a bare `window`) has
  // no matchMedia, and a missing preference is not a preference against motion.
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

/**
 * Grow or shrink `element` from `from` into `to`, returning the running
 * animation (null when it was not worth animating, or motion is unwanted, or
 * the host has no Web Animations API -- in every one of those cases the card
 * is already at its new size, so there is nothing to undo).
 *
 * Cancel the returned animation to hand the element straight back to its own
 * sizing: nothing is written to `style`, so a cancel mid-flight reverts to the
 * content size rather than freezing at the interpolated one.
 */
export function animateCardResize(element: HTMLElement, from: CardBox, to: CardBox): Animation | null {
  if (!isResizeWorthAnimating(from, to)) return null;
  if (typeof element.animate !== "function") return null;
  if (isMotionUnwanted()) return null;
  return element.animate(
    [
      { width: `${from.width}px`, height: `${from.height}px` },
      { width: `${to.width}px`, height: `${to.height}px` },
    ],
    { duration: CARD_RESIZE_MS, easing: CARD_RESIZE_EASING },
  );
}
