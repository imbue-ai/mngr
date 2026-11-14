import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  RecoveryCardBody,
  RecoveryConnectionDetails,
  isDeviceConditionShownOnCard,
  plainRecoveryProgress,
  recoveryBusyActionLabel,
  recoveryHeading,
} from "./RecoveryCard";
import {
  RecoveryModel,
  type LifecycleDeps,
  type RecoveryInfo,
} from "../../models/backups";
import { healthBadgeLabelFor } from "../pages/landing-controls";
import { Button } from "../components/Button";
import { ErrorNotice } from "../components/ErrorDisplay";
import type { UserFacingError } from "../../models/errors";
import type { RecoveryKind } from "../../models/health";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  renderDeep,
  renderRoot,
  renderedText,
} from "../../testing";

/** Deps that answer nothing and schedule nothing: these tests render a state,
 * they do not drive one. */
const IDLE_DEPS: LifecycleDeps = {
  getJson: async () => null,
  postJson: async () => ({ status: 500, body: null }),
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
  const model = new RecoveryModel("agent-33", {
    ...IDLE_DEPS,
    getJson: async () => info,
  });
  await model.load();
  return model;
}

/** A model that dispatched its own recovery, of the given kind, and is still
 * following it. */
async function modelRecoveringOwn(
  info: RecoveryInfo,
  kind: RecoveryKind,
): Promise<RecoveryModel> {
  const model = new RecoveryModel("agent-33", {
    ...IDLE_DEPS,
    postJson: async () => ({ status: 202, body: null }),
  });
  model.info = info;
  await model.dispatchRecovery(kind);
  return model;
}

/** What the card puts on screen for a given reading of the machine. */
function renderCard(info: RecoveryInfo, model = modelShowing(info)): string {
  return renderedText(
    renderDeep(renderRoot(RecoveryCardBody, { model })) as m.Vnode,
  );
}

/** Draw a view repeatedly against one instance, as a mount does: which
 * collapsible section is open lives in the view's own closure, and a fresh
 * instance per draw would forget it. */
function mounted<A>(component: () => m.Component<A>, attrs: A): () => m.Vnode {
  const instance = component() as unknown as m.Component;
  return () => {
    const vnode = m(instance, attrs as m.Attributes) as m.Vnode;
    return renderDeep(
      (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
        instance,
        vnode,
      ),
    ) as m.Vnode;
  };
}

/** Open the collapsible section whose summary reads `label`.
 *
 * A shut section draws nothing at all, so what it holds is off the screen and
 * out of the tree until a reader asks for it. A test that wants to assert on
 * that content has to ask too. */
function openSectionLabeled(rendered: m.Vnode, label: string): void {
  const trigger = collectVnodes(rendered).find(
    (vnode) =>
      attrsOf(vnode)["aria-expanded"] !== undefined &&
      allText(vnode.children).includes(label),
  );
  if (trigger === undefined)
    throw new Error(`no collapsible section labeled ${label}`);
  (attrsOf(trigger).onclick as () => void)();
}

/** Press the button reading `label`, so a test can assert what a click does
 * rather than only what the card says. */
