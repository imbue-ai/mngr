import { describe, expect, it } from "vitest";
import {
  accentSourceForRoute,
  classifyRoute,
  isAppOverlayPath,
  isWorkspaceOverlayPath,
  overlayBehindWorkspaceId,
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
    expect(classifyRoute("/workspace/agent-ab12/settings").activeTab).toBe(
      "settings",
    );
    expect(classifyRoute("/workspace/agent-ab12/options").kind).toBe(
      "workspace",
    );
    expect(classifyRoute("/workspace/agent-ab12/backups").activeTab).toBe(
      "settings",
    );
  });

  it("derives the options tab from ?tab, defaulting to share like the page does", () => {
    expect(classifyRoute("/workspace/agent-ab12/options").activeTab).toBe(
      "share",
    );
    expect(
      classifyRoute("/workspace/agent-ab12/options", "tab=share").activeTab,
    ).toBe("share");
    expect(
      classifyRoute(
        "/workspace/agent-ab12/options",
        "tab=settings&group=backup",
      ).activeTab,
    ).toBe("settings");
    expect(
      classifyRoute("/workspace/agent-ab12/options", "tab=permissions")
        .activeTab,
    ).toBe("permissions");
    expect(
      classifyRoute(
        "/workspace/agent-ab12/options",
        "tab=permissions&section=local-files",
      ).activeTab,
    ).toBe("permissions");
    expect(
      classifyRoute("/workspace/agent-ab12/options", "tab=nonsense").activeTab,
    ).toBe("share");
  });

  it("labels hub pages like the legacy chrome", () => {
    expect(classifyRoute("/create")).toMatchObject({
      kind: "page",
      pageLabel: "New machine",
    });
    expect(classifyRoute("/creating/agent-ff00").kind).toBe("page");
    // Template over a machine is that machine's modal; standalone it is a
    // plain New machine page (until it redirects to the create form).
    expect(classifyRoute("/create/template")).toMatchObject({
      kind: "page",
      pageLabel: "New machine",
    });
    expect(
      classifyRoute("/create/template", "workspace=agent-ab12"),
    ).toMatchObject({
      kind: "workspace",
      workspaceAnyId: "agent-ab12",
    });
    expect(classifyRoute("/workspaces/destroyed").pageLabel).toBe(
      "Recently destroyed",
    );
    expect(classifyRoute("/welcome").kind).toBe("welcome");
    expect(classifyRoute("/").kind).toBe("home");
    expect(classifyRoute("/definitely/unknown").kind).toBe("home");
  });

  it("treats app modals as their opener's context, not a standalone page", () => {
    // Minds settings / Accounts / Get help / inbox opened from Home -> home context.
    expect(classifyRoute("/settings").kind).toBe("home");
    expect(classifyRoute("/accounts").kind).toBe("home");
    expect(classifyRoute("/help").kind).toBe("home");
    expect(classifyRoute("/inbox").kind).toBe("home");
    // Get help / the inbox opened over a workspace keep that workspace's context + accent.
    expect(classifyRoute("/help", "workspace=agent-ab12")).toMatchObject({
      kind: "workspace",
      workspaceAnyId: "agent-ab12",
    });
    expect(classifyRoute("/inbox", "workspace=agent-ab12")).toMatchObject({
      kind: "workspace",
      workspaceAnyId: "agent-ab12",
    });
    expect(accentSourceForRoute("/help", "workspace=agent-ab12")).toBe(
      "agent-ab12",
    );
    expect(accentSourceForRoute("/inbox", "workspace=agent-ab12")).toBe(
      "agent-ab12",
    );
    // The AI-keys mint dialog floats over the machine that opened it (a
    // host-scoped ?workspace), keeping that machine's context + accent; opened
    // without one it floats over Home.
    expect(
      classifyRoute("/settings/ai-keys", "workspace=host-99aa"),
    ).toMatchObject({
      kind: "workspace",
      workspaceAnyId: "host-99aa",
    });
    expect(
      accentSourceForRoute("/settings/ai-keys", "workspace=host-99aa"),
    ).toBe("host-99aa");
    expect(classifyRoute("/settings/ai-keys").kind).toBe("home");
  });

  it("keeps the workspace accent on destroying and recovery routes", () => {
    expect(accentSourceForRoute("/destroying/agent-ab12")).toBe("agent-ab12");
    expect(accentSourceForRoute("/agents/host-cd34/recovery")).toBe(
      "host-cd34",
    );
    expect(accentSourceForRoute("/settings")).toBeNull();
  });
});

