import { afterEach, describe, expect, it, vi } from "vitest";
import { BACKOFF_CAP_MS, VISIBLE_AFTER_FAILURES, backoffDelayMs } from "./backoff";
import { parseServerMessage } from "./messages";
import {
  SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS,
  UiChannelClient,
  type ChannelSocketLike,
} from "./client";
import { createEmptyStores } from "../models/boot";

class FakeSocket implements ChannelSocketLike {
  sent: string[] = [];
  isClosed = false;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.isClosed = true;
  }
  open(): void {
    this.onopen?.({});
  }
  receive(frame: object): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

afterEach(() => {
  vi.useRealTimers();
});

describe("backoffDelayMs", () => {
  it("doubles from the base and caps, with plus-minus 25 percent jitter bounds", () => {
    expect(backoffDelayMs(1, 0.5)).toBe(500);
    expect(backoffDelayMs(2, 0.5)).toBe(1000);
    expect(backoffDelayMs(1, 0)).toBe(375);
    expect(backoffDelayMs(1, 1)).toBe(625);
    expect(backoffDelayMs(20, 0.5)).toBe(BACKOFF_CAP_MS);
    // The jitter deliberately spreads AROUND the capped base -- steady-state
    // draws must not collapse to exactly the cap.
    expect(backoffDelayMs(20, 0)).toBe(BACKOFF_CAP_MS * 0.75);
    expect(backoffDelayMs(20, 1)).toBe(BACKOFF_CAP_MS * 1.25);
  });
});

describe("parseServerMessage", () => {
  it("accepts known types and rejects junk and unknown types", () => {
    expect(parseServerMessage(JSON.stringify({ type: "hello", schema_version: 1 }))).toEqual({
      type: "hello",
      schema_version: 1,
    });
    expect(parseServerMessage("not json")).toBeNull();
    expect(parseServerMessage(JSON.stringify({ type: "later_addition" }))).toBeNull();
    expect(parseServerMessage(JSON.stringify({ no_type: true }))).toBeNull();
  });
});

