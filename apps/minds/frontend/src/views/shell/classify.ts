// Titlebar-context classification: which crumb state the current route
// implies. Port of chrome.js's classifyContent, keyed on SPA routes instead
// of content URLs (the swap engine's URL-sniffing is gone; the router is the
// source of truth).

export interface TitlebarContext {
  /** `start`: the first-run start flow, whose titlebar holds only the centered
   * mark. `creating`: a create attempt's page, rendered like a workspace
   * context with `workspaceAnyId` holding the attempt id (its list row already
   * carries the name and accent). */
  kind: "home" | "workspace" | "page" | "start" | "creating";
  workspaceAnyId: string | null;
  pageLabel: string;
}

const HOME_CONTEXT: TitlebarContext = {
  kind: "home",
  workspaceAnyId: null,
  pageLabel: "",
};

function workspaceContext(anyId: string): TitlebarContext {
  return {
    kind: "workspace",
    workspaceAnyId: anyId,
    pageLabel: "",
  };
}

function pageContext(label: string): TitlebarContext {
  return {
    kind: "page",
    workspaceAnyId: null,
    pageLabel: label,
  };
}

// A workspace's agent or host coordinate, or the `shared-<hex>` route alias of
// a pasted shared link (whose share key is a bare digest; see models/sharedWithMe).
const ID_SEGMENT = "((?:agent|host|shared)-[a-f0-9]+)";
// An account id as the hub routes carry it: a SuperTokens user id, or "local".
const ACCOUNT_ID_SEGMENT = "([A-Za-z0-9][A-Za-z0-9_-]{0,63})";
const HUB_PATH_RE = new RegExp(`^/account/${ACCOUNT_ID_SEGMENT}(/.*)?$`);
const ACCOUNT_ID_RE = new RegExp(`^${ACCOUNT_ID_SEGMENT}$`);

/** Whether `value` is an account id as the routes carry one (a hub segment, an overlay's `?account=`). */
export function isAccountId(value: string): boolean {
  return ACCOUNT_ID_RE.test(value);
}

/** The hub pages, by the path they take under `/account/<id>`: the home page is the bare `/`. */
export type HubPage =
  | "/"
  | "/create"
  | "/create/template"
  | "/workspaces/destroyed"
  | "/workspaces/shared";

export const HUB_PAGES: readonly HubPage[] = [
  "/",
  "/create",
  "/create/template",
  "/workspaces/destroyed",
  "/workspaces/shared",
];

/** The account a hub route names (`/account/<id>/...`), or null for any other path. */
export function accountIdFromHubPath(path: string): string | null {
  const match = path.match(HUB_PATH_RE);
  return match ? match[1] : null;
}

/** The hub route of `page` on `accountId`: `/account/<id>/` for the home page, `/account/<id>/create` and so on. */
export function accountHubRoute(
  accountId: string,
  page: HubPage = "/",
): string {
  return `/account/${encodeURIComponent(accountId)}${page === "/" ? "/" : page}`;
}

/** `path` with its `/account/<id>` prefix stripped: the hub page it shows (`/`, `/create`, ...), or the
 * path itself when it carries no account. What the titlebar and the Shell classify on, so a hub page
 * reads the same whichever account it is for. */
export function hubPagePath(path: string): string {
  const match = path.match(HUB_PATH_RE);
  if (!match) return path;
  const rest = match[2] ?? "/";
  return rest === "" || rest === "/" ? "/" : rest.replace(/\/$/, "");
}

/** Whether `path` is a hub page the SPA offers under an account (the bare spellings redirect to one). */
export function isHubPage(path: string): path is HubPage {
  return (HUB_PAGES as readonly string[]).includes(path);
}
const CREATE_ATTEMPT_ID_SEGMENT = "(create-attempt-[a-f0-9]+)";
// A workspace window id as the workspace shell mints it (`win-<hex>`).
const WINDOW_ID_SEGMENT = "(win-[a-f0-9]+)";

/** A pulled-out workspace window's route: the workspace it belongs to and
 * the window shown edge to edge (the pull-out-window spec). */
export interface PopoutRoute {
  workspaceAnyId: string;
  windowId: string;
}

/** The popout a path names (/popout/<workspace-id>/<window-id>), else null. */
export function popoutFromPath(path: string): PopoutRoute | null {
  const match = path.match(
    new RegExp(`^/popout/${ID_SEGMENT}/${WINDOW_ID_SEGMENT}$`, "i"),
  );
  return match ? { workspaceAnyId: match[1], windowId: match[2] } : null;
}

/** The workspace id when `path` is the destroying page (/destroying/<id>), else null. */
export function destroyingWorkspaceIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/destroying/${ID_SEGMENT}$`, "i"));
  return match ? match[1] : null;
}

/** The create attempt id when `path` is the creation page, else null. */
export function creatingAttemptIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/creating/${CREATE_ATTEMPT_ID_SEGMENT}$`, "i"));
  return match ? match[1] : null;
}

/** The workspace id when `path` is the workspace content surface
 * (/workspace/<agent-or-host-id>, no sub-page suffix), else null. A sub-page
 * that replaces the surface (/backups) returns null; a flyout or modal
 * floating over the surface names it in `?workspace=` instead (see
 * overlayBehindWorkspaceId). */