describe("app overlay routing", () => {
  it("flags the app modal routes", () => {
    expect(isAppOverlayPath("/settings")).toBe(true);
    expect(isAppOverlayPath("/accounts")).toBe(true);
    expect(isAppOverlayPath("/help")).toBe(true);
    expect(isAppOverlayPath("/inbox")).toBe(true);
    // The notification feed is a state-keyed popover, not a route.
    expect(isAppOverlayPath("/notifications")).toBe(false);
    expect(isAppOverlayPath("/settings/ai-keys")).toBe(true);
    expect(isAppOverlayPath("/settings")).toBe(true);
    expect(isAppOverlayPath("/workspace/agent-ab12")).toBe(false);
  });

  it("reads the workspace behind /help, /inbox, the template modal, and AI-keys from ?workspace only", () => {
    expect(overlayBehindWorkspaceId("/help", "workspace=agent-ab12")).toBe(
      "agent-ab12",
    );
    expect(overlayBehindWorkspaceId("/help", "workspace=host-99aa")).toBe(
      "host-99aa",
    );
    expect(overlayBehindWorkspaceId("/help", "")).toBeNull();
    expect(overlayBehindWorkspaceId("/help", "workspace=not-an-id")).toBeNull();
    // The request popup floats over the workspace it was opened from, or Home.
    expect(overlayBehindWorkspaceId("/inbox", "workspace=agent-ab12")).toBe(
      "agent-ab12",
    );
    expect(overlayBehindWorkspaceId("/inbox", "")).toBeNull();
    // The New machine template stepper floats over the machine it was opened
    // from; with none it redirects to the create form (no behind-workspace).
    expect(
      overlayBehindWorkspaceId("/create/template", "workspace=agent-ab12"),
    ).toBe("agent-ab12");
    expect(overlayBehindWorkspaceId("/create/template", "")).toBeNull();
    // The AI-keys mint dialog floats over the machine that opened it, keyed by
    // that machine's HOST id (the mint endpoint resolves the account from it).
    expect(
      overlayBehindWorkspaceId("/settings/ai-keys", "workspace=host-99aa"),
    ).toBe("host-99aa");
    expect(overlayBehindWorkspaceId("/settings/ai-keys", "")).toBeNull();
    // Settings / Accounts never carry a behind-workspace -> float over Home.
    expect(
      overlayBehindWorkspaceId("/settings", "workspace=agent-ab12"),
    ).toBeNull();
  });
});

describe("workspaceSurfaceIdFromPath", () => {
  it("keeps the surface mounted on the bare route and the options overlay only", () => {
    expect(workspaceSurfaceIdFromPath("/workspace/agent-ab12")).toBe(
      "agent-ab12",
    );
    expect(workspaceSurfaceIdFromPath("/workspace/host-99aa/options")).toBe(
      "host-99aa",
    );
    expect(
      workspaceSurfaceIdFromPath("/workspace/agent-ab12/settings"),
    ).toBeNull();
    expect(
      workspaceSurfaceIdFromPath("/workspace/agent-ab12/backups"),
    ).toBeNull();
    expect(workspaceSurfaceIdFromPath("/")).toBeNull();
  });

  it("keeps the display matcher strict to the bare surface", () => {
    expect(workspaceDisplayIdFromPath("/workspace/agent-ab12")).toBe(
      "agent-ab12",
    );
    expect(
      workspaceDisplayIdFromPath("/workspace/agent-ab12/options"),
    ).toBeNull();
  });

  it("flags only the options route as the workspace overlay", () => {
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12/options")).toBe(true);
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12")).toBe(false);
    expect(isWorkspaceOverlayPath("/workspace/agent-ab12/settings")).toBe(
      false,
    );
  });
});

describe("parseWorkspaceIdFromUrl", () => {
  it("extracts workspace ids (agent- and legacy host-keyed) from origin, goto, and forward-bridge URLs", () => {
    expect(parseWorkspaceIdFromUrl("http://web.host-ab12.localhost:8421/x")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("http://web.agent-cd34.localhost:8421/x")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/goto/host-ab12/")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("/forward-bridge?next=%2Fgoto%2Fhost-ab12%2F")).toBe("host-ab12");
    expect(parseWorkspaceIdFromUrl("/forward-bridge?next=%2Fgoto%2Fagent-cd34%2F")).toBe(
      "agent-cd34",
    );
    expect(parseWorkspaceIdFromUrl("/goto/agent-cd34/")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/workspace/agent-cd34")).toBe("agent-cd34");
    expect(parseWorkspaceIdFromUrl("/workspace/host-ab12")).toBe("host-ab12");
    // Stale pre-SPA persisted window URL shape still resolves on upgrade.
    expect(parseWorkspaceIdFromUrl("/_chrome?agent=agent-cd34")).toBe(
      "agent-cd34",
    );
    expect(parseWorkspaceIdFromUrl("/settings")).toBeNull();
    expect(
      parseWorkspaceIdFromUrl("http://evil.example/gotoevil/host-ab12/"),
    ).toBeNull();
  });
});
