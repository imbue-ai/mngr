// Custom hover/focus tooltips for the SPA chrome.
//
// Legacy chrome loaded static/tooltip_triggers.js, which wired listeners onto
// every [data-tooltip] element found by a one-shot querySelectorAll at page
// load. That model dies under mithril: the shell re-renders its DOM on every
// route change and redraw, so elements created after boot were never wired and
// the titlebar (and every other chrome affordance) lost its tooltips.
//
// Instead we install a single set of document-level delegated listeners once at
// startup. They find the nearest [data-tooltip] ancestor of each hover/focus
// target at event time, so they cover elements that mithril mounts, unmounts,
// and remounts freely. One shared bubble is appended to <body> (escaping any
// card's overflow-hidden) and styled with the same .minds-tooltip class the
// legacy backend used, so the look is unchanged.
//
// The bubble fades + slides a hair on enter and (before leaving layout) on
// exit: the .minds-tooltip class carries the opacity/transform transition, and
// this controller toggles the from/to states, honoring prefers-reduced-motion
// via that CSS (the transition collapses to none, so the toggles are instant).

const TOOLTIP_DELAY_MS = 250; // hover-intent delay before a tooltip appears
const TOOLTIP_MARGIN = 6; // min gap from the window edges
const TOOLTIP_GAP = 6; // gap between the trigger and the bubble
const TOOLTIP_MOTION_MS = 80; // enter/leave fade+slide duration; MUST match the .minds-tooltip CSS transition
const TOOLTIP_SLIDE_PX = 2; // vertical distance the bubble slides on enter/leave

export interface Rect {
  left: number;
  top: number;
  bottom: number;
  width: number;
}

export interface Size {
  width: number;
  height: number;
}

/**
 * Where to place a tooltip bubble of `bubble` size for a trigger at `trigger`
 * within a `viewport`. Centered under the trigger and flipped above when it
 * would overflow the bottom, then clamped into the viewport with a small
 * margin. Pure (no DOM) so the positioning is unit-testable; mirrors the
 * legacy overlay_layer.js / tooltip_triggers.js math exactly.
 */
export function computeTooltipPosition(trigger: Rect, bubble: Size, viewport: Size): { left: number; top: number } {
  let left = trigger.left + trigger.width / 2 - bubble.width / 2;
  let top = trigger.bottom + TOOLTIP_GAP;
  // Flip above the trigger if the bubble would spill past the bottom edge and
  // there is room above; otherwise keep it below and let the clamp handle it.
  if (top + bubble.height > viewport.height - TOOLTIP_MARGIN) {
    const above = trigger.top - bubble.height - TOOLTIP_GAP;
    if (above >= TOOLTIP_MARGIN) top = above;
  }
  if (left + bubble.width > viewport.width - TOOLTIP_MARGIN) left = viewport.width - TOOLTIP_MARGIN - bubble.width;
  if (left < TOOLTIP_MARGIN) left = TOOLTIP_MARGIN;
  if (top < TOOLTIP_MARGIN) top = TOOLTIP_MARGIN;
  return { left, top };
}

/**
 * Install the delegated tooltip controller on `doc`. Returns a disposer that
 * removes every listener and the shared bubble (used by tests; the app installs
 * once for its whole lifetime and never disposes).
 */
