import { describe, expect, it } from "vitest";
import type {
  UiNotificationsMessage,
  UiRequestsMessage,
} from "../channel/messages";
import { notificationEntry, workspacesMessage } from "../testing";
import {
  applySnapshotToStores,
  bootFromBootstrap,
  createEmptyStores,
} from "./boot";
import { HealthStore } from "./health";
import { NotificationsStore } from "./notifications";
import { RequestsStore } from "./requests";
import { WorkspacesStore } from "./workspaces";

function requestsMessage(ids: string[]): UiRequestsMessage {
  return { type: "requests", count: ids.length, request_ids: ids };
}

function notificationsMessage(ids: string[]): UiNotificationsMessage {
  return {
    type: "notifications",
    entries: ids.map((id) => notificationEntry(id)),
    unresolved_count: ids.length,
  };
}

describe("WorkspacesStore", () => {
  it("resolves legacy host coordinates to the agent id from the list message", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    expect(store.toAgentScopedId("host-bb22")).toBe("agent-aa11");
    expect(store.toAgentScopedId("agent-unknown")).toBe("agent-unknown");
  });

  it("builds workspace-scoped forward-bridge frame URLs (host input resolves through the alias)", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    // Content URLs are keyed by the workspace id, so they survive machine changes.
    expect(store.workspaceFrameUrl("agent-aa11")).toBe(
      "/forward-bridge?next=" + encodeURIComponent("/goto/agent-aa11/"),
    );
    expect(store.workspaceFrameUrl("host-bb22")).toBe(
      "/forward-bridge?next=" + encodeURIComponent("/goto/agent-aa11/"),
    );
  });

  it("caches accents under both coordinates and applies previews", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    expect(store.accentEntry("host-bb22")?.accent).toBe("#aabbcc");
    store.applyAccentPreview("agent-aa11", "#112233");
    expect(store.accentEntry("agent-aa11")).toEqual({
      accent: "#112233",
      name: "alpha",
    });
  });

  it("notifies subscribers when the list changes", () => {
    const store = new WorkspacesStore();
    let notified = 0;
    store.onChanged(() => (notified += 1));
    store.applyWorkspacesMessage(workspacesMessage());
    expect(notified).toBe(1);
  });
});

describe("HealthStore", () => {
  it("treats untracked workspaces as healthy and clears on healthy transitions", () => {
    const store = new HealthStore();
    expect(store.statusFor("agent-x")).toBe("healthy");
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "stuck",
      error: null,
    });
    expect(store.statusFor("agent-x")).toBe("stuck");
    expect(store.isContentAssumedReady("agent-x")).toBe(false);
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "healthy",
      error: null,
    });
    expect(store.statusFor("agent-x")).toBe("healthy");
  });

  it("never carries a no-op start past the episode that reported it", () => {
    // The flag is what makes the row read "Not responding" rather than "Restart
    // failed", so a leftover would mislabel a later, genuine restart failure for
    // the same workspace. Every way an episode can end has to drop it.
    const store = new HealthStore();
    const noOpFailure = {
      type: "health",
      agent_id: "agent-x",
      status: "restart_failed",
      error: "no answer",
      is_restart_a_no_op: true,
    } as const;

    store.applyHealthMessage(noOpFailure);
    expect(store.isRestartANoOpFor("agent-x")).toBe(true);

    // A later non-healthy frame that does not carry it: a fresh restart attempt
    // resets the tracker's record, so the frame's silence is the answer.
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "restarting",
      error: null,
    });
    expect(store.isRestartANoOpFor("agent-x")).toBe(false);

    store.applyHealthMessage(noOpFailure);
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "healthy",
      error: null,
    });
    expect(store.isRestartANoOpFor("agent-x")).toBe(false);

    // Reconnect resync: the snapshot only carries non-HEALTHY agents, so
    // anything reset() leaves behind is never overwritten.
    store.applyHealthMessage(noOpFailure);
    store.reset();
    expect(store.isRestartANoOpFor("agent-x")).toBe(false);
  });

  it("keeps a restart's shape distinct from having no restart to describe", () => {
    // The badge says "Restarting" only on a false, so collapsing null into
    // false would call every unattended dispatch a restart -- and collapsing a
    // finished episode into its last shape would keep saying so afterwards.
    const store = new HealthStore();
    const bounce = {
      type: "health",
      agent_id: "agent-x",
      status: "restarting",
      error: null,
      is_restart_start_only: false,
    } as const;

    expect(store.isRestartStartOnlyFor("agent-x")).toBeNull();

    store.applyHealthMessage(bounce);
    expect(store.isRestartStartOnlyFor("agent-x")).toBe(false);

    store.applyHealthMessage({ ...bounce, is_restart_start_only: true });
    expect(store.isRestartStartOnlyFor("agent-x")).toBe(true);

    // The episode ends: the frame stops describing a restart, and so must the
    // store -- a held `false` would go on claiming one over a machine that has
    // stopped restarting.
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "restart_failed",
      error: "no answer",
    });
    expect(store.isRestartStartOnlyFor("agent-x")).toBeNull();

    store.applyHealthMessage(bounce);
    store.applyHealthMessage({
      type: "health",
      agent_id: "agent-x",
      status: "healthy",
      error: null,
    });
    expect(store.isRestartStartOnlyFor("agent-x")).toBeNull();

    store.applyHealthMessage(bounce);
    store.reset();
    expect(store.isRestartStartOnlyFor("agent-x")).toBeNull();
  });
});

