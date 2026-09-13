import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ChatTypewriter,
  CloudWheel,
  LAST_STEP,
  TIPS,
  TipsRotator,
  TOTAL_STEPS,
  WalkthroughStepper,
  dwellForStep,
  graphicForStep,
} from "./walkthrough";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("dwellForStep / graphicForStep", () => {
  it("gives the chat, connections and publishing steps a longer dwell", () => {
    expect(dwellForStep(1)).toBe(7000);
    expect(dwellForStep(3)).toBe(10000);
    expect(dwellForStep(6)).toBe(16000);
    expect(dwellForStep(8)).toBe(9000);
  });

  it("maps every step to exactly one graphic, and the last step to none", () => {
    expect(graphicForStep(1)).toBe("gfx-minds");
    expect(graphicForStep(2)).toBe("gfx-machine");
    expect(graphicForStep(4)).toBe("gfx-browser");
    expect(graphicForStep(TOTAL_STEPS)).toBe("gfx-tips");
  });
});

describe("WalkthroughStepper", () => {
  it("auto-advances on the step's own dwell and stops at the last step", () => {
    const redraw = vi.fn();
    const stepper = new WalkthroughStepper(redraw);
    stepper.start();
    expect(stepper.step).toBe(1);
    vi.advanceTimersByTime(dwellForStep(1));
    expect(stepper.step).toBe(2);
    expect(redraw).toHaveBeenCalled();

    stepper.goToStep(LAST_STEP);
    redraw.mockClear();
    vi.advanceTimersByTime(60_000);
    expect(stepper.step).toBe(LAST_STEP);
    expect(redraw).not.toHaveBeenCalled();
  });

  it("clamps out-of-range targets and bumps visitId on every navigation", () => {
    const stepper = new WalkthroughStepper(() => undefined);
    stepper.goToStep(0);
    expect(stepper.step).toBe(1);
    const firstVisit = stepper.visitId;
    stepper.goToStep(1); // re-clicking the current dot still counts as a visit
    expect(stepper.visitId).toBeGreaterThan(firstVisit);
    stepper.goToStep(999);
    expect(stepper.step).toBe(LAST_STEP);
  });

  it("records the step navigated away from", () => {
    const stepper = new WalkthroughStepper(() => undefined);
    stepper.goToStep(4);
    expect(stepper.arrivedFrom).toBe(1);
    stepper.goToStep(5);
    expect(stepper.arrivedFrom).toBe(4);
  });

  it("stop() cancels the pending auto-advance", () => {
    const stepper = new WalkthroughStepper(() => undefined);
    stepper.start();
    stepper.stop();
    vi.advanceTimersByTime(60_000);
    expect(stepper.step).toBe(1);
  });
});

describe("ChatTypewriter", () => {
  // The prefix's first character is typed synchronously from start(), so
  // reaching all of it takes (length - 1) more 45ms ticks; everything after
  // (options, erasing) runs entirely on scheduled ticks with no free first
  // character. Generous windows below avoid hand-deriving each boundary.
  it("types the prefix before any option text appears", () => {
    const typewriter = new ChatTypewriter(() => undefined);
    typewriter.start();
    vi.advanceTimersByTime(45 * ("I want ".length - 1));
    expect(typewriter.text).toBe("I want ");
  });

  it("types each option in turn, erasing between them, and holds on the last", () => {
    const typewriter = new ChatTypewriter(() => undefined);
    typewriter.start();

    // First option fully typed.
    vi.advanceTimersByTime(45 * 'a custom "to do" app.'.length + 1000);
    expect(typewriter.text).toBe('I want a custom "to do" app.');

    // Holds, then erases back to the prefix and types the next option.
    vi.advanceTimersByTime(1300 + 22 * 'a custom "to do" app.'.length + 1000);
    expect(typewriter.text).toBe("I want you to filter my emails.");

    // The last option is reached and never erased, however long we wait.
    vi.advanceTimersByTime(1300 + 22 * "you to filter my emails.".length + 45 * "to build a dashboard.".length + 1000);
    expect(typewriter.text).toBe("I want to build a dashboard.");
    vi.advanceTimersByTime(60_000);
    expect(typewriter.text).toBe("I want to build a dashboard.");
  });

  it("start() while already running is a no-op, not a restart", () => {
    const typewriter = new ChatTypewriter(() => undefined);
    typewriter.start();
    vi.advanceTimersByTime(45 * 3);
    const textPartway = typewriter.text;
    typewriter.start();
    expect(typewriter.text).toBe(textPartway);
  });

  it("stop() halts typing where it stood", () => {
    const typewriter = new ChatTypewriter(() => undefined);
    typewriter.start();
    vi.advanceTimersByTime(45 * 3);
    typewriter.stop();
    const frozen = typewriter.text;
    vi.advanceTimersByTime(10_000);
    expect(typewriter.text).toBe(frozen);
  });
});

