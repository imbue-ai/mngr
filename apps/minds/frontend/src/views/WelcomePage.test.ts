import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModalRequest } from "../host";
import { registerModalHost, resetHostForTesting } from "../host";
import { mountWelcome } from "./WelcomePage";

function mountFixture(): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ welcome: {} });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "welcome-root";
  document.body.appendChild(container);
  mountWelcome(container);
  return container;
}

// The browser host lazily opens the shared chrome EventSource on the first
// onChromeEvent subscription (which mountWelcome makes), so every fixture
// needs a fake installed.
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  private messageListeners: Array<(event: { data: string }) => void> = [];

  constructor() {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: { data: string }) => void): void {
    if (type === "message") this.messageListeners.push(listener);
  }

  emitMessage(data: string): void {
    this.messageListeners.forEach((listener) => listener({ data }));
  }

  close(): void {}
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.__mindsOpenModal;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountWelcome", () => {
  it("renders the splash with live /auth/* fallback links and the skip route", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const container = mountFixture();
    expect(container.textContent).toContain("Welcome to Minds");
    expect(container.querySelector("#welcome-signup-btn")?.getAttribute("href")).toBe("/auth/signup");
    expect(container.querySelector("#welcome-login-btn")?.getAttribute("href")).toBe("/auth/login");
    // The skip link goes through /welcome/skip (recording the choice, then
    // the consent-bearing landing route), never straight to /create.
    expect(container.querySelector("#skip-account-btn")?.getAttribute("href")).toBe("/welcome/skip");
  });

  it("opens the sign-in modal with the clicked mode when a modal surface exists", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const openRequests: ModalRequest[] = [];
    // Signals modal availability (chrome.js sets this at shell boot)...
    window.__mindsOpenModal = (request) => openRequests.push(request);
    // ...and the host routes through the registered in-document modal layer.
    registerModalHost({ open: (request) => openRequests.push(request), close: () => {}, isOpen: () => false });
    const container = mountFixture();

    (container.querySelector("#welcome-signup-btn") as HTMLAnchorElement).click();
    (container.querySelector("#welcome-login-btn") as HTMLAnchorElement).click();
    expect(openRequests).toEqual([
      { kind: "signin", returnTo: "/", mode: "signup" },
      { kind: "signin", returnTo: "/", mode: "signin" },
    ]);
  });

  it("self-advances to home when the chrome stream reports an account", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
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
      mountFixture();
      const stream = FakeEventSource.instances[0];
      stream.emitMessage(JSON.stringify({ type: "workspaces", workspaces: [], has_accounts: false }));
      expect(setHref).not.toHaveBeenCalled();
      stream.emitMessage(JSON.stringify({ type: "workspaces", workspaces: [], has_accounts: true }));
      expect(setHref).toHaveBeenCalledWith("/");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });
});
