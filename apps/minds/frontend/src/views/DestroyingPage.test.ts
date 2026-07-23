import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EventSourceLike } from "../host";
import { mountDestroying } from "./DestroyingPage";

const AGENT = "agent-" + "a".repeat(32);

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  isClosed = false;
  private messageListeners: Array<(event: { data: string }) => void> = [];

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: "message" | "error", listener: ((event: { data: string }) => void) | (() => void)): void {
    if (type === "message") this.messageListeners.push(listener as (event: { data: string }) => void);
  }

  emitMessage(data: string): void {
    this.messageListeners.forEach((listener) => listener({ data }));
  }

  close(): void {
    this.isClosed = true;
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function mountFixture(status: string): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({
    destroying: { agent_id: AGENT, agent_name: "alpha", pid: 12345, status },
  });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "destroying-root";
  document.body.appendChild(container);
  mountDestroying(container);
  return container;
}

// Settle the fetch promise chain queued by the initial poll tick. Microtask
// flushes work under both real and fake timers.
async function flushAsync(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  // Unmounts the page (mountWithTeardown) and stops the controller's timers.
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("mountDestroying", () => {
  it("renders the heading, pid line, and a running indicator from the island", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "RUNNING" })));
    const container = mountFixture("running");
    expect(container.textContent).toContain("Destroying alpha");
    expect(container.textContent).toContain(`${AGENT} · pid 12345`);
    expect(container.textContent).toContain("Running...");
    expect(container.querySelector("#destroying-actions")).toBeNull();
    // The log tail opened against the operation's SSE endpoint.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe(`/api/v1/workspaces/operations/destroy/${AGENT}/logs`);
  });

  it("appends streamed log frames to the log view", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "RUNNING" })));
    const container = mountFixture("running");
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ log: "removing volume\n" }));
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ log: "done removing\n" }));
    m.redraw.sync();
    expect(container.querySelector("#destroying-log")?.textContent).toBe("removing volume\ndone removing\n");
  });

  it("reveals Retry/Dismiss when the status poll reports FAILED", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "FAILED" })));
    const container = mountFixture("running");
    await flushAsync();
    m.redraw.sync();
    expect(container.textContent).toContain("Failed");
    expect(container.querySelector("#destroying-retry-btn")).not.toBeNull();
    expect(container.querySelector("#destroying-dismiss-btn")).not.toBeNull();
    // Terminal state: the log stream is closed.
    expect(FakeEventSource.instances[0].isClosed).toBe(true);
  });

  it("redirects Home shortly after the poll reports DONE", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "DONE" })));
    const setHref = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        set href(value: string) {
          setHref(value);
        },
      },
    });
    try {
      const container = mountFixture("running");
      await flushAsync();
      m.redraw.sync();
      expect(container.textContent).toContain("Done. Redirecting...");
      expect(setHref).not.toHaveBeenCalled();
      vi.advanceTimersByTime(800);
      expect(setHref).toHaveBeenCalledWith("/");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("re-issues the destroy and resets the flow when Retry is clicked", async () => {
    const fetchMock = vi
      .fn()
      // Initial poll confirms the failure.
      .mockResolvedValueOnce(jsonResponse({ status: "FAILED" }))
      // The retry POST.
      .mockResolvedValueOnce(new Response("", { status: 200 }))
      // Subsequent polls report running again.
      .mockResolvedValue(jsonResponse({ status: "RUNNING" }));
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture("running");
    // Log content arrives before the failure lands, so the retry's reset is
    // observable below.
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ log: "old log\n" }));
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#destroying-log")?.textContent).toBe("old log\n");
    (container.querySelector("#destroying-retry-btn") as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/workspaces/${AGENT}/destroy`, { method: "POST" });
    // The flow restarted: log cleared, running indicator back, a fresh SSE tail.
    expect(container.querySelector("#destroying-log")?.textContent).toBe("");
    expect(container.textContent).toContain("Running...");
    expect(container.querySelector("#destroying-actions")).toBeNull();
    expect(FakeEventSource.instances.length).toBeGreaterThan(1);
  });
});
