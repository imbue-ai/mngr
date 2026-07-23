import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EventSourceLike } from "../host";
import { resetHostForTesting } from "../host";
import { mountCreating } from "./CreatingPage";

const CREATION_ID = "creation-" + "1".repeat(32);

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

function mountFixture(): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({
    creating: { agent_id: CREATION_ID, status_text: "Preparing...", expected_duration_seconds: 30.0 },
  });
  document.body.appendChild(island);
  const container = document.createElement("main");
  container.id = "creating-root";
  document.body.appendChild(container);
  mountCreating(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.__mindsNavigateContent;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountCreating", () => {
  it("renders the progress view with the island's stage caption", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "INITIALIZING" })));
    const container = mountFixture();
    expect(container.textContent).toContain("Setting up your workspace");
    expect(container.querySelector("#progress-view")).not.toBeNull();
    expect(container.querySelector("#failure-view")).toBeNull();
    expect(container.querySelector("#bar-fill")).not.toBeNull();
    expect(container.querySelector("#stage")?.textContent).toBe("Preparing...");
    // Logs stream from the create operation's SSE endpoint.
    expect(FakeEventSource.instances[0].url).toBe(`/api/v1/workspaces/operations/create/${CREATION_ID}/logs`);
  });

  it("updates the stage caption from the status poll", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: "CLONING_REPO", status_text: "Cloning repository..." })),
    );
    const container = mountFixture();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#stage")?.textContent).toBe("Cloning repository...");
  });

  it("shows the failure view with GitHub guidance for GITHUB_AUTH_REQUIRED", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ status: "FAILED", error: "auth failed for repo", error_kind: "GITHUB_AUTH_REQUIRED" }),
      ),
    );
    const container = mountFixture();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#progress-view")).toBeNull();
    expect(container.querySelector("#failure-view")).not.toBeNull();
    expect(container.querySelector("#error-message")?.textContent).toBe("auth failed for repo");
    // The GitHub-specific guidance names the CLI sign-in command, links the
    // official docs, and offers the local-path alternative.
    const githubHelp = container.querySelector("#github-auth-help");
    expect(githubHelp).not.toBeNull();
    expect(githubHelp?.textContent).toContain("gh auth login");
    expect(githubHelp?.querySelector("a")?.getAttribute("href")).toBe(
      "https://docs.github.com/en/github-cli/github-cli/quickstart",
    );
    expect(githubHelp?.textContent).toContain("path in the form instead of the URL");
    expect(container.querySelector("#git-auth-help")).toBeNull();
    // The prominent error box carries the message; the footer caption clears.
    expect(container.querySelector("#stage")?.textContent).toBe("");
  });

  it("shows the generic git guidance (without GitHub CLI advice) for GIT_AUTH_REQUIRED", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: "FAILED", error: "boom", error_kind: "GIT_AUTH_REQUIRED" })),
    );
    const container = mountFixture();
    await flushAsync();
    m.redraw.sync();
    const gitHelp = container.querySelector("#git-auth-help");
    expect(gitHelp).not.toBeNull();
    expect(gitHelp?.textContent).toContain("path in the form instead of the URL");
    // The generic block must not carry the GitHub-CLI advice, which only
    // fits github.com.
    expect(gitHelp?.textContent).not.toContain("gh auth login");
    expect(container.querySelector("#github-auth-help")).toBeNull();
  });

  it("omits both guidance blocks for an unclassified failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: "FAILED", error: "disk full" })),
    );
    const container = mountFixture();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#failure-view")).not.toBeNull();
    expect(container.querySelector("#github-auth-help")).toBeNull();
    expect(container.querySelector("#git-auth-help")).toBeNull();
  });

  it("hands the ready workspace URL to the host on DONE", async () => {
    const navigateSpy = vi.fn();
    window.__mindsNavigateContent = navigateSpy;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: "DONE", redirect_url: "https://localhost:8421/goto/agent-x/" })),
    );
    mountFixture();
    await flushAsync();
    expect(navigateSpy).toHaveBeenCalledWith("https://localhost:8421/goto/agent-x/");
  });

  it("reveals streamed logs behind the details toggle", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "INITIALIZING" })));
    const container = mountFixture();
    expect(container.querySelector("#logs")).toBeNull();
    (container.querySelector("#details-toggle") as HTMLButtonElement).click();
    m.redraw.sync();
    expect(container.querySelector("#details-toggle")?.textContent).toBe("Hide details");
    // A done frame flushes any batched lines synchronously.
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ log: "cloning" }));
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ log: "building" }));
    FakeEventSource.instances[0].emitMessage(JSON.stringify({ done: true }));
    m.redraw.sync();
    expect(container.querySelector("#logs")?.textContent).toBe("cloning\nbuilding\n");
    expect(FakeEventSource.instances[0].isClosed).toBe(true);
  });
});
