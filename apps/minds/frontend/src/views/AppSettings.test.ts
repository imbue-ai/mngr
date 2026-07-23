import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SettingsBootExtras } from "../chrome_state";
import { resetHostForTesting } from "../host";
import { mountSettingsModal, mountSettingsPage } from "./AppSettings";

function extras(overrides: Partial<SettingsBootExtras> = {}): SettingsBootExtras {
  return {
    report_unexpected_errors: false,
    include_error_logs: false,
    services_overview: [],
    file_sharing_grants: [],
    workspace_delegation_grants: [],
    permissions_unavailable: false,
    is_master_password_set: false,
    is_modal: false,
    ...overrides,
  };
}

const SLACK_OVERVIEW = [
  {
    service_name: "slack",
    display_name: "Slack",
    workspace_grants: [
      {
        workspace_agent_id: "agent-" + "a".repeat(32),
        workspace_name: "My Workspace",
        host_id: "host-1",
        color: "#336699",
        permissions: [{ label: "slack-read-all", description: "Read everything" }],
      },
    ],
  },
];

function mountFixture(settings: SettingsBootExtras, variant: "page" | "modal" = "page"): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ settings });
  document.body.appendChild(island);
  const container = document.createElement("div");
  document.body.appendChild(container);
  if (variant === "page") mountSettingsPage(container);
  else mountSettingsModal(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  window.location.hash = "";
  delete window.minds;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountSettingsPage", () => {
  it("renders the section nav, the granted connector, and the back link", () => {
    const container = mountFixture(extras({ services_overview: SLACK_OVERVIEW }));
    for (const section of ["Connectors", "Local files", "Workspaces", "Error reporting", "Master password"]) {
      expect(container.textContent).toContain(section);
    }
    expect(container.textContent).toContain("Slack");
    expect(container.textContent).toContain("My Workspace");
    expect(container.textContent).toContain("slack-read-all");
    expect(container.textContent).toContain("Back to workspaces");
  });

  it("switches sections from the nav and records the hash", () => {
    const container = mountFixture(extras());
    expect(container.querySelector('[data-settings-panel="connectors"]')).not.toBeNull();
    (container.querySelector('[data-settings-nav="file-sharing"]') as HTMLButtonElement).click();
    m.redraw.sync();
    expect(container.querySelector('[data-settings-panel="connectors"]')).toBeNull();
    expect(container.querySelector('[data-settings-panel="file-sharing"]')).not.toBeNull();
    expect(window.location.hash).toBe("#file-sharing");
  });

  it("restores the active section from the URL hash", () => {
    window.location.hash = "#backups";
    const container = mountFixture(extras());
    expect(container.querySelector('[data-settings-panel="backups"]')).not.toBeNull();
    expect(container.querySelector('[data-settings-panel="connectors"]')).toBeNull();
  });

  it("shows per-category empty states and the unavailable notice", () => {
    const container = mountFixture(extras());
    expect(container.textContent).toContain("No connectors have been added yet.");

    window.dispatchEvent(new Event("minds:page-teardown"));
    document.body.innerHTML = "";
    const unavailable = mountFixture(extras({ permissions_unavailable: true }));
    expect(unavailable.textContent).toContain("Connectors can't be loaded right now.");
  });

  it("persists the error-reporting toggles and clears logs when reporting turns off", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    window.location.hash = "#error-reporting";
    const container = mountFixture(extras({ report_unexpected_errors: true, include_error_logs: true }));
    // With reporting on, the include-logs row is revealed.
    const logsToggle = container.querySelector("#include-logs-toggle") as HTMLInputElement;
    expect(logsToggle.checked).toBe(true);

    const reportToggle = container.querySelector("#report-errors-toggle") as HTMLInputElement;
    reportToggle.checked = false;
    reportToggle.dispatchEvent(new Event("change"));
    m.redraw.sync();
    await flushAsync();
    // Turning reporting off hides the logs row and persists both flags off.
    expect(container.querySelector("#include-logs-row")).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith("/_chrome/error-reporting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_unexpected_errors: false, include_logs: false }),
    });
  });

  it("confirms a card revoke through the dialog and POSTs the revoke", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const reload = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { hash: "", reload },
    });
    try {
      const container = mountFixture(extras({ services_overview: SLACK_OVERVIEW }));
      const cardRevoke = Array.from(container.querySelectorAll("button")).find(
        (button) => button.textContent === "Revoke",
      ) as HTMLButtonElement;
      cardRevoke.click();
      m.redraw.sync();
      expect(container.textContent).toContain("Revoke Slack access?");
      expect(container.textContent).toContain("My Workspace's Slack permissions");
      (container.querySelector("#revoke-confirm-btn") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith("/settings/permissions/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_agent_id: "agent-" + "a".repeat(32),
          service_name: "slack",
        }),
      });
      expect(reload).toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("surfaces a failed revoke in the dialog instead of reloading", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 502 })));
    const container = mountFixture(extras({ services_overview: SLACK_OVERVIEW }));
    (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Revoke all") as HTMLButtonElement).click();
    m.redraw.sync();
    (container.querySelector("#revoke-confirm-btn") as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#revoke-error")?.textContent).toBe("Could not revoke (HTTP 502)");
  });

  it("validates and submits the master-password change", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ ok: true, results: [{ account: "a@example.com", is_ok: true }] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.location.hash = "#backups";
    const container = mountFixture(extras());
    const newInput = container.querySelector("#backup-new-password") as HTMLInputElement;
    const confirmInput = container.querySelector("#backup-new-password-confirm") as HTMLInputElement;

    // Mismatched passwords never reach the server.
    newInput.value = "one";
    newInput.dispatchEvent(new Event("input"));
    confirmInput.value = "two";
    confirmInput.dispatchEvent(new Event("input"));
    (container.querySelector("#backup-change-password-btn") as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#backup-change-error")?.textContent).toBe("The two passwords do not match.");
    expect(fetchMock).not.toHaveBeenCalled();

    confirmInput.value = "one";
    confirmInput.dispatchEvent(new Event("input"));
    (container.querySelector("#backup-change-password-btn") as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(fetchMock).toHaveBeenCalledWith("/_chrome/backup-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: "one", new_password_confirm: "one" }),
    });
    const results = container.querySelector("#backup-change-results");
    expect(results?.textContent).toContain("a@example.com: updated");
    expect(results?.textContent).toContain("Master password updated for every account.");
    // The inputs clear after a successful change.
    expect((container.querySelector("#backup-new-password") as HTMLInputElement).value).toBe("");
  });
});

describe("mountSettingsModal", () => {
  it("renders the card without a back link and dismisses through the host", () => {
    const closeModal = vi.fn();
    window.minds = { closeModal } as unknown as typeof window.minds;
    const container = mountFixture(extras({ is_modal: true }), "modal");
    expect(container.querySelector("#settings-modal-backdrop")).not.toBeNull();
    expect(container.textContent).not.toContain("Back to workspaces");
    (container.querySelector('[aria-label="Close"]') as HTMLButtonElement).click();
    expect(closeModal).toHaveBeenCalled();
  });
});
