import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CreateFormBootExtras } from "../chrome_state";
import type { ModalRequest } from "../host";
import { registerModalHost, resetHostForTesting } from "../host";
import { hostNameFormatError, mountCreateForm } from "./CreateFormPage";

function extras(overrides: Partial<CreateFormBootExtras> = {}): CreateFormBootExtras {
  return {
    git_url: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v0.3.8",
    host_name: "",
    color: "#0b292b",
    launch_modes: ["DOCKER", "LIMA", "VULTR", "AWS", "MODAL", "IMBUE_CLOUD"],
    selected_launch_mode: "IMBUE_CLOUD",
    ai_providers: ["IMBUE_CLOUD", "SUBSCRIPTION", "API_KEY"],
    selected_ai_provider: "IMBUE_CLOUD",
    docker_runtimes: ["RUNC", "RUNSC"],
    selected_docker_runtime: "RUNC",
    backup_providers: ["IMBUE_CLOUD", "API_KEY", "CONFIGURE_LATER"],
    selected_backup_provider: "IMBUE_CLOUD",
    backup_api_key_env: "",
    accounts: [{ user_id: "u-1", email: "a@example.com" }],
    default_account_id: "u-1",
    anthropic_api_key: "",
    error_message: "",
    region_options_by_launch_mode: { IMBUE_CLOUD: ["us-east", "eu-west"] },
    region_selected_by_launch_mode: { IMBUE_CLOUD: "eu-west" },
    selected_preset: "remote",
    start_advanced: false,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mountFixture(create: CreateFormBootExtras): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ create });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "create-root";
  document.body.appendChild(container);
  mountCreateForm(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.__mindsOpenModal;
  vi.unstubAllGlobals();
  vi.useRealTimers();
  resetHostForTesting();
});

describe("hostNameFormatError", () => {
  it("mirrors the HostName rules with friendly messages", () => {
    expect(hostNameFormatError("")).toBe("");
    expect(hostNameFormatError("my-workspace_2")).toBe("");
    expect(hostNameFormatError("a.b")).toBe("Dots aren't allowed in a name.");
    expect(hostNameFormatError("a b")).toBe("Spaces aren't allowed in a name.");
    expect(hostNameFormatError("a!b")).toBe("Use only letters, numbers, dashes, and underscores.");
    expect(hostNameFormatError("-ab")).toBe("Can't start with a dash or underscore.");
    expect(hostNameFormatError("ab_")).toBe("Can't end with a dash or underscore.");
  });
});

describe("mountCreateForm", () => {
  it("renders the preset cards with the remote preset selected", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ available: true })));
    const container = mountFixture(extras());
    expect(container.querySelector('[data-preset="remote"]')?.getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector('[data-preset="local"]')?.getAttribute("aria-checked")).toBe("false");
    expect(container.querySelector("#advanced-view")).toBeNull();
    expect(container.textContent).toContain("Imbue Cloud");
    expect(container.textContent).toContain("Directly on your computer");
  });

  it("switches presets and fills the advanced selects with the preset defaults", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ available: true })));
    const container = mountFixture(extras());
    (container.querySelector('[data-preset="local"]') as HTMLButtonElement).click();
    m.redraw.sync();
    expect(container.querySelector('[data-preset="local"]')?.getAttribute("aria-checked")).toBe("true");
    (container.querySelector("#toggle-advanced") as HTMLButtonElement).click();
    m.redraw.sync();
    expect((container.querySelector("#launch_mode") as HTMLSelectElement).value).toBe("LIMA");
    expect((container.querySelector("#ai_provider") as HTMLSelectElement).value).toBe("SUBSCRIPTION");
    expect((container.querySelector("#backup_provider") as HTMLSelectElement).value).toBe("CONFIGURE_LATER");
    // LIMA has no region options, so the region row is gone.
    expect(container.querySelector("#region-row")).toBeNull();
  });

  it("submits the form to the create API and navigates to the creating page", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/workspaces") {
        return Promise.resolve(jsonResponse({ operation_id: "creation-123" }, 202));
      }
      return Promise.resolve(jsonResponse({ available: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
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
      const container = mountFixture(extras());
      (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
      await flushAsync();
      const createCall = fetchMock.mock.calls.find((call) => call[0] === "/api/v1/workspaces");
      expect(createCall).toBeDefined();
      const body = JSON.parse((createCall![1] as RequestInit).body as string) as Record<string, unknown>;
      expect(body.launch_mode).toBe("IMBUE_CLOUD");
      expect(body.account_id).toBe("u-1");
      expect(body.color).toBe("#0b292b");
      // The pre-selected region for the provider rides along.
      expect(body.region).toBe("eu-west");
      // Docker-only runtime is omitted for non-Docker providers.
      expect("runtime" in body).toBe(false);
      expect(setHref).toHaveBeenCalledWith("/creating/creation-123");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("shows the API error and rings the named field on a rejected create", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/workspaces") {
        return Promise.resolve(jsonResponse({ error: "That branch does not exist.", field: "branch" }, 400));
      }
      return Promise.resolve(jsonResponse({ available: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras());
    (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#create-error")?.textContent).toBe("That branch does not exist.");
    // The named field's advanced view is revealed and the input ringed.
    expect(container.querySelector("#advanced-view")).not.toBeNull();
    expect(container.querySelector("#branch")?.className).toContain("ring-important");
    // The submit button is usable again.
    expect((container.querySelector("#create-submit") as HTMLButtonElement).disabled).toBe(false);
  });

  it("blocks submit on an invalid name and reveals the inline error", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ available: true }));
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras({ start_advanced: true, host_name: "bad name" }));
    (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#host-name-error")?.textContent).toBe("Spaces aren't allowed in a name.");
    expect(fetchMock.mock.calls.every((call) => call[0] !== "/api/v1/workspaces")).toBe(true);
  });

  it("marks a taken name from the availability check and blocks submit for it", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/desktop/host-name-available")) {
        return Promise.resolve(jsonResponse({ available: false }));
      }
      return Promise.resolve(jsonResponse({}, 500));
    });
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras({ start_advanced: true }));
    const nameInput = container.querySelector("#host_name") as HTMLInputElement;
    nameInput.value = "taken-name";
    nameInput.dispatchEvent(new Event("input"));
    vi.advanceTimersByTime(300);
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#host-name-error")?.textContent).toBe(
      "That name is already taken. Pick a different one.",
    );
    (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    expect(fetchMock.mock.calls.every((call) => call[0] !== "/api/v1/workspaces")).toBe(true);
  });

  it("opens the sign-in modal instead of submitting when signed out on Imbue Cloud", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ available: true })));
    const openRequests: ModalRequest[] = [];
    window.__mindsOpenModal = (request) => openRequests.push(request);
    registerModalHost({ open: (request) => openRequests.push(request), close: () => {}, isOpen: () => false });
    const container = mountFixture(extras({ accounts: [], default_account_id: "" }));
    (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    expect(openRequests).toEqual([{ kind: "signin", returnTo: "", mode: "signup" }]);
  });

  it("shows the account-picker error when signed in but no account picked", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ available: true })));
    const container = mountFixture(extras({ default_account_id: "" }));
    (container.querySelector("#create-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    m.redraw.sync();
    expect(container.querySelector("#account-error")?.textContent).toContain("Pick an account");
    // Picking an account clears it.
    const accountSelect = container.querySelector("#account_id") as HTMLSelectElement;
    accountSelect.value = "u-1";
    accountSelect.dispatchEvent(new Event("change"));
    m.redraw.sync();
    expect(container.querySelector("#account-error")).toBeNull();
  });
});
