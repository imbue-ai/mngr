import { describe, expect, it } from "vitest";
import type { UiRequestsMessage, UiWorkspacesMessage } from "../channel/messages";
import { applySnapshotToStores, bootFromBootstrap, createEmptyStores } from "./boot";
import { HealthStore } from "./health";
import { RequestsStore } from "./requests";
import { WorkspacesStore } from "./workspaces";

function workspacesMessage(overrides: Partial<UiWorkspacesMessage> = {}): UiWorkspacesMessage {
  return {
    type: "workspaces",
    workspaces: [
      {
        id: "agent-aa11",
        name: "alpha",
        accent: "#aabbcc",
        host_id: "host-bb22",
        is_stale: false,
        supports_shutdown: true,
        liveness: "RUNNING",
        account: "",
        create_attempt_state: "",
        is_remote: false,
        location: "",
      },
    ],
    destroying_agent_ids: [],
    restorable_workspace_ids: ["agent-aa11", "host-bb22"],
    remote_workspace_states: {},
    ...overrides,
  };
}

function requestsMessage(ids: string[], autoOpen: boolean): UiRequestsMessage {
  return { type: "requests", count: ids.length, request_ids: ids, auto_open: autoOpen };
}

describe("WorkspacesStore", () => {
  it("maps between agent and host coordinates from the list message", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    expect(store.toAgentScopedId("host-bb22")).toBe("agent-aa11");
    expect(store.toHostScopedId("agent-aa11")).toBe("host-bb22");
    expect(store.toAgentScopedId("agent-unknown")).toBe("agent-unknown");
  });

  it("builds host-scoped forward-bridge frame URLs", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    expect(store.workspaceFrameUrl("agent-aa11")).toBe(
      "/forward-bridge?next=" + encodeURIComponent("/goto/host-bb22/"),
    );
  });

  it("caches accents under both coordinates and applies previews", () => {
    const store = new WorkspacesStore();
    store.applyWorkspacesMessage(workspacesMessage());
    expect(store.accentEntry("host-bb22")?.accent).toBe("#aabbcc");
    store.applyAccentPreview("agent-aa11", "#112233");
    expect(store.accentEntry("agent-aa11")).toEqual({ accent: "#112233", name: "alpha" });
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
    store.applyHealthMessage({ type: "health", agent_id: "agent-x", status: "stuck", error: null });
    expect(store.statusFor("agent-x")).toBe("stuck");
    expect(store.isContentAssumedReady("agent-x")).toBe(false);
    store.applyHealthMessage({ type: "health", agent_id: "agent-x", status: "healthy", error: null });
    expect(store.statusFor("agent-x")).toBe("healthy");
  });
});

describe("RequestsStore auto-open policy", () => {
  it("never fires for the connect-time snapshot", () => {
    const store = new RequestsStore();
    let fired = 0;
    store.onAutoOpen(() => (fired += 1));
    store.applyRequestsMessage(requestsMessage(["evt-1"], true));
    expect(fired).toBe(0);
  });

  it("fires once per genuinely new id and respects the preference", () => {
    const store = new RequestsStore();
    const seenBatches: string[][] = [];
    store.onAutoOpen((ids) => seenBatches.push([...ids]));
    store.applyRequestsMessage(requestsMessage([], true));
    store.applyRequestsMessage(requestsMessage(["evt-1"], true));
    store.applyRequestsMessage(requestsMessage(["evt-1"], true));
    expect(seenBatches).toEqual([["evt-1"]]);
    store.applyRequestsMessage(requestsMessage(["evt-1", "evt-2"], false));
    expect(seenBatches).toEqual([["evt-1"]]);
  });

  it("re-fires for an id that left and re-entered the pending set", () => {
    const store = new RequestsStore();
    let fired = 0;
    store.onAutoOpen(() => (fired += 1));
    store.applyRequestsMessage(requestsMessage([], true));
    store.applyRequestsMessage(requestsMessage(["evt-1"], true));
    store.applyRequestsMessage(requestsMessage([], true));
    store.applyRequestsMessage(requestsMessage(["evt-1"], true));
    expect(fired).toBe(2);
  });
});

describe("boot seeding", () => {
  it("seeds every store from the bootstrap snapshot", () => {
    const boot = bootFromBootstrap({
      seed: { accent: "#123456", is_mac: true, mngr_forward_origin: "http://localhost:8421" },
      schema_version: 1,
      snapshot: {
        workspaces: workspacesMessage(),
        accounts: { type: "accounts", has_accounts: true, account_email: "a@b.c", extra_account_count: 1 },
        providers: { type: "providers", providers: [], last_event_at: null, last_full_snapshot_at: null },
        requests: requestsMessage(["evt-9"], true),
        health: [{ type: "health", agent_id: "agent-aa11", status: "restarting", error: null }],
        discovery_health: { type: "discovery_health", state: "healthy" },
      },
    });
    expect(boot.seed.isMac).toBe(true);
    expect(boot.stores.workspaces.workspaces).toHaveLength(1);
    expect(boot.stores.accounts.accountEmail).toBe("a@b.c");
    expect(boot.stores.requests.count).toBe(1);
    expect(boot.stores.health.statusFor("agent-aa11")).toBe("restarting");
  });

  it("applySnapshotToStores treats snapshot requests as connect-time (no auto-open)", () => {
    const stores = createEmptyStores();
    let fired = 0;
    stores.requests.onAutoOpen(() => (fired += 1));
    applySnapshotToStores(stores, {
      snapshot: {
        workspaces: workspacesMessage(),
        accounts: { type: "accounts", has_accounts: true, account_email: "a@b.c", extra_account_count: 0 },
        providers: { type: "providers", providers: [], last_event_at: null, last_full_snapshot_at: null },
        requests: requestsMessage(["evt-1"], true),
        health: [],
        discovery_health: { type: "discovery_health", state: "healthy" },
      },
    });
    expect(fired).toBe(0);
    // The snapshot must actually land in the stores it was applied to.
    expect(stores.accounts.hasAccounts).toBe(true);
    expect(stores.accounts.accountEmail).toBe("a@b.c");
  });
});
