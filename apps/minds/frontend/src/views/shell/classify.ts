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

/** App-level modal routes the Shell floats as a centered overlay over the
 * surface they were opened from (Minds settings, Accounts, Get help, the
 * Requests inbox, and the AI-keys mint dialog) instead of a full breadcrumbed
 * page. The AI-keys mint dialog is workspace-triggered ("Sign in with Imbue"
 * inside a machine) and floats over that machine, mirroring Get help. */
const APP_OVERLAY_PATHS = new Set(["/settings", "/accounts", "/help", "/inbox", "/settings/ai-keys"]);

export function isAppOverlayPath(path: string): boolean {
  return APP_OVERLAY_PATHS.has(path);
}

/** The workspace kept mounted behind an app-overlay modal: the ?workspace= that
 * Get help, the Requests inbox, the New machine template flow, and the
 * AI-keys mint dialog forward, so those overlays float over the live workspace
 * they were opened from (kept mounted, no reload). The AI-keys dialog forwards
 * the machine's HOST id (the mint endpoint keys on it); the others forward the
 * agent id. Settings / Accounts are launched from Home and carry none, the
 * inbox opened from Home carries none, and a template link with no machine
 * open redirects to the full create form -- so their overlay floats over Home /
 * never renders (returns null). */
const OVERLAY_BEHIND_WORKSPACE_PATHS = new Set([
  "/help",
  "/inbox",
  "/create/template",
  "/settings/ai-keys",
]);

export function overlayBehindWorkspaceId(path: string, search = ""): string | null {
  if (!OVERLAY_BEHIND_WORKSPACE_PATHS.has(path)) return null;
  const workspace = new URLSearchParams(search).get("workspace");
  return workspace !== null && new RegExp(`^${ID_SEGMENT}$`, "i").test(workspace) ? workspace : null;
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
  if (path === "/create/template") {
    // Over a machine the template stepper is a modal floating on that
    // machine's surface (its context + accent); with no machine it redirects to
    // the /create form, so it stays a plain New machine page until that lands.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null ? workspaceContext(behind, null) : pageContext("New machine", false);
  }
  if (path === "/create" || path.startsWith("/creating/")) {
    return pageContext("New machine", path === "/create");
  }
  if (isAppOverlayPath(path)) {
    // Minds settings / Accounts / Get help / the AI-keys mint dialog float as a
    // centered modal over the surface they were opened from; the titlebar keeps
    // that surface's context (the workspace behind Get help / AI-keys, else
    // Home) rather than a back-button page.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null ? workspaceContext(behind, null) : HOME_CONTEXT;
  }
  if (path === "/workspaces/destroyed") return pageContext("Recently destroyed", true);
  if (path === "/consent") return pageContext("Consent", false);
  if (path === "/welcome") {
    return { kind: "welcome", workspaceAnyId: null, activeTab: null, pageLabel: "", isBackShown: false };
  }
  return HOME_CONTEXT;
}

/** Which workspace's accent (if any) a route belongs to (accent survives on
 * workspace-scoped pages like destroying/recovery, exactly as before). */
export function accentSourceForRoute(path: string, search = ""): string | null {
  const context = classifyRoute(path, search);
  return context.kind === "workspace" ? context.workspaceAnyId : null;
}
