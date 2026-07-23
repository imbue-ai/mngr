import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceSettingsBootExtras } from "../chrome_state";
import { resetHostForTesting } from "../host";
import { mountWorkspaceSettings } from "./WorkspaceSettingsPage";

const AGENT = "agent-" + "b".repeat(32);

function extras(overrides: Partial<WorkspaceSettingsBootExtras> = {}): WorkspaceSettingsBootExtras {
  return {
    agent_id: AGENT,
    ws_name: "alpha",
    current_color: "#336699",
    palette: { teal: "#0b292b", blue: "#336699" },
    is_stale: false,
    is_leased_imbue_cloud: false,
    has_account: true,
    current_account_email: "a@example.com",
    associate_accounts: [],
    servers: ["web", "api"],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

// The initial mount fires the backups fetch; a benign default entry keeps
// unrelated tests quiet.
function stubFetchWithBackups(entry: unknown = { is_configured: false, check_state: "OK" }): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith("/backups")) return Promise.resolve(jsonResponse(entry));
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mountFixture(settings: WorkspaceSettingsBootExtras): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ workspace_settings: settings });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "workspace-settings-root";
  document.body.appendChild(container);
  mountWorkspaceSettings(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

class FakeEventSource {
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  close(): void {}
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
  window.mindsAccent = {
    normalizeHex: (value: string) => (/^#[0-9a-f]{6}$/i.test(value.trim()) ? value.trim().toLowerCase() : null),
  };
});

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  delete window.mindsAccent;
  delete window.minds;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountWorkspaceSettings", () => {
  it("renders every section from the island", async () => {
    stubFetchWithBackups();
    const container = mountFixture(extras());
    await flushAsync();
    m.redraw.sync();
    expect(container.textContent).toContain("alpha");
    expect(container.textContent).toContain(AGENT);
    expect((container.querySelector("#workspace-name-input") as HTMLInputElement).value).toBe("alpha");
    expect(container.querySelectorAll(".color-swatch")).toHaveLength(2);
    expect(container.querySelector('[data-color="#336699"]')?.getAttribute("aria-checked")).toBe("true");
    expect(container.textContent).toContain("Associated with: ");
    expect(container.textContent).toContain("a@example.com");
    // Sharing servers render with modal-or-href manage links.
    expect(container.textContent).toContain("web");
    expect(container.textContent).toContain("api");
    expect(container.textContent).toContain("Destroy workspace");
  });

  it("saves a swatch pick and previews the accent through the host", async () => {
    const fetchMock = stubFetchWithBackups();
    const previewCalls: Array<[string, string]> = [];
    window.minds = {
      previewWorkspaceAccent: (agentId: string, accent: string) => previewCalls.push([agentId, accent]),
    } as unknown as typeof window.minds;
    const container = mountFixture(extras());
    await flushAsync();
    (container.querySelector('[data-color="#0b292b"]') as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(previewCalls).toContainEqual([AGENT, "#0b292b"]);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/workspaces/${AGENT}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ color: "#0b292b" }),
    });
    expect(container.querySelector('[data-color="#0b292b"]')?.getAttribute("aria-checked")).toBe("true");
  });

  it("reverts a failed color save and shows the mapped error", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "PATCH") return Promise.resolve(jsonResponse({ error: "stale_provider" }, 502));
      return Promise.resolve(jsonResponse({ is_configured: false }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const previewCalls: string[] = [];
    window.minds = {
      previewWorkspaceAccent: (_agentId: string, accent: string) => previewCalls.push(accent),
    } as unknown as typeof window.minds;
    const container = mountFixture(extras());
    await flushAsync();
    (container.querySelector('[data-color="#0b292b"]') as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#color-error")?.textContent).toBe(
      "This workspace is currently unreachable; try again later.",
    );
    // The optimistic preview is rolled back to the saved color.
    expect(previewCalls).toEqual(["#0b292b", "#336699"]);
    expect(container.querySelector('[data-color="#336699"]')?.getAttribute("aria-checked")).toBe("true");
  });

  it("disables the rename and picker controls when the workspace is stale", async () => {
    stubFetchWithBackups();
    const container = mountFixture(extras({ is_stale: true }));
    await flushAsync();
    m.redraw.sync();
    expect(container.textContent).toContain("currently unreachable");
    expect((container.querySelector("#workspace-name-input") as HTMLInputElement).disabled).toBe(true);
    expect((container.querySelector("#rename-save-btn") as HTMLButtonElement).disabled).toBe(true);
    for (const swatch of Array.from(container.querySelectorAll(".color-swatch"))) {
      expect((swatch as HTMLButtonElement).disabled).toBe(true);
    }
    expect((container.querySelector("#color-hex-input") as HTMLInputElement).disabled).toBe(true);
  });

  it("marks the hex pill (not a swatch) as selected for a custom color", async () => {
    stubFetchWithBackups();
    const container = mountFixture(extras({ current_color: "#123456" }));
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector('[aria-checked="true"]')).toBeNull();
    expect(container.querySelector("#color-hex-input")?.classList.contains("is-selected")).toBe(true);
    // A palette color, by contrast, selects its swatch and drops the ring.
    window.dispatchEvent(new Event("minds:page-teardown"));
    document.body.innerHTML = "";
    const paletteContainer = mountFixture(extras({ current_color: "#0b292b" }));
    expect(paletteContainer.querySelector('[data-color="#0b292b"]')?.getAttribute("aria-checked")).toBe("true");
    expect(paletteContainer.querySelector("#color-hex-input")?.classList.contains("is-selected")).toBe(false);
  });

  it("shows the associate prompt when unassociated and PATCHes the picked account", async () => {
    const fetchMock = stubFetchWithBackups();
    const reload = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", { configurable: true, value: { reload } });
    try {
      const container = mountFixture(
        extras({
          current_account_email: "",
          associate_accounts: [
            { user_id: "u-1", email: "a@example.com" },
            { user_id: "u-2", email: "b@example.com" },
          ],
        }),
      );
      await flushAsync();
      m.redraw.sync();
      expect(container.textContent).toContain("needs to be associated with an account");
      const select = container.querySelector('select[name="user_id"]') as HTMLSelectElement;
      select.value = "u-2";
      select.dispatchEvent(new Event("change"));
      (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Associate") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith(`/api/v1/workspaces/${AGENT}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: "u-2" }),
      });
      expect(reload).toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("disables account controls on a leased Imbue Cloud host", async () => {
    stubFetchWithBackups();
    const container = mountFixture(extras({ is_leased_imbue_cloud: true }));
    await flushAsync();
    m.redraw.sync();
    expect(container.textContent).toContain("account association is fixed");
    expect((container.querySelector("#disassociate-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("renders backup status, problems, and the verification toggle from the fetch", async () => {
    const fetchMock = stubFetchWithBackups({
      is_configured: true,
      is_verification_enabled: true,
      check_state: "PROBLEMS",
      problems: ["SERVICE_NOT_RUNNING"],
      check_detail: "supervisor reports FATAL",
      installed_version: "1.2.0",
      minimum_version: "1.3.0",
    });
    const container = mountFixture(extras());
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#backup-status-line")?.textContent).toContain("No successful backup yet.");
    expect(container.textContent).toContain("The backup service is not running.");
    expect(container.textContent).toContain("supervisor reports FATAL");
    expect(container.textContent).toContain("Installed backup service: 1.2.0 / minimum required: 1.3.0");
    expect(container.querySelector("#backup-verification-btn")?.textContent).toBe("Disable");
    expect(container.querySelector("#backup-update-btn")).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/workspaces/${AGENT}/backups`);
  });

  it("confirms destroy through the dialog and navigates home", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "POST" && url.endsWith("/destroy")) return Promise.resolve(new Response("", { status: 200 }));
      return Promise.resolve(jsonResponse({ is_configured: false }));
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
      await flushAsync();
      (container.querySelector("#destroy-btn") as HTMLButtonElement).click();
      m.redraw.sync();
      expect(container.textContent).toContain("Destroy workspace?");
      (container.querySelector("#destroy-confirm-btn") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith(`/api/v1/workspaces/${AGENT}/destroy`, { method: "POST" });
      expect(setHref).toHaveBeenCalledWith("/");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });
});
