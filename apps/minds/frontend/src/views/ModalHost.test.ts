import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EventSourceLike } from "../host";
import { getHost, resetHostForTesting } from "../host";
import { resetStoreForTesting } from "../store";
import { modalPageUrl, mountModalHost } from "./ModalHost";

const AGENT = "agent-" + "d".repeat(32);

afterEach(() => {
  document.body.innerHTML = "";
  resetStoreForTesting();
  resetHostForTesting();
  delete window.__mindsModalHostBridge;
  delete window.__mindsNavigateContent;
  vi.unstubAllGlobals();
});

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  private messageListeners: Array<(event: { data: string }) => void> = [];

  constructor() {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: "message" | "error", listener: ((event: { data: string }) => void) | (() => void)): void {
    if (type === "message") this.messageListeners.push(listener as (event: { data: string }) => void);
  }

  emitMessage(data: string): void {
    this.messageListeners.forEach((listener) => listener({ data }));
  }

  close(): void {}
}

function mountFixture(): ReturnType<typeof mountModalHost> {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  const container = document.createElement("div");
  container.id = "minds-modal-host";
  document.body.appendChild(container);
  return mountModalHost(container);
}

function frameEl(): HTMLIFrameElement | null {
  return document.getElementById("minds-modal-frame") as HTMLIFrameElement | null;
}

describe("modalPageUrl", () => {
  it("mirrors the overlay URLs Electron's openModal loads", () => {
    expect(modalPageUrl({ kind: "minds-settings" })).toBe("/settings/modal");
    expect(modalPageUrl({ kind: "accounts" })).toBe("/accounts/modal");
    expect(modalPageUrl({ kind: "signin", returnTo: "/create", mode: "signin" })).toBe(
      "/auth/signin-modal?return_to=%2Fcreate&mode=signin",
    );
    expect(modalPageUrl({ kind: "signin", returnTo: "", mode: "signup" })).toBe("/auth/signin-modal");
    // An unsafe return_to is dropped (the server default takes over).
    expect(modalPageUrl({ kind: "signin", returnTo: "//evil.example.com", mode: "signup" })).toBe(
      "/auth/signin-modal",
    );
    expect(modalPageUrl({ kind: "inbox" })).toBe("/inbox?keep_open=1");
    expect(modalPageUrl({ kind: "inbox", selectedRequestId: "evt-1" })).toBe("/inbox?selected=evt-1");
    expect(modalPageUrl({ kind: "help", workspaceAgentId: AGENT, isAssistAvailable: true })).toBe(
      `/help?workspace=${AGENT}&assist=1`,
    );
    expect(modalPageUrl({ kind: "sharing", agentId: AGENT, serviceName: "web" })).toBe(
      `/sharing/${AGENT}/web/modal`,
    );
    expect(modalPageUrl({ kind: "sharing", agentId: AGENT })).toBeNull();
  });
});

