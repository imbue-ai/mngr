import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import type { ChromeWorkspaceEntry } from "../chrome_state";
import type { Host } from "../host";
import { applyChromeEvent, resetStoreForTesting, setAccentScopeAgentId } from "../store";
import { Badge } from "./Badge";
import { WorkspaceMenu, groupWorkspaces } from "./WorkspaceMenu";
import { WorkspaceRow } from "./WorkspaceRow";

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  resetStoreForTesting();
});

function mountInto(component: m.ComponentTypes): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  m.mount(container, component);
  return container;
}

function entry(overrides: Partial<ChromeWorkspaceEntry> & { id: string }): ChromeWorkspaceEntry {
  return { name: overrides.id, accent: "#112233", ...overrides };
}

function recordingHost(): { host: Host; calls: string[] } {
  const calls: string[] = [];
  const host: Host = {
    kind: "electron",
    onChromeEvent: () => undefined,
    previewWorkspaceAccent: () => undefined,
    navigate: (url) => calls.push(`navigate:${url}`),
    goBack: () => undefined,
    openWorkspaceInNewWindow: (agentId) => calls.push(`openNew:${agentId}`),
    showWorkspaceContextMenu: (agentId, x, y) => calls.push(`context:${agentId}:${x}:${y}`),
    confirmStopMind: () => Promise.resolve(false),
    openModal: () => undefined,
    closeModal: () => calls.push("closeModal"),
    minimizeWindow: () => undefined,
    maximizeWindow: () => undefined,
    closeWindow: () => undefined,
  };
  return { host, calls };
}

describe("groupWorkspaces", () => {
  it("puts the account-less Private group first, then accounts alphabetically", () => {
    const groups = groupWorkspaces([
      entry({ id: "agent-1", account: "zoe@example.com" }),
      entry({ id: "agent-2" }),
      entry({ id: "agent-3", account: "amy@example.com" }),
      entry({ id: "agent-4" }),
    ]);

    expect(groups.map((group) => group.label)).toEqual(["Private", "amy@example.com", "zoe@example.com"]);
    expect(groups[0].workspaces.map((workspace) => workspace.id)).toEqual(["agent-2", "agent-4"]);
  });
});

describe("WorkspaceRow", () => {
  it("renders a plain local row without status icons or action buttons", () => {
    const container = mountInto({
      view: () => m(WorkspaceRow, { workspace: entry({ id: "agent-1", name: "ws-one" }) }),
    });

    const row = container.querySelector(".sidebar-item");
    expect(row?.textContent).toContain("ws-one");
    expect(row?.classList.contains("is-remote")).toBe(false);
    expect(container.querySelector(".sidebar-status-icon")).toBeNull();
    expect(container.querySelector("[data-open-new]")).toBeNull();
  });

  it("marks the current row and never gives it the open-new arrow", () => {
    const container = mountInto({
      view: () =>
        m(WorkspaceRow, { workspace: entry({ id: "agent-1" }), isCurrent: true, withOpenNew: true }),
    });

    const row = container.querySelector(".sidebar-item");
    expect(row?.classList.contains("is-current")).toBe(true);
    expect(container.querySelector("[data-open-new]")).toBeNull();
  });

  it("gives other local rows the open-new arrow when enabled", () => {
    const container = mountInto({
      view: () => m(WorkspaceRow, { workspace: entry({ id: "agent-2" }), withOpenNew: true }),
    });

    expect(container.querySelector("[data-open-new]")).not.toBeNull();
  });

  it("renders remote rows greyed, badge-labeled, without actions, and non-navigable", () => {
    let selectedCount = 0;
    const container = mountInto({
      view: () =>
        m(WorkspaceRow, {
          workspace: entry({ id: "agent-3", is_remote: "true", location: "my-laptop" }),
          withOpenNew: true,
          onSelect: () => {
            selectedCount += 1;
          },
        }),
    });

    const row = container.querySelector(".sidebar-item") as HTMLElement;
    expect(row.classList.contains("is-remote")).toBe(true);
    expect(row.textContent).toContain("on my-laptop");
    expect(container.querySelector("[data-open-new]")).toBeNull();
    row.dispatchEvent(new MouseEvent("click"));
    expect(selectedCount).toBe(0);
  });

  it("shows the stopped status icon and the stale + backup dots", () => {
    const container = mountInto({
      view: () =>
        m(WorkspaceRow, {
          workspace: entry({ id: "agent-4", liveness: "STOPPED", is_stale: "true" }),
          backupWarning: "Backup warning: credentials drifted.",
        }),
    });

    expect(container.querySelector(".sidebar-status-icon")?.getAttribute("title")).toBe("Stopped");
    expect(container.querySelector(".sidebar-stale-dot")).not.toBeNull();
    expect(container.querySelector(".sidebar-backup-dot")?.getAttribute("title")).toBe(
      "Backup warning: credentials drifted.",
    );
  });

  it("shows no status icon for RUNNING rows", () => {
    const container = mountInto({
      view: () => m(WorkspaceRow, { workspace: entry({ id: "agent-5", liveness: "RUNNING" }) }),
    });

    expect(container.querySelector(".sidebar-status-icon")).toBeNull();
  });
});

