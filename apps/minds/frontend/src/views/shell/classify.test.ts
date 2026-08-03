import { describe, expect, it } from "vitest";
import {
  accentSourceForRoute,
  classifyRoute,
  isWorkspaceOverlayPath,
  workspaceDisplayIdFromPath,
  workspaceSurfaceIdFromPath,
} from "./classify";
import { parseWorkspaceIdFromUrl } from "../../router";

describe("classifyRoute", () => {
  it("classifies the content surface as a workspace context", () => {
    const context = classifyRoute("/workspace/agent-ab12");
    expect(context.kind).toBe("workspace");
    expect(context.workspaceAnyId).toBe("agent-ab12");
    expect(context.activeTab).toBeNull();
    const hostScoped = classifyRoute("/workspace/host-99aa");
    expect(hostScoped.kind).toBe("workspace");
    expect(hostScoped.workspaceAnyId).toBe("host-99aa");
  });

  it("marks workspace-scoped pages with the settings tab", () => {
    expect(classifyRoute("/workspace/agent-ab12/settings").activeTab).toBe("settings");
    expect(classifyRoute("/workspace/agent-ab12/options").kind).toBe("workspace");
    expect(classifyRoute("/workspace/agent-ab12/backups").activeTab).toBe("settings");
  });

  it("derives the options tab from ?tab, defaulting to share like the page does", () => {
    expect(classifyRoute("/workspace/agent-ab12/options").activeTab).toBe("share");
    expect(classifyRoute("/workspace/agent-ab12/options", "tab=share").activeTab).toBe("share");
    expect(classifyRoute("/workspace/agent-ab12/options", "tab=settings&group=backup").activeTab).toBe(
      "settings",
    );
  });

  it("labels hub pages and back visibility like the legacy chrome", () => {
    expect(classifyRoute("/create")).toMatchObject({ kind: "page", pageLabel: "New machine", isBackShown: true });
    expect(classifyRoute("/creating/agent-ff00")).toMatchObject({ kind: "page", isBackShown: false });
    expect(classifyRoute("/settings").pageLabel).toBe("Settings");
    expect(classifyRoute("/accounts").pageLabel).toBe("Accounts");
    expect(classifyRoute("/workspaces/destroyed").pageLabel).toBe("Recently destroyed");
    expect(classifyRoute("/welcome").kind).toBe("welcome");
    expect(classifyRoute("/").kind).toBe("home");
    expect(classifyRoute("/definitely/unknown").kind).toBe("home");
  });

  it("keeps the workspace accent on destroying and recovery routes", () => {
    expect(accentSourceForRoute("/destroying/agent-ab12")).toBe("agent-ab12");
    expect(accentSourceForRoute("/agents/host-cd34/recovery")).toBe("host-cd34");
    expect(accentSourceForRoute("/settings")).toBeNull();
  });
});

describe("workspaceSurfaceIdFromPath", () => {
  it("keeps the surface mounted on the bare route and the options overlay only", () => {
    expect(workspaceSurfaceIdFromPath("/workspace/agent-ab12")).toBe("agent-ab12");
    expect(workspaceSurfaceIdFromPath("/workspace/host-99aa/options")).toBe("host-99aa");
    expect(workspaceSurfaceIdFromPath("/workspace/agent-ab12/settings")).toBeNull();
    expect(workspaceSurfaceIdFromPath("/workspace/agent-ab12/backups")).toBeNull();
    expect(workspaceSurfaceIdFromPath("/")).toBeNull();
  });

  it("keeps the display matcher strict to the bare surface", () => {
    expect(workspaceDisplayIdFromPath("/workspace/agent-ab12")).toBe("agent-ab12");
    expect(workspaceDisplayIdFromPath("/workspace/agent-ab12/options")).toBeNull();
  });

  it("flags only the options route as the workspace overlay", () => {
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12/options")).toBe(true);
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12")).toBe(false);
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12/settings")).toBe(false);
  });
});

describe("parseWorkspaceIdFromUrl", () => {
  it("extracts host ids from origin, goto, and forward-bridge URLs", () => {
    expect(parseWorkspaceIdFromUrl("http://web.host-ab12.localhost:8421/x")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("/goto/host-ab12/")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("/forward-bridge?next=%2Fgoto%2Fhost-ab12%2F")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("/goto/agent-cd34/")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/workspace/agent-cd34")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/workspace/host-ab12")).toBe("host-ab12");
    // Stale pre-SPA persisted window URL shape still resolves on upgrade.
    expect(parseWorkspaceIdFromUrl("/_chrome?agent=agent-cd34")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/settings")).toBeNull();
    expect(parseWorkspaceIdFromUrl("http://evil.example/gotoevil/host-ab12/")).toBeNull();
  });
});
