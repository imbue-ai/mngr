// The transient flash for the newest feed entries, ported from the prototype's
// stack choreography. Each toast auto-dismisses after TOAST_MS (or via its X),
// retiring only the flash -- the durable entry lives on in the bell's feed.
//
// Multiple live toasts pile into a stack: the newest sits in front, the next
// TOAST_STACK_MAX - 1 peek TOAST_PEEK px below it as bottom-aligned clipped
// slivers (visible, but not legible), and anything past the front card --
// peeking slivers included -- counts toward an "N more" line, since only the
// front card is actually readable. Mousing the pile fans it open into a full
// list; leaving re-collapses it. Every card is
// absolutely positioned so the collapsed pile and the open list are the same
// elements with different transforms -- the morph animates instead of
// reflowing. Hovering the pile also freezes every card's countdown, so
// nothing clears out from under a reader who is mid-hover.
//
// Reduced motion (prefers-reduced-motion): no transforms, no transitions, no
// scale choreography -- a plain list whose cards appear and disappear at once.

import m from "mithril";
import type { UiNotificationEntry } from "../../channel/messages";
import { Icon16 } from "../components/Icon";
import { notificationLine } from "../components/NotificationLine";

/** How long a toast lingers before it retires itself. Kept short -- the feed
 * is the durable trail, so a toast is only a brief "just now" flash. */
export const TOAST_MS = 15000;
/** Slide-in / slide-up-out duration; kept in step with the card's transition. */
export const TOAST_EXIT_MS = 200;
/** How many toasts peek in the collapsed stack before the rest fold into "N more". */
export const TOAST_STACK_MAX = 3;
/** Vertical peek of each card behind the front one, collapsed (px). */
export const TOAST_PEEK = 4;
/** Gap between cards once the stack is hovered open (px). */
export const TOAST_EXPAND_GAP = 8;
/** Fallback card height before its real height is measured (px). */
export const TOAST_EST_HEIGHT = 68;

/** Style for card `index` (0 = front) in the collapsed pile. The front card is
 * fully shown and the only one that takes clicks; the next TOAST_STACK_MAX-1
 * are clipped to a fixed sliver and bottom-aligned, so only a clean bottom
 * edge shows however tall their own bodies are; everything deeper hides
 * behind the "N more" line. */
export function collapsedToastStyle(
  index: number,
  count: number,
  frontHeightPx: number,
): Record<string, string> {
  if (index === 0) {
    return {
      transform: "translateY(0)",
      opacity: "1",
      "z-index": String(count),
    };
  }
  if (index < TOAST_STACK_MAX) {
    return {
      height: `${frontHeightPx + index * TOAST_PEEK}px`,
      overflow: "hidden",
      display: "flex",
      "flex-direction": "column",
      "justify-content": "flex-end",
      transform: `scale(${1 - index * 0.03})`,
      "transform-origin": "bottom center",
      opacity: String(1 - index * 0.06),
      "z-index": String(count - index),
      "pointer-events": "none",
    };
  }
  return {
    opacity: "0",
    "pointer-events": "none",
    "z-index": String(count - index),
  };
}

/** Cumulative tops for the hovered-open list (each card below the previous). */
export function expandedToastTops(heightsPx: readonly number[]): number[] {
  const tops: number[] = [];
  let accumulated = 0;
  for (const height of heightsPx) {
    tops.push(accumulated);
    accumulated += height + TOAST_EXPAND_GAP;
  }
  return tops;
}

export function expandedToastStyle(
  index: number,
  count: number,
  topPx: number,
): Record<string, string> {
  return {
    transform: `translateY(${topPx}px)`,
    opacity: "1",
    "z-index": String(count - index),
  };
}

/** How many cards fold into the "N more" line: everything but the front
 * card, which is the only one actually readable while collapsed -- the
 * peeking slivers behind it count as "more" too, not just the fully hidden
 * ones. */
export function toastOverflowCount(count: number, isExpanded: boolean): number {
  return isExpanded ? 0 : Math.max(0, count - 1);
}

/** Where the "N more" line floats: below the whole collapsed pile. */
export function toastMoreTopPx(frontHeightPx: number, count: number): number {
  return (
    frontHeightPx + (Math.min(count, TOAST_STACK_MAX) - 1) * TOAST_PEEK + 12
  );
}

interface ToastCardAttrs {
  entry: UiNotificationEntry;
  isReducedMotion: boolean;
  /** True while the pointer is over the stack: freezes the auto-dismiss
   * countdown so nothing vanishes out from under a reader who is mid-hover. */
  isPaused: boolean;
  onDismiss: () => void;
  onReview: (workspaceAgentId: string, requestId: string) => void;
}

/** One toast card: role="status" for the live-region announcement, with the
 * whole body wrapped in a real button (the uniform review gesture, keyboard
 * accessible like `NotificationsPage.ts`'s `feedRow`) and a corner X that
 * only retires the flash. Owns its own enter/exit motion and the TOAST_MS
 * auto-dismiss timer. */
