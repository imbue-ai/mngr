import { describe, expect, it } from "vitest";
import { settle } from "../testing";
import { WASH_DISC_PX, WASH_EARLIEST_LIFT_MS, WASH_LIFT_MS, WASH_SWAP_AT_MS, WashModel, washScaleFor } from "./wash";

describe("washScaleFor", () => {
  it("grows the disc to reach the farthest corner from its origin", () => {
    // From the top-left corner the farthest corner is the bottom-right one.
    const scale = washScaleFor({ x: 0, y: 0 }, 300, 400);
    expect(scale).toBeCloseTo(500 / (WASH_DISC_PX / 2));
  });

  it("from the center every corner is equally far", () => {
    const scale = washScaleFor({ x: 150, y: 200 }, 300, 400);
    expect(scale).toBeCloseTo(250 / (WASH_DISC_PX / 2));
  });
});

describe("WashModel", () => {
  function fakeTimers(): {
    setTimer: (handler: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
    fire: (upToMs: number) => void;
  } {
    const scheduled: Array<{ handler: () => void; delayMs: number }> = [];
    return {
      setTimer: (handler, delayMs) => {
        scheduled.push({ handler, delayMs });
        return 0 as unknown as ReturnType<typeof setTimeout>;
      },
      fire: (upToMs) => {
        for (const entry of scheduled.splice(0)) {
          if (entry.delayMs <= upToMs) entry.handler();
          else scheduled.push(entry);
        }
      },
    };
  }

  /** A cover that stays until its `release` is called. */
  function heldCover(): { coverUntil: Promise<void>; release: () => void } {
    let release = (): void => undefined;
    const coverUntil = new Promise<void>((resolve) => {
      release = resolve;
    });
    return { coverUntil, release };
  }

  const VIEWPORT = { width: 800, height: 600 };
  const ORIGIN = { x: 10, y: 20 };

  it("covers, swaps while whole, and lifts on the shared timeline once nothing holds it", async () => {
    const redraws: number[] = [];
    const timers = fakeTimers();
    const model = new WashModel(() => redraws.push(redraws.length), timers.setTimer);
    let swaps = 0;
    model.start("#abcdef", ORIGIN, VIEWPORT, () => (swaps += 1), Promise.resolve(), false);
    await settle();

    expect(model.active).toMatchObject({ accent: "#abcdef", origin: ORIGIN, isLifting: false });
    expect(swaps).toBe(0);
    timers.fire(WASH_SWAP_AT_MS);
    expect(swaps).toBe(1);
    expect(model.active?.isLifting).toBe(false);
    timers.fire(WASH_EARLIEST_LIFT_MS);
    expect(model.active?.isLifting).toBe(true);
    timers.fire(WASH_LIFT_MS);
    expect(model.active).toBeNull();
    expect(redraws.length).toBe(3);
  });

  it("stays whole past the timeline until it is released, then lifts", async () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    const { coverUntil, release } = heldCover();
    model.start("#abcdef", ORIGIN, VIEWPORT, () => undefined, coverUntil, false);

    timers.fire(WASH_EARLIEST_LIFT_MS * 100);
    await settle();
    expect(model.active?.isLifting).toBe(false);

    release();
    await settle();
    expect(model.active?.isLifting).toBe(true);
    timers.fire(WASH_LIFT_MS);
    expect(model.active).toBeNull();
  });

  it("under reduced motion swaps at once and still holds until released", async () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    let swaps = 0;
    const { coverUntil, release } = heldCover();
    model.start("#abcdef", ORIGIN, VIEWPORT, () => (swaps += 1), coverUntil, true);

    timers.fire(0);
    expect(swaps).toBe(1);
    await settle();
    expect(model.active?.isLifting).toBe(false);

    release();
    await settle();
    timers.fire(0);
    expect(model.active).toBeNull();
  });

  it("cancel drops the cover and every pending beat", async () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    let swaps = 0;
    model.start("#abcdef", ORIGIN, VIEWPORT, () => (swaps += 1), Promise.resolve(), false);
    model.cancel();
    expect(model.active).toBeNull();
    // Past the whole timeline: a cancelled wash swaps nothing and lifts nothing.
    await settle();
    timers.fire(WASH_EARLIEST_LIFT_MS + WASH_LIFT_MS);
    expect(swaps).toBe(0);
    expect(model.active).toBeNull();
  });

  it("a wash that replaces a running one still runs its own beats", () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    const swapped: string[] = [];
    model.start("#111111", ORIGIN, VIEWPORT, () => swapped.push("first"), Promise.resolve(), false);
    model.start("#222222", { x: 30, y: 40 }, VIEWPORT, () => swapped.push("second"), Promise.resolve(), false);
    timers.fire(WASH_SWAP_AT_MS);
    expect(swapped).toEqual(["second"]);
  });
});
