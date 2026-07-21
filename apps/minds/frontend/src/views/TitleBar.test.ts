import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import type { Host } from "../host";
import { applyChromeEvent, resetStoreForTesting, setContentUrl, setDisplayedWorkspaceAgentId } from "../store";
import { classifyContent } from "../titlebar";
import { TitleBar } from "./TitleBar";

const AGENT = "agent-" + "b".repeat(32);

afterEach(() => {
  document.body.innerHTML = "";
  resetStoreForTesting();
});

function recordingHost(): { host: Host; calls: string[] } {
  const calls: string[] = [];
  const host: Host = {
    kind: "electron",
    onChromeEvent: () => undefined,
    navigate: (url) => calls.push(`navigate:${url}`),
    goBack: () => calls.push("goBack"),
    openWorkspaceInNewWindow: () => undefined,
    showWorkspaceContextMenu: () => undefined,
    confirmStopMind: () => Promise.resolve(false),
    openModal: (request) =>
      calls.push(
        `openModal:${request.kind}${"isAssistAvailable" in request ? `:assist=${String(request.isAssistAvailable)}` : ""}`,
      ),
    closeModal: () => undefined,
    minimizeWindow: () => calls.push("minimize"),
    maximizeWindow: () => undefined,
    closeWindow: () => undefined,
  };
  return { host, calls };
}

function mountBar(host: Host): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  m.mount(container, {
    view: () =>
      m(TitleBar, { host, isMac: false, mngrForwardOrigin: "https://localhost:8421", onToggleSwitcher: () => undefined }),
  });
  return container;
}

describe("classifyContent", () => {
  it("classifies the app routes like the deleted chrome.js twin", () => {
    expect(classifyContent(`https://localhost:8421/goto/${AGENT}/`)).toEqual({
      kind: "workspace",
      agentId: AGENT,
      activeTab: "workspace",
    });
    expect(classifyContent(`/workspace/${AGENT}/settings`)).toEqual({
      kind: "workspace",
      agentId: AGENT,
      activeTab: "settings",
    });
    expect(classifyContent(`/sharing/${AGENT}`)).toEqual({
      kind: "workspace",
      agentId: AGENT,
      activeTab: null,
      showBack: true,
    });
    expect(classifyContent("/create")).toEqual({ kind: "page", pageLabel: "New workspace" });
    expect(classifyContent("/welcome")).toEqual({ kind: "welcome" });
    expect(classifyContent("/")).toEqual({ kind: "home" });
    expect(classifyContent(`http://${AGENT}.localhost:8421/x`)).toEqual({
      kind: "workspace",
      agentId: AGENT,
      activeTab: "workspace",
    });
  });
});

describe("TitleBar", () => {
  it("shows the workspace crumb with the cached name and the active tab highlighted", () => {
    applyChromeEvent({
      type: "workspaces",
      workspaces: [{ id: AGENT, name: "ws-bravo", accent: "#112233" }],
      destroying_agent_ids: [],
      destroying_status_by_agent_id: {},
      remote_workspace_states: {},
    });
    setContentUrl(`/workspace/${AGENT}/settings`);
    const container = mountBar(recordingHost().host);

    expect((container.querySelector("#ws-crumb") as HTMLElement).hidden).toBe(false);
    expect(container.querySelector("#workspace-switcher-name")?.textContent).toBe("ws-bravo");
    expect(container.querySelector("#ws-tab-settings")?.getAttribute("aria-current")).toBe("page");
    expect(container.querySelector("#ws-tab-workspace")?.getAttribute("aria-current")).toBeNull();
  });

  it("hides the home button on welcome and shows the page crumb on pages", () => {
    setContentUrl("/welcome");
    const container = mountBar(recordingHost().host);
    expect((container.querySelector("#home-btn") as HTMLElement).hidden).toBe(true);

    setContentUrl("/create");
    m.redraw.sync();
    expect((container.querySelector("#home-btn") as HTMLElement).hidden).toBe(false);
    expect(container.querySelector("#page-crumb-name")?.textContent).toBe("New workspace");
  });

  it("caps the requests badge at 99+ and hides it at zero", () => {
    const container = mountBar(recordingHost().host);
    expect(container.querySelector("#requests-badge")).toBeNull();

    applyChromeEvent({ type: "requests", count: 150, request_ids: [], cards: [], auto_open: true });
    m.redraw.sync();

    expect(container.querySelector("#requests-badge")?.textContent).toBe("99+");
  });

  it("gates the help button's assist flag on health + content readiness", () => {
    const { host, calls } = recordingHost();
    setDisplayedWorkspaceAgentId(AGENT);
    const container = mountBar(host);

    (container.querySelector("#help-toggle") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    expect(calls).toContain("openModal:help:assist=true");

    applyChromeEvent({ type: "system_interface_status", agent_id: AGENT, status: "stuck" });
    m.redraw.sync();
    (container.querySelector("#help-toggle") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    expect(calls[calls.length - 1]).toBe("openModal:help:assist=false");
  });

  it("routes the workspace tabs and window controls through the host", () => {
    const { host, calls } = recordingHost();
    setContentUrl(`/workspace/${AGENT}/settings`);
    const container = mountBar(host);

    (container.querySelector("#ws-tab-workspace") as HTMLElement).dispatchEvent(new MouseEvent("click"));
    (container.querySelector("#min-btn") as HTMLElement).dispatchEvent(new MouseEvent("click"));

    expect(calls).toContain(`navigate:https://localhost:8421/goto/${AGENT}/`);
    expect(calls).toContain("minimize");
  });
});
