import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  RecoveryCardBody,
  RecoveryTroubleshooting,
  recoveryBusyActionLabel,
  recoveryHeading,
} from "./RecoveryCard";
import { RecoveryModel, type LifecycleDeps, type RecoveryInfo } from "../../models/backups";
import { healthBadgeLabelFor } from "../pages/landing-controls";
import type { RecoveryKind } from "../../models/health";
import { allText, attrsOf, collectVnodes, renderRoot, renderedText } from "../../testing";

/** Deps that answer nothing and schedule nothing: these tests render a state,
 * they do not drive one. */
const IDLE_DEPS: LifecycleDeps = {
  getJson: async () => null,
  postJson: async () => ({ status: 500, json: null }),
  deleteResource: async () => 500,
  openEventSource: () => ({ close: () => {}, onmessage: null, onerror: null }),
  schedule: () => {},
  redraw: () => {},
};

const UNRESPONSIVE: RecoveryInfo = {
  agent_id: "agent-33",
  workspace_name: "my-machine",
  health: "recovery_failed",
  health_error: "",
  recovery_kind: null,
  ssh_command: "",
  is_host_offline: false,
  device_environment: "NONE",
  is_backend_unreachable: false,
  provider_label: "",
  unreachable_reason: "",
  is_device_cannot_connect: false,
  device_error_detail: "",
};

/** A model holding a given reading, with no poller running behind it. */
function modelShowing(info: RecoveryInfo): RecoveryModel {
  const model = new RecoveryModel("agent-33", IDLE_DEPS);
  model.info = info;
  return model;
}

/** A model that adopted the reading the way the poller does, rather than having
 * it assigned. Loading is what attaches the model to a restart it did not
 * dispatch, so a card rendered off one of these sees the state production holds
 * -- which `modelShowing` cannot reproduce. */
async function modelAttachedTo(info: RecoveryInfo): Promise<RecoveryModel> {
  const model = new RecoveryModel("agent-33", { ...IDLE_DEPS, getJson: async () => info });
  await model.load();
  return model;
}

/** A model that dispatched its own recovery, of the given kind, and is still
 * following it. */
async function modelRecoveringOwn(info: RecoveryInfo, kind: RecoveryKind): Promise<RecoveryModel> {
  const model = new RecoveryModel("agent-33", {
    ...IDLE_DEPS,
    postJson: async () => ({ status: 202, json: null }),
  });
  model.info = info;
  await model.dispatchRecovery(kind);
  return model;
}

/** What the card puts on screen for a given reading of the machine. */
function renderCard(info: RecoveryInfo, model = modelShowing(info)): string {
  return renderedText(renderRoot(RecoveryCardBody, { model }));
}

/** Press the button reading `label`, so a test can assert what a click does
 * rather than only what the card says. */
function clickButtonLabeled(rendered: m.Vnode, label: string): void {
  const button = collectVnodes(rendered).find(
    (vnode) => typeof attrsOf(vnode).onclick === "function" && allText(vnode.children).trim() === label,
  );
  if (button === undefined) throw new Error(`no button labeled ${label}`);
  (attrsOf(button).onclick as () => void)();
}

/** The same, with the app running as the desktop client. The Electron bridge
 * feature-detects `window.mindsNative`, and the device-side card asks it whether
 * restarting the app is something this build can actually do. */
function renderCardOnDesktop(info: RecoveryInfo): string {
  vi.stubGlobal("window", { mindsNative: { retry: () => {} } });
  return renderCard(info);
}

