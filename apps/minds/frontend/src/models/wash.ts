// The wash: the workspace's accent color growing out of the creation page's
// loading box until it covers the window, the screen changing underneath, and
// the color lifting off the finished workspace. Owned by neither page -- it
// outlives the route change -- so the shell renders it from this one model.

import m from "mithril";

/**
 * The beats, on one timeline so the swap can never land on a frame the cover
 * has not reached: the disc is whole from WASH_COVER_MS, and the swap comes
 * after that. How long the cover then stays is up to whoever started the wash.
 */
export const WASH_COVER_MS = 864;
export const WASH_SWAP_AT_MS = 950;
/** The cover stays at least this long, so the swap has a beat under it. */
export const WASH_EARLIEST_LIFT_MS = 1128;
export const WASH_LIFT_MS = 312;
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
  isLifting: boolean;
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
   * while the cover is whole, and lift once `coverUntil` settles. Any wash
   * already running is replaced. `isInstant` (reduced motion) drops every beat
   * but the wait for `coverUntil`.
   */
  start(
    accent: string,
    origin: WashOrigin,
    viewport: { width: number; height: number },
    onSwap: () => void,
    coverUntil: Promise<void>,
    isInstant: boolean,
  ): void {
    this.cancel();
    const generation = this.generation;
    this.active = {
      accent,
      origin,
      scale: washScaleFor(origin, viewport.width, viewport.height),
      isLifting: false,
    };
    this.redraw();
    this.timers.push(
      this.setTimer(
        () => {
          if (generation !== this.generation) return;
          onSwap();
        },
        isInstant ? 0 : WASH_SWAP_AT_MS,
      ),
    );
    let isLiftDue = false;
    let isCoverReleased = false;
    const liftWhenBothAreIn = (): void => {
      if (isLiftDue && isCoverReleased && generation === this.generation)
        this.lift(generation, isInstant ? 0 : WASH_LIFT_MS);
    };
    this.timers.push(
      this.setTimer(
        () => {
          isLiftDue = true;
          liftWhenBothAreIn();
        },
        isInstant ? 0 : WASH_EARLIEST_LIFT_MS,
      ),
    );
    void coverUntil.then(() => {
      isCoverReleased = true;
      liftWhenBothAreIn();
    });
  }

  private lift(generation: number, liftMs: number): void {
    if (this.active === null) return;
    this.active = { ...this.active, isLifting: true };
    this.redraw();
    this.timers.push(
      this.setTimer(() => {
        if (generation !== this.generation) return;
        this.active = null;
        this.redraw();
      }, liftMs),
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
