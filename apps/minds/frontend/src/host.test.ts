import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChromeEvent } from "./chrome_state";
import type { EventSourceLike, MindsBridge } from "./host";
import { createBrowserHost, createElectronHost, getHost, resetHostForTesting } from "./host";

afterEach(() => {
  resetHostForTesting();
  delete window.minds;
  delete window.__mindsNavigateContent;
  document.body.innerHTML = "";
  vi.useRealTimers();
});

function fakeBridge(): { bridge: MindsBridge; calls: string[] } {
  const calls: string[] = [];
  const record =
    (name: string) =>
    (...args: unknown[]) => {
      calls.push(`${name}(${args.map(String).join(",")})`);
    };
  const bridge: MindsBridge = {
    onChromeEvent: record("onChromeEvent") as MindsBridge["onChromeEvent"],
    navigateContent: record("navigateContent"),
    contentGoBack: record("contentGoBack"),
    openWorkspaceInNewWindow: record("openWorkspaceInNewWindow"),
    showWorkspaceContextMenu: record("showWorkspaceContextMenu"),
    confirmStopMind: record("confirmStopMind"),
    openMindsSettings: record("openMindsSettings"),
    openAccounts: record("openAccounts"),
    openSigninModal: record("openSigninModal"),
    toggleInbox: record("toggleInbox"),
    toggleHelp: record("toggleHelp"),
    openSharingModal: record("openSharingModal"),
    closeModal: record("closeModal"),
  };
  return { bridge, calls };
}

class FakeEventSource implements EventSourceLike {
  private messageListeners: Array<(event: { data: string }) => void> = [];
  private errorListeners: Array<() => void> = [];
  closeCount = 0;

  addEventListener(type: "message" | "error", listener: ((event: { data: string }) => void) | (() => void)): void {
    if (type === "message") this.messageListeners.push(listener as (event: { data: string }) => void);
    else this.errorListeners.push(listener as () => void);
  }

  emitMessage(data: string): void {
    this.messageListeners.forEach((listener) => listener({ data }));
  }

  emitError(): void {
    this.errorListeners.forEach((listener) => listener());
  }

  close(): void {
    this.closeCount += 1;
  }
}

describe("electron host", () => {
  it("routes navigation and window actions to the bridge", () => {
    const { bridge, calls } = fakeBridge();
    const host = createElectronHost(bridge);

    host.navigate("/create");
    host.goBack();
    host.openWorkspaceInNewWindow("agent-1");
    host.showWorkspaceContextMenu("agent-1", 10, 20);

    expect(calls).toEqual([
      "navigateContent(/create)",
      "contentGoBack()",
      "openWorkspaceInNewWindow(agent-1)",
      "showWorkspaceContextMenu(agent-1,10,20)",
    ]);
  });

  it("delegates confirmStopMind to main and tells the caller not to run the in-page flow", async () => {
    const { bridge, calls } = fakeBridge();
    const host = createElectronHost(bridge);

    const shouldRunInPageFlow = await host.confirmStopMind("agent-1", "my mind");

    expect(shouldRunInPageFlow).toBe(false);
    expect(calls).toEqual(["confirmStopMind(agent-1,my mind)"]);
  });

  it("maps each modal kind to its overlay IPC", () => {
    const { bridge, calls } = fakeBridge();
    const host = createElectronHost(bridge);

    host.openModal({ kind: "minds-settings" });
    host.openModal({ kind: "signin", returnTo: "/", mode: "signin" });
    host.openModal({ kind: "help", workspaceAgentId: "agent-1", isAssistAvailable: true });
    host.closeModal();

    expect(calls).toEqual([
      "openMindsSettings()",
      "openSigninModal(/,signin)",
      "toggleHelp(agent-1,true)",
      "closeModal()",
    ]);
  });
});

describe("browser host", () => {
  it("navigates through the swap engine export when chrome.js provides it", () => {
    const swapNavigations: string[] = [];
    window.__mindsNavigateContent = (url) => swapNavigations.push(url);
    const host = createBrowserHost({ createEventSource: () => new FakeEventSource(), reconnectDelayMs: 5000 });

    host.navigate("/settings");

    expect(swapNavigations).toEqual(["/settings"]);
  });

  it("shares one EventSource among subscribers and fans events out", () => {
    const sources: FakeEventSource[] = [];
    const host = createBrowserHost({
      createEventSource: () => {
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
      reconnectDelayMs: 5000,
    });
    const received: ChromeEvent[] = [];

    host.onChromeEvent((event) => received.push(event));
    host.onChromeEvent((event) => received.push(event));
    expect(sources).toHaveLength(1);

    sources[0].emitMessage(JSON.stringify({ type: "requests", count: 1, request_ids: ["e"], auto_open: true }));

    expect(received).toHaveLength(2);
    expect(received[0].type).toBe("requests");
  });

  it("reconnects after an error on one shared reconnect loop", () => {
    vi.useFakeTimers();
    const sources: FakeEventSource[] = [];
    const host = createBrowserHost({
      createEventSource: () => {
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
      reconnectDelayMs: 5000,
    });
    host.onChromeEvent(() => undefined);

    sources[0].emitError();
    expect(sources).toHaveLength(1);
    vi.advanceTimersByTime(5000);

    expect(sources).toHaveLength(2);
    expect(sources[0].closeCount).toBeGreaterThan(0);
  });

  it("ignores malformed event payloads", () => {
    const sources: FakeEventSource[] = [];
    const host = createBrowserHost({
      createEventSource: () => {
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
      reconnectDelayMs: 5000,
    });
    const received: ChromeEvent[] = [];
    host.onChromeEvent((event) => received.push(event));

    sources[0].emitMessage("{not json");

    expect(received).toEqual([]);
  });

  it("maps modal kinds to the full-page fallback routes", () => {
    const navigations: string[] = [];
    window.__mindsNavigateContent = (url) => navigations.push(url);
    const host = createBrowserHost({ createEventSource: () => new FakeEventSource(), reconnectDelayMs: 5000 });

    host.openModal({ kind: "minds-settings" });
    host.openModal({ kind: "inbox" });
    host.openModal({ kind: "inbox", selectedRequestId: "evt-1" });
    host.openModal({ kind: "help", workspaceAgentId: "agent-1", isAssistAvailable: true });

    expect(navigations).toEqual([
      "/settings",
      "/inbox?keep_open=1",
      "/inbox?selected=evt-1",
      "/help?workspace=agent-1&assist=1",
    ]);
  });

  it("opens a workspace in a new tab via the mngr-forward origin", () => {
    document.body.dataset.mngrForwardOrigin = "https://localhost:8421";
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const host = createBrowserHost({ createEventSource: () => new FakeEventSource(), reconnectDelayMs: 5000 });

    host.openWorkspaceInNewWindow("agent-1");

    expect(openSpy).toHaveBeenCalledWith("https://localhost:8421/goto/agent-1/", "_blank", "noopener");
    openSpy.mockRestore();
    delete document.body.dataset.mngrForwardOrigin;
  });
});

describe("getHost", () => {
  it("chooses the electron host when the bridge exists", () => {
    window.minds = fakeBridge().bridge;

    expect(getHost().kind).toBe("electron");
  });

  it("chooses the browser host without the bridge", () => {
    expect(getHost().kind).toBe("browser");
  });
});
