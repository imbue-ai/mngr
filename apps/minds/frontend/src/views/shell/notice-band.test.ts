import { describe, expect, it } from "vitest";
import { localPageNoticeFor, noticeBandFor, workspacePageNoticeFor } from "./notice-band";

describe("noticeBandFor", () => {
  it("shows nothing while the machine and the app are both healthy", () => {
    expect(noticeBandFor("healthy", "healthy", true)).toBeNull();
  });

  it("bands a machine that stops answering, and keeps one payload across the recovery states", () => {
    const stuck = noticeBandFor("stuck", "healthy", true);
    const recovering = noticeBandFor("recovering", "healthy", true);
    expect(stuck?.key).toBe("workspace-recovering");
    // Recovery steps between stuck and recovering on its own; sharing the
    // payload is what keeps the strip from rewriting itself mid-read.
    expect(recovering).toEqual(stuck);
    expect(stuck?.action?.kind).toBe("open-recovery");
  });

  it("separates a spent restart from one still in progress", () => {
    const failed = noticeBandFor("recovery_failed", "healthy", true);
    expect(failed?.key).toBe("workspace-restart-failed");
    expect(failed?.variant).toBe("error");
    expect(failed?.message).not.toBe(noticeBandFor("stuck", "healthy", true)?.message);
  });

  it("states the condition without recounting a restart the user never made", () => {
    // The app starts a wedged machine unasked, so an account of a failed
    // recovery usually describes an event the user never caused and never saw.
    // The remedy and its cost live on the card behind the action.
    expect(noticeBandFor("recovery_failed", "healthy", true)?.message).toBe("This machine stopped responding.");
  });

  it("names the backend it cannot reach instead of the machine that reads stuck because of it", () => {
    // The machine is unreachable because its provider is, and a restart routes
    // through that same provider -- so the band explains the condition rather
    // than repeating the symptom. It keeps the recovering key: this is still
    // "lost contact, still trying", only better explained, and rewriting the
    // strip as a provider error lands and clears would only interrupt a read.
    const band = noticeBandFor("stuck", "healthy", true, { unreachableProviderLabel: "Imbue Cloud" });
    expect(band?.key).toBe("workspace-recovering");
    // The cause, and nothing else: the band is one line, and the card behind
    // the action is where what it means for this machine belongs.
    expect(band?.message).toBe("Can't connect to Imbue Cloud");
    expect(band?.action?.kind).toBe("open-recovery");
    // The card behind it carries the provider's own error verbatim.
    expect(noticeBandFor("recovery_failed", "healthy", true, { unreachableProviderLabel: "Imbue Cloud" })?.message).toBe(
      band?.message,
    );
  });

  it("leaves a healthy machine unbanded even while its provider is erroring", () => {
    // A stale row is not a broken machine: the workspace keeps answering
    // through the forward whatever discovery last managed to poll.
    expect(noticeBandFor("healthy", "healthy", true, { unreachableProviderLabel: "Imbue Cloud" })).toBeNull();
  });

  it("blames this device when that is what could not connect, and only while the machine reads unhealthy", () => {
    // A machine the app could not build a connection to reads stuck like any
    // other, and the band would otherwise report a generic loss of contact for
    // a machine that is very likely running fine. It keeps the recovering key,
    // so the strip is not rewritten as the explanation lands.
    const band = noticeBandFor("stuck", "healthy", true, { isDeviceCannotConnect: true });
    expect(band?.message).toBe("Can't connect to this machine from this device");
    expect(band?.key).toBe("workspace-recovering");
    expect(band?.action?.kind).toBe("open-recovery");
    // The terminal state is the same condition better explained, so it reads alike.
    expect(noticeBandFor("recovery_failed", "healthy", true, { isDeviceCannotConnect: true })?.message).toBe(
      band?.message,
    );
    // A machine that is answering is not one this device cannot reach.
    expect(noticeBandFor("healthy", "healthy", true, { isDeviceCannotConnect: true })).toBeNull();
  });

  it("prefers the provider's outage over this device's when both are reported", () => {
    // A whole backend being down is the larger fact, and the one with a name
    // to give; the device-side line would say less about the same outage.
    expect(
      noticeBandFor("stuck", "healthy", true, { unreachableProviderLabel: "Imbue Cloud", isDeviceCannotConnect: true })
        ?.message,
    ).toBe("Can't connect to Imbue Cloud");
  });

  it("names the dead consumer instead of the stuck machine it produces", () => {
    // Every machine reads stuck while the consumer is dead, and restarting
    // one would not help -- only the app restart does.
    const band = noticeBandFor("stuck", "blocked", true);
    expect(band?.key).toBe("discovery-blocked");
    expect(band?.action?.kind).toBe("restart-app");
  });

  it("leaves a reconnecting consumer to the shell's own indicator", () => {
    expect(noticeBandFor("healthy", "reconnecting", true)).toBeNull();
  });

  it("withholds the band from hub pages, which have no machine behind it", () => {
    expect(noticeBandFor("stuck", "healthy", false)).toBeNull();
    expect(noticeBandFor("recovery_failed", "blocked", false)).toBeNull();
  });

  it("names this device's dead network rather than the machine that reads stuck because of it", () => {
    const band = noticeBandFor("stuck", "healthy", true, { deviceEnvironment: "OFFLINE" });
    // Still "lost contact, still trying", so it shares the recovering key and
    // does not rewrite the strip as the condition is diagnosed.
    expect(band?.key).toBe("workspace-recovering");
    expect(band?.message).toBe("No network connection.");
    expect(band?.action?.kind).toBe("open-recovery");
  });

  it("never tells a user with a working browser that they are offline", () => {
    // On an SSH-blocking network the user can see their browser working, so
    // claiming they are offline is a claim they know to be false -- and they
    // would discount whatever the app says next.
    const band = noticeBandFor("stuck", "healthy", true, { deviceEnvironment: "SSH_BLOCKED" });
    expect(band?.message).toBe("This network blocks the connection to your machines.");
  });

  it("blames this device before it blames the backend behind it", () => {
    // A laptop with no network cannot reach the provider either, so the
    // provider's poll errors too -- and naming the provider would blame a
    // backend that is working for a condition the user can actually fix.
    const band = noticeBandFor("stuck", "healthy", true, {
      unreachableProviderLabel: "Imbue Cloud",
      deviceEnvironment: "OFFLINE",
    });
    expect(band?.message).toBe("No network connection.");
  });

  it("says the network is down on a machine page before any machine has been blamed", () => {
    // Landing straight on a machine with the wifi off: nothing is stuck yet,
    // and the hub page's own notice is not mounted here. Without the device's
    // condition the page says nothing at all while the frame visibly fails to
    // load.
    const band = noticeBandFor("healthy", "healthy", true, { deviceEnvironment: "OFFLINE" });
    expect(band?.message).toBe("No network connection.");
    // Nothing is stuck, so there is no recovery card to open.
    expect(band?.action).toBeNull();
  });

  it("keeps naming the device over a machine whose restart already failed", () => {
    // The terminal state is the one most likely to be read as the machine's own
    // fault, and it is where a restart the network doomed ends up. Left to the
    // recovery_failed branch, the band would say "This machine stopped
    // responding." about a machine nothing here ever reached.
    const failed = noticeBandFor("recovery_failed", "healthy", true, { deviceEnvironment: "SSH_BLOCKED" });
    expect(failed?.message).toBe("This network blocks the connection to your machines.");
  });

  it("does not blame this device's network for a machine that runs on it", () => {
    // A docker container answers over loopback with the wifi off, so a dead
    // network explains nothing about its outage -- and the server withholds no
    // restart from it for the same reason. Letting the device's condition
    // displace its recovery notice would blame the network for a machine the
    // network cannot touch, and send the user to a card whose restart would
    // have worked.
    const band = noticeBandFor("stuck", "healthy", true, {
      deviceEnvironment: "OFFLINE",
      isWorkspaceNetworkDependent: false,
    });
    expect(band?.message).toBe("Lost connection to this machine. Reconnecting…");
    const healthy = noticeBandFor("healthy", "healthy", true, {
      deviceEnvironment: "OFFLINE",
      isWorkspaceNetworkDependent: false,
    });
    expect(healthy).toBeNull();
  });

  it("lets the restart the user asked for narrate itself", () => {
    // The band reports the restart rather than waiting for a network: the
    // user's own stop+start bounce is in flight either way, and its progress
    // is what they asked to see.
    const band = noticeBandFor("recovering", "healthy", true, {
      deviceEnvironment: "OFFLINE",
      recoveryKind: "restart",
    });
    expect(band?.message).toBe("Lost connection to this machine. Reconnecting…");
  });

  it("keeps naming the device over the app's own unattended start", () => {
    // The app enters "recovering" on its own within seconds of any network
    // flap, and stays there for as long as the network is down -- the whole of
    // the episode the device's condition exists to explain. Hiding it behind
    // the dispatch left a user with a dead wifi reading "Lost connection" for
    // the length of a lid-closed sleep. The tracker's word ("start") and no
    // word at all (null) both decline the exception; only the user's click
    // earns it.
    for (const recoveryKind of ["start", null] as const) {
      const band = noticeBandFor("recovering", "healthy", true, {
        deviceEnvironment: "OFFLINE",
        recoveryKind,
      });
      expect(band?.message).toBe("No network connection.");
    }
  });

  it("blames nobody while this device's network is unmeasured", () => {
    // After a wake the reading is blank until a probe lands, and the provider's
    // own poll errored because the laptop was asleep. Naming the provider then
    // is the wrong headline; the generic recovering line still says contact
    // was lost, without saying whose fault it is.
    const band = noticeBandFor("stuck", "healthy", true, {
      deviceEnvironment: "UNKNOWN",
      unreachableProviderLabel: "Imbue Cloud",
      isDeviceCannotConnect: true,
    });
    expect(band?.key).toBe("workspace-recovering");
    expect(band?.message).toBe("Lost connection to this machine. Reconnecting…");
    // And there is nothing to say over a healthy machine, or on a hub page.
    expect(noticeBandFor("healthy", "healthy", true, { deviceEnvironment: "UNKNOWN" })).toBeNull();
    expect(localPageNoticeFor("healthy", true, "UNKNOWN")).toBeNull();
  });

  it("does not rewrite the strip as the machine it already speaks for goes stuck", () => {
    // Same key and same line across the edge, so the band is replaced in place
    // rather than torn down and rebuilt as the condition is finally attributed.
    const before = noticeBandFor("healthy", "healthy", true, { deviceEnvironment: "OFFLINE" });
    const after = noticeBandFor("stuck", "healthy", true, { deviceEnvironment: "OFFLINE" });
    expect(before?.key).toBe(after?.key);
    expect(before?.message).toBe(after?.message);
  });
});

