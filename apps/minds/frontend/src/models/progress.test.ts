import { describe, expect, it } from "vitest";
import type { OperationProgress } from "./progress";
import { MAX_RUNNING_PERCENT, ProgressDisplay, progressOf } from "./progress";

function progress(overrides: Partial<OperationProgress>): OperationProgress {
  return {
    percent: 0,
    percent_cap: 0,
    percent_per_second: 0,
    remaining_text: "",
    current_label: "",
    steps: [],
    ...overrides,
  };
}

describe("ProgressDisplay", () => {
  it("draws nothing before the first poll and 100 once done", () => {
    const display = new ProgressDisplay();
    expect(display.percentAt(5000, false)).toBe(0);
    expect(display.percentAt(5000, true)).toBe(100);
  });

  it("extrapolates along the server's rate but never past the cap", () => {
    const display = new ProgressDisplay();
    display.apply(progress({ percent: 20, percent_cap: 30, percent_per_second: 2 }), 1000);

    expect(display.percentAt(1000, false)).toBe(20);
    expect(display.percentAt(3000, false)).toBe(24);
    expect(display.percentAt(30000, false)).toBe(30);
  });

  it("never pulls the bar back when a later poll reads lower than what was drawn", () => {
    const display = new ProgressDisplay();
    display.apply(progress({ percent: 20, percent_cap: 40, percent_per_second: 5 }), 0);
    expect(display.percentAt(3000, false)).toBe(35);

    display.apply(progress({ percent: 33, percent_cap: 50, percent_per_second: 1 }), 3000);

    expect(display.percentAt(3000, false)).toBe(35);
    expect(display.percentAt(6000, false)).toBe(36);
  });

  it("caps a running operation below full even when the server reads higher", () => {
    const display = new ProgressDisplay();
    display.apply(progress({ percent: 99.9, percent_cap: 100, percent_per_second: 0 }), 0);
    expect(display.percentAt(0, false)).toBe(MAX_RUNNING_PERCENT);
  });

  it("prefers the running step's caption and passes the remaining time through", () => {
    const display = new ProgressDisplay();
    expect(display.labelOr("Working...")).toBe("Working...");
    expect(display.remainingText()).toBe("");
    display.apply(progress({ current_label: "Building the image", remaining_text: "About a minute remaining" }), 0);
    expect(display.labelOr("Working...")).toBe("Building the image");
    expect(display.remainingText()).toBe("About a minute remaining");
  });
});

describe("progressOf", () => {
  it("reads a progress object off a status body and nothing else", () => {
    expect(progressOf(null)).toBeNull();
    expect(progressOf({})).toBeNull();
    expect(progressOf({ progress: null })).toBeNull();
    const body = { progress: progress({ percent: 12 }) };
    expect(progressOf(body)).toBe(body.progress);
  });
});