export function ToastCard(): m.Component<ToastCardAttrs> {
  // Drives both the enter (first paint hidden-above -> slide down) and the
  // exit (slide up + fade). False until the hidden first paint has landed.
  let isShown = false;
  let isClosing = false;
  let isPausedLocally = false;
  // Time left on the countdown; banked by pauseCountdown() so a resume picks
  // up where it left off instead of granting a fresh TOAST_MS.
  let remainingMs: number = TOAST_MS;
  let countdownStartedAtMs = 0;
  let autoTimer: ReturnType<typeof setTimeout> | undefined;
  let exitTimer: ReturnType<typeof setTimeout> | undefined;
  let raf1 = 0;
  let raf2 = 0;
  let latestAttrs: ToastCardAttrs | null = null;

  // Play the exit, then retire from the live set. Guarded so the auto timer
  // and a manual dismiss cannot both fire it.
  function close(): void {
    const attrs = latestAttrs;
    if (isClosing || attrs === null) return;
    isClosing = true;
    clearTimeout(autoTimer);
    // Cancel the entrance rAF chain too: closing before it lands (a quick
    // corner-X or review click right as the card arrives) would otherwise
    // let it still fire afterward and flip isShown back to true mid-exit --
    // a visible flicker between the exit look and "shown" right before
    // onDismiss actually removes the card.
    if (typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
    }
    if (attrs.isReducedMotion) {
      attrs.onDismiss();
      return;
    }
    isShown = false;
    m.redraw();
    exitTimer = setTimeout(() => {
      attrs.onDismiss();
      m.redraw();
    }, TOAST_EXIT_MS);
  }

  function resumeCountdown(): void {
    countdownStartedAtMs = Date.now();
    autoTimer = setTimeout(close, remainingMs);
  }

  function pauseCountdown(): void {
    clearTimeout(autoTimer);
    remainingMs = Math.max(
      0,
      remainingMs - (Date.now() - countdownStartedAtMs),
    );
  }

  return {
    oncreate(vnode) {
      latestAttrs = vnode.attrs;
      isPausedLocally = vnode.attrs.isPaused;
      if (
        vnode.attrs.isReducedMotion ||
        typeof requestAnimationFrame !== "function"
      ) {
        isShown = true;
      } else {
        // Double rAF so the hidden first paint lands and the transition
        // actually runs instead of being collapsed into the initial render.
        raf1 = requestAnimationFrame(() => {
          raf2 = requestAnimationFrame(() => {
            isShown = true;
            m.redraw();
          });
        });
      }
      // A card that arrives already under a hovering pointer starts paused
      // rather than ticking down unseen.
      if (!isPausedLocally) resumeCountdown();
    },
    onupdate(vnode) {
      latestAttrs = vnode.attrs;
      if (isClosing || vnode.attrs.isPaused === isPausedLocally) return;
      isPausedLocally = vnode.attrs.isPaused;
      if (isPausedLocally) pauseCountdown();
      else resumeCountdown();
    },
    onremove() {
      clearTimeout(autoTimer);
      clearTimeout(exitTimer);
      if (typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(raf1);
        cancelAnimationFrame(raf2);
      }
    },
    view(vnode) {
      latestAttrs = vnode.attrs;
      const { entry, isReducedMotion, onReview } = vnode.attrs;
      const motion = isReducedMotion
        ? ""
        : " transition-all duration-200 ease-out " +
          (isShown ? "translate-y-0 opacity-100" : "-translate-y-3 opacity-0");
      return m(
        "div",
        {
          role: "status",
          "data-toast-id": entry.id,
          class:
            "pointer-events-auto relative rounded-lg border border-subtle " +
            "bg-surface-primary shadow-overlay hover:border-strong" +
            motion,
        },
        [
          m(
            "button",
            {
              type: "button",
              class:
                "flex w-full cursor-pointer flex-col py-2.5 pr-8 pl-3 text-left " +
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
              onclick: () => {
                // The card acts like the feed row, from anywhere: hop to the
                // asking machine and open the review popup over it. The flash
                // retires either way.
                onReview(entry.workspace_agent_id, entry.request_id);
                close();
              },
            },
            notificationLine({ entry }),
          ),
          m(
            "button",
            {
              type: "button",
              "aria-label": "Dismiss",
              class:
                "absolute top-1.5 right-1.5 z-10 inline-flex h-6 w-6 cursor-pointer items-center " +
                "justify-center rounded-md text-tertiary hover:bg-fill-hover hover:text-primary",
              onclick: (event: MouseEvent) => {
                // The card itself is clickable; keep the X from also reviewing.
                event.stopPropagation();
                close();
              },
            },
            m(Icon16, { name: "close", size: "sm" }),
          ),
        ],
      );
    },
  };
}

interface ToastStackItemAttrs {
  entryId: string;
  style: Record<string, string>;
  padBottomPx: number;
  onHeight: (entryId: string, heightPx: number) => void;
}

