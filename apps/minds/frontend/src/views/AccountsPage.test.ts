import { afterEach, describe, expect, it, vi } from "vitest";

import type { AccountEntryPayload, AccountsBootExtras } from "../chrome_state";
import type { ModalRequest } from "../host";
import { registerModalHost, resetHostForTesting } from "../host";
import { mountAccountsModal, mountAccountsPage } from "./AccountsPage";

function account(overrides: Partial<AccountEntryPayload> = {}): AccountEntryPayload {
  return {
    user_id: "user-1",
    email: "a@example.com",
    workspace_count: 2,
    is_default: false,
    is_enabled: true,
    ...overrides,
  };
}

function mountFixture(extras: AccountsBootExtras, variant: "page" | "modal" = "page"): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ accounts: extras });
  document.body.appendChild(island);
  const container = document.createElement("div");
  document.body.appendChild(container);
  if (variant === "page") mountAccountsPage(container);
  else mountAccountsModal(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.minds;
  delete window.__mindsOpenModal;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountAccountsPage", () => {
  it("shows the empty state with the Add account launcher", () => {
    const container = mountFixture({ accounts: [], is_modal: false });
    expect(container.textContent).toContain("No accounts logged in.");
    expect(container.textContent).toContain("Add account");
  });

  it("renders account rows with default marker and signed-out indicator", () => {
    const container = mountFixture({
      accounts: [
        account({ user_id: "u-default", email: "default@example.com", is_default: true, workspace_count: 3 }),
        account({ user_id: "u-disabled", email: "disabled@example.com", is_enabled: false }),
      ],
      is_modal: false,
    });
    expect(container.textContent).toContain("default@example.com");
    expect(container.textContent).toContain("3 workspace(s)");
    expect(container.textContent).toContain("· Default");
    // The default account shows the inert Default chip, not Set default.
    const buttons = Array.from(container.querySelectorAll("button")).map((button) => button.textContent);
    expect(buttons.filter((label) => label === "Set default")).toHaveLength(1);
    expect(container.textContent).toContain("Signed out");
    expect(buttons).toContain("Sign in again");
  });

  it("POSTs set-default and log-out then reloads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const reload = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", { configurable: true, value: { reload } });
    try {
      const container = mountFixture({ accounts: [account({ user_id: "u&1" })], is_modal: false });
      (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Set default") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith("/accounts/set-default", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "user_id=u%261",
      });
      (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Log out") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith("/accounts/u%261/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "",
      });
      expect(reload).toHaveBeenCalledTimes(2);
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("opens the sign-in modal through the host when a modal surface exists", () => {
    const openRequests: ModalRequest[] = [];
    window.__mindsOpenModal = (request) => openRequests.push(request);
    registerModalHost({ open: (request) => openRequests.push(request), close: () => {}, isOpen: () => false });
    const container = mountFixture({ accounts: [], is_modal: false });
    (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add account") as HTMLButtonElement).click();
    expect(openRequests).toEqual([{ kind: "signin", returnTo: "/", mode: "signin" }]);
  });
});

describe("mountAccountsModal", () => {
  it("renders the card chrome and dismisses through the host", () => {
    const closeModal = vi.fn();
    window.minds = { closeModal } as unknown as typeof window.minds;
    const container = mountFixture({ accounts: [account()], is_modal: true }, "modal");
    expect(container.querySelector("#accounts-modal-backdrop")).not.toBeNull();
    expect(container.textContent).toContain("Manage Accounts");
    (container.querySelector('[aria-label="Close"]') as HTMLButtonElement).click();
    expect(closeModal).toHaveBeenCalled();
  });
});