describe("RequestsStore", () => {
  it("mirrors the pending set, replacing it wholesale on every message", () => {
    const store = new RequestsStore();
    store.applyRequestsMessage(requestsMessage(["evt-1"]));
    expect(store.requestIds).toEqual(["evt-1"]);
    store.applyRequestsMessage(requestsMessage(["evt-2", "evt-3"]));
    expect(store.requestIds).toEqual(["evt-2", "evt-3"]);
    store.applyRequestsMessage(requestsMessage([]));
    expect(store.requestIds).toEqual([]);
  });

  // A pending request must never open anything by itself: it waits behind the
  // in-chat card's "Review & respond" button and the Waiting-on-you rows. The
  // arrival of a genuinely new id is exactly what used to fire the auto-open
  // policy, so the store is pinned to having no arrival hook at all -- putting
  // one back (a listener set, a "new ids" callback, a first-message flag)
  // fails here before it can reach a user.
  it("gives a newly arrived request no hook that could open the popup", () => {
    const store = new RequestsStore();
    store.applyRequestsMessage(requestsMessage([]));
    store.applyRequestsMessage(requestsMessage(["evt-brand-new"]));
    expect(Object.keys(store)).toEqual(["requestIds"]);
    const methods = Object.getOwnPropertyNames(
      Object.getPrototypeOf(store),
    ).filter((name) => name !== "constructor");
    expect(methods).toEqual(["applyRequestsMessage"]);
  });
});

describe("HealthStore device environment", () => {
  it("reports the device condition the server sends, with nothing convicted", () => {
    // The cold-start case: minds opened on a dead network. No machine has been
    // asked to load, so none is stuck -- and the hub page still has to be able
    // to say what is wrong. Until the server has said anything, nothing has
    // been measured, and a store that read "fine" here would let the surfaces
    // blame the provider before the first frame lands.
    const store = new HealthStore();
    expect(store.appEnvironmentCondition()).toBe("UNKNOWN");

    store.applyEnvironmentMessage({ type: "environment", state: "OFFLINE" });

    expect(store.appEnvironmentCondition()).toBe("OFFLINE");
    expect(store.statusFor("agent-aa11")).toBe("healthy");
  });

  it("clears the device condition when the network comes back", () => {
    const store = new HealthStore();
    store.applyEnvironmentMessage({
      type: "environment",
      state: "SSH_BLOCKED",
    });
    expect(store.appEnvironmentCondition()).toBe("SSH_BLOCKED");

    store.applyEnvironmentMessage({ type: "environment", state: "NONE" });

    expect(store.appEnvironmentCondition()).toBe("NONE");
  });

  it("keeps the device condition across a reconnect resync", () => {
    // reset() clears per-machine state so a machine that recovered while the
    // socket was down does not stay stuck. The device condition is not
    // per-machine, and the snapshot always re-sends it.
    const store = new HealthStore();
    store.applyEnvironmentMessage({ type: "environment", state: "OFFLINE" });

    store.reset();

    expect(store.appEnvironmentCondition()).toBe("OFFLINE");
  });
});

