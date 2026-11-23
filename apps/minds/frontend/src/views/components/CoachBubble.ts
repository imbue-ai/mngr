// A coach bubble: a small card with an arrow, a line of text and one button,
// pointing at a control the reader was just led to. It says what the control
// is for; the button sends it away.
//
// The bubble is drawn as the last child of a `relative` element that wraps the
// control, so it moves with the control when the pane scrolls or resizes and
// needs no positioning of its own. It sits below the control when it fits in
// the part of the window the control can be seen in, and above otherwise.

import m from "mithril";
import { Button } from "./Button";
import { joinClasses, splitAttrs } from "./attrs";

export type CoachBubbleSide = "below" | "above";

export const COACH_BUBBLE_DISMISS_LABEL = "Got it";

/** The gap between the control and the bubble, which the arrow spans: what
 * the `mt-2.5` / `mb-2.5` of the side classes measure. */
const GAP_PX = 10;

const BUBBLE_CLASS =
  "absolute left-0 z-10 flex w-max max-w-[320px] flex-col items-start gap-2 " +
  "rounded-lg border border-strong bg-surface-primary px-3 py-2.5 " +
  "type-body text-primary shadow-overlay";

const SIDE_CLASS: Record<CoachBubbleSide, string> = {
  below: "top-full mt-2.5",
  above: "bottom-full mb-2.5",
};

// A square turned on its corner, half out of the edge that faces the control,
// bordered on the two sides that show.
const ARROW_CLASS: Record<CoachBubbleSide, string> = {
  below:
    "absolute left-4 -top-[6px] size-2.5 rotate-45 border-t border-l border-strong bg-surface-primary",
  above:
    "absolute left-4 -bottom-[6px] size-2.5 rotate-45 border-b border-r border-strong bg-surface-primary",
};

/** A stretch of the window, top to bottom, in viewport px. */
export interface VerticalSpan {
  top: number;
  bottom: number;
}

/**
 * The side of `anchor` a bubble `bubbleHeight` tall sits on: below when it
 * fits inside `bounds` there, above when it only fits there, and below when it
 * fits on neither side, where the pane can at least be scrolled to it.
 */
export function coachBubbleSide(
  anchor: VerticalSpan,
  bubbleHeight: number,
  bounds: VerticalSpan,
): CoachBubbleSide {
  const needed = bubbleHeight + GAP_PX;
  if (anchor.bottom + needed <= bounds.bottom) return "below";
  return anchor.top - needed >= bounds.top ? "above" : "below";
}

/** The `overflow-y` values under which an element hides what runs past it. */
const CLIPPING_OVERFLOWS = ["auto", "scroll", "hidden", "clip"];

/** The part of the window `element` can be seen in: the viewport, narrowed by
 * every ancestor that scrolls or clips what is inside it. */
function visibleSpan(element: Element): VerticalSpan {
  const span: VerticalSpan = { top: 0, bottom: window.innerHeight };
  for (
    let ancestor = element.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    if (!CLIPPING_OVERFLOWS.includes(getComputedStyle(ancestor).overflowY))
      continue;
    const rect = ancestor.getBoundingClientRect();
    span.top = Math.max(span.top, rect.top);
    span.bottom = Math.min(span.bottom, rect.bottom);
  }
  return span;
}

export interface CoachBubbleAttrs extends m.Attributes {
  text: string;
  /** The button was pressed. The owner stops drawing the bubble; it also
   * stops when the control itself is used, which only the owner can see. */
  onDismiss: () => void;
}

const OWN_KEYS = ["text", "onDismiss"] as const;

export function CoachBubble(): m.Component<CoachBubbleAttrs> {
  let side: CoachBubbleSide = "below";
  let bubble: HTMLElement | null = null;

  function chooseSide(): void {
    const anchor = bubble?.parentElement ?? null;
    if (bubble === null || anchor === null) return;
    const next = coachBubbleSide(
      anchor.getBoundingClientRect(),
      bubble.offsetHeight,
      visibleSpan(anchor),
    );
    if (next === side) return;
    side = next;
    m.redraw();
  }

  return {
    oncreate(vnode) {
      bubble = vnode.dom as HTMLElement;
      window.addEventListener("resize", chooseSide);
      // Capture, so a scroll of the pane the control sits in counts.
      window.addEventListener("scroll", chooseSide, true);
      chooseSide();
    },
    onremove() {
      window.removeEventListener("resize", chooseSide);
      window.removeEventListener("scroll", chooseSide, true);
      bubble = null;
    },
    view(vnode) {
      const { text, onDismiss } = vnode.attrs;
      return m(
        "div",
        {
          role: "note",
          "data-coach-bubble": side,
          class: joinClasses(BUBBLE_CLASS, SIDE_CLASS[side]),
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        [
          m("span", { "aria-hidden": "true", class: ARROW_CLASS[side] }),
          m("p", text),
          m(
            Button,
            { variant: "primary", size: "xs", onclick: onDismiss },
            COACH_BUBBLE_DISMISS_LABEL,
          ),
        ],
      );
    },
  };
}
