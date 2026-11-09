import { describe, expect, it } from "vitest";
import { WASH_DISC_PX, WASH_SWAP_AT_MS, WASH_TOTAL_MS, WashModel, washScaleFor } from "./wash";

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

  it("covers, swaps while whole, and lifts on the shared timeline", () => {
    const redraws: number[] = [];
    const timers = fakeTimers();
    const model = new WashModel(() => redraws.push(redraws.length), timers.setTimer);
    let swaps = 0;
    model.start("#abcdef", { x: 10, y: 20 }, { width: 800, height: 600 }, () => (swaps += 1));

    expect(model.active).toMatchObject({ accent: "#abcdef", origin: { x: 10, y: 20 } });
    expect(swaps).toBe(0);
    timers.fire(WASH_SWAP_AT_MS);
    expect(swaps).toBe(1);
    expect(model.active).not.toBeNull();
    timers.fire(WASH_TOTAL_MS);
    expect(model.active).toBeNull();
    expect(redraws.length).toBe(2);
  });

  it("cancel drops the cover and every pending beat", () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    let swaps = 0;
    model.start("#abcdef", { x: 10, y: 20 }, { width: 800, height: 600 }, () => (swaps += 1));
    model.cancel();
    expect(model.active).toBeNull();
    // Past the whole timeline: a cancelled wash swaps nothing and lifts nothing.
    timers.fire(WASH_TOTAL_MS);
    expect(swaps).toBe(0);
    expect(model.active).toBeNull();
  });

  it("a wash that replaces a running one still runs its own beats", () => {
    const timers = fakeTimers();
    const model = new WashModel(() => undefined, timers.setTimer);
    const swapped: string[] = [];
    model.start("#111111", { x: 10, y: 20 }, { width: 800, height: 600 }, () => swapped.push("first"));
    model.start("#222222", { x: 30, y: 40 }, { width: 800, height: 600 }, () => swapped.push("second"));
    timers.fire(WASH_SWAP_AT_MS);
    expect(swapped).toEqual(["second"]);
  });
});