describe("NotificationsStore", () => {
  it("mirrors the feed and unresolved count, replacing both wholesale on every message", () => {
    const store = new NotificationsStore();
    store.applyNotificationsMessage(notificationsMessage(["evt-1", "evt-2"]));
    expect(store.entries.map((entry) => entry.id)).toEqual(["evt-1", "evt-2"]);
    expect(store.unresolvedCount).toBe(2);
    store.applyNotificationsMessage(notificationsMessage([]));
    expect(store.entries).toEqual([]);
    expect(store.unresolvedCount).toBe(0);
  });
});

describe("boot seeding", () => {
  it("seeds every store from the bootstrap snapshot", () => {
    const boot = bootFromBootstrap({
      seed: {
        accent: "#123456",
        is_mac: true,
        mngr_forward_origin: "http://localhost:8421",
      },
      schema_version: 1,
      snapshot: {
        workspaces: workspacesMessage(),
        accounts: {
          type: "accounts",
          has_accounts: true,
          account_email: "a@b.c",
          extra_account_count: 1,
        },
        providers: {
          type: "providers",
          providers: [],
          last_event_at: null,
          last_full_snapshot_at: null,
        },
        requests: requestsMessage(["evt-9"]),
        notifications: notificationsMessage(["evt-9"]),
        health: [
          {
            type: "health",
            agent_id: "agent-aa11",
            status: "restarting",
            error: null,
          },
        ],
        discovery_health: { type: "discovery_health", state: "healthy" },
        // Not NONE: an app cold-started on a dead network is the case this
        // frame exists for, and NONE is the value that would read the same
        // whether the seeding happened or not.
        environment: { type: "environment", state: "OFFLINE" },
      },
    });
    expect(boot.seed.isMac).toBe(true);
    expect(boot.stores.workspaces.workspaces).toHaveLength(1);
    expect(boot.stores.accounts.accountEmail).toBe("a@b.c");
    expect(boot.stores.requests.requestIds).toEqual(["evt-9"]);
    expect(boot.stores.notifications.entries.map((entry) => entry.id)).toEqual([
      "evt-9",
    ]);
    expect(boot.stores.health.statusFor("agent-aa11")).toBe("restarting");
    expect(boot.stores.health.appEnvironmentCondition()).toBe("OFFLINE");
  });

  it("applySnapshotToStores lands the connect-time snapshot in every store", () => {
    const stores = createEmptyStores();
    applySnapshotToStores(stores, {
      snapshot: {
        workspaces: workspacesMessage(),
        accounts: {
          type: "accounts",
          has_accounts: true,
          account_email: "a@b.c",
          extra_account_count: 0,
        },
        providers: {
          type: "providers",
          providers: [],
          last_event_at: null,
          last_full_snapshot_at: null,
        },
        requests: requestsMessage(["evt-1"]),
        notifications: notificationsMessage(["evt-1"]),
        health: [],
        discovery_health: { type: "discovery_health", state: "healthy" },
        environment: { type: "environment", state: "OFFLINE" },
      },
    });
    expect(stores.requests.requestIds).toEqual(["evt-1"]);
    expect(stores.notifications.unresolvedCount).toBe(1);
    expect(stores.accounts.hasAccounts).toBe(true);
    expect(stores.accounts.accountEmail).toBe("a@b.c");
    expect(stores.health.appEnvironmentCondition()).toBe("OFFLINE");
  });
});