describe("TipsRotator", () => {
  it("holds the first tip for a full turn rather than swapping immediately", () => {
    const rotator = new TipsRotator(() => undefined);
    rotator.start();
    expect(rotator.text).toBe(TIPS[0]);
    vi.advanceTimersByTime(6999);
    expect(rotator.text).toBe(TIPS[0]);
  });

  it("fades out, swaps text, and fades back in on each turn", () => {
    const rotator = new TipsRotator(() => undefined);
    rotator.start();
    vi.advanceTimersByTime(7000);
    expect(rotator.opacity).toBe(0);
    expect(rotator.text).toBe(TIPS[0]); // text swaps only after the fade
    vi.advanceTimersByTime(250);
    expect(rotator.opacity).toBe(1);
    expect(rotator.text).toBe(TIPS[1]);
  });

  it("wraps around after the last tip", () => {
    const rotator = new TipsRotator(() => undefined);
    rotator.start();
    for (let i = 0; i < TIPS.length; i++) {
      vi.advanceTimersByTime(7000 + 250);
    }
    expect(rotator.text).toBe(TIPS[0]);
  });

  it("stop() mid-fade cancels the pending swap, so a later start() begins fresh", () => {
    const rotator = new TipsRotator(() => undefined);
    rotator.start();
    vi.advanceTimersByTime(7000); // now mid-fade (opacity 0, swap pending)
    rotator.stop();
    vi.advanceTimersByTime(10_000);
    rotator.start();
    expect(rotator.text).toBe(TIPS[0]);
    expect(rotator.opacity).toBe(1);
  });
});

describe("CloudWheel", () => {
  const apps = [
    { url: "a.svg", name: "Alpha" },
    { url: "b.svg", name: "Beta" },
    { url: "c.svg", name: "Gamma" },
  ];

  it("does nothing with an empty app list", () => {
    const wheel = new CloudWheel([], () => undefined);
    wheel.start();
    expect(wheel.renderItems).toEqual([]);
  });

  it("seeds four items across the enter/left/center/right positions", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    expect(wheel.renderItems).toHaveLength(4);
    expect(wheel.renderItems.map((item) => item.position)).toEqual([0, 1, 2, 3]);
    expect(wheel.centerName).toBe(apps[2].name);
  });

  it("advances every item forward by one position and feeds a new one in", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    vi.advanceTimersByTime(2200);
    // The four seeded items moved to 1,2,3,4(exit); a fifth enters at 0.
    const positions = wheel.renderItems.map((item) => item.position).sort();
    expect(positions).toEqual([0, 1, 2, 3, 4]);
  });

  it("drops a retired item from renderItems only after its transition finishes", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    vi.advanceTimersByTime(2200); // one item now at is-exit (position 4)
    expect(wheel.renderItems.some((item) => item.position === 4)).toBe(true);
    vi.advanceTimersByTime(899);
    expect(wheel.renderItems.some((item) => item.position === 4)).toBe(true);
    vi.advanceTimersByTime(1);
    expect(wheel.renderItems.some((item) => item.position === 4)).toBe(false);
  });

  it("pops the centered app's name after the arrival delay", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    const firstCenterName = wheel.centerName;
    vi.advanceTimersByTime(2200);
    // Name has not popped yet (arrival delay not elapsed).
    expect(wheel.centerName).toBe(firstCenterName);
    vi.advanceTimersByTime(450);
    expect(wheel.centerName).not.toBe("");
  });

  it("stop() halts advancing", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    wheel.stop();
    const before = wheel.renderItems.length;
    vi.advanceTimersByTime(20_000);
    expect(wheel.renderItems.length).toBe(before);
  });

  it("start() after stop() replays from a clean seed rather than piling on stale items", () => {
    const wheel = new CloudWheel(apps, () => undefined);
    wheel.start();
    vi.advanceTimersByTime(2200 * 3); // advance a few ticks so state is not just the initial seed
    wheel.stop();
    wheel.start();
    expect(wheel.renderItems).toHaveLength(4);
    expect(wheel.renderItems.map((item) => item.position)).toEqual([0, 1, 2, 3]);
  });
});
