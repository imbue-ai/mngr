import { describe, expect, it } from "vitest";
import { mindControlsFor } from "./landing-controls";

describe("mindControlsFor", () => {
  it("offers only Start for a shutdown-capable stopped machine", () => {
    expect(mindControlsFor({ supports_shutdown: true }, "STOPPED")).toEqual({
      isStartShown: true,
      isStopShown: false,
    });
  });

  it("offers only Stop for a shutdown-capable running machine", () => {
    expect(mindControlsFor({ supports_shutdown: true }, "RUNNING")).toEqual({
      isStartShown: false,
      isStopShown: true,
    });
  });

  it("offers neither when the liveness is unknown or transitioning", () => {
    for (const liveness of ["UNKNOWN", "STARTING", "STOPPING"] as const) {
      expect(mindControlsFor({ supports_shutdown: true }, liveness)).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
  });

  it("offers neither when the machine does not support shutdown", () => {
    for (const liveness of ["STOPPED", "RUNNING", "UNKNOWN"] as const) {
      expect(mindControlsFor({ supports_shutdown: false }, liveness)).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
      expect(mindControlsFor({}, liveness)).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
  });
});
