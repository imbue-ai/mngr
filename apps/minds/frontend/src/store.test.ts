import { afterEach, describe, expect, it } from "vitest";

import type { ChromeBootState, ChromeProvidersPayload, ChromeWorkspacesPayload } from "./chrome_state";
import {
  applyChromeEvent,
  beginMindAction,
  beginProviderToggle,
  connect,
  failMindAction,
  failProviderToggle,
  getAccentCacheEntry,
  getEffectiveLiveness,
  getHasAccounts,
  getRequestIds,
  getRequestsCount,
  getSystemInterfaceStatus,
  getWorkspaces,
  isAuthRequired,
  isProviderTogglePending,
  resetStoreForTesting,
  seed,
  subscribe,
} from "./store";
import type { ChromeEvent } from "./chrome_state";
import type { Host } from "./host";

afterEach(() => {
  resetStoreForTesting();
});

function workspacesEvent(overrides: Partial<ChromeWorkspacesPayload> = {}): ChromeWorkspacesPayload {
  return {
    type: "workspaces",
    workspaces: [],
    destroying_agent_ids: [],
    remote_workspace_states: {},
    ...overrides,
  };
}

function providersEvent(lastFullSnapshotAt: string | null): ChromeProvidersPayload {
  return {
    type: "providers_state",
    providers: [],
    last_event_at: null,
    last_full_snapshot_at: lastFullSnapshotAt,
  };
}

describe("accent cache merge", () => {
  it("caches accent and name from workspaces events", () => {
    applyChromeEvent(
      workspacesEvent({ workspaces: [{ id: "agent-1", name: "ws-one", accent: "#112233" }] }),
    );

    expect(getAccentCacheEntry("agent-1")).toEqual({ accent: "#112233", name: "ws-one" });
  });

  it("keeps the cached name when an accent preview updates the color", () => {
    applyChromeEvent(
      workspacesEvent({ workspaces: [{ id: "agent-1", name: "ws-one", accent: "#112233" }] }),
    );

    applyChromeEvent({ type: "workspace_accent_preview", agent_id: "agent-1", accent: "#445566" });

    expect(getAccentCacheEntry("agent-1")).toEqual({ accent: "#445566", name: "ws-one" });
  });

  it("caches a preview for an agent the SSE has not delivered yet", () => {
    applyChromeEvent({ type: "workspace_accent_preview", agent_id: "agent-9", accent: "#445566" });

    expect(getAccentCacheEntry("agent-9")).toEqual({ accent: "#445566", name: null });
  });
});

describe("optimistic mind liveness guard", () => {
  const runningRow = { id: "agent-1", name: "ws", accent: "#111111", supports_shutdown: "true", liveness: "RUNNING" };
  const stoppedRow = { ...runningRow, liveness: "STOPPED" };

  it("renders the transient while the action is in flight", () => {
    applyChromeEvent(workspacesEvent({ workspaces: [runningRow] }));

    beginMindAction("agent-1", "STOPPED");

    expect(getEffectiveLiveness(getWorkspaces()[0])).toBe("STOPPING");
  });

  it("ignores an interim payload still carrying the pre-action state", () => {
    beginMindAction("agent-1", "STOPPED");

    applyChromeEvent(workspacesEvent({ workspaces: [runningRow] }));

    expect(getEffectiveLiveness(getWorkspaces()[0])).toBe("STOPPING");
  });

  it("clears the transient once the authoritative state reaches the target", () => {
    beginMindAction("agent-1", "STOPPED");

    applyChromeEvent(workspacesEvent({ workspaces: [stoppedRow] }));

    expect(getEffectiveLiveness(getWorkspaces()[0])).toBe("STOPPED");
  });

  it("reverts to the authoritative state when the action fails", () => {
    applyChromeEvent(workspacesEvent({ workspaces: [runningRow] }));
    beginMindAction("agent-1", "STOPPED");

    failMindAction("agent-1");

    expect(getEffectiveLiveness(getWorkspaces()[0])).toBe("RUNNING");
  });
});