describe("localPageNoticeFor", () => {
  it("carries the consumer-death condition into hub pages", () => {
    expect(localPageNoticeFor("blocked")?.key).toBe("discovery-blocked");
    expect(localPageNoticeFor("healthy")).toBeNull();
    expect(localPageNoticeFor("reconnecting")).toBeNull();
  });

  it("says the same thing as the band, from the same source", () => {
    // The two surfaces drifted apart when each wrote its own copy.
    expect(localPageNoticeFor("blocked")).toEqual(noticeBandFor("healthy", "blocked", true));
  });

  it("carries this device's condition into hub pages, where there is no band to read", () => {
    // One fact about the laptop, however many machines it takes down -- and a
    // user looking at the machines list has nothing else telling them why every
    // row went quiet at once.
    const notice = localPageNoticeFor("healthy", true, "OFFLINE");
    expect(notice?.key).toBe("environment-blocked");
    expect(notice?.message).toBe("No network connection.");
    // Nothing in the app fixes it, so it offers nothing.
    expect(notice?.action).toBeNull();
    expect(localPageNoticeFor("healthy", true, "NONE")).toBeNull();
  });

  it("keeps the dead consumer ahead of the dead network", () => {
    // A dead consumer needs the app restart whatever the network is doing, and
    // the network coming back would not fix it.
    expect(localPageNoticeFor("blocked", true, "OFFLINE")?.key).toBe("discovery-blocked");
  });
});

