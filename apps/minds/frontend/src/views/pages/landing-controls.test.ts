import { describe, expect, it } from "vitest";
import { isMachineStateKnown, mindControlsFor, rowClickActionFor } from "./landing-controls";

describe("mindControlsFor", () => {
  it("offers only Start for a shutdown-capable stopped machine", () => {
    expect(mindControlsFor({ supports_shutdown: true }, "STOPPED", "healthy")).toEqual({
      isStartShown: true,
      isStopShown: false,
    });
  });

  it("offers only Stop for a shutdown-capable running machine", () => {
    expect(mindControlsFor({ supports_shutdown: true }, "RUNNING", "healthy")).toEqual({
      isStartShown: false,
      isStopShown: true,
    });
  });

  it("offers neither when the liveness is unknown or transitioning", () => {
    for (const liveness of ["UNKNOWN", "STARTING", "STOPPING"] as const) {
      expect(mindControlsFor({ supports_shutdown: true }, liveness, "healthy")).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
  });

  it("offers neither when the machine does not support shutdown", () => {
    for (const liveness of ["STOPPED", "RUNNING", "UNKNOWN"] as const) {
      expect(mindControlsFor({ supports_shutdown: false }, liveness, "healthy")).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
      expect(mindControlsFor({}, liveness, "healthy")).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
  });

  it("withholds both while the discovery consumer is dead", () => {
    // The reading behind them is frozen; acting on it could stop a machine
    // the user is looking at a stale RUNNING badge for.
    for (const liveness of ["STOPPED", "RUNNING"] as const) {
      expect(mindControlsFor({ supports_shutdown: true }, liveness, "blocked")).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
  });

  it("keeps the controls through a reconnect, which is not a loss of state", () => {
    expect(mindControlsFor({ supports_shutdown: true }, "RUNNING", "reconnecting").isStopShown).toBe(true);
  });
});

describe("rowClickActionFor", () => {
  it("routes an unhealthy machine to recovery regardless of liveness", () => {
    expect(rowClickActionFor({ supports_shutdown: true }, "STOPPED", false)).toBe("recover");
    expect(rowClickActionFor({ supports_shutdown: false }, "RUNNING", false)).toBe("recover");
  });

  it("auto-starts a stopped quick-start machine through recovery", () => {
    expect(rowClickActionFor({ supports_shutdown: true }, "STOPPED", true)).toBe("recover-start");
    expect(rowClickActionFor({ supports_shutdown: true, is_slow_start: false }, "STOPPED", true)).toBe(
      "recover-start",
    );
  });

  it("prompts for an explicit Start on a stopped slow-start (cloud) machine", () => {
    // A cloud restore takes minutes; recovery's auto-dispatched start is
    // sized for local bounces, so the click must not silently kick one off.
    expect(rowClickActionFor({ supports_shutdown: true, is_slow_start: true }, "STOPPED", true)).toBe(
      "prompt-start",
    );
  });

  it("enters a running or non-shutdown-capable machine directly", () => {
    expect(rowClickActionFor({ supports_shutdown: true, is_slow_start: true }, "RUNNING", true)).toBe("enter");
    expect(rowClickActionFor({ supports_shutdown: false }, "STOPPED", true)).toBe("enter");
    expect(rowClickActionFor({}, "UNKNOWN", true)).toBe("enter");
  });
});

describe("isMachineStateKnown", () => {
  it("is false only once the consumer is gone for good", () => {
    expect(isMachineStateKnown("healthy")).toBe(true);
    expect(isMachineStateKnown("reconnecting")).toBe(true);
    expect(isMachineStateKnown("blocked")).toBe(false);
  });
});