/** One positioned slot in the stack. Reports its card's measured height up so
 * the parent can lay out the open list; the bottom pad (when open) keeps the
 * wrappers touching AND hit-testable (pointer-events-auto, overriding the
 * layer's own pointer-events-none), so moving the pointer through the gap
 * between two open cards never hits page content behind the stack -- which
 * would otherwise register as having left #toast-layer entirely and flap
 * the fan open/closed. A collapsed peeking slot still forces its own
 * pointer-events: none via collapsedToastStyle, which wins over this class
 * (inline style beats a class). */
export function ToastStackItem(): m.Component<ToastStackItemAttrs> {
  let observer: ResizeObserver | null = null;
  return {
    oncreate(vnode) {
      // The inner div is measured, not the padded wrapper.
      const measured = (vnode.dom as HTMLElement)
        .firstElementChild as HTMLElement | null;
      if (measured === null) return;
      const { entryId, onHeight } = vnode.attrs;
      const report = (): void => onHeight(entryId, measured.offsetHeight);
      report();
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(report);
        observer.observe(measured);
      }
    },
    onremove() {
      observer?.disconnect();
      observer = null;
    },
    view(vnode) {
      return m(
        "div",
        {
          class:
            "absolute top-0 right-0 w-full pointer-events-auto transition-all duration-200 ease-out",
          style: {
            ...vnode.attrs.style,
            "padding-bottom": `${vnode.attrs.padBottomPx}px`,
          },
        },
        m("div", vnode.children),
      );
    },
  };
}

export interface ToastLayerAttrs {
  /** The toasts to show, newest first. Pass an empty array (rather than a
   * conditional mount) whenever they should be suppressed -- e.g. the bell's
   * feed overlay is open, where the floating cards would be redundant. */
  toasts: readonly UiNotificationEntry[];
  /** Whether the "Reconnecting…" chip is showing, so the stack starts below
   * it instead of overlapping. */
  isReconnecting: boolean;
  onDismiss: (entryId: string) => void;
  onReview: (workspaceAgentId: string, requestId: string) => void;
}

export function ToastLayer(): m.Component<ToastLayerAttrs> {
  let isExpanded = false;
  const heightsById = new Map<string, number>();

  function onHeight(entryId: string, heightPx: number): void {
    if (heightsById.get(entryId) === heightPx) return;
    heightsById.set(entryId, heightPx);
    m.redraw();
  }

  return {
    view(vnode) {
      const { toasts, isReconnecting, onDismiss, onReview } = vnode.attrs;
      if (toasts.length === 0) {
        // mouseleave never fires on element removal, so a stack that empties
        // while hovered (timers, dismissals) would otherwise leave isExpanded
        // stuck and render the next batch pre-fanned-open.
        isExpanded = false;
        return null;
      }
      for (const id of [...heightsById.keys()]) {
        if (!toasts.some((toast) => toast.id === id)) heightsById.delete(id);
      }
      // Below the Reconnecting chip when it is showing, else at its spot.
      const topClass = isReconnecting ? "top-[74px]" : "top-[42px]";
      const layerClass =
        "pointer-events-none fixed right-2 " + topClass + " z-[250] w-[320px]";
      const isReducedMotion =
        typeof matchMedia === "function" &&
        matchMedia("(prefers-reduced-motion: reduce)").matches;
      const hoverHandlers = {
        onmouseenter: () => {
          isExpanded = true;
        },
        onmouseleave: () => {
          isExpanded = false;
        },
      };
      if (isReducedMotion) {
        return m(
          "div#toast-layer",
          { class: layerClass + " flex flex-col gap-2", ...hoverHandlers },
          toasts.map((entry) =>
            m(ToastCard, {
              key: entry.id,
              entry,
              isReducedMotion: true,
              isPaused: isExpanded,
              onDismiss: () => onDismiss(entry.id),
              onReview,
            }),
          ),
        );
      }
      const count = toasts.length;
      const heightOf = (index: number): number =>
        heightsById.get(toasts[index].id) ?? TOAST_EST_HEIGHT;
      const openTops = expandedToastTops(
        toasts.map((_, index) => heightOf(index)),
      );
      const overflow = toastOverflowCount(count, isExpanded);
      const children: m.Children[] = toasts.map((entry, index) =>
        m(
          ToastStackItem,
          {
            key: entry.id,
            entryId: entry.id,
            style: isExpanded
              ? expandedToastStyle(index, count, openTops[index])
              : collapsedToastStyle(index, count, heightOf(0)),
            padBottomPx: isExpanded ? TOAST_EXPAND_GAP : 0,
            onHeight,
          },
          m(ToastCard, {
            entry,
            isReducedMotion: false,
            isPaused: isExpanded,
            onDismiss: () => onDismiss(entry.id),
            onReview,
          }),
        ),
      );
      if (overflow > 0) {
        children.push(
          m(
            "div",
            {
              key: "toast-overflow",
              class:
                "pointer-events-none absolute right-1 z-0 type-helper text-tertiary " +
                "transition-all duration-200 ease-out",
              style: {
                transform: `translateY(${toastMoreTopPx(heightOf(0), count)}px)`,
              },
            },
            `${overflow} more`,
          ),
        );
      }
      return m(
        "div#toast-layer",
        { class: layerClass, ...hoverHandlers },
        children,
      );
    },
  };
}
