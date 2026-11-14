import { describe, expect, it } from "vitest";
import {
  backupsControlFor,
  healthBadgeLabelFor,
  isOutageExplainedByDevice,
  isMachineStateKnown,
  keyStateChipFor,
  lifecycleConfirmation,
  lifecycleFailureError,
  livenessBadgeFor,
  mindControlsFor,
  remoteLocationBadgeFor,
  remoteStateChipFor,
  remoteTileEntries,
  removeRecordFailureFor,
  rowClickActionFor,
} from "./landing-controls";
import { WINDOW_LOGIN_LAPSED_SENTENCE } from "../../models/errors";

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

  it("withholds Start from a machine an operator is holding, or whose stop kind this build does not know", () => {
    for (const stopKind of ["maintenance", "suspension", "retired", "unknown"]) {
      expect(mindControlsFor({ supports_shutdown: true, stop_kind: stopKind }, "STOPPED", "healthy")).toEqual({
        isStartShown: false,
        isStopShown: false,
      });
    }
    for (const stopKind of ["", "owner", "idle"]) {
      expect(mindControlsFor({ supports_shutdown: true, stop_kind: stopKind }, "STOPPED", "healthy").isStartShown).toBe(
        true,
      );
    }
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
  it("blocks a cloud machine this device holds no key for, whatever its health or liveness", () => {
    expect(rowClickActionFor({ supports_shutdown: true, key_state: "locked" }, "RUNNING", true)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, key_state: "syncing" }, "RUNNING", false)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, key_state: "unavailable" }, "STOPPED", true)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, key_state: "" }, "RUNNING", true)).toBe("enter");
  });

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

  it("blocks the row of a held machine and routes an idle-stopped one to the start", () => {
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "maintenance" }, "STOPPED", true)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "retired" }, "STOPPED", true)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "maintenance" }, "STOPPING", false)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "unknown" }, "STOPPED", true)).toBe("blocked");
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "idle" }, "STOPPED", true)).toBe("recover-start");
    expect(rowClickActionFor({ supports_shutdown: true, stop_kind: "maintenance" }, "RUNNING", true)).toBe("enter");
  });
});

describe("livenessBadgeFor", () => {
  const label = (liveness: string, stopKind: string) => livenessBadgeFor(liveness, stopKind).label;

  it("names a hold for a held machine that is stopped or stopping, and the lifecycle label otherwise", () => {
    expect(label("STOPPED", "maintenance")).toBe("Maintenance");
    expect(label("STOPPING", "maintenance")).toBe("Maintenance");
    expect(label("STARTING", "maintenance")).toBe("Starting…");
    expect(label("STOPPED", "retired")).toBe("Retired");
    expect(label("STOPPING", "retired")).toBe("Retired");
    expect(label("STARTING", "retired")).toBe("Starting…");
    expect(label("STOPPED", "suspension")).toBe("Suspended");
    expect(label("STOPPED", "unknown")).toBe("Unavailable");
    expect(label("STOPPED", "idle")).toBe("Stopped");
    expect(label("STOPPED", "")).toBe("Stopped");
    expect(label("WEIRD", "")).toBe("Status unknown");
  });

  it("says on the badge why a held row has no Start", () => {
    expect(livenessBadgeFor("STOPPED", "retired").tooltip).toContain("Its backups are still available from the Backups button.");
    // A tooltip is a plain attribute, so the sentence reaches it without its
    // markup.
    expect(livenessBadgeFor("STOPPED", "suspension").tooltip).toBe(
      "This account is suspended. If you believe this is a mistake, contact support@imbue.com.",
    );
    expect(livenessBadgeFor("STOPPED", "unknown").tooltip).toContain("Imbue Cloud is holding this workspace stopped");
    expect(livenessBadgeFor("STOPPED", "owner").tooltip).toBe("");
  });
});