describe("WorkspaceMenu", () => {
  function seedWorkspaces(workspaces: ChromeWorkspaceEntry[]): void {
    applyChromeEvent({
      type: "workspaces",
      workspaces,
      destroying_agent_ids: [],
      destroying_status_by_agent_id: {},
      remote_workspace_states: {},
    });
  }

  function mountMenu(host: Host, onDismiss: () => void = () => undefined): HTMLElement {
    return mountInto({
      view: () => m(WorkspaceMenu, { host, mngrForwardOrigin: "https://localhost:8421", withOpenNew: true, onDismiss }),
    });
  }

  it("renders grouped rows with headers and the New workspace CTA", () => {
    seedWorkspaces([entry({ id: "agent-1" }), entry({ id: "agent-2", account: "amy@example.com" })]);
    const container = mountMenu(recordingHost().host);

    const headers = Array.from(container.querySelectorAll(".type-section")).map((header) => header.textContent);
    expect(headers).toEqual(["Private", "amy@example.com"]);
    expect(container.querySelectorAll(".sidebar-item")).toHaveLength(2);
    expect(container.querySelector("#sidebar-new-workspace")?.textContent).toContain("New workspace");
  });

  it("renders no group headers for a single private group", () => {
    seedWorkspaces([entry({ id: "agent-1" }), entry({ id: "agent-2" })]);
    const container = mountMenu(recordingHost().host);

    expect(container.querySelectorAll(".type-section")).toHaveLength(0);
  });

  it("navigates to the workspace via the forward origin and dismisses on row click", () => {
    seedWorkspaces([entry({ id: "agent-1" })]);
    const { host, calls } = recordingHost();
    let dismissedCount = 0;
    const container = mountMenu(host, () => {
      dismissedCount += 1;
    });

    (container.querySelector(".sidebar-item") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toEqual(["navigate:https://localhost:8421/goto/agent-1/"]);
    expect(dismissedCount).toBe(1);
  });

  it("highlights the accent-scope workspace as current", () => {
    seedWorkspaces([entry({ id: "agent-1" }), entry({ id: "agent-2" })]);
    setAccentScopeAgentId("agent-2");
    const container = mountMenu(recordingHost().host);

    const currentRows = Array.from(container.querySelectorAll(".sidebar-item.is-current"));
    expect(currentRows.map((row) => row.getAttribute("data-agent-id"))).toEqual(["agent-2"]);
  });

  it("routes open-in-new-window and context-menu through the host", () => {
    seedWorkspaces([entry({ id: "agent-1" }), entry({ id: "agent-2" })]);
    setAccentScopeAgentId("agent-1");
    const { host, calls } = recordingHost();
    const container = mountMenu(host);

    (container.querySelector("[data-open-new]") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    const row = container.querySelector('[data-agent-id="agent-2"]') as HTMLElement;
    row.dispatchEvent(new MouseEvent("contextmenu", { clientX: 11, clientY: 22 }));

    expect(calls).toContain("openNew:agent-2");
    expect(calls).toContain("context:agent-2:11:22");
  });

  it("navigates to /create from the New workspace CTA", () => {
    seedWorkspaces([entry({ id: "agent-1" })]);
    const { host, calls } = recordingHost();
    const container = mountMenu(host);

    (container.querySelector("#sidebar-new-workspace") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toEqual(["navigate:/create"]);
  });
});

describe("mountWorkspaceMenu overlay dismissal", () => {
  it("closes the modal on backdrop clicks but not on clicks inside the panel", async () => {
    const closeCalls: string[] = [];
    window.minds = {
      onChromeEvent: () => undefined,
      previewWorkspaceAccent: () => undefined,
      onAccentChanged: () => undefined,
      onCurrentWorkspaceChanged: () => undefined,
      onContentURLChange: () => undefined,
      navigateContent: () => undefined,
      contentGoBack: () => undefined,
      openWorkspaceInNewWindow: () => undefined,
      showWorkspaceContextMenu: () => undefined,
      confirmStopMind: () => undefined,
      openMindsSettings: () => undefined,
      openAccounts: () => undefined,
      openSigninModal: () => undefined,
      toggleInbox: () => undefined,
      toggleHelp: () => undefined,
      openSharingModal: () => undefined,
      closeModal: () => closeCalls.push("close"),
      minimize: () => undefined,
      maximize: () => undefined,
      close: () => undefined,
    };
    const { mountWorkspaceMenu } = await import("./WorkspaceMenu");
    const { resetHostForTesting } = await import("../host");
    resetHostForTesting();
    const backdrop = document.createElement("div");
    const menu = document.createElement("div");
    menu.id = "sidebar-menu";
    backdrop.appendChild(menu);
    document.body.appendChild(backdrop);

    mountWorkspaceMenu(menu, { isOverlayModal: true });

    menu.querySelector("#sidebar-new-workspace")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(closeCalls).toEqual([]);
    backdrop.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(closeCalls).toEqual(["close"]);

    resetHostForTesting();
    delete window.minds;
  });
});

describe("Badge", () => {
  it("renders a dot without a count and a capped pill with one", () => {
    const container = mountInto({
      view: () => [m(Badge, {}), m(Badge, { count: 3 }), m(Badge, { count: 150 })],
    });

    const spans = Array.from(container.querySelectorAll("span"));
    expect(spans[0].className).toContain("w-2 h-2");
    expect(spans[1].textContent).toBe("3");
    expect(spans[2].textContent).toBe("99+");
  });
});
