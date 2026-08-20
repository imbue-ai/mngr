import { describe, expect, it } from "vitest";
import { RecoveryCardBody, RecoveryTroubleshooting, recoveryHeading } from "./RecoveryCard";
import { RecoveryModel, type LifecycleDeps, type RecoveryInfo } from "../../models/backups";
import { renderRoot, renderedText } from "../../testing";

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
  health: "restart_failed",
  health_error: "",
  ssh_command: "",
  is_host_offline: false,
  is_backend_unreachable: false,
  provider_label: "",
  unreachable_reason: "",
};

/** A model holding a given reading, with no poller running behind it. */
function modelShowing(info: RecoveryInfo): RecoveryModel {
  const model = new RecoveryModel("agent-33", IDLE_DEPS);
  model.info = info;
  return model;
}

/** What the card puts on screen for a given reading of the machine. */
function renderCard(info: RecoveryInfo, model = modelShowing(info)): string {
  return renderedText(renderRoot(RecoveryCardBody, { model }));
}

describe("recoveryHeading", () => {
  it("reserves the unresponsive verdict for a machine a restart has already failed", () => {
    // stuck is "we don't know yet": the app is still checking, and the machine
    // may come back on its own. Calling that unresponsive would be claiming a
    // verdict the classifier declined to give.
    expect(recoveryHeading("my-machine", "restart_failed", false)).toBe("my-machine unresponsive");
    expect(recoveryHeading("my-machine", "stuck", false)).toBe("my-machine isn't responding yet.");
    expect(recoveryHeading("my-machine", "healthy", true)).toBe("my-machine is stopped.");
    expect(recoveryHeading("my-machine", "restarting", true)).toBe("Bringing my-machine back online...");
    expect(recoveryHeading("my-machine", "healthy", false)).toBe("my-machine is responding again.");
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

  it("reads a restart nobody started here as busy, not as an idle machine", () => {
    // The unattended dispatcher restarts a wedged machine on its own, so the
    // card must not sit there offering to start a second one.
    const text = renderCard({ ...UNRESPONSIVE, health: "restarting" });
    expect(text).toContain("Restarting my-machine...");
    expect(text).toContain("Restarting...");
    expect(text).not.toContain("Restart Machine");
  });

  it("reports a machine that came back without also calling it unresponsive", () => {
    // The card outlives the failure that raised it: the server marks the
    // machine healthy BEFORE the restart operation reports done, so a card that
    // stays up through a successful restart is reading a healthy machine. It
    // used to fall through to the still-checking copy and render that over the
    // success -- one card saying both things at once.
    const model = modelShowing({ ...UNRESPONSIVE, health: "healthy" });
    model.isRestartSucceeded = true;
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
    model.isRestartSucceeded = true;
    const text = renderedText(renderRoot(RecoveryCardBody, { model, isSelfDismissing: true }));
    expect(text).toContain("Restarting...");
    expect(text).not.toContain("Restart Machine");
  });
});

/** What the troubleshooting block puts on screen for a given reading. */
function renderTroubleshooting(info: RecoveryInfo, restartError: string | null = null): string {
  const model = modelShowing(info);
  model.restartError = restartError;
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