describe("restart-app availability", () => {
  it("drops the action where there is no app to restart", () => {
    // Browser mode has no main process: the button would be inert, which is
    // worse than stating the condition and offering nothing.
    const band = noticeBandFor("healthy", "blocked", true, { isRestartAppAvailable: false });
    expect(band?.message).toBe(noticeBandFor("healthy", "blocked", true)?.message);
    expect(band?.action).toBeNull();
    expect(localPageNoticeFor("blocked", false)?.action).toBeNull();
  });
});

describe("noticeBandFor, out of date", () => {
  it("bands a healthy machine that is a version behind", () => {
    const band = noticeBandFor("healthy", "healthy", true, { standingUpdateNotice: "out-of-date" });
    expect(band?.key).toBe("workspace-out-of-date");
    expect(band?.variant).toBe("warn");
    expect(band?.action?.kind).toBe("update-workspace");
  });

  it("says nothing about the version while the machine is not answering", () => {
    // A health condition is happening now; the version notice will still be
    // true tomorrow.
    expect(noticeBandFor("stuck", "healthy", true, { standingUpdateNotice: "out-of-date" })?.key).toBe("workspace-recovering");
    expect(noticeBandFor("recovery_failed", "healthy", true, { standingUpdateNotice: "out-of-date" })?.key).toBe(
      "workspace-restart-failed",
    );
  });

  it("says nothing about the version while discovery is dead", () => {
    expect(noticeBandFor("healthy", "blocked", true, { standingUpdateNotice: "out-of-date" })?.key).toBe("discovery-blocked");
  });

  it("bands nothing when no machine is displayed", () => {
    expect(noticeBandFor("healthy", "healthy", false, { standingUpdateNotice: "out-of-date" })).toBeNull();
  });

  it("bands a machine too old to update in place with the same way into the modal", () => {
    const band = noticeBandFor("healthy", "healthy", true, { standingUpdateNotice: "needs-recreation" });
    expect(band?.key).toBe("workspace-needs-recreation");
    expect(band?.variant).toBe("warn");
    expect(band?.action?.kind).toBe("update-workspace");
    // Still a standing condition: a health condition outranks it.
    expect(noticeBandFor("stuck", "healthy", true, { standingUpdateNotice: "needs-recreation" })?.key).toBe(
      "workspace-recovering",
    );
  });
});