describe("UiChannelClient", () => {
  function makeClient(overrides: { expectedSchemaVersion?: number | null; seededLatch?: boolean } = {}) {
    const stores = createEmptyStores();
    const sockets: FakeSocket[] = [];
    const reloads: number[] = [];
    const storageMap = new Map<string, string>();
    if (overrides.seededLatch === true) storageMap.set(SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS, "1");
    const storage = {
      getItem: (key: string) => storageMap.get(key) ?? null,
      setItem: (key: string, value: string) => void storageMap.set(key, value),
      removeItem: (key: string) => void storageMap.delete(key),
    };
    const client = new UiChannelClient({
      stores,
      expectedSchemaVersion: overrides.expectedSchemaVersion === undefined ? 1 : overrides.expectedSchemaVersion,
      createSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      reloadPage: () => reloads.push(1),
      jitter01: () => 0.5,
      redraw: () => undefined,
      storage,
    });
    return { client, stores, sockets, reloads, storageMap };
  }

  it("sends client_state on open and on route changes", () => {
    const { client, sockets } = makeClient();
    client.start();
    sockets[0].open();
    client.setClientState("/settings", null);
    const frames = sockets[0].sent.map((raw) => JSON.parse(raw) as { type: string; route: string });
    expect(frames.every((frame) => frame.type === "client_state")).toBe(true);
    expect(frames.at(-1)?.route).toBe("/settings");
  });

  it("sends one frame for repeated identical setClientState calls", () => {
    const { client, sockets } = makeClient();
    client.start();
    sockets[0].open();
    const framesAfterOpen = sockets[0].sent.length;

    client.setClientState("/settings", null);
    client.setClientState("/settings", null);
    client.setClientState("/settings", null);

    expect(sockets[0].sent.length).toBe(framesAfterOpen + 1);

    client.setClientState("/workspace/agent-1", "agent-1");
    expect(sockets[0].sent.length).toBe(framesAfterOpen + 2);
  });

  it("dispatches workspaces frames into the store", () => {
    const { client, stores, sockets } = makeClient();
    client.start();
    sockets[0].open();
    sockets[0].receive({
      type: "workspaces",
      workspaces: [],
      destroying_agent_ids: ["agent-1"],
      restorable_workspace_ids: [],
      remote_workspace_states: {},
    });
    expect(stores.workspaces.destroyingAgentIds).toEqual(["agent-1"]);
  });

  it("clears per-workspace health on the reconnect hello (reconnect is resync)", () => {
    vi.useFakeTimers();
    const { client, stores, sockets } = makeClient();
    client.start();
    sockets[0].open();
    sockets[0].receive({ type: "hello", schema_version: 1 });
    sockets[0].receive({ type: "health", agent_id: "agent-1", status: "stuck", error: null });
    expect(stores.health.statusFor("agent-1")).toBe("stuck");

    // The agent recovers while the socket is down; the reconnect snapshot
    // carries no frame for it, so the hello alone must clear it.
    sockets[0].onclose?.({});
    vi.advanceTimersByTime(600);
    sockets[1].open();
    sockets[1].receive({ type: "hello", schema_version: 1 });
    expect(stores.health.statusFor("agent-1")).toBe("healthy");
  });

  it("reconnects with backoff after close", () => {
    vi.useFakeTimers();
    const { client, sockets } = makeClient();
    client.start();
    sockets[0].open();
    sockets[0].onclose?.({});
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(600);
    expect(sockets).toHaveLength(2);
  });

  it("accumulates consecutive failures across repeated closes and resets on open", () => {
    vi.useFakeTimers();
    const { client, sockets } = makeClient();
    client.start();

    sockets[0].onclose?.({});
    expect(client.consecutiveFailures).toBe(1);
    vi.runOnlyPendingTimers();
    sockets[1].onclose?.({});
    expect(client.consecutiveFailures).toBe(2);
    vi.runOnlyPendingTimers();
    sockets[2].onclose?.({});
    expect(client.consecutiveFailures).toBe(3);

    vi.runOnlyPendingTimers();
    sockets[3].open();
    expect(client.consecutiveFailures).toBe(0);
    expect(client.isConnected).toBe(true);
  });

  it("surfaces the reconnect indicator only once VISIBLE_AFTER_FAILURES is reached", () => {
    vi.useFakeTimers();
    const { client, sockets } = makeClient();
    client.start();

    for (let failure = 1; failure < VISIBLE_AFTER_FAILURES; failure += 1) {
      sockets[failure - 1].onclose?.({});
      expect(client.isVisiblyReconnecting).toBe(false);
      vi.runOnlyPendingTimers();
    }

    sockets[VISIBLE_AFTER_FAILURES - 1].onclose?.({});
    expect(client.isVisiblyReconnecting).toBe(true);
  });

  it("stop() closes the socket and prevents any further reconnect", () => {
    vi.useFakeTimers();
    const { client, sockets } = makeClient();
    client.start();
    sockets[0].open();

    client.stop();
    expect(sockets[0].isClosed).toBe(true);
    sockets[0].onclose?.({});
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it("stop() cancels an already-scheduled reconnect", () => {
    vi.useFakeTimers();
    const { client, sockets } = makeClient();
    client.start();
    sockets[0].onclose?.({});

    client.stop();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it("hard-reloads once on schema mismatch and latches", () => {
    const { client, sockets, reloads, storageMap } = makeClient({ expectedSchemaVersion: 1 });
    client.start();
    sockets[0].open();
    sockets[0].receive({ type: "hello", schema_version: 2 });
    expect(reloads).toHaveLength(1);
    expect(storageMap.get(SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS)).toBe("1");
    sockets[0].receive({ type: "hello", schema_version: 2 });
    expect(reloads).toHaveLength(1);
  });

  it("never reloads when there is no expected schema version (no bootstrap)", () => {
    const { client, sockets, reloads, storageMap } = makeClient({ expectedSchemaVersion: null });
    client.start();
    sockets[0].open();
    sockets[0].receive({ type: "hello", schema_version: 7 });
    expect(reloads).toHaveLength(0);
    expect(storageMap.has(SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS)).toBe(false);
  });

  it("clears the reload latch when versions match again", () => {
    const { client, sockets, storageMap } = makeClient({ expectedSchemaVersion: 1, seededLatch: true });
    client.start();
    sockets[0].open();
    sockets[0].receive({ type: "hello", schema_version: 1 });
    expect(storageMap.has(SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS)).toBe(false);
  });
});
