import { afterEach, describe, expect, it, vi } from "vitest";
import { animateCardResize, isResizeWorthAnimating, measureCardBox } from "./card-resize";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A running animation that can be cancelled, telling whoever listens for it. */
function fakeAnimation(): { animation: Animation; isCancelled: () => boolean } {
  const cancelListeners: (() => void)[] = [];
  let isCancelled = false;
  const animation = {
    addEventListener: (type: string, listener: () => void) => {
      if (type === "cancel") cancelListeners.push(listener);
    },
    cancel: () => {
      isCancelled = true;
      for (const listener of cancelListeners) listener();
    },
  } as unknown as Animation;
  return { animation, isCancelled: () => isCancelled };
}

/** An element that reports `box` and records what it was asked to animate. */
function fakeCard(
  box: { width: number; height: number },
  options: { canAnimate?: boolean } = {},
): { element: HTMLElement; keyframes: () => Keyframe[] | null } {
  let keyframes: Keyframe[] | null = null;
  const element = {
    getBoundingClientRect: () => ({ width: box.width, height: box.height }) as DOMRect,
    ...(options.canAnimate === false
      ? {}
      : {
          animate: (frames: Keyframe[]) => {
            keyframes = frames;
            return fakeAnimation().animation;
          },
        }),
  } as unknown as HTMLElement;
  return { element, keyframes: () => keyframes };
}

/** A card's scroll container, recording what it was asked to animate and whether that was cancelled. */
function fakeScroller(): { element: HTMLElement; keyframes: () => Keyframe[] | null; isCancelled: () => boolean } {
  let keyframes: Keyframe[] | null = null;
  const running = fakeAnimation();
  const element = {
    animate: (frames: Keyframe[]) => {
      keyframes = frames;
      return running.animation;
    },
  } as unknown as HTMLElement;
  return { element, keyframes: () => keyframes, isCancelled: running.isCancelled };
}

/** The media query the helper asks about, answering `matches` to every query. */
function stubReducedMotion(matches: boolean): void {
  vi.stubGlobal("window", { matchMedia: () => ({ matches }) });
}

describe("isResizeWorthAnimating", () => {
  it("animates a change big enough to see", () => {
    expect(isResizeWorthAnimating({ width: 880, height: 660 }, { width: 600, height: 420 })).toBe(true);
    // One dimension is enough: the popup keeps its width while its content lands.
    expect(isResizeWorthAnimating({ width: 600, height: 110 }, { width: 600, height: 420 })).toBe(true);
  });

  it("leaves a sub-pixel change alone", () => {
    // Every redraw asks, so the no-op answer is what keeps that check cheap.
    expect(isResizeWorthAnimating({ width: 600, height: 420 }, { width: 600, height: 420 })).toBe(false);
    expect(isResizeWorthAnimating({ width: 600, height: 420 }, { width: 600.4, height: 420.2 })).toBe(false);
  });

  it("refuses a box that is not laid out", () => {
    // Animating from or to nothing would flatten the card for a frame, which is
    // worse than the jump it is meant to smooth.
    expect(isResizeWorthAnimating({ width: 0, height: 0 }, { width: 600, height: 420 })).toBe(false);
    expect(isResizeWorthAnimating({ width: 600, height: 420 }, { width: 600, height: 0 })).toBe(false);
  });
});

describe("measureCardBox", () => {
  it("reports the laid-out box", () => {
    const { element } = fakeCard({ width: 600, height: 420 });
    expect(measureCardBox(element)).toEqual({ width: 600, height: 420 });
  });

  it("reports nothing for an element with no box yet", () => {
    const { element } = fakeCard({ width: 0, height: 0 });
    expect(measureCardBox(element)).toBeNull();
  });
});

describe("animateCardResize", () => {
  it("animates the box in pixels, from the old size to the new", () => {
    // Pixels on both ends on purpose: the size it animates TO is `auto`, which
    // is why this cannot be a CSS transition.
    stubReducedMotion(false);
    const { element, keyframes } = fakeCard({ width: 600, height: 420 });

    const animation = animateCardResize(element, { width: 880, height: 660 }, { width: 600, height: 420 }, null);

    expect(animation).not.toBeNull();
    expect(keyframes()).toEqual([
      { width: "880px", height: "660px" },
      { width: "600px", height: "420px" },
    ]);
  });

  it("does nothing when the reader asked for less motion", () => {
    stubReducedMotion(true);
    const { element, keyframes } = fakeCard({ width: 600, height: 420 });

    expect(animateCardResize(element, { width: 880, height: 660 }, { width: 600, height: 420 }, null)).toBeNull();
    expect(keyframes()).toBeNull();
  });

  it("does nothing on a host with no Web Animations API", () => {
    // The card is already at its new size in that case, so skipping is the
    // whole fallback -- there is nothing to undo.
    stubReducedMotion(false);
    const { element } = fakeCard({ width: 600, height: 420 }, { canAnimate: false });

    expect(animateCardResize(element, { width: 880, height: 660 }, { width: 600, height: 420 }, null)).toBeNull();
  });

  it("skips a change too small to see", () => {
    stubReducedMotion(false);
    const { element, keyframes } = fakeCard({ width: 600, height: 420 });

    expect(animateCardResize(element, { width: 600, height: 420 }, { width: 600, height: 420 }, null)).toBeNull();
    expect(keyframes()).toBeNull();
  });

  it("keeps the card's scroller from scrolling while the card grows, so no scrollbar flashes on the way", () => {
    stubReducedMotion(false);
    const { element } = fakeCard({ width: 600, height: 420 });
    const scroller = fakeScroller();

    animateCardResize(element, { width: 600, height: 360 }, { width: 600, height: 420 }, scroller.element);

    expect(scroller.keyframes()).toEqual([{ overflowY: "hidden" }, { overflowY: "hidden" }]);
  });

  it("hands the scroller back when the resize is cancelled mid-flight", () => {
    stubReducedMotion(false);
    const { element } = fakeCard({ width: 600, height: 420 });
    const scroller = fakeScroller();

    const animation = animateCardResize(
      element,
      { width: 600, height: 360 },
      { width: 600, height: 420 },
      scroller.element,
    );
    expect(scroller.isCancelled()).toBe(false);
    animation?.cancel();

    expect(scroller.isCancelled()).toBe(true);
  });

  it("leaves the scroller alone while the card shrinks or only changes width", () => {
    // A card that is not growing taller is never shorter than its content, and
    // a scrollbar it already has is one the content needs.
    stubReducedMotion(false);
    const { element } = fakeCard({ width: 600, height: 420 });
    const scroller = fakeScroller();

    animateCardResize(element, { width: 880, height: 660 }, { width: 600, height: 420 }, scroller.element);
    animateCardResize(element, { width: 880, height: 420 }, { width: 600, height: 420 }, scroller.element);

    expect(scroller.keyframes()).toBeNull();
  });
});