describe("ModalHost open/close", () => {
  it("shows the modal page in a full-viewport iframe and closes it", () => {
    const handle = mountFixture();
    expect(frameEl()).toBeNull();
    expect(handle.isOpen()).toBe(false);

    handle.open({ kind: "inbox" });
    m.redraw.sync();

    const frame = frameEl();
    expect(frame?.getAttribute("src")).toBe("/inbox?keep_open=1");
    expect(handle.isOpen()).toBe(true);

    handle.close();
    m.redraw.sync();
    expect(frameEl()).toBeNull();
    expect(handle.isOpen()).toBe(false);
  });

  it("routes the browser host's openModal through the mounted layer", () => {
    const navigations: string[] = [];
    window.__mindsNavigateContent = (url) => navigations.push(url);
    mountFixture();

    getHost().openModal({ kind: "minds-settings" });
    m.redraw.sync();

    // No full-page navigation: the modal opened in place.
    expect(navigations).toEqual([]);
    expect(frameEl()?.getAttribute("src")).toBe("/settings/modal");
  });

  it("toggleInbox closes an open inbox and opens it otherwise", () => {
    const handle = mountFixture();
    handle.toggleInbox();
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/inbox?keep_open=1");

    handle.toggleInbox();
    m.redraw.sync();
    expect(frameEl()).toBeNull();

    // With a DIFFERENT modal open, the requests button switches to the inbox.
    handle.open({ kind: "minds-settings" });
    handle.toggleInbox();
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/inbox?keep_open=1");
  });

  it("a whole-inbox open toggles like the electron host's requests button", () => {
    mountFixture();
    getHost().openModal({ kind: "inbox" });
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/inbox?keep_open=1");

    getHost().openModal({ kind: "inbox" });
    m.redraw.sync();
    expect(frameEl()).toBeNull();

    // A targeted open always shows the selection (never toggle-closes).
    getHost().openModal({ kind: "inbox", selectedRequestId: "evt-9" });
    getHost().openModal({ kind: "inbox", selectedRequestId: "evt-9" });
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/inbox?selected=evt-9");
  });

  it("reopening the same URL loads a fresh frame (its subscribers were dropped)", () => {
    const handle = mountFixture();
    handle.open({ kind: "inbox", selectedRequestId: "evt-1" });
    m.redraw.sync();
    const firstFrame = frameEl();
    expect(firstFrame?.getAttribute("src")).toBe("/inbox?selected=evt-1");

    // A repeat of the same targeted open (e.g. the content relay firing
    // again) clears the frame's bridge subscribers, so the frame must be
    // recreated -- a fresh load re-subscribes; the old element staying put
    // would leave the hosted page's chrome-event feed dead.
    handle.open({ kind: "inbox", selectedRequestId: "evt-1" });
    m.redraw.sync();
    const secondFrame = frameEl();
    expect(secondFrame?.getAttribute("src")).toBe("/inbox?selected=evt-1");
    expect(secondFrame).not.toBe(firstFrame);
  });

  it("dismisses on Escape pressed inside the hosted frame", () => {
    const handle = mountFixture();
    handle.open({ kind: "minds-settings" });
    m.redraw.sync();

    const frame = frameEl() as HTMLIFrameElement;
    // jsdom does not load iframe URLs; fire the load handler by hand against
    // its about:blank document.
    frame.dispatchEvent(new Event("load"));
    frame.contentDocument?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    m.redraw.sync();

    expect(frameEl()).toBeNull();
  });
});

describe("ModalHost frame bridge", () => {
  it("exposes a window.minds-compatible bridge whose closeModal dismisses", () => {
    const handle = mountFixture();
    handle.open({ kind: "sharing", agentId: AGENT, serviceName: "web" });
    m.redraw.sync();

    const bridge = window.__mindsModalHostBridge;
    expect(bridge).toBeDefined();
    bridge?.closeModal();
    m.redraw.sync();

    expect(frameEl()).toBeNull();
  });

  it("navigateContent dismisses and lands the navigation behind the modal", () => {
    const navigations: string[] = [];
    window.__mindsNavigateContent = (url) => navigations.push(url);
    const handle = mountFixture();
    handle.open({ kind: "signin", returnTo: "/create", mode: "signup" });
    m.redraw.sync();

    // The sign-in page's MINDS_AUTH_NAV path: window.minds.navigateContent.
    window.__mindsModalHostBridge?.navigateContent("/create");
    m.redraw.sync();

    expect(frameEl()).toBeNull();
    expect(navigations).toEqual(["/create"]);
  });

  it("bridge modal-open members swap the hosted page in place", () => {
    mountFixture();
    const bridge = window.__mindsModalHostBridge;

    bridge?.openSigninModal("/", "signin");
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/auth/signin-modal?return_to=%2F&mode=signin");

    bridge?.openAccounts();
    m.redraw.sync();
    expect(frameEl()?.getAttribute("src")).toBe("/accounts/modal");
  });

  it("fans this document's chrome events into the open frame's subscribers", () => {
    const handle = mountFixture();
    handle.open({ kind: "inbox" });
    m.redraw.sync();

    const received: string[] = [];
    // The hosted inbox page's store connect() subscribes through the bridge.
    window.__mindsModalHostBridge?.onChromeEvent((event) => received.push(event.type));
    FakeEventSource.instances[0].emitMessage(
      JSON.stringify({ type: "requests", count: 1, request_ids: ["e"], cards: [], auto_open: true }),
    );

    expect(received).toEqual(["requests"]);

    // Closing drops the frame's subscribers: no forwarding to dead frames.
    handle.close();
    FakeEventSource.instances[0].emitMessage(
      JSON.stringify({ type: "requests", count: 2, request_ids: ["e", "f"], cards: [], auto_open: true }),
    );
    expect(received).toEqual(["requests"]);
  });
});