describe("noticeBandFor, an update run", () => {
  it("tells the reader which half of the run they are in", () => {
    // Preparing touches nothing; applying takes the services away.
    const preparing = noticeBandFor("healthy", "healthy", true, { updateRunPhase: "preparing" });
    const applying = noticeBandFor("healthy", "healthy", true, { updateRunPhase: "applying" });
    expect(preparing?.key).toBe("workspace-update-preparing");
    expect(applying?.key).toBe("workspace-update-applying");
    expect(applying?.message).toContain("services restart");
    expect(preparing?.action?.kind).toBe("update-workspace");
  });

  it("lets the apply speak over the machine's own health, because it explains it", () => {
    // Minds took those services down itself; "Lost connection" there misreads
    // its own work.
    expect(noticeBandFor("stuck", "healthy", true, { updateRunPhase: "applying" })?.key).toBe(
      "workspace-update-applying",
    );
    expect(noticeBandFor("recovery_failed", "healthy", true, { updateRunPhase: "applying" })?.key).toBe(
      "workspace-update-applying",
    );
  });

  it("does not speak over this device's own condition, which no run explains", () => {
    // An apply explains nothing about the laptop's network, and over a dead one
    // the phase can no longer be refreshed. A loopback machine is untouched.
    expect(
      noticeBandFor("healthy", "healthy", true, { updateRunPhase: "applying", deviceEnvironment: "OFFLINE" })
        ?.message,
    ).toBe("No network connection.");
    expect(
      noticeBandFor("healthy", "healthy", true, {
        updateRunPhase: "applying",
        deviceEnvironment: "OFFLINE",
        isWorkspaceNetworkDependent: false,
      })?.key,
    ).toBe("workspace-update-applying");
  });

  it("still speaks over a device nothing has been measured on yet", () => {
    // UNKNOWN names nobody; an apply we know is running beats the generic
    // recovering line.
    expect(
      noticeBandFor("stuck", "healthy", true, { updateRunPhase: "applying", deviceEnvironment: "UNKNOWN" })?.key,
    ).toBe("workspace-update-applying");
  });

  it("leaves a machine that dies while merely preparing to the ordinary outage notice", () => {
    // Nothing has been applied, so nothing about the run explains the outage.
    expect(noticeBandFor("stuck", "healthy", true, { updateRunPhase: "preparing" })?.key).toBe("workspace-recovering");
  });

  it("names what a held run is waiting on when the run said", () => {
    // The hold is about the reader's creation; the run's own line says which.
    const band = noticeBandFor("healthy", "healthy", true, {
      updateRunPhase: "waiting",
      updateHoldDetail: "Your dashboard widget has no place in the new layout.",
    });
    expect(band?.key).toBe("workspace-update-waiting");
    expect(band?.variant).toBe("warn");
    expect(band?.message.startsWith("Your dashboard widget has no place in the new layout.")).toBe(true);
    expect(band?.message).toContain("waiting for your decision");
  });

  it("says a waiting run is the reader's move rather than something in progress", () => {
    const band = noticeBandFor("healthy", "healthy", true, { updateRunPhase: "waiting" });
    expect(band?.key).toBe("workspace-update-waiting");
    expect(band?.variant).toBe("warn");
  });

  it("reports how the last run ended, which the row badge cannot do from inside the machine", () => {
    const failed = noticeBandFor("healthy", "healthy", true, { updateRunOutcome: "failed" });
    const attention = noticeBandFor("healthy", "healthy", true, { updateRunOutcome: "needs-attention" });
    expect(failed?.variant).toBe("error");
    // The update landed, so this is neither an error nor a warning: a note.
    expect(attention?.variant).toBe("info");
  });

  it("prefers what a machine is doing now over how its last attempt ended", () => {
    const band = noticeBandFor("healthy", "healthy", true, {
      updateRunPhase: "preparing",
      updateRunOutcome: "failed",
    });
    expect(band?.key).toBe("workspace-update-preparing");
  });

  it("keeps a run ahead of the standing version notice, and discovery death ahead of both", () => {
    expect(noticeBandFor("healthy", "healthy", true, { updateRunPhase: "preparing", standingUpdateNotice: "out-of-date" })?.key).toBe(
      "workspace-update-preparing",
    );
    expect(noticeBandFor("healthy", "blocked", true, { updateRunPhase: "applying" })?.key).toBe("discovery-blocked");
  });
});

describe("workspacePageNoticeFor", () => {
  it("reaches the same payload as the band, so the two cannot drift apart", () => {
    // The payload only; which machines get one is standingUpdateNotice's call.
    expect(workspacePageNoticeFor("out-of-date")).toEqual(noticeBandFor("healthy", "healthy", true, { standingUpdateNotice: "out-of-date" }));
  });

  it("says nothing for a machine that is current", () => {
    expect(workspacePageNoticeFor("none")).toBeNull();
  });
});
