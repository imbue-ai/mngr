// The wash: the workspace's accent color growing out of the creation page's
// loading box until it covers the window, the screen changing underneath, and
// the color lifting off the finished workspace. Owned by neither page -- it
// outlives the route change -- so the shell renders it from this one model.

import m from "mithril";

/**
 * The three beats on one timeline, so the swap can never land on a frame the
 * cover has not reached: the disc is whole from 36% to 47% of the run, and the
 * swap sits in the middle of that window.
 */
export const WASH_TOTAL_MS = 2400;
export const WASH_SWAP_AT_MS = 950;
/** The disc's drawn diameter; the growth scale is measured against it. */
export const WASH_DISC_PX = 120;

export interface WashOrigin {
  /** Where the color grows from, in window coordinates. */
  x: number;
  y: number;
}

export interface ActiveWash {
  accent: string;
  origin: WashOrigin;
  /** How far the disc has to grow: from its origin to the farthest window corner. */
  scale: number;
}

/** A circle of radius WASH_DISC_PX / 2 at `origin` must reach the farthest corner of a `width` x `height` window. */
export function washScaleFor(origin: WashOrigin, width: number, height: number): number {
  const farthest = Math.max(
    Math.hypot(origin.x, origin.y),
    Math.hypot(width - origin.x, origin.y),
    Math.hypot(origin.x, height - origin.y),
    Math.hypot(width - origin.x, height - origin.y),
  );
  return farthest / (WASH_DISC_PX / 2);
}

export class WashModel {
  active: ActiveWash | null = null;
  private timers: ReturnType<typeof setTimeout>[] = [];
  // Bumped by cancel() (and so by each start(), which cancels first), so a
  // beat that was already scheduled knows its wash is over. Clearing the
  // timers is not enough on its own: the scheduler is injectable.
  private generation = 0;
  private readonly redraw: () => void;
  private readonly setTimer: (handler: () => void, delayMs: number) => ReturnType<typeof setTimeout>;

  constructor(
    redraw: () => void,
    // Wrapped rather than passed bare: called as a method, the host's own
    // setTimeout sees the model as `this` and Chrome throws "Illegal invocation".
    setTimer: (handler: () => void, delayMs: number) => ReturnType<typeof setTimeout> = (handler, delayMs) =>
      setTimeout(handler, delayMs),
  ) {
    this.redraw = redraw;
    this.setTimer = setTimer;
  }

  /**
   * Run the wash: cover the window in `accent` from `origin`, call `onSwap`
   * while the cover is whole, and lift. Any wash already running is replaced.
   */
  start(accent: string, origin: WashOrigin, viewport: { width: number; height: number }, onSwap: () => void): void {
    this.cancel();
    const generation = this.generation;
    this.active = { accent, origin, scale: washScaleFor(origin, viewport.width, viewport.height) };
    this.redraw();
    this.timers.push(
      this.setTimer(() => {
        if (generation !== this.generation) return;
        onSwap();
      }, WASH_SWAP_AT_MS),
    );
    this.timers.push(
      this.setTimer(() => {
        if (generation !== this.generation) return;
        this.active = null;
        this.redraw();
      }, WASH_TOTAL_MS),
    );
  }

  cancel(): void {
    this.generation += 1;
    for (const timer of this.timers) clearTimeout(timer);
    this.timers = [];
    this.active = null;
  }
}

/** The one wash a window can run at a time. */
export const wash = new WashModel(() => m.redraw());