export function installTooltips(doc: Document = document): () => void {
  let bubble: HTMLElement | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let hideTimer: ReturnType<typeof setTimeout> | null = null;
  let currentTrigger: Element | null = null;

  function ensureBubble(): HTMLElement {
    if (bubble) return bubble;
    const el = doc.createElement("div");
    el.className = "minds-tooltip";
    el.setAttribute("role", "tooltip");
    el.style.position = "fixed";
    el.style.left = "0";
    el.style.top = "0";
    el.style.zIndex = "2147483647";
    el.style.display = "none";
    // Resting (hidden) motion state; showFor animates to opacity 1 / no slide.
    el.style.opacity = "0";
    el.style.transform = `translateY(-${TOOLTIP_SLIDE_PX}px)`;
    doc.body.appendChild(el);
    bubble = el;
    return el;
  }

  // Fade + slide the bubble out, then drop it from layout once the transition
  // finishes; a re-show before then cancels the removal (via cancelHideTimer in
  // showFor). Leaving display:inline-flex during the fade lets the exit animate.
  function hideBubble(): void {
    if (!bubble) return;
    bubble.style.opacity = "0";
    bubble.style.transform = `translateY(-${TOOLTIP_SLIDE_PX}px)`;
    cancelHideTimer();
    hideTimer = setTimeout(() => {
      hideTimer = null;
      if (bubble) bubble.style.display = "none";
    }, TOOLTIP_MOTION_MS);
  }

  function cancelTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function cancelHideTimer(): void {
    if (hideTimer !== null) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  // Full teardown of the current interaction: used on click / scroll / resize /
  // window blur / leaving the document, where the trigger has moved out from
  // under any shown bubble.
  function reset(): void {
    cancelTimer();
    currentTrigger = null;
    hideBubble();
  }

  function showFor(el: Element): void {
    const label = el.getAttribute("data-tooltip");
    if (!label) return;
    cancelHideTimer(); // we're showing -- abort any pending fade-out removal
    const b = ensureBubble();
    b.textContent = label;
    // Measure at natural width: clear any width fixed by a prior show and reset
    // left/top to 0 first, since a stale large left would cap the shrink-to-fit
    // width and wrap the label, mis-measuring it. Ceil the fractional
    // border-box size so the fixed width is never a hair short (which would
    // wrap the last word). Measure in the hidden motion state so the enter
    // transition has a from-state to animate out of.
    b.style.width = "";
    b.style.left = "0";
    b.style.top = "0";
    b.style.visibility = "hidden";
    b.style.display = "inline-flex";
    b.style.opacity = "0";
    b.style.transform = `translateY(-${TOOLTIP_SLIDE_PX}px)`;
    const measured = b.getBoundingClientRect();
    const size: Size = { width: Math.ceil(measured.width), height: Math.ceil(measured.height) };
    const pos = computeTooltipPosition(el.getBoundingClientRect(), size, { width: window.innerWidth, height: window.innerHeight });
    // Fix the width so it doesn't reflow if the viewport later changes.
    b.style.width = size.width + "px";
    b.style.left = pos.left + "px";
    b.style.top = pos.top + "px";
    b.style.visibility = "visible";
    // Commit the from-state (reading offsetHeight forces a style/layout flush)
    // before flipping to the shown state, so the browser animates the change
    // rather than coalescing both writes into a single paint (no transition).
    void b.offsetHeight;
    b.style.opacity = "1";
    b.style.transform = "translateY(0)";
  }

  function triggerAt(target: EventTarget | null): Element | null {
    return target instanceof Element ? target.closest("[data-tooltip]") : null;
  }

  function onMouseOver(event: MouseEvent): void {
    const el = triggerAt(event.target);
    // Same trigger (or still over none): nothing changed, keep any pending
    // schedule so moving within one button doesn't re-arm the delay.
    if (el === currentTrigger) return;
    cancelTimer();
    hideBubble();
    currentTrigger = el;
    if (el) {
      timer = setTimeout(() => {
        timer = null;
        showFor(el);
      }, TOOLTIP_DELAY_MS);
    }
  }

  function onFocusIn(event: FocusEvent): void {
    const el = triggerAt(event.target);
    if (!el) return;
    // Keyboard focus only -- a focus that came from a mouse click would flash
    // the tooltip and then hide it again on the click. :focus-visible may be
    // unsupported in older engines, in which case we simply skip focus tooltips
    // (hover still works).
    try {
      if (!el.matches(":focus-visible")) return;
    } catch {
      return;
    }
    cancelTimer();
    currentTrigger = el;
    showFor(el);
  }

  doc.addEventListener("mouseover", onMouseOver);
  // Leaving the document (the pointer exits the window) fires no further
  // mouseover, so drop any shown bubble here.
  doc.addEventListener("mouseleave", reset);
  doc.addEventListener("focusin", onFocusIn);
  doc.addEventListener("focusout", reset);
  // A click anywhere dismisses the current tooltip (e.g. after pressing the
  // button it labels).
  doc.addEventListener("click", reset, true);
  // Any scroll (capture, so nested scrollers count), resize, or window blur
  // moves the trigger out from under a shown bubble, so drop it.
  window.addEventListener("scroll", reset, true);
  window.addEventListener("resize", reset);
  window.addEventListener("blur", reset);

  return () => {
    reset();
    cancelHideTimer();
    doc.removeEventListener("mouseover", onMouseOver);
    doc.removeEventListener("mouseleave", reset);
    doc.removeEventListener("focusin", onFocusIn);
    doc.removeEventListener("focusout", reset);
    doc.removeEventListener("click", reset, true);
    window.removeEventListener("scroll", reset, true);
    window.removeEventListener("resize", reset);
    window.removeEventListener("blur", reset);
    if (bubble) {
      bubble.remove();
      bubble = null;
    }
  };
}
