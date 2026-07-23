import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HelpBootExtras } from "../chrome_state";
import { resetHostForTesting } from "../host";
import { mountHelpModal } from "./HelpModal";

function extras(overrides: Partial<HelpBootExtras> = {}): HelpBootExtras {
  return {
    include_logs_setting: false,
    workspace_agent_id: "agent-" + "e".repeat(32),
    assist_available: true,
    description: "",
    is_agent_report: false,
    workspace_name: "alpha",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mountFixture(help: HelpBootExtras): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ help });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "help-root";
  document.body.appendChild(container);
  mountHelpModal(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  window.localStorage.clear();
  delete window.minds;
  vi.unstubAllGlobals();
  resetHostForTesting();
});

describe("mountHelpModal", () => {
  it("defaults to agent mode with a reachable workspace and swaps the submit label", () => {
    const container = mountFixture(extras());
    const agentRadio = container.querySelector('input[value="agent"]') as HTMLInputElement;
    expect(agentRadio.checked).toBe(true);
    expect(container.querySelector("#help-submit")?.textContent).toBe("Start agent");
    // Report-only diagnostics are hidden in agent mode.
    expect(container.querySelector("#help-report-options")).toBeNull();

    (container.querySelector('input[value="report"]') as HTMLInputElement).click();
    m.redraw.sync();
    expect(container.querySelector("#help-submit")?.textContent).toBe("Send report");
    expect(container.querySelector("#help-report-options")).not.toBeNull();
  });

  it("defaults to report mode when a description was pre-filled (agent escalation source)", () => {
    const container = mountFixture(extras({ description: "the diagnosis" }));
    expect((container.querySelector('input[value="report"]') as HTMLInputElement).checked).toBe(true);
    expect((container.querySelector("#help-description") as HTMLTextAreaElement).value).toBe("the diagnosis");
  });

  it("hides the mode choice entirely for an agent report and frames the workspace", () => {
    const container = mountFixture(extras({ is_agent_report: true, description: "agent text" }));
    expect(container.querySelector('input[name="help-mode"]')).toBeNull();
    expect(container.textContent).toContain("An agent in workspace alpha wants to submit this report:");
  });

  it("disables agent help away from a reachable workspace", () => {
    const container = mountFixture(extras({ assist_available: false, workspace_agent_id: "" }));
    const agentRadio = container.querySelector('input[value="agent"]') as HTMLInputElement;
    expect(agentRadio.disabled).toBe(true);
    expect(container.textContent).toContain("Open a workspace to use this.");
    expect((container.querySelector('input[value="report"]') as HTMLInputElement).checked).toBe(true);
  });

  it("submits a report with the chosen diagnostics and shows the event id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ event_id: "evt-42" }));
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras({ assist_available: false }));
    (container.querySelector("#help-description") as HTMLTextAreaElement).value = "it broke";
    (container.querySelector("#help-description") as HTMLTextAreaElement).dispatchEvent(new Event("input"));
    (container.querySelector("#help-app-diagnostics") as HTMLInputElement).click();
    m.redraw.sync();
    (container.querySelector("#help-submit") as HTMLButtonElement).click();
    await flushAsync();
    m.redraw.sync();
    expect(fetchMock).toHaveBeenCalledWith("/help/report", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        description: "it broke",
        include_logs: false,
        include_app_diagnostics: true,
        include_workspace_details: false,
        remote_access: false,
        workspace_agent_id: extras().workspace_agent_id,
      }),
    });
    expect(container.querySelector("#help-sent")).not.toBeNull();
    expect(container.querySelector("#help-event-id")?.textContent).toBe("evt-42");
    // The sticky diagnostics choice persisted for the next report.
    expect(window.localStorage.getItem("minds.help.help-app-diagnostics")).toBe("true");
  });

  it("requires a description before submitting", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras({ assist_available: false }));
    (container.querySelector("#help-submit") as HTMLButtonElement).click();
    m.redraw.sync();
    expect(container.querySelector("#help-status")?.textContent).toBe("Please describe the problem first.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("runs the agent-help flow through loading and error back to the report form", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ error: "no assist skill" }, 502));
    vi.stubGlobal("fetch", fetchMock);
    const container = mountFixture(extras());
    (container.querySelector("#help-description") as HTMLTextAreaElement).value = "please help";
    (container.querySelector("#help-description") as HTMLTextAreaElement).dispatchEvent(new Event("input"));
    (container.querySelector("#help-submit") as HTMLButtonElement).click();
    m.redraw.sync();
    // Loading state while the request blocks... then the server's error.
    await flushAsync();
    m.redraw.sync();
    expect(container.querySelector("#help-error-body")?.textContent).toBe("no assist skill");
    (container.querySelector("#help-error-back-btn") as HTMLButtonElement).click();
    m.redraw.sync();
    // Back lands on the form in report mode.
    expect(container.querySelector("#help-form")).not.toBeNull();
    expect((container.querySelector('input[value="report"]') as HTMLInputElement).checked).toBe(true);
  });

  it("closes on success of agent help and on Escape through the host", async () => {
    const closeCalls: number[] = [];
    window.minds = { closeModal: () => closeCalls.push(1) } as unknown as typeof window.minds;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({})));
    const container = mountFixture(extras());
    (container.querySelector("#help-description") as HTMLTextAreaElement).value = "go";
    (container.querySelector("#help-description") as HTMLTextAreaElement).dispatchEvent(new Event("input"));
    (container.querySelector("#help-submit") as HTMLButtonElement).click();
    await flushAsync();
    expect(closeCalls).toHaveLength(1);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(closeCalls).toHaveLength(2);
  });
});
