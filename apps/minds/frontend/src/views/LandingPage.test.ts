import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import type { ChromeWorkspaceEntry, LandingBootExtras } from "../chrome_state";
import type { Host } from "../host";
import { resetLandingServiceForTesting, setFetchForTesting } from "../landing_service";
import { applyChromeEvent, resetStoreForTesting } from "../store";
import { LandingPage } from "./LandingPage";

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  resetStoreForTesting();
  resetLandingServiceForTesting();
});

function extras(overrides: Partial<LandingBootExtras> = {}): LandingBootExtras {
  return {
    mngr_forward_origin: "https://localhost:8421",
    account_email: "alice@example.com",
    extra_account_count: 0,
    locked_account_emails: [],
    is_discovering: false,
    ...overrides,
  };
}

function recordingHost(): { host: Host; calls: string[]; confirmResult: { value: boolean } } {
  const calls: string[] = [];
  const confirmResult = { value: true };
  const host: Host = {
    kind: "browser",
    onChromeEvent: () => undefined,
    navigate: (url) => calls.push(`navigate:${url}`),
    goBack: () => undefined,
    openWorkspaceInNewWindow: (agentId) => calls.push(`openNew:${agentId}`),
    showWorkspaceContextMenu: () => undefined,
    confirmStopMind: (agentId) => {
      calls.push(`confirmStop:${agentId}`);
      return Promise.resolve(confirmResult.value);
    },
    openModal: (request) => calls.push(`openModal:${request.kind}`),
    closeModal: () => undefined,
    minimizeWindow: () => undefined,
    maximizeWindow: () => undefined,
    closeWindow: () => undefined,
  };
  return { host, calls, confirmResult };
}

function seedRows(workspaces: ChromeWorkspaceEntry[], destroying: Record<string, string> = {}): void {
  applyChromeEvent({
    type: "workspaces",
    workspaces,
    destroying_agent_ids: Object.keys(destroying),
    destroying_status_by_agent_id: destroying,
    remote_workspace_states: {},
  });
}

function mountLandingPage(host: Host, pageExtras: LandingBootExtras): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  m.mount(container, { view: () => m(LandingPage, { host, extras: pageExtras }) });
  return container;
}

const AGENT = "agent-" + "a".repeat(32);

function localEntry(overrides: Partial<ChromeWorkspaceEntry> = {}): ChromeWorkspaceEntry {
  return { id: AGENT, name: "ws-one", accent: "#112233", ...overrides };
}

function stubFetchOk(): string[] {
  const requests: string[] = [];
  setFetchForTesting((url, init) => {
    requests.push(`${init?.method ?? "GET"} ${url}`);
    return Promise.resolve(new Response("{}", { status: 200 }));
  });
  return requests;
}