function clickButtonLabeled(rendered: m.Vnode, label: string): void {
  const button = collectVnodes(rendered).find(
    (vnode) =>
      typeof attrsOf(vnode).onclick === "function" &&
      allText(vnode.children).trim() === label,
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
    expect(recoveryHeading("my-machine", "recovery_failed", false)).toBe(
      "my-machine unresponsive",
    );
    expect(recoveryHeading("my-machine", "stuck", false)).toBe(
      "my-machine isn't responding yet.",
    );
    expect(recoveryHeading("my-machine", "healthy", true)).toBe(
      "my-machine is stopped.",
    );
    expect(recoveryHeading("my-machine", "healthy", false)).toBe(
      "my-machine is responding again.",
    );
  });

  it("claims a restart only where something is known to be recovering", () => {
    // The three in-flight readings, and why they differ. A stopped host is
    // genuinely being booted. A RESTART is only ever the user's own click, so
    // their action makes the claim honest. A START is what the app fires at any
    // machine that stops answering -- it no-ops against a host that is already
    // up, so "Restarting" would tell the user their work was interrupted when
    // nothing happened to the machine.
    expect(recoveryHeading("my-machine", "recovering", true, null)).toBe(
      "Bringing my-machine back online...",
    );
    expect(recoveryHeading("my-machine", "recovering", false, "restart")).toBe(
      "Restarting my-machine...",
    );
    expect(recoveryHeading("my-machine", "recovering", false, "start")).toBe(
      "Reconnecting to my-machine...",
    );
    // No reading at all is not evidence of a restart either.
    expect(recoveryHeading("my-machine", "recovering", false, null)).toBe(
      "Reconnecting to my-machine...",
    );
  });

  it("agrees with the machines-list badge about the same recovery", () => {
    // The card and the row are two views of one episode, and a user looking at
    // the list and then opening the card must not be told two different things
    // about it. Both read the tracker's recovery kind off their own frame,
    // so the pairing is what keeps them in step -- the badge silently reporting
    // the weaker word for a bounce the user themselves clicked is exactly the
    // divergence this pins.
    for (const kind of ["start", "restart", null] as const) {
      const isRestartClaimedOnCard = recoveryHeading(
        "my-machine",
        "recovering",
        false,
        kind,
      ).startsWith("Restarting");
      const isRestartClaimedOnBadge =
        healthBadgeLabelFor("recovering", false, kind, false) ===
        "Restarting...";
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
        const isRestartClaimedOnButton =
          recoveryBusyActionLabel(isHostOffline, kind) === "Restarting...";
        expect(isRestartClaimedOnButton).toBe(isRestartClaimedInHeading);
      }
    }
  });
});