export function workspaceDisplayIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}$`, "i"));
  return match ? match[1] : null;
}

/** The workspace id when `path` is any of the workspace's own pages: the
 * content surface, a sub-page that replaces it (/backups), or one of the
 * legacy spellings that redirect into Settings (/options, /settings). The
 * route implies the workspace's account. */
export function workspacePageIdFromPath(path: string): string | null {
  const match = path.match(
    new RegExp(`^/workspace/${ID_SEGMENT}(/options|/settings|/backups)?$`, "i"),
  );
  return match ? match[1] : null;
}

/** The machine a recovery page (/agents/<id>/recovery) speaks for, else null. */
export function recoveryWorkspaceIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/agents/${ID_SEGMENT}/recovery$`, "i"));
  return match ? match[1] : null;
}

/** App-level routes the Shell floats over the surface they were opened from
 * instead of drawing as a page: Settings (hung from the bar's gear), Get help
 * (hung from the bug button), the request-review popup and the AI-keys mint
 * dialog (centered cards). (The bell's notification feed is a separate,
 * non-route overlay -- it is not in this Set -- see Shell.ts.) */
const APP_OVERLAY_PATHS = new Set([
  "/settings",
  "/help",
  "/inbox",
  "/settings/ai-keys",
]);

/** Settings: the one overlay route with a slot of its own in the Shell, since it stays mounted behind the
 * request popup opened from it. */
export const SETTINGS_PATH = "/settings";

export function isAppOverlayPath(path: string): boolean {
  return APP_OVERLAY_PATHS.has(path);
}

/** Whether `path` is one of the bar's routed flyouts: Settings or Get help.
 * (The feed and the two menus are flyouts too, but local state, not routes.)
 * A switch between flyouts REPLACES these routes' history entry rather than
 * stacking on it, so the flyout being left is never one Back away under the
 * new one. */
export function isTitlebarPopupRoutePath(path: string): boolean {
  return path === SETTINGS_PATH || path === "/help";
}

/** The workspace kept mounted behind a floating route: the ?workspace= that
 * Settings, Get help, the request-review popup, the New workspace template
 * flow, and the AI-keys mint dialog forward, so they float over the live
 * workspace they were opened from (kept mounted, no reload). The AI-keys
 * dialog forwards the machine's HOST id (the mint endpoint keys on it); the
 * others forward the agent id. Opened from Home they carry none, and a
 * template link with no machine open redirects to the full create form -- so
 * their overlay floats over Home / never renders (returns null). */
const OVERLAY_BEHIND_WORKSPACE_PATHS = new Set([
  SETTINGS_PATH,
  "/help",
  "/inbox",
  "/create/template",
  "/settings/ai-keys",
]);

export function overlayBehindWorkspaceId(
  path: string,
  search = "",
): string | null {
  // The template stepper is a hub page, so its route carries the account prefix.
  if (!OVERLAY_BEHIND_WORKSPACE_PATHS.has(hubPagePath(path))) return null;
  const workspace = new URLSearchParams(search).get("workspace");
  return workspace !== null &&
    new RegExp(`^${ID_SEGMENT}$`, "i").test(workspace)
    ? workspace
    : null;
}

export function classifyRoute(routePath: string, search = ""): TitlebarContext {
  // A hub route is classified by the page it shows, whichever account it is for.
  const path = hubPagePath(routePath);
  const displayId = workspaceDisplayIdFromPath(path);
  if (displayId !== null) {
    return workspaceContext(displayId);
  }
  // A popout wears its workspace's context (its accent above all); the
  // Shell draws the popout's own bar in place of the titlebar.
  const popout = popoutFromPath(path);
  if (popout !== null) return workspaceContext(popout.workspaceAnyId);
  const backupsMatch = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/backups$`, "i"));
  if (backupsMatch) return workspaceContext(backupsMatch[1]);
  const destroyingId = destroyingWorkspaceIdFromPath(path);
  if (destroyingId !== null) return workspaceContext(destroyingId);
  const recoveryId = recoveryWorkspaceIdFromPath(path);
  if (recoveryId !== null) return workspaceContext(recoveryId);
  if (path === "/create/template") {
    // Over a machine the template stepper is a modal floating on that
    // machine's surface (its context + accent); with no machine it redirects to
    // the /create form, so it stays a plain New workspace page until that lands.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null ? workspaceContext(behind) : pageContext("New workspace");
  }
  const creatingAttemptId = creatingAttemptIdFromPath(path);
  if (creatingAttemptId !== null) {
    return { kind: "creating", workspaceAnyId: creatingAttemptId, pageLabel: "" };
  }
  if (path === "/create" || path.startsWith("/creating/")) {
    return pageContext("New workspace");
  }
  if (isAppOverlayPath(path)) {
    // Settings, Get help, the request popup and the AI-keys mint dialog float
    // over the surface they were opened from; the titlebar keeps that
    // surface's context (the workspace behind them, else Home) rather than a
    // standalone page.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null ? workspaceContext(behind) : HOME_CONTEXT;
  }
  if (path === "/workspaces/destroyed")
    return pageContext("Recently destroyed");
  if (path === "/workspaces/shared") return pageContext("Shared workspaces");
  if (path === "/consent") return pageContext("Consent");
  if (path === "/start") {
    return { kind: "start", workspaceAnyId: null, pageLabel: "" };
  }
  return HOME_CONTEXT;
}

/** Which workspace's accent (if any) a route belongs to (accent survives on
 * workspace-scoped pages like destroying/recovery, exactly as before). A
 * creation page takes its in-flight attempt's accent. */
export function accentSourceForRoute(path: string, search = ""): string | null {
  const context = classifyRoute(path, search);
  return context.kind === "workspace" || context.kind === "creating" ? context.workspaceAnyId : null;
}