describe("LandingPage row click routing", () => {
  it("navigates a healthy running row to its goto URL", () => {
    stubFetchOk();
    seedRows([localEntry({ supports_shutdown: "true", liveness: "RUNNING" })]);
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras());

    (container.querySelector("[data-agent-id]") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toEqual([`navigate:https://localhost:8421/goto/${AGENT}/`]);
  });

  it("routes a STOPPED mind straight to recovery with intent=restart", () => {
    stubFetchOk();
    seedRows([localEntry({ supports_shutdown: "true", liveness: "STOPPED" })]);
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras());

    (container.querySelector("[data-agent-id]") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    const expected = `/agents/${AGENT}/recovery?return_to=${encodeURIComponent(
      `https://localhost:8421/goto/${AGENT}/`,
    )}&intent=restart`;
    expect(calls).toEqual([`navigate:${expected}`]);
  });

  it("routes a stuck workspace to recovery without the restart intent", () => {
    stubFetchOk();
    seedRows([localEntry()]);
    applyChromeEvent({ type: "system_interface_status", agent_id: AGENT, status: "stuck" });
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras());

    (container.querySelector("[data-agent-id]") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    const expected = `/agents/${AGENT}/recovery?return_to=${encodeURIComponent(
      `https://localhost:8421/goto/${AGENT}/`,
    )}`;
    expect(calls).toEqual([`navigate:${expected}`]);
  });
});

describe("LandingPage start/stop controls", () => {
  it("starts a stopped mind optimistically and posts the start endpoint", () => {
    const requests = stubFetchOk();
    seedRows([localEntry({ supports_shutdown: "true", liveness: "STOPPED" })]);
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    (container.querySelector('[aria-label="Start workspace"]') as HTMLElement).dispatchEvent(
      new MouseEvent("click"),
    );
    m.redraw.sync();

    expect(requests).toContain(`POST /api/v1/workspaces/${AGENT}/start`);
    expect(container.querySelector(".landing-mind-state-badge")?.textContent).toBe("Starting…");
  });

  it("runs the in-page stop flow only when the host confirm resolves true", async () => {
    const requests = stubFetchOk();
    seedRows([localEntry({ supports_shutdown: "true", liveness: "RUNNING" })]);
    const { host, calls, confirmResult } = recordingHost();
    confirmResult.value = false;
    const container = mountLandingPage(host, extras());

    (container.querySelector('[aria-label="Stop workspace"]') as HTMLElement).dispatchEvent(new MouseEvent("click"));
    await Promise.resolve();

    expect(calls).toContain(`confirmStop:${AGENT}`);
    expect(requests.filter((request) => request.includes("/stop"))).toHaveLength(0);

    confirmResult.value = true;
    (container.querySelector('[aria-label="Stop workspace"]') as HTMLElement).dispatchEvent(new MouseEvent("click"));
    await Promise.resolve();
    await Promise.resolve();

    expect(requests).toContain(`POST /api/v1/workspaces/${AGENT}/stop`);
  });

  it("gives non-shutdown-capable rows the restart button routing to recovery", () => {
    stubFetchOk();
    seedRows([localEntry()]);
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras());

    expect(container.querySelector('[aria-label="Start workspace"]')).toBeNull();
    (container.querySelector('[aria-label="Restart workspace"]') as HTMLElement).dispatchEvent(
      new MouseEvent("click"),
    );

    expect(calls[0]).toContain("&intent=restart");
  });
});

describe("LandingPage row variants", () => {
  it("renders destroying rows as links with the running chip", () => {
    stubFetchOk();
    seedRows([localEntry()], { [AGENT]: "running" });
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    const link = container.querySelector(`a[href="/destroying/${AGENT}"]`);
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain("Destroying...");
  });

  it("renders failed destroys with the failure chip", () => {
    stubFetchOk();
    seedRows([localEntry()], { [AGENT]: "failed" });
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    expect(container.textContent).toContain("Destroy failed");
  });

  it("renders remote rows with location + provider chips and no action buttons", () => {
    stubFetchOk();
    seedRows([
      localEntry({
        is_remote: "true",
        location: "my-laptop",
        host_id: "host-1",
      }),
    ]);
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    expect(container.textContent).toContain("on my-laptop");
    expect(container.querySelector('[aria-label="Workspace settings"]')).toBeNull();
    expect(container.querySelector("[data-remove-host-id]")).not.toBeNull();
  });

  it("gives the row action buttons custom tooltips, not native titles", () => {
    // data-tooltip labels render as in-page custom tooltips via
    // tooltip_triggers.js (the content view has no overlay bridge); the
    // aria-label keeps an accessible name on these icon-only buttons.
    stubFetchOk();
    seedRows([localEntry()]);
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    ["Restart workspace", "Open in new window", "Settings"].forEach((label) => {
      expect(container.querySelector(`[data-tooltip="${label}"]`)).not.toBeNull();
      expect(container.querySelector(`[title="${label}"]`)).toBeNull();
    });
  });

  it("shows the provider chip on local rows", () => {
    stubFetchOk();
    seedRows([localEntry({ provider: "Docker" })]);
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras());

    expect(container.querySelector(".landing-provider-badge")?.textContent).toBe("Docker");
  });
});

describe("LandingPage chrome around the rows", () => {
  it("opens the settings and accounts launchers through the host", () => {
    stubFetchOk();
    seedRows([localEntry()]);
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras());

    (container.querySelector("#landing-minds-settings") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    (container.querySelector("#landing-account") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toEqual(["openModal:minds-settings", "openModal:accounts"]);
  });

  it("opens the sign-in modal when signed out", () => {
    stubFetchOk();
    seedRows([localEntry()]);
    const { host, calls } = recordingHost();
    const container = mountLandingPage(host, extras({ account_email: "" }));

    expect(container.querySelector("#landing-account")?.textContent).toContain("Log in");
    (container.querySelector("#landing-account") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toEqual(["openModal:signin"]);
  });

  it("shows the unlock banner and surfaces unlock errors", async () => {
    setFetchForTesting(() =>
      Promise.resolve(new Response(JSON.stringify({ ok: false, error: "wrong password" }), { status: 200 })),
    );
    seedRows([localEntry()]);
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras({ locked_account_emails: ["alice@example.com"] }));

    expect(container.querySelector("#sync-unlock-banner")).not.toBeNull();
    (container.querySelector("#sync-unlock-btn") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    // Flush the fetch -> json -> error promise chain (a macrotask hop clears
    // every queued microtask).
    await new Promise((resolve) => setTimeout(resolve, 0));
    m.redraw.sync();

    expect(container.querySelector("#sync-unlock-error")?.textContent).toBe("wrong password");
  });

  it("shows the discovering state without rows", () => {
    stubFetchOk();
    const { host } = recordingHost();
    const container = mountLandingPage(host, extras({ is_discovering: true }));

    expect(container.textContent).toContain("Discovering agents...");
  });
});