describe("RecoveryCardBody", () => {
  it("names the machine, what it costs, and offers the restart", () => {
    const text = renderCard(UNRESPONSIVE);
    expect(text).toContain("my-machine unresponsive");
    expect(text).toContain(
      "In progress work will be interrupted, but saved data will not be lost.",
    );
    expect(text).toContain("Restart workspace");
    expect(text).toContain("Report a bug");
    expect(text).not.toContain("Report a problem");
  });

  it("puts the restart in the failure it answers, and dispatches it from there", () => {
    let dispatched = 0;
    const model = modelShowing(UNRESPONSIVE);
    model.dispatchRecovery = async () => {
      dispatched += 1;
    };
    const tree = renderDeep(renderRoot(RecoveryCardBody, { model }));
    const notice = byId(tree, "recovery-error-0");
    expect(notice).toBeDefined();
    const restart = collectVnodes(notice).find(
      (vnode) => vnode.tag === "button" && allText(vnode).trim() === "Restart workspace",
    );
    expect(restart).toBeDefined();
    (attrsOf(restart as AnyVnode).onclick as () => void)();
    expect(dispatched).toBe(1);
    // No second row of buttons under it.
    expect(renderedText(tree as m.Vnode).match(/Restart workspace/g)).toHaveLength(1);
  });

  it("offers a working restart on a machine it cannot classify yet", () => {
    // A surface the user opened on purpose never withholds the action; the
    // copy simply does not urge it.
    const text = renderCard({ ...UNRESPONSIVE, health: "stuck" });
    expect(text).toContain("my-machine isn't responding yet.");
    expect(text).toContain("Imbue Studio is still checking what's wrong.");
    expect(text).toContain("Restart workspace");
  });

  it("names the backend and withholds the restart when the backend is unreachable", () => {
    // The restart is dispatched through the same provider, so offering it here
    // would be offering an action that cannot work. With no fix of its own to
    // name, the provider's error is the only clue to what to do, so it shows.
    const info = {
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "Docker",
      unreachable_reason:
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    };
    const text = renderCard(info);

    expect(text).toContain("my-machine unreachable: Can't connect to Docker");
    expect(text).toContain(
      "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    );
    expect(text).not.toContain("Restart workspace");
    // A card with no remedy still has to let the user say something about it.
    expect(text).toContain("Report a bug");
  });

  it("tells the user the provider's own fix, with its error behind See details", () => {
    const info = {
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "Docker",
      unreachable_reason:
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
      unreachable_remediation: "start Docker",
    };
    const text = renderCard(info);

    expect(text).toContain("my-machine unreachable: Can't connect to Docker");
    // The fix is set apart, so its asterisks are markup rather than text.
    expect(text).toContain("start Docker");
    expect(text).not.toContain("*start Docker*");
    expect(text).not.toContain("Cannot connect to the Docker daemon");
    expect(offeredDetails(modelShowing(info))).toContain(
      "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    );
  });

  it("names the workspace, not a component, when the provider has no name", () => {
    const text = renderCard({
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "",
      unreachable_reason: "boom",
    });

    expect(text).toContain("Can't reach my-machine right now");
    expect(text).not.toContain("machine backend");
  });

  it("still says something when the provider reported an error with no text", () => {
    const text = renderCard({
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "Docker",
      unreachable_reason: "",
    });

    expect(text).toContain(
      "Imbue Studio can't reach the service that runs this workspace right now.",
    );
  });

  it("says plainly that the machine refused this device's connection", () => {
    const sentence =
      "This workspace's machine is refusing Imbue Studio's connection, and Imbue Studio can't fix that from here. " +
      "Report a bug so we can restore access.";
    const text = renderCard({
      ...UNRESPONSIVE,
      is_backend_unreachable: true,
      provider_label: "Imbue Cloud",
      unreachable_reason: sentence,
      is_access_rejected: true,
    });

    expect(text).toContain(sentence);
    expect(text).toContain("Report a bug");
    expect(text).not.toContain("Try again");
  });

  it("still reports a machine that is answering, whatever its provider's last poll did", () => {
    // A provider poll can error while its machines keep answering through the
    // forward, so an erroring provider is not by itself a machine Imbue Studio cannot
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
    expect(text).toContain("Restart workspace");
  });

  it("explains this device's dead network and offers no restart to route over it", () => {
    // The restart would go over the same network that is down, so there is
    // nothing here for the user to decide -- not even a disabled button.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "OFFLINE",
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain("This device has no network connection.");
    expect(text).not.toContain("Restart workspace");
  });

  it("tells a user on an SSH-blocking network what is actually wrong", () => {
    // Their browser works, so "you are offline" is a claim they can see is
    // false -- and unlike a dead network, this one never fixes itself.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "SSH_BLOCKED",
    });

    // Said once, with nothing left over to put behind See details. The fix is
    // set apart from the condition, so the two are separate runs of text.
    expect(text).toContain(
      "This network blocks the connection Imbue Studio uses (SSH), even though web pages still load.",
    );
    expect(text).toContain("Try a different network, such as a phone hotspot.");
    expect(text).not.toContain("See details");
    expect(text).not.toContain("This device has no network connection.");
    expect(text).not.toContain("Restart workspace");
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
    expect(text).not.toContain("Restart workspace");
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
    expect(text).not.toContain("from this device");
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
    // lines for the device's condition over a restart that is running.
    const info: RecoveryInfo = {
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "OFFLINE",
    };
    const model = modelShowing(info);
    model.isRecoveryRunning = true;
    // A click from this card is a full stop+start bounce, which is what
    // licenses the card to call it a restart at all.
    model.dispatchedRecoveryKind = "restart";

    const text = renderCard(info, model);

    expect(text).toContain("Restarting my-machine...");
    expect(text).not.toContain("This device has no network connection.");
  });

  it("shows why a start was declined, without an error and without a success", () => {
    // A start the connector refused as an operator hold: the machine is still
    // stopped, the card says what the connector said, and reports neither a
    // failed recovery nor a machine that is answering.
    const held: RecoveryInfo = {
      ...UNRESPONSIVE,
      health: "stuck",
      is_host_offline: true,
    };
    const model = modelShowing(held);
    model.recoveryNotice =
      "This workspace is undergoing maintenance and will be back shortly.";

    const text = renderCard(held, model);

    expect(text).toContain(
      "This workspace is undergoing maintenance and will be back shortly.",
    );
    expect(text).toContain("my-machine is stopped.");
    expect(text).not.toContain("unresponsive");
    expect(text).not.toContain("Nothing further is needed here");
    // A declined start is not a failure: no error notice.
    expect(text).not.toContain("Try again. If this persists");
  });

  it("returns the machine to its normal states once it answers again", () => {
    // The device may still be blocked for everything else, but this machine is
    // answering -- the card must not keep the waiting state on it.
    const text = renderCard({
      ...UNRESPONSIVE,
      health: "healthy",
      device_environment: "OFFLINE",
    });

    expect(text).toContain("my-machine is responding again.");
    expect(text).not.toContain("This device has no network connection.");
  });

  it("reads a recovery nobody started here as busy, not as an idle machine", async () => {
    // The unattended dispatcher starts a wedged machine on its own, so the card
    // must not sit there offering to start a second one. It is a plain start,
    // so the heading says reconnecting rather than claiming a restart.
    //
    // Loaded rather than assigned on purpose: adopting a "recovering" reading is
    // what attaches the model to that episode, and a card that read its own
    // busy flag as evidence of its own click would claim a restart here.
    const info = {
      ...UNRESPONSIVE,
      health: "recovering",
      recovery_kind: "start" as const,
    };
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
    expect(text).not.toContain("Restart workspace");
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
    const info = {
      ...UNRESPONSIVE,
      is_device_cannot_connect: true,
      device_error_detail:
        "No known_hosts file at /keys/known_hosts; refusing to connect",
    };
    const text = renderCardOnDesktop(info);

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain(
      "the connection failed on this device, before reaching it",
    );
    expect(text).toContain("Restart Imbue Studio");
    // The verbatim error is offered rather than shown: it sits behind "See
    // details", shut, so it does not push the remedy off the card.
    expect(text).toContain("See details");
    expect(text).not.toContain(
      "No known_hosts file at /keys/known_hosts; refusing to connect",
    );
    expect(offeredDetails(modelShowing(info))).toContain(
      "No known_hosts file at /keys/known_hosts; refusing to connect",
    );
    expect(text).not.toContain("Restart workspace");
    expect(text).toContain("Report a bug");
  });

  it("offers no app restart in a browser, where there is no app to restart", () => {
    // The bridge's restart is a no-op outside Electron, so a button here would
    // do nothing at all when clicked -- and the copy must not name a remedy the
    // user has no way to carry out. The condition, the error, and the way to
    // report it are still worth saying.
    const text = renderCardInBrowser({
      ...UNRESPONSIVE,
      is_device_cannot_connect: true,
      device_error_detail:
        "No known_hosts file at /keys/known_hosts; refusing to connect",
    });

    expect(text).toContain("Can't connect to my-machine from this device");
    expect(text).toContain(
      "the connection failed on this device, before reaching it",
    );
    expect(text).toContain("See details");
    expect(text).toContain("Report a bug");
    expect(text).not.toContain("Restart Imbue Studio");
    expect(text).not.toContain(
      "Restarting Imbue Studio rebuilds the connection",
    );
  });

  it("outranks the restart episode's own account of the machine", () => {
    // A machine this device cannot reach goes STUCK and gets a start dispatched
    // at it whether or not anything is wrong with it, so RECOVERY_FAILED here is
    // an effect of the device-side fault. Reporting it would blame the machine
    // for the app's own broken connection.
    const text = renderCardOnDesktop({
      ...UNRESPONSIVE,
      health: "recovery_failed",
      health_error:
        "The system interface did not respond within 300s of the host restart.",
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
    expect(text).toContain("This workspace is answering again.");
    expect(text).not.toContain("isn't responding yet");
    expect(text).not.toContain("Imbue Studio is still checking what's wrong.");
  });

  it("keeps reporting the restart as running on a card that is about to dismiss itself", () => {
    // An auto-raised card leaves once the app's own probe confirms the machine.
    // Until then, an idle-reading card would offer a Restart button for the
    // restart that just ran.
    const model = modelShowing(UNRESPONSIVE);
    model.isRecoverySucceeded = true;
    const text = renderedText(
      renderRoot(RecoveryCardBody, { model, isSelfDismissing: true }),
    );
    expect(text).toContain("Reconnecting...");
    expect(text).not.toContain("Restart workspace");
  });

  it("keeps that window free of the device's condition too", () => {
    // The same window as above, with a device block still recorded: the machine
    // has answered, so the block is stale and the card is one confirmation away
    // from leaving. Rendering the wait here would replace a restart that just
    // succeeded with an indefinite wait for the network.
    const model = modelShowing({
      ...UNRESPONSIVE,
      health: "stuck",
      device_environment: "OFFLINE",
    });
    model.isRecoverySucceeded = true;
    const text = renderedText(
      renderRoot(RecoveryCardBody, { model, isSelfDismissing: true }),
    );
    // No evidence of which recovery this is here, so the busy label is the
    // weakest honest one -- what matters is that it narrates the recovery.
    expect(text).toContain("Reconnecting...");
    expect(text).not.toContain("This device has no network connection.");
  });

  it("offers the way into the machine when the surface it is on has one", () => {
    // The recovery page stands between the reader and the machine; the modal
    // has the machine behind it and an X to get there. Only the first supplies
    // this, and when it does, entering is the action -- the card is saying
    // nothing further is needed.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    const enter = vi.fn();
    const rendered = renderRoot(RecoveryCardBody, {
      model,
      onEnterMachine: enter,
    });

    expect(renderedText(rendered)).toContain("Open workspace");
    clickButtonLabeled(rendered, "Open workspace");
    expect(enter).toHaveBeenCalledOnce();
  });

  it("says nothing about entering the machine on a surface that already can", () => {
    // The modal passes no way into the machine, and gets no Open workspace: it
    // would sit on top of the very machine it offered to open.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    expect(renderedText(renderRoot(RecoveryCardBody, { model }))).not.toContain(
      "Open workspace",
    );
  });

  it("leads with closing the modal over a machine that is answering again", () => {
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    const close = vi.fn();
    const rendered = renderRoot(RecoveryCardBody, { model, onClose: close });

    expect(buttonVariants(rendered)).toEqual([
      ["Close", "primary"],
      ["Restart workspace", "secondary"],
      ["Report a problem", "secondary"],
    ]);
    clickButtonLabeled(rendered, "Close");
    expect(close).toHaveBeenCalledOnce();
  });

  it("keeps the restart as the modal's action while the machine still needs one", () => {
    // Stuck, and stopped: neither is a card saying nothing further is needed,
    // so the X stays the only dismissal and the restart keeps its weight.
    for (const info of [
      { ...UNRESPONSIVE, health: "stuck" },
      { ...UNRESPONSIVE, health: "healthy", is_host_offline: true },
    ]) {
      const rendered = renderRoot(RecoveryCardBody, {
        model: modelShowing(info),
        onClose: () => {},
      });
      expect(buttonVariants(rendered)).toEqual([
        ["Restart workspace", "primary"],
        ["Report a problem", "secondary"],
      ]);
    }
  });
});

/** Each Button the card renders, as its label and variant, in order. */
function buttonVariants(rendered: m.Vnode): [string, unknown][] {
  return collectVnodes(rendered)
    .filter((vnode) => vnode.tag === Button)
    .map((vnode) => [allText(vnode.children).trim(), attrsOf(vnode).variant]);
}

/** Every text the card keeps behind a "See details", collected from the
 * errors it draws (a shut See details draws none of it). */
function offeredDetails(model: RecoveryModel): string {
  const found: string[] = [];
  function walk(node: unknown): void {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (node === null || typeof node !== "object") return;
    const vnode = node as { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown };
    if (vnode.tag === ErrorNotice) {
      const error = vnode.attrs?.error as UserFacingError | undefined;
      found.push(error?.details ?? "");
      return;
    }
    if (typeof vnode.tag === "function") {
      const instance = (vnode.tag as () => m.Component)();
      walk((instance.view as (v: m.Vnode) => m.Children).call(instance, vnode as m.Vnode));
      return;
    }
    walk(vnode.children);
  }
  walk(renderRoot(RecoveryCardBody, { model }));
  return found.join("\n");
}

describe("recovery failures on the card", () => {
  it("says what failed in a sentence of its own, with the stored reason behind See details", () => {
    const model = modelShowing({
      ...UNRESPONSIVE,
      recovery_kind: "start",
      recovery_failure_cause: "NOT_ANSWERING_AFTER_START",
      health_error:
        "The system interface did not respond within 300s of the host recovery (last probe: HTTP 502).",
    });
    const text = renderCard(model.info as RecoveryInfo, model);

    expect(text).toContain(
      "Imbue Studio tried to bring this workspace back, but it still isn't answering. " +
        "Try again. If this persists, report a bug.",
    );
    expect(text).not.toContain("system interface");
    expect(offeredDetails(model)).toContain("The system interface did not respond within 300s");
  });

  it.each([
    ["STEP_FAILED", "Imbue Studio couldn't start this workspace."],
    ["PROVIDER_UNREACHABLE", "so it couldn't start it."],
    ["WORKSPACE_NOT_FOUND", "so it couldn't start it."],
  ] as const)("does not call the app's own start a restart (%s)", (cause, sentence) => {
    const text = renderCard({
      ...UNRESPONSIVE,
      recovery_kind: "start",
      recovery_failure_cause: cause,
      health_error: "Start step of host recovery failed: exited 1",
    });

    expect(text).toContain(sentence);
    expect(text).not.toContain("couldn't restart");
  });

  it("shows a refused restart above the buttons rather than in a closed section", () => {
    const model = modelShowing({ ...UNRESPONSIVE, health: "stuck" });
    model.recoveryError = {
      message:
        "Imbue Studio is already working on this workspace (restoring a backup). Wait for that to finish, then try again.",
      details: "HTTP 409\nA restore is already in progress for this workspace.",
    };

    const text = renderCard(model.info as RecoveryInfo, model);

    expect(text).toContain("Imbue Studio is already working on this workspace (restoring a backup).");
    expect(text).not.toContain("HTTP 409");
  });

  it("states one failure once when this card's restart and the workspace's state report it alike", () => {
    const reason = "Start step of host recovery failed: exited 1";
    const model = modelShowing({
      ...UNRESPONSIVE,
      recovery_kind: "restart",
      recovery_failure_cause: "STEP_FAILED",
      health_error: reason,
    });
    model.recoveryError = {
      message: "Imbue Studio couldn't restart this workspace. Try again. If this persists, report a bug.",
      details: reason,
    };

    const text = renderCard(model.info as RecoveryInfo, model);

    expect(text.split("Imbue Studio couldn't restart this workspace.").length - 1).toBe(1);
  });

  it("states a restart the app could not begin once, though the refusal wraps the reason in its status", async () => {
    const reason = "Could not start the recovery worker: group exited";
    const model = new RecoveryModel("agent-33", {
      ...IDLE_DEPS,
      postJson: async () => ({ status: 503, body: { error: reason } }),
    });
    model.info = { ...UNRESPONSIVE, health: "stuck" };
    await model.dispatchRecovery("restart");
    model.info = {
      ...UNRESPONSIVE,
      recovery_kind: "restart",
      recovery_failure_cause: "WORKER_NOT_STARTED",
      health_error: reason,
    };

    const text = renderCard(model.info, model);

    expect(text.split("Imbue Studio couldn't restart this workspace.").length - 1).toBe(1);
  });

  it("says visibly when it lost track of a recovery, calling only a restart a restart", () => {
    const model = modelShowing({ ...UNRESPONSIVE, health: "stuck" });
    model.isRecoveryFollowLost = true;
    model.followedRecoveryKind = "restart";

    expect(renderCard(model.info as RecoveryInfo, model)).toContain(
      "Imbue Studio lost track of this restart. The workspace may still be coming back",
    );

    // The app's own start, attached to rather than dispatched here.
    model.followedRecoveryKind = "start";
    const attachedStart = renderCard(model.info as RecoveryInfo, model);
    expect(attachedStart).toContain("Imbue Studio lost track of its attempt to reconnect to this workspace.");
    expect(attachedStart).not.toContain("lost track of this restart");
  });

  it("lists the restart's progress plainly, keeping the log lines behind a See details", () => {
    const lines = [
      "Starting host recovery.",
      "Stopping the system-services agent.",
      "Starting the system-services agent.",
      "Waiting for the system interface to respond.",
    ];

    expect(plainRecoveryProgress(lines)).toEqual([
      "Stopping the workspace.",
      "Starting the workspace.",
      "Waiting for the workspace to answer.",
    ]);
    const model = modelShowing({ ...UNRESPONSIVE, health: "recovering" });
    model.logLines = lines;
    const text = renderCard(model.info as RecoveryInfo, model);
    expect(text).toContain("Stopping the workspace.");
    expect(text).not.toContain("system-services");
    expect(text).toContain("See details");
  });

  it("puts the attempt's log behind the same See details as the verdict once it fails", () => {
    const model = modelShowing({
      ...UNRESPONSIVE,
      health: "recovery_failed",
      recovery_kind: "restart",
      recovery_failure_cause: "NOT_ANSWERING_AFTER_START",
      health_error: "The system interface did not respond within 300s.",
    });
    model.logLines = ["Starting host recovery.", "Stopping the system-services agent."];

    const details = offeredDetails(model);

    // One See details holds both: what went wrong, and how far the attempt got.
    expect(details).toContain("The system interface did not respond within 300s.");
    expect(details).toContain("Restart log:");
    expect(details).toContain("Stopping the system-services agent.");
    expect(renderCard(model.info as RecoveryInfo, model)).not.toContain(
      "system-services",
    );
  });
});

describe("isDeviceConditionShownOnCard", () => {
  it("is true exactly when the card states this device's condition in place of its own", () => {
    // The recovery page hides its app-wide network notice on this, so it must
    // agree with what the card draws.
    const blocked = { ...UNRESPONSIVE, health: "stuck", device_environment: "SSH_BLOCKED" } as RecoveryInfo;
    expect(isDeviceConditionShownOnCard(modelShowing(blocked))).toBe(true);
    expect(renderCard(blocked)).toContain("This network blocks the connection Imbue Studio uses (SSH)");

    // A machine that answers, one the network cannot explain, and the user's
    // own restart all leave the condition unsaid on the card.
    const unsaid: RecoveryInfo[] = [
      { ...blocked, health: "healthy" },
      { ...blocked, device_environment: "NONE" },
      { ...blocked, health: "recovering", recovery_kind: "restart" },
    ];
    for (const info of unsaid) {
      expect(isDeviceConditionShownOnCard(modelShowing(info))).toBe(false);
      expect(renderCard(info)).not.toContain("This network blocks the connection");
    }
    const loading = new RecoveryModel("agent-33", IDLE_DEPS);
    expect(isDeviceConditionShownOnCard(loading)).toBe(false);
  });
});

describe("RecoveryConnectionDetails", () => {
  it("offers the connection command collapsed", () => {
    const model = modelShowing({ ...UNRESPONSIVE, ssh_command: "ssh -i k -p 22 user@h" });
    const draw = mounted(RecoveryConnectionDetails, { model });

    expect(renderedText(draw())).toContain("Connection details");
    expect(renderedText(draw())).not.toContain("ssh -i k");
    openSectionLabeled(draw(), "Connection details");
    expect(renderedText(draw())).toContain("connect to this workspace's machine from a terminal");
  });

  it("stays off the card for a machine whose host coordinates are unknown", () => {
    const model = modelShowing(UNRESPONSIVE);
    expect(renderedText(renderDeep(renderRoot(RecoveryConnectionDetails, { model })) as m.Vnode)).toBe("");
  });
});