describe("keyStateChipFor", () => {
  it("names the remedy for each state and nothing for a machine this device can open", () => {
    expect(keyStateChipFor("locked")?.label).toBe("Enter your master password to open");
    expect(keyStateChipFor("syncing")?.label).toBe("Syncing access…");
    expect(keyStateChipFor("unavailable")?.label).toBe("No access from this device");
    expect(keyStateChipFor("rejected")?.label).toBe("Access refused from this device");
    expect(keyStateChipFor("")).toBeNull();
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
    expect(healthBadgeLabelFor("recovery_failed", true, "restart", false)).toBe("Not responding");
    expect(healthBadgeLabelFor("recovery_failed", false, "restart", false, "STEP_FAILED")).toBe("Restart failed");
  });

  it("names a failed recovery after the recovery that ran", () => {
    // The app's own start never took the machine down, so it failed to
    // reconnect; only the user's restart failed as a restart.
    expect(healthBadgeLabelFor("recovery_failed", false, "start", false, "NOT_ANSWERING_AFTER_START")).toBe(
      "Couldn't reconnect",
    );
    expect(healthBadgeLabelFor("recovery_failed", false, "restart", false, "NOT_ANSWERING_AFTER_START")).toBe(
      "Restart failed",
    );
  });

  it("does not blame a restart for a recovery that failed before it ran anything", () => {
    expect(healthBadgeLabelFor("recovery_failed", false, "restart", false, "WORKSPACE_NOT_FOUND")).toBe(
      "Not responding",
    );
    expect(healthBadgeLabelFor("recovery_failed", false, "restart", false, "WORKER_NOT_STARTED")).toBe(
      "Not responding",
    );
  });

  it("calls a workspace that stopped answering not responding, without naming a server", () => {
    expect(healthBadgeLabelFor("stuck", false, null, false)).toBe("Not responding");
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

describe("lifecycleFailureError", () => {
  const MAINTENANCE_BODY = {
    error: "Could not start the workspace host: This workspace is undergoing maintenance and will be back shortly.",
  };

  it("gives a held start Imbue Cloud's own sentence, with no report to file", () => {
    const failure = lifecycleFailureError("start", "Fox", "maintenance", { status: 502, body: MAINTENANCE_BODY });
    expect(failure.error.message).toBe("This workspace is undergoing maintenance and will be back shortly.");
    expect(failure.isHold).toBe(true);
    expect(failure.error.details).toContain("Could not start the workspace host");
  });

  it("tells a suspended owner the account is suspended, though the refusal reads as maintenance", () => {
    const failure = lifecycleFailureError("start", "Fox", "suspension", { status: 502, body: MAINTENANCE_BODY });
    expect(failure.error.message).toBe(
      "This account is suspended. *If you believe this is a mistake, contact support@imbue.com.*",
    );
  });

  it("says the retired sentence for a retired workspace", () => {
    const failure = lifecycleFailureError("start", "Fox", "retired", {
      status: 502,
      body: {
        error:
          "Could not start the workspace host: This workspace has been retired and cannot be started again. " +
          "Download its data from its backups, or contact support if it has none.",
      },
    });
    expect(failure.error.message).toContain("has been retired");
    expect(failure.isHold).toBe(true);
  });

  it("names any other failure, keeping mngr's verdict behind See details", () => {
    const failure = lifecycleFailureError("stop", "Fox", "", {
      status: 502,
      body: { error: "Could not stop the workspace host: Error: host lock held" },
    });
    expect(failure.error.message).toBe(
      'Couldn\'t stop "Fox". Try again. If this persists, report a bug.',
    );
    expect(failure.error.message).not.toContain("provider");
    expect(failure.error.details).toBe("HTTP 502\nCould not stop the workspace host: Error: host lock held");
    expect(failure.isHold).toBe(false);
  });

  it("leads with the provider's own reason when the provider is what failed", () => {
    const failure = lifecycleFailureError("start", "Fox", "", {
      status: 502,
      body: {
        error: "Could not start the workspace host: Error: Provider 'docker' is not available: Cannot connect to Docker daemon",
        provider_reason: "Cannot connect to Docker daemon",
      },
    });
    expect(failure.error.message).toBe('Couldn\'t start "Fox". Cannot connect to Docker daemon.');
    expect(failure.error.details).toContain("Provider 'docker' is not available");
    expect(failure.isHold).toBe(false);
  });

  it("says the app didn't respond when the request got no answer", () => {
    const failure = lifecycleFailureError("start", "Fox", "", {
      status: 0,
      body: { error: "Could not reach the app server." },
    });
    expect(failure.error.message).toBe("Imbue Studio didn't respond. Try again in a moment.");
    expect(failure.isHold).toBe(false);
  });
});

describe("isOutageExplainedByDevice", () => {
  it("lets this device's lost network explain only the workspaces that need the network", () => {
    expect(isOutageExplainedByDevice("OFFLINE", true)).toBe(true);
    expect(isOutageExplainedByDevice("SSH_BLOCKED", true)).toBe(true);
    // A workspace on this computer answers with the network down.
    expect(isOutageExplainedByDevice("OFFLINE", false)).toBe(false);
    // Unmeasured is not offline.
    expect(isOutageExplainedByDevice("UNKNOWN", true)).toBe(false);
    expect(isOutageExplainedByDevice("NONE", true)).toBe(false);
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
  const PROBLEM_STATES = ["unreachable", "error", "missing_connection", "no_credentials", "read_only"];
  const ALL_STATES = ["connecting", "locked", ...PROBLEM_STATES];

  it("shows nothing for the plain state or one this build does not know", () => {
    expect(remoteStateChipFor({ state: "" })).toBeNull();
    expect(remoteStateChipFor({ state: "", detail: null })).toBeNull();
    expect(remoteStateChipFor({ state: "some_future_state" })).toBeNull();
  });

  it("marks only the states that keep this computer from reaching the workspace as problems", () => {
    for (const state of PROBLEM_STATES) expect(remoteStateChipFor({ state })?.isImportant).toBe(true);
    expect(remoteStateChipFor({ state: "connecting" })?.isImportant).toBe(false);
    expect(remoteStateChipFor({ state: "locked" })?.isImportant).toBe(false);
  });

  it("explains every state on hover in the workspace's terms, never the device's", () => {
    for (const state of ALL_STATES) {
      const tooltip = remoteStateChipFor({ state })?.tooltip ?? "";
      expect(tooltip, state).toContain("workspace");
      expect(tooltip, state).not.toMatch(/device/i);
    }
  });

  it("keeps a failed setup's server detail off the chip and behind See details", () => {
    const detail = "Could not write the host access material: [Errno 13] Permission denied: '/keys/host-1'";
    const chip = remoteStateChipFor({ state: "error", detail });
    expect(chip?.label).not.toContain("Errno");
    expect(chip?.tooltip).not.toContain("Errno");
    expect(chip?.error?.message).not.toContain("Errno");
    expect(chip?.error?.details).toBe(detail);
    expect(remoteStateChipFor({ state: "error", detail: null })?.error?.details).toBe("");
  });

  it("offers a details view only for the failed setup", () => {
    for (const state of ALL_STATES.filter((state) => state !== "error")) {
      expect(remoteStateChipFor({ state })?.error, state).toBeNull();
    }
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
    expect(unavailable.tooltip).toContain("device that created this workspace");
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

describe("removeRecordFailureFor", () => {
  it("tells a held lease plainly, with the server's text behind See details and no report asked", () => {
    const refusal =
      "This workspace's machine still holds its cloud lease, so its record cannot be removed; destroy the workspace instead.";
    const failure = removeRecordFailureFor({ status: 409, body: { error: refusal, code: "LEASE_ACTIVE" } });
    expect(failure.error.message).toBe(
      "This workspace's machine is still held in Imbue Cloud, so it can't just be taken off the list. " +
        "*Remove the workspace instead.*",
    );
    expect(failure.error.details).toContain(refusal);
    expect(failure.isBugReportOffered).toBe(false);
  });

  it("tells a record from a newer Imbue Studio in our words, with the server's text behind See details", () => {
    const refusal = "This workspace was managed by a newer version of the app; update the app to manage it.";
    const failure = removeRecordFailureFor({ status: 409, body: { error: refusal, code: "RECORD_TOO_NEW" } });
    expect(failure.error.message).toBe(
      "This workspace was set up by a newer version of Imbue Studio. *Update Imbue Studio to take it off the list.*",
    );
    expect(failure.error.details).toContain(refusal);
    expect(failure.isBugReportOffered).toBe(false);
    expect(failure.isUpdateAppOffered).toBe(true);
  });

  it("offers a restart when the window's own login lapsed", () => {
    const failure = removeRecordFailureFor({ status: 403, body: { error: "Not authenticated" } });
    expect(failure.error.message).toBe(WINDOW_LOGIN_LAPSED_SENTENCE);
    expect(failure.isRestartAppOffered).toBe(true);
  });

  it("keeps server text and bare statuses behind See details under the fallback sentence", () => {
    const failure = removeRecordFailureFor({ status: 503, body: { error: "Sync is unavailable" } });
    expect(failure.error.message).toBe(
      "Couldn't take this workspace off the list. Try again. If this persists, report a bug.",
    );
    expect(failure.error.details).toBe("HTTP 503\nSync is unavailable");
    expect(failure.isBugReportOffered).toBe(true);
    expect(removeRecordFailureFor({ status: 502, body: null }).error.details).toBe("HTTP 502");
  });

  it("says Imbue Studio didn't respond when no answer came back", () => {
    const failure = removeRecordFailureFor({ status: 0, body: null });
    expect(failure.error.message).toBe("Imbue Studio didn't respond. Try again in a moment.");
    expect(failure.isRestartAppOffered).toBe(false);
  });
});

describe("remoteTileEntries", () => {
  const remote = (id: string) => ({ id, is_remote: true });

  it("keeps record-only entries and drops live ones", () => {
    const entries = [remote("agent-a"), { id: "agent-live", is_remote: false }];
    expect(remoteTileEntries(entries, [], () => false).map((e) => e.id)).toEqual(["agent-a"]);
  });

  it("hides the tile of a workspace being destroyed here, so its record cannot be removed mid-destroy", () => {
    const entries = [remote("agent-a"), remote("agent-destroying")];
    const isDestroying = (agentId: string) => agentId === "agent-destroying";
    expect(remoteTileEntries(entries, [], isDestroying).map((e) => e.id)).toEqual(["agent-a"]);
  });

  it("leaves a failed destroy whose host is gone to its own row", () => {
    const entries = [remote("agent-a"), remote("agent-orphaned")];
    expect(remoteTileEntries(entries, ["agent-orphaned"], () => false).map((e) => e.id)).toEqual(["agent-a"]);
  });
});
