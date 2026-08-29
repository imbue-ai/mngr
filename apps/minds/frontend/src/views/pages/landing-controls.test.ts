import { describe, expect, it } from "vitest";
import {
  backupsControlFor,
  healthBadgeLabelFor,
  isMachineStateKnown,
  lifecycleConfirmation,
  mindControlsFor,
  remoteLocationBadgeFor,
  remoteStateChipFor,
  rowClickActionFor,
} from "./landing-controls";

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

  it("auto-starts any stopped shutdown-capable machine through recovery", () => {
    // Cloud machines included: recovery's start step shares the Start/Stop
    // buttons' generous budget, so even a minutes-long restore is handled.
    expect(rowClickActionFor({ supports_shutdown: true }, "STOPPED", true)).toBe("recover-start");
  });

  it("enters a running or non-shutdown-capable machine directly", () => {
    expect(rowClickActionFor({ supports_shutdown: true }, "RUNNING", true)).toBe("enter");
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

describe("lifecycleConfirmation", () => {
  // Only the apply rewrites the live machine, so only it changes the question;
  // a run that is merely preparing (or waiting) must not lock the reader out.
  it("warns that a stop mid-apply can leave the machine half-updated", () => {
    const question = lifecycleConfirmation("stop", "Fox", true);
    expect(question).toContain("half-updated");
    expect(question).toContain("Stop anyway?");
  });

  it("asks the ordinary stop question when no apply is under way", () => {
    const question = lifecycleConfirmation("stop", "Fox", false);
    expect(question).toContain('Stop "Fox"?');
    expect(question).not.toContain("half-updated");
  });

  it("asks before a restart only mid-apply", () => {
    expect(lifecycleConfirmation("restart", "Fox", false)).toBeNull();
    expect(lifecycleConfirmation("restart", "Fox", true)).toContain("half-updated");
  });
});

describe("healthBadgeLabelFor", () => {
  it("reports a machine that is answering with no badge at all", () => {
    expect(healthBadgeLabelFor("healthy", false, null, false)).toBeNull();
  });

  it("reports an unattended start as reconnecting rather than as a restart", () => {
    // The start the app dispatches on its own is start-only and idempotent, so
    // against a machine whose host is up it does nothing. Calling that a
    // restart tells the user their work was interrupted when it was not.
    expect(healthBadgeLabelFor("recovering", false, "start", false)).toBe("Reconnecting...");
  });

  it("calls the user's own full bounce a restart", () => {
    // A stop+start is only ever dispatched by a click, and it really does
    // interrupt the machine -- so here the stronger word is the honest one.
    expect(healthBadgeLabelFor("recovering", false, "restart", false)).toBe("Restarting...");
  });

  it("takes the weaker reading for a recovery the tracker cannot describe", () => {
    // No kind reported yet (or the episode ended under the frame): there is no
    // evidence for the claim that work was interrupted, so it is not made.
    expect(healthBadgeLabelFor("recovering", false, null, false)).toBe("Reconnecting...");
  });

  it("does not blame a restart that never ran", () => {
    // Same start, one step later: it reported that it booted nothing, so the
    // machine is unresponsive and no restart failed. Only a start that really
    // booted the host keeps the restart framing.
    expect(healthBadgeLabelFor("recovery_failed", true, null, false)).toBe("Not responding");
    expect(healthBadgeLabelFor("recovery_failed", false, null, false)).toBe("Restart failed");
  });

  it("keeps the still-checking state distinct from both", () => {
    expect(healthBadgeLabelFor("stuck", false, null, false)).toBe("Server not responding");
  });

  it("names this device wherever the machine's own reading would blame the machine", () => {
    // Every reading the row can otherwise show is a claim about the machine,
    // and the device verdict contradicts all of them at once: nothing was ever
    // sent to the machine, so a failed restart, an unresponsive server and a
    // reconnect in progress are all describing the wrong end of the connection.
    expect(healthBadgeLabelFor("recovery_failed", false, null, true)).toBe("Can't connect from this device");
    expect(healthBadgeLabelFor("recovery_failed", true, null, true)).toBe("Can't connect from this device");
    expect(healthBadgeLabelFor("stuck", false, null, true)).toBe("Can't connect from this device");
    expect(healthBadgeLabelFor("recovering", false, "restart", true)).toBe("Can't connect from this device");
  });

  it("says nothing about a machine that is answering, whatever failed earlier", () => {
    // The card withholds the device verdict over a healthy machine for the same
    // reason: whatever could not connect, it is connecting now.
    expect(healthBadgeLabelFor("healthy", false, null, true)).toBeNull();
  });
});

describe("remoteLocationBadgeFor", () => {
  it("names the provider for a cloud workspace, never a device", () => {
    expect(
      remoteLocationBadgeFor({ remote_kind: "cloud", location: "Imbue Cloud" }),
    ).toBe("Imbue Cloud");
  });

  it("names the hosting device for an other-device machine", () => {
    expect(
      remoteLocationBadgeFor({ remote_kind: "other_device", location: "mac" }),
    ).toBe("on mac");
    expect(remoteLocationBadgeFor({ location: "mac" })).toBe("on mac");
  });
});

describe("remoteStateChipFor", () => {
  it("shows nothing for the plain state", () => {
    expect(remoteStateChipFor("")).toBeNull();
  });

  it("points a signed-out provider at the Accounts page", () => {
    expect(remoteStateChipFor("signed_out")).toEqual({
      label: "Signed out",
      isImportant: true,
      isAccountsLink: true,
    });
  });

  it("reports the access states with their tones", () => {
    expect(remoteStateChipFor("connecting")).toEqual({
      label: "connecting…",
      isImportant: false,
      isAccountsLink: false,
    });
    expect(remoteStateChipFor("unreachable")).toEqual({
      label: "unreachable",
      isImportant: true,
      isAccountsLink: false,
    });
    expect(remoteStateChipFor("error")).toEqual({
      label: "sync error",
      isImportant: true,
      isAccountsLink: false,
    });
  });
});

describe("backupsControlFor", () => {
  it("offers usable backups on a remote row whose credentials are on this device", () => {
    expect(
      backupsControlFor({ is_remote: true, backup_access: "available" }, ""),
    ).toEqual({
      isShown: true,
      isEnabled: true,
      tooltip: "Backups",
    });
  });

  it("explains a locked or never-synced remote row instead of hiding the button", () => {
    const locked = backupsControlFor(
      { is_remote: true, backup_access: "locked" },
      "",
    );
    expect(locked.isShown).toBe(true);
    expect(locked.isEnabled).toBe(false);
    expect(locked.tooltip).toContain("master password");
    const unavailable = backupsControlFor(
      { is_remote: true, backup_access: "unavailable" },
      "",
    );
    expect(unavailable.isShown).toBe(true);
    expect(unavailable.isEnabled).toBe(false);
    expect(unavailable.tooltip).toContain("device that created this machine");
  });

  it("hides the button on a remote row with no computed access", () => {
    expect(backupsControlFor({ is_remote: true }, "").isShown).toBe(false);
  });

  it("offers backups on a live row only while it is stopped", () => {
    expect(backupsControlFor({ is_remote: false }, "STOPPED")).toEqual({
      isShown: true,
      isEnabled: true,
      tooltip: "Backups",
    });
    for (const liveness of ["RUNNING", "STOPPING", "STARTING", "UNKNOWN", ""]) {
      expect(backupsControlFor({ is_remote: false }, liveness).isShown).toBe(
        false,
      );
    }
  });
});