describe("provider toggle pending logic", () => {
  it("is pending while no snapshot has caught up with the click", () => {
    applyChromeEvent(providersEvent("2026-07-20T00:00:00+00:00"));

    beginProviderToggle("docker", Date.parse("2026-07-20T00:00:05+00:00"));

    expect(isProviderTogglePending("docker")).toBe(true);
  });

  it("clears once a snapshot at or past the click arrives", () => {
    beginProviderToggle("docker", Date.parse("2026-07-20T00:00:05+00:00"));

    applyChromeEvent(providersEvent("2026-07-20T00:00:06+00:00"));

    expect(isProviderTogglePending("docker")).toBe(false);
  });

  it("stays pending across a stale snapshot from before the click", () => {
    beginProviderToggle("docker", Date.parse("2026-07-20T00:00:05+00:00"));

    applyChromeEvent(providersEvent("2026-07-20T00:00:04+00:00"));

    expect(isProviderTogglePending("docker")).toBe(true);
  });

  it("is pending regardless of snapshot age when no snapshot exists", () => {
    applyChromeEvent(providersEvent(null));

    beginProviderToggle("docker", 12345);

    expect(isProviderTogglePending("docker")).toBe(true);
  });

  it("clears on explicit failure so the button reverts", () => {
    beginProviderToggle("docker", 12345);

    failProviderToggle("docker");

    expect(isProviderTogglePending("docker")).toBe(false);
  });
});

describe("requests and status events", () => {
  it("tracks the requests count and ids", () => {
    applyChromeEvent({ type: "requests", count: 2, request_ids: ["evt-1", "evt-2"], auto_open: false });

    expect(getRequestsCount()).toBe(2);
    expect(getRequestIds()).toEqual(["evt-1", "evt-2"]);
  });

  it("stores non-healthy statuses and drops entries on healthy", () => {
    applyChromeEvent({ type: "system_interface_status", agent_id: "agent-1", status: "stuck" });
    expect(getSystemInterfaceStatus("agent-1")).toBe("stuck");

    applyChromeEvent({ type: "system_interface_status", agent_id: "agent-1", status: "healthy" });
    expect(getSystemInterfaceStatus("agent-1")).toBeNull();
  });

  it("latches auth_required", () => {
    applyChromeEvent({ type: "auth_required" });

    expect(isAuthRequired()).toBe(true);
  });
});

describe("seed", () => {
  it("hydrates every payload from the boot state synchronously", () => {
    const bootState: ChromeBootState = {
      workspaces: workspacesEvent({
        workspaces: [{ id: "agent-1", name: "ws", accent: "#112233" }],
        has_accounts: true,
      }),
      providers: providersEvent(null),
      requests: { type: "requests", count: 1, request_ids: ["evt-1"], auto_open: true },
      system_interface_statuses: [{ type: "system_interface_status", agent_id: "agent-1", status: "stuck" }],
    };

    seed(bootState);

    expect(getWorkspaces().map((workspace) => workspace.id)).toEqual(["agent-1"]);
    expect(getHasAccounts()).toBe(true);
    expect(getRequestsCount()).toBe(1);
    expect(getSystemInterfaceStatus("agent-1")).toBe("stuck");
  });
});

describe("connect", () => {
  function fakeHost(): { host: Host; emit: (event: ChromeEvent) => void; subscriberCount: () => number } {
    const callbacks: Array<(event: ChromeEvent) => void> = [];
    const host: Host = {
      kind: "browser",
      onChromeEvent: (callback) => callbacks.push(callback),
      navigate: () => undefined,
      goBack: () => undefined,
      openWorkspaceInNewWindow: () => undefined,
      showWorkspaceContextMenu: () => undefined,
      confirmStopMind: () => Promise.resolve(false),
      openModal: () => undefined,
      closeModal: () => undefined,
    };
    return {
      host,
      emit: (event) => callbacks.forEach((callback) => callback(event)),
      subscriberCount: () => callbacks.length,
    };
  }

  it("subscribes exactly once per document and applies pushed events", () => {
    const fake = fakeHost();

    connect(fake.host);
    connect(fake.host);

    expect(fake.subscriberCount()).toBe(1);
    fake.emit({ type: "requests", count: 4, request_ids: ["a", "b", "c", "d"], auto_open: true });
    expect(getRequestsCount()).toBe(4);
  });

  it("notifies subscribers on every mutation", () => {
    let notifiedCount = 0;
    subscribe(() => {
      notifiedCount += 1;
    });

    applyChromeEvent({ type: "requests", count: 1, request_ids: ["evt-1"], auto_open: true });

    expect(notifiedCount).toBe(1);
  });
});
