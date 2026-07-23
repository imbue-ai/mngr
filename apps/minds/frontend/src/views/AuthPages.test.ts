import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthFormBootExtras } from "../chrome_state";
import { resetHostForTesting } from "../host";
import { mountAccountSettings, mountAuthPage, mountCheckEmail, mountSigninModal } from "./AuthPages";

function authExtras(overrides: Partial<AuthFormBootExtras> = {}): AuthFormBootExtras {
  return { default_to_signup: true, intro: "", message: "", return_to: "", is_modal: false, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mountWithIsland(island: Record<string, unknown>, mount: (el: HTMLElement) => void): HTMLElement {
  const islandEl = document.createElement("script");
  islandEl.type = "application/json";
  islandEl.id = "minds-boot-state";
  islandEl.textContent = JSON.stringify(island);
  document.body.appendChild(islandEl);
  const container = document.createElement("div");
  document.body.appendChild(container);
  mount(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

// window.location stub that records href assignments.
function stubLocation(): { setHref: ReturnType<typeof vi.fn>; restore: () => void; search: string } {
  const setHref = vi.fn();
  const original = window.location;
  const state = { search: "" };
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get search() {
        return state.search;
      },
      set href(value: string) {
        setHref(value);
      },
    },
  });
  return {
    setHref,
    search: "",
    restore: () => Object.defineProperty(window, "location", { configurable: true, value: original }),
  };
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.minds;
  vi.unstubAllGlobals();
  vi.useRealTimers();
  resetHostForTesting();
});

describe("mountAuthPage", () => {
  it("leads with the tab from the island and switches tabs", () => {
    const container = mountWithIsland({ auth: authExtras({ default_to_signup: true }) }, mountAuthPage);
    expect(container.querySelector("#signup-tab")).not.toBeNull();
    expect(container.querySelector("#signin-tab")).toBeNull();
    (Array.from(container.querySelectorAll("a")).find((a) => a.textContent === "Sign in") as HTMLAnchorElement).click();
    m.redraw.sync();
    expect(container.querySelector("#signin-tab")).not.toBeNull();
    expect(container.querySelector("#signup-tab")).toBeNull();
  });

  it("renders the back link + banner from the island", () => {
    const container = mountWithIsland(
      { auth: authExtras({ return_to: "/create", message: "sign in first" }) },
      mountAuthPage,
    );
    expect(container.querySelector("#auth-back-link")?.getAttribute("href")).toBe("/create");
    expect(container.textContent).toContain("sign in first");
    expect(container.textContent).toContain("Continue with Google");
    expect(container.textContent).toContain("Continue with GitHub");
  });

  it("signs in and navigates to /post-login on OK", async () => {
    const location = stubLocation();
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "OK" }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const container = mountWithIsland({ auth: authExtras({ default_to_signup: false }) }, mountAuthPage);
      (container.querySelector("#signin-email") as HTMLInputElement).value = "a@b.com";
      (container.querySelector("#signin-email") as HTMLInputElement).dispatchEvent(new Event("input"));
      (container.querySelector("#signin-password") as HTMLInputElement).value = "pw";
      (container.querySelector("#signin-password") as HTMLInputElement).dispatchEvent(new Event("input"));
      (container.querySelector("#signin-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith(
        "/auth/api/signin",
        expect.objectContaining({ method: "POST", body: JSON.stringify({ email: "a@b.com", password: "pw" }) }),
      );
      expect(location.setHref).toHaveBeenCalledWith("/post-login");
    } finally {
      location.restore();
    }
  });

  it("goes to check-email when a sign-in needs verification", async () => {
    const location = stubLocation();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "OK", needsEmailVerification: true })));
    try {
      const container = mountWithIsland({ auth: authExtras({ default_to_signup: false }) }, mountAuthPage);
      (container.querySelector("#signin-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
      await flushAsync();
      expect(location.setHref).toHaveBeenCalledWith("/auth/check-email");
    } finally {
      location.restore();
    }
  });

  it("shows the sign-in error on WRONG_CREDENTIALS", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "WRONG_CREDENTIALS", message: "nope" })));
    const container = mountWithIsland({ auth: authExtras({ default_to_signup: false }) }, mountAuthPage);
    (container.querySelector("#signin-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#signin-error")?.textContent).toBe("nope");
  });

  it("drives the OAuth poll to completion", async () => {
    vi.useFakeTimers();
    const location = stubLocation();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: "OK", flow_id: "flow-1" }))
      .mockResolvedValue(jsonResponse({ status: "OK", state: "done" }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const container = mountWithIsland({ auth: authExtras() }, mountAuthPage);
      (container.querySelector('[data-oauth="google"]') as HTMLButtonElement).click();
      await flushAsync();
      m.redraw.sync();
      // The waiting message shows, buttons disabled.
      expect(container.textContent).toContain("Waiting for you to finish signing in with Google");
      vi.advanceTimersByTime(2000);
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith("/auth/oauth/status/flow-1");
      expect(location.setHref).toHaveBeenCalledWith("/post-login");
    } finally {
      location.restore();
    }
  });
});

describe("mountSigninModal", () => {
  it("dismisses through the host and navigates the modal return_to on auth success", async () => {
    const navigate = vi.fn();
    const closeModal = vi.fn();
    window.minds = { navigateContent: navigate, closeModal } as unknown as typeof window.minds;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "OK" })));
    const container = mountWithIsland(
      { auth: authExtras({ default_to_signup: false, is_modal: true, return_to: "/create", intro: "why" }) },
      mountSigninModal,
    );
    expect(container.querySelector("#signin-modal-backdrop")).not.toBeNull();
    expect(container.textContent).toContain("why");
    // Auth success routes to return_to via the host (Electron navigateContent).
    (container.querySelector("#signin-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    expect(navigate).toHaveBeenCalledWith("/create");
    // The close button dismisses through the host.
    (container.querySelector('[aria-label="Close"]') as HTMLButtonElement).click();
    expect(closeModal).toHaveBeenCalled();
  });
});

describe("mountCheckEmail", () => {
  it("polls and redirects once verified", async () => {
    vi.useFakeTimers();
    const location = stubLocation();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ verified: true })));
    try {
      const container = mountWithIsland({ check_email: { email: "a@b.com" } }, mountCheckEmail);
      expect(container.textContent).toContain("a@b.com");
      vi.advanceTimersByTime(3000);
      await flushAsync();
      m.redraw.sync();
      expect(container.querySelector("#success-msg")).not.toBeNull();
      vi.advanceTimersByTime(1500);
      expect(location.setHref).toHaveBeenCalledWith("/post-login");
    } finally {
      location.restore();
    }
  });
});

describe("mountAccountSettings", () => {
  it("shows Change password only for email accounts and signs out", async () => {
    const location = stubLocation();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 200 })));
    try {
      const email = mountWithIsland(
        { account_settings: { email: "a@b.com", display_name: "A", provider: "email", user_id_prefix: "abc" } },
        mountAccountSettings,
      );
      expect(email.textContent).toContain("Change password");
      expect(email.textContent).toContain("abc");
      (email.querySelector("#signout-btn") as HTMLButtonElement).click();
      await flushAsync();
      expect(location.setHref).toHaveBeenCalledWith("/");

      document.body.innerHTML = "";
      const oauth = mountWithIsland(
        { account_settings: { email: "a@b.com", display_name: "", provider: "github", user_id_prefix: "abc" } },
        mountAccountSettings,
      );
      expect(oauth.textContent).not.toContain("Change password");
    } finally {
      location.restore();
    }
  });
});