/** And in a plain browser: a window with no native surface behind it. */
function renderCardInBrowser(info: RecoveryInfo): string {
  vi.stubGlobal("window", {});
  return renderCard(info);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("recoveryHeading", () => {
  it("reserves the unresponsive verdict for a machine a restart has already failed", () => {
    // stuck is "we don't know yet": the app is still checking, and the machine
    // may come back on its own. Calling that unresponsive would be claiming a
    // verdict the classifier declined to give.
    expect(recoveryHeading("my-machine", "recovery_failed", false)).toBe("my-machine unresponsive");
    expect(recoveryHeading("my-machine", "stuck", false)).toBe("my-machine isn't responding yet.");
    expect(recoveryHeading("my-machine", "healthy", true)).toBe("my-machine is stopped.");
    expect(recoveryHeading("my-machine", "healthy", false)).toBe("my-machine is responding again.");
  });

  it("claims a restart only where something is known to be recovering", () => {
    // The three in-flight readings, and why they differ. A stopped host is
    // genuinely being booted. A RESTART is only ever the user's own click, so
    // their action makes the claim honest. A START is what the app fires at any
    // machine that stops answering -- it no-ops against a host that is already
    // up, so "Restarting" would tell the user their work was interrupted when
    // nothing happened to the machine.
    expect(recoveryHeading("my-machine", "recovering", true, null)).toBe("Bringing my-machine back online...");
    expect(recoveryHeading("my-machine", "recovering", false, "restart")).toBe("Restarting my-machine...");
    expect(recoveryHeading("my-machine", "recovering", false, "start")).toBe("Reconnecting to my-machine...");
    // No reading at all is not evidence of a restart either.
    expect(recoveryHeading("my-machine", "recovering", false, null)).toBe("Reconnecting to my-machine...");
  });

  it("agrees with the machines-list badge about the same recovery", () => {
    // The card and the row are two views of one episode, and a user looking at
    // the list and then opening the card must not be told two different things
    // about it. Both read the tracker's recovery kind off their own frame,
    // so the pairing is what keeps them in step -- the badge silently reporting
    // the weaker word for a bounce the user themselves clicked is exactly the
    // divergence this pins.
    for (const kind of ["start", "restart", null] as const) {
      const isRestartClaimedOnCard = recoveryHeading("my-machine", "recovering", false, kind).startsWith("Restarting");
      const isRestartClaimedOnBadge = healthBadgeLabelFor("recovering", false, kind, false) === "Restarting...";
      expect(isRestartClaimedOnBadge).toBe(isRestartClaimedOnCard);
    }
  });

  it("agrees with its own action button about the same recovery", () => {
    // The shortest range at which one episode can be described two ways: the
    // button sits directly under the heading, so a fixed "Restarting..." there
    // contradicts a heading that has just declined to claim a restart.
    for (const isHostOffline of [true, false]) {
      for (const kind of ["start", "restart", null] as const) {
        const isRestartClaimedInHeading = recoveryHeading(
          "my-machine",
          "recovering",
          isHostOffline,
          kind,
        ).startsWith("Restarting");
        const isRestartClaimedOnButton = recoveryBusyActionLabel(isHostOffline, kind) === "Restarting...";
        expect(isRestartClaimedOnButton).toBe(isRestartClaimedInHeading);
      }
    }
  });
});

describe("RecoveryCardBody", () => {
  it("names the machine, what it costs, and offers the restart", () => {
    const text = renderCard(UNRESPONSIVE);
    expect(text).toContain("my-machine unresponsive");
    expect(text).toContain("In progress work will be interrupted, but saved data will not be lost.");
    expect(text).toContain("Restart Machine");
    expect(text).toContain("Report a problem");
  });

  it("offers a working restart on a machine it cannot classify yet", () => {
    // A surface the user opened on purpose never withholds the action; the
    // copy simply does not urge it.
    const text = renderCard({ ...UNRESPONSIVE, health: "stuck" });
    expect(text).toContain("my-machine isn't responding yet.");
    expect(text).toContain("Minds is still checking what's wrong.");
    expect(text).toContain("Restart Machine");
  });

  it("names the backend and withholds the restart when the backend is unreachable", () => {
    // The restart is dispatched through the same provider, so offering it here
    // would be offering an action that cannot work. The provider's own error is
    // shown verbatim rather than collapsed into copy minds authored.
    const text = renderCard({
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "Docker",
      unreachable_reason: "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    });

    expect(text).toContain("my-machine unreachable: Can't connect to Docker");
    expect(text).toContain("Minds will reconnect you to your machine as soon as it can be reached again.");
    expect(text).toContain("Cannot connect to the Docker daemon at unix:///var/run/docker.sock");
    expect(text).not.toContain("Restart Machine");
    // A card with no remedy still has to let the user say something about it.
    expect(text).toContain("Report a problem");
  });

  it("still reports a machine that is answering, whatever its provider's last poll did", () => {
    // A provider poll can error while its machines keep answering through the
    // forward, so an erroring provider is not by itself a machine minds cannot
    // reach. The band withholds the same verdict on a healthy machine; the card
    // has to agree, and it is the surface that owes the user the ending.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "healthy",
      is_backend_unreachable: true,
      provider_label: "Imbue Cloud",
      unreachable_reason: "could not reach Imbue Cloud",
    });

    expect(text).toContain("my-machine is responding again.");
    expect(text).not.toContain("Can't connect to Imbue Cloud");
    expect(text).toContain("Restart Machine");
  });

  it("explains this device's dead network and offers no restart to route over it", () => {
    // The restart would go over the same network that is down, so there is
    // nothing here for the user to decide -- not even a disabled button.
    const text = renderCard({ ...UNRESPONSIVE, health: "stuck", device_environment: "OFFLINE" });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain("This device has no network connection.");
    expect(text).toContain("Minds will reconnect to your machine as soon as it does.");
    expect(text).toContain("Waiting for network");
    expect(text).not.toContain("Restart Machine");
    expect(text).toContain("Report a problem");
  });

  it("tells a user on an SSH-blocking network what is actually wrong", () => {
    // Their browser works, so "you are offline" is a claim they can see is
    // false -- and unlike a dead network, this one never fixes itself.
    const text = renderCard({ ...UNRESPONSIVE, health: "stuck", device_environment: "SSH_BLOCKED" });

    expect(text).toContain("This network blocks the connection Minds uses to reach your machines (SSH).");
    expect(text).toContain("Try another network or a VPN.");
    expect(text).not.toContain("This device has no network connection.");
    expect(text).not.toContain("Restart Machine");
  });

  it("blames this device before it blames the backend behind it", () => {
    // A laptop with no network cannot reach the provider either, so the
    // provider's error is a symptom of the same condition rather than a second
    // one -- and naming the provider would send the user after the wrong thing.
    const text = renderCard({
      ...UNRESPONSIVE,
      device_environment: "OFFLINE",
      is_backend_unreachable: true,
      provider_label: "Imbue Cloud",
      unreachable_reason: "could not reach Imbue Cloud",
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).not.toContain("Can't connect to Imbue Cloud");
  });

  it("explains the device for a machine stuck before the network died", () => {
    // No stuck edge fired under the condition, so no dispatch was ever
    // withheld for this machine -- and the band that sent the user here named
    // the device, so the card must not blame the backend.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "OFFLINE",
      is_backend_unreachable: true,
      provider_label: "Imbue Cloud",
      unreachable_reason: "could not reach Imbue Cloud",
    });

    expect(text).toContain("This device has no network connection.");
    expect(text).not.toContain("Can't connect to Imbue Cloud");
    expect(text).not.toContain("Restart Machine");
  });

  it("keeps narrating the restart the user asked for", () => {
    // The device may well be offline, but the user's own stop+start bounce is
    // in flight and its progress is what they asked to see.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "recovering",
      recovery_kind: "restart",
      device_environment: "OFFLINE",
    });

    expect(text).toContain("Restarting my-machine...");
    expect(text).not.toContain("This device has no network connection.");
    expect(text).not.toContain("Waiting for network");
  });

  it("explains the device over the app's own unattended start", () => {
    // The app enters "recovering" unasked within seconds of a network flap and
    // stays there for as long as the network is down, which is the whole of
    // the episode this explanation exists for. The tracker's word ("start")
    // and no word at all (null) both decline the exception the user's click
    // earns.
    for (const recovery_kind of ["start", null] as const) {
      const text = renderCard({
        ...UNRESPONSIVE,
        health: "recovering",
        recovery_kind,
        device_environment: "OFFLINE",
      });
      expect(text).toContain("This device has no network connection.");
      expect(text).toContain("Waiting for network");
      expect(text).not.toContain("Reconnecting to my-machine...");
    }
  });

  it("withholds the backend verdict while this device's network is unmeasured", () => {
    // After a wake the reading is blank until a probe lands, and the provider's
    // poll errored because the laptop was asleep -- so the verdict that names
    // the provider is built on no measurement, and the card declines to state
    // it. The machine's ordinary state describes the wait instead.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "UNKNOWN",
      is_backend_unreachable: true,
      provider_label: "Imbue Cloud",
      unreachable_reason: "The read operation timed out",
    });
    expect(text).not.toContain("Can't connect to Imbue Cloud");
    expect(text).not.toContain("Waiting for network");
    expect(text).toContain("my-machine isn't responding yet.");
  });

  it("withholds the device-side connection verdict while the network is unmeasured", () => {
    // Same withholding as the backend verdict above, and as the band's line
    // for this state: the copy claims the connection failed on a network that
    // works, and before a probe lands nothing has measured that network.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "UNKNOWN",
      is_device_cannot_connect: true,
      device_error_detail: "pool timeout",
    });
    expect(text).not.toContain("from this device");
    expect(text).toContain("my-machine isn't responding yet.");
  });

  it("keeps narrating a restart this card just started, before the poll catches up", () => {
    // The click flips the model at once; info.health only moves on the next 2s
    // poll, and a read already in flight can land after it still saying stuck.
    // Reading the server's health alone would swap the spinner and the log
    // lines for "Waiting for network..." over a restart that is running.
    const info: RecoveryInfo = { ...UNRESPONSIVE, health: "stuck", device_environment: "OFFLINE" };
    const model = modelShowing(info);
    model.isRecoveryRunning = true;
    // A click from this card is a full stop+start bounce, which is what
    // licenses the card to call it a restart at all.
    model.dispatchedRecoveryKind = "restart";

    const text = renderCard(info, model);

    expect(text).toContain("Restarting my-machine...");
    expect(text).not.toContain("Waiting for network");
  });

  it("returns the machine to its normal states once it answers again", () => {
    // The device may still be blocked for everything else, but this machine is
    // answering -- the card must not keep the waiting state on it.
    const text = renderCard({ ...UNRESPONSIVE, health: "healthy", device_environment: "OFFLINE" });

    expect(text).toContain("my-machine is responding again.");
    expect(text).not.toContain("Waiting for network");
  });

  it("reads a recovery nobody started here as busy, not as an idle machine", async () => {
    // The unattended dispatcher starts a wedged machine on its own, so the card
    // must not sit there offering to start a second one. It is a plain start,
    // so the heading says reconnecting rather than claiming a restart.
    //
    // Loaded rather than assigned on purpose: adopting a "recovering" reading is
    // what attaches the model to that episode, and a card that read its own
    // busy flag as evidence of its own click would claim a restart here.
    const info = { ...UNRESPONSIVE, health: "recovering", recovery_kind: "start" as const };
    const model = await modelAttachedTo(info);
    // The precondition that makes this a regression test: attaching sets the
    // same busy flag the card's own click sets, so the flag cannot stand in for
    // "this card dispatched it".
    expect(model.isRecoveryRunning).toBe(true);
    const text = renderCard(info, model);
    expect(text).toContain("Reconnecting to my-machine...");
    // Busy, and busy in the same words the heading chose: the button under a
    // heading that declines to claim a restart must decline to claim one too.
    expect(text).toContain("Reconnecting...");
    expect(text).not.toContain("Restarting...");
    expect(text).not.toContain("Restart Machine");
  });

  it("says restarting from the click, before the tracker has caught up", async () => {
    // The card's own button dispatches a full bounce, so it is a restart
    // whatever the server has published yet -- and the window before the
    // tracker answers is exactly when the user is looking at the card they
    // just clicked.
    const info = { ...UNRESPONSIVE, health: "stuck" };
    const text = renderCard(info, await modelRecoveringOwn(info, "restart"));
    expect(text).toContain("Restarting my-machine...");
  });

  it("does not claim a restart for a start-only dispatch of its own", async () => {
    // The recovery page's "open this stopped machine" click-through runs the
    // idempotent start through this same model. It may well no-op, so the
    // claim the full bounce earns is not available to it.
    const info = { ...UNRESPONSIVE, health: "stuck" };
    const text = renderCard(info, await modelRecoveringOwn(info, "start"));
    expect(text).toContain("Reconnecting to my-machine...");
  });

  it("blames this device, not the machine, when the connection never left the device", () => {
    // The failure was raised before anything was sent, so the machine is not
    // implicated and bouncing it would interrupt real work to fix a fault that
    // is not its own. Restarting the app is the remedy for both causes that
    // land here, and the verbatim error is what makes a broken install
    // diagnosable at all.
    const text = renderCardOnDesktop({
      ...UNRESPONSIVE,
      is_device_cannot_connect: true,
      device_error_detail: "No known_hosts file at /keys/known_hosts; refusing to connect",
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain("the connection failed on this device, before reaching it");
    expect(text).toContain("Restart Minds");
    expect(text).toContain("No known_hosts file at /keys/known_hosts; refusing to connect");
    expect(text).not.toContain("Restart Machine");
    expect(text).toContain("Report a problem");
  });

  it("offers no app restart in a browser, where there is no app to restart", () => {
    // The bridge's restart is a no-op outside Electron, so a button here would
    // do nothing at all when clicked -- and the copy must not name a remedy the
    // user has no way to carry out. The condition, the error, and the way to
    // report it are still worth saying.
    const text = renderCardInBrowser({
      ...UNRESPONSIVE,
      is_device_cannot_connect: true,
      device_error_detail: "No known_hosts file at /keys/known_hosts; refusing to connect",
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain("the connection failed on this device, before reaching it");
    expect(text).toContain("No known_hosts file at /keys/known_hosts; refusing to connect");
    expect(text).toContain("Report a problem");
    expect(text).not.toContain("Restart Minds");
    expect(text).not.toContain("Restarting Minds rebuilds the connection");
  });

  it("outranks the restart episode's own account of the machine", () => {
    // A machine this device cannot reach goes STUCK and gets a start dispatched
    // at it whether or not anything is wrong with it, so RECOVERY_FAILED here is
    // an effect of the device-side fault. Reporting it would blame the machine
    // for the app's own broken connection.
    const text = renderCardOnDesktop({
      ...UNRESPONSIVE,
      health: "recovery_failed",
      health_error: "The system interface did not respond within 300s of the host restart.",
      is_device_cannot_connect: true,
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).not.toContain("my-machine unresponsive");
  });

  it("stops blaming this device once the machine is answering", () => {
    // Whatever failed earlier is not failing now, and the card that stayed up
    // owes the user the ending rather than a stale fault.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "healthy",
      is_device_cannot_connect: true,
      device_error_detail: "pool timeout",
    });

    expect(text).toContain("my-machine is responding again.");
    expect(text).not.toContain("from this device");
  });

  it("reports a machine that came back without also calling it unresponsive", () => {
    // The card outlives the failure that raised it: the server marks the
    // machine healthy BEFORE the restart operation reports done, so a card that
    // stays up through a successful restart is reading a healthy machine. It
    // used to fall through to the still-checking copy and render that over the
    // success -- one card saying both things at once.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    model.isRecoverySucceeded = true;
    const text = renderedText(renderRoot(RecoveryCardBody, { model }));
    expect(text).toContain("my-machine is responding again.");
    expect(text).toContain("This machine is answering again.");
    expect(text).not.toContain("isn't responding yet");
    expect(text).not.toContain("Minds is still checking what's wrong.");
  });

  it("keeps reporting the restart as running on a card that is about to dismiss itself", () => {
    // An auto-raised card leaves once the app's own probe confirms the machine.
    // Until then, an idle-reading card would offer a Restart button for the
    // restart that just ran.
    const model = modelShowing(UNRESPONSIVE);
    model.isRecoverySucceeded = true;
    const text = renderedText(renderRoot(RecoveryCardBody, { model, isSelfDismissing: true }));
    expect(text).toContain("Reconnecting...");
    expect(text).not.toContain("Restart Machine");
  });

  it("keeps that window free of the device's condition too", () => {
    // The same window as above, with a device block still recorded: the machine
    // has answered, so the block is stale and the card is one confirmation away
    // from leaving. Rendering the wait here would replace a restart that just
    // succeeded with an indefinite "Waiting for network".
    const model = modelShowing({ ...UNRESPONSIVE, health: "stuck", device_environment: "OFFLINE" });
    model.isRecoverySucceeded = true;
    const text = renderedText(renderRoot(RecoveryCardBody, { model, isSelfDismissing: true }));
    // No evidence of which recovery this is here, so the busy label is the
    // weakest honest one -- what matters is that it narrates the recovery.
    expect(text).toContain("Reconnecting...");
    expect(text).not.toContain("Waiting for network");
    expect(text).not.toContain("This device has no network connection.");
  });

  it("offers the way into the machine when the surface it is on has one", () => {
    // The recovery page stands between the reader and the machine; the modal
    // has the machine behind it and an X to get there. Only the first supplies
    // this, and when it does, entering is the action -- the card is saying
    // nothing further is needed.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    const enter = vi.fn();
    const rendered = renderRoot(RecoveryCardBody, { model, onEnterMachine: enter });

    expect(renderedText(rendered)).toContain("Open machine");
    clickButtonLabeled(rendered, "Open machine");
    expect(enter).toHaveBeenCalledOnce();
  });

  it("says nothing about entering the machine on a surface that already can", () => {
    // The modal passes nothing, and gets no button: it would sit on top of the
    // very machine it offered to open.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    expect(renderedText(renderRoot(RecoveryCardBody, { model }))).not.toContain("Open machine");
  });
});

/** What the troubleshooting block puts on screen for a given reading. */
function renderTroubleshooting(info: RecoveryInfo, recoveryError: string | null = null): string {
  const model = modelShowing(info);
  model.recoveryError = recoveryError;
  return renderedText(renderRoot(RecoveryTroubleshooting, { model }));
}

describe("RecoveryTroubleshooting", () => {
  it("stays off the card entirely when there is nothing in it", () => {
    // The restart button is what almost everyone came for; an empty block
    // between it and the bottom of the panel is pure noise.
    expect(renderTroubleshooting(UNRESPONSIVE)).toBe("");
  });

  it("carries the restart error the tracker holds and the one this card's dispatch reported", () => {
    const text = renderTroubleshooting(
      { ...UNRESPONSIVE, health_error: "Start step of host restart failed: ssh: dead" },
      "Could not start the restart (HTTP 409).",
    );
    expect(text).toContain("Troubleshooting");
    expect(text).toContain("ssh: dead");
    expect(text).toContain("Could not start the restart (HTTP 409).");
  });

  it("states a failure once when both sources are reporting the same one", () => {
    // Every server-side restart failure hands the identical message to the
    // tracker and to the operation record, so the card was rendering the same
    // sentence twice and reading as two separate faults.
    const message = "Start step of host restart failed: exited 1: Agent not found";
    const text = renderTroubleshooting({ ...UNRESPONSIVE, health_error: message }, message);
    expect(text.split(message).length - 1).toBe(1);
  });

  it("offers the SSH block only for a machine whose host coordinates are known", () => {
    const withSsh = renderTroubleshooting({ ...UNRESPONSIVE, ssh_command: "ssh -i k -p 22 user@h" });
    expect(withSsh).toContain("connect to the machine's host from a terminal");
    const withoutSsh = renderTroubleshooting({ ...UNRESPONSIVE, health_error: "boom" });
    expect(withoutSsh).not.toContain("connect to the machine's host from a terminal");
  });
});
