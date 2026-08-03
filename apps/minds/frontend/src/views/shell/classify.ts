// Titlebar-context classification: which crumb/tab state the current route
// implies. Port of chrome.js's classifyContent, keyed on SPA routes instead
// of content URLs (the swap engine's URL-sniffing is gone; the router is the
// source of truth).

export interface TitlebarContext {
  kind: "home" | "workspace" | "page" | "welcome";
  workspaceAnyId: string | null;
  activeTab: "share" | "settings" | null;
  pageLabel: string;
  isBackShown: boolean;
}

const HOME_CONTEXT: TitlebarContext = {
  kind: "home",
  workspaceAnyId: null,
  activeTab: null,
  pageLabel: "",
  isBackShown: false,
};

function workspaceContext(anyId: string, activeTab: "share" | "settings" | null): TitlebarContext {
  return { kind: "workspace", workspaceAnyId: anyId, activeTab, pageLabel: "", isBackShown: false };
}

function pageContext(label: string, isBackShown: boolean): TitlebarContext {
  return { kind: "page", workspaceAnyId: null, activeTab: null, pageLabel: label, isBackShown };
}

const ID_SEGMENT = "((?:agent|host)-[a-f0-9]+)";

/** The workspace id when `path` is the workspace content surface
 * (/workspace/<agent-or-host-id>, no sub-page suffix), else null. */
export function workspaceDisplayIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}$`, "i"));
  return match ? match[1] : null;
}

/** The workspace id when `path` keeps the workspace surface mounted: the bare
 * content surface OR the options overlay rendered on top of it. Sub-pages
 * that replace the surface (/settings, /backups) return null. */
export function workspaceSurfaceIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}(/options)?$`, "i"));
  return match ? match[1] : null;
}

/** Whether `path` is the options overlay route (share + machine-settings
 * panel floating over the still-mounted workspace surface). */
export function isWorkspaceOverlayPath(path: string): boolean {
  return new RegExp(`^/workspace/${ID_SEGMENT}/options$`, "i").test(path);
}

export function classifyRoute(path: string, search = ""): TitlebarContext {
  const displayId = workspaceDisplayIdFromPath(path);
  if (displayId !== null) {
    return workspaceContext(displayId, null);
  }
  let match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/settings$`, "i"));
  if (match) return workspaceContext(match[1], "settings");
  match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/options$`, "i"));
  if (match) {
    // Mirrors WorkspaceOptionsPage.requestedTab: the share pane is the
    // default; only ?tab=settings selects the settings pane.
    const tab = new URLSearchParams(search).get("tab") === "settings" ? "settings" : "share";
    return workspaceContext(match[1], tab);
  }
  match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/backups$`, "i"));
  if (match) return workspaceContext(match[1], "settings");
  match = path.match(new RegExp(`^/destroying/${ID_SEGMENT}$`, "i"));
  if (match) return workspaceContext(match[1], null);
  match = path.match(new RegExp(`^/agents/${ID_SEGMENT}/recovery$`, "i"));
  if (match) return workspaceContext(match[1], null);
  if (path === "/create" || path === "/create/inspiration" || path.startsWith("/creating/")) {
    return pageContext("New machine", path === "/create");
  }
  if (path === "/settings" || path === "/settings/ai-keys") return pageContext("Settings", true);
  if (path === "/accounts") return pageContext("Accounts", true);
  if (path === "/workspaces/destroyed") return pageContext("Recently destroyed", true);
  if (path === "/inbox") return pageContext("Requests", true);
  if (path === "/help") return pageContext("Get help", true);
  if (path === "/consent") return pageContext("Consent", false);
  if (path === "/welcome") {
    return { kind: "welcome", workspaceAnyId: null, activeTab: null, pageLabel: "", isBackShown: false };
  }
  return HOME_CONTEXT;
}

/** Which workspace's accent (if any) a route belongs to (accent survives on
 * workspace-scoped pages like destroying/recovery, exactly as before). */
export function accentSourceForRoute(path: string): string | null {
  const context = classifyRoute(path);
  return context.kind === "workspace" ? context.workspaceAnyId : null;
}
