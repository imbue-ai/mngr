// Titlebar-context classification: which crumb/tab state the current route
// implies. Port of chrome.js's classifyContent, keyed on SPA routes instead
// of content URLs (the swap engine's URL-sniffing is gone; the router is the
// source of truth).

import type { OptionsTab } from "../../models/workspaceOptions";
import { toOptionsTab } from "../../models/workspaceOptions";

export interface TitlebarContext {
  /** `start`: the first-run start flow, whose titlebar holds only the centered
   * mark. `creating`: a create attempt's page, rendered like a workspace
   * context with `workspaceAnyId` holding the attempt id (its list row already
   * carries the name and accent). */
  kind: "home" | "workspace" | "page" | "start" | "creating";
  workspaceAnyId: string | null;
  activeTab: OptionsTab | null;
  pageLabel: string;
}

const HOME_CONTEXT: TitlebarContext = {
  kind: "home",
  workspaceAnyId: null,
  activeTab: null,
  pageLabel: "",
};

function workspaceContext(
  anyId: string,
  activeTab: OptionsTab | null,
): TitlebarContext {
  return {
    kind: "workspace",
    workspaceAnyId: anyId,
    activeTab,
    pageLabel: "",
  };
}

function pageContext(label: string): TitlebarContext {
  return {
    kind: "page",
    workspaceAnyId: null,
    activeTab: null,
    pageLabel: label,
  };
}

const ID_SEGMENT = "((?:agent|host)-[a-f0-9]+)";
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
 * (/workspace/<agent-or-host-id>, no sub-page suffix), else null. */
export function workspaceDisplayIdFromPath(path: string): string | null {
  const match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}$`, "i"));
  return match ? match[1] : null;
}

/** The workspace id when `path` keeps the workspace surface mounted: the bare
 * content surface OR the options overlay rendered on top of it. Sub-pages
 * that replace the surface (/settings, /backups) return null. */
export function workspaceSurfaceIdFromPath(path: string): string | null {
  const match = path.match(
    new RegExp(`^/workspace/${ID_SEGMENT}(/options)?$`, "i"),
  );
  return match ? match[1] : null;
}

/** The workspace id when `path` is any of the workspace's own pages: the
 * content surface, the options overlay, or a sub-page that replaces the
 * surface (/settings, /backups). The route implies the workspace's account. */
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

/** Whether `path` is the options overlay route (share + machine-settings
 * panel floating over the still-mounted workspace surface). */
export function isWorkspaceOverlayPath(path: string): boolean {
  return new RegExp(`^/workspace/${ID_SEGMENT}/options$`, "i").test(path);
}

/** App-level modal routes the Shell floats as a centered overlay over the
 * surface they were opened from (Imbue Studio settings, Accounts, Get help, the
 * request-review popup, and the AI-keys mint dialog)
 * instead of a full breadcrumbed page. The AI-keys mint dialog is
 * workspace-triggered ("Sign in with Imbue" inside a machine) and floats over
 * that machine, mirroring Get help. The request popup ("/inbox") hangs from
 * the titlebar's key tab rather than centering, but is otherwise the same
 * kind of route-driven overlay. (The bell's notification feed is a separate,
 * non-route overlay -- it is not in this Set -- see Shell.ts.) */
const APP_OVERLAY_PATHS = new Set([
  "/settings",
  "/accounts",
  "/help",
  "/inbox",
  "/settings/ai-keys",
]);

export function isAppOverlayPath(path: string): boolean {
  return APP_OVERLAY_PATHS.has(path);
}

/** Whether `path` is one of the raised titlebar strip's own routed surfaces:
 * the docked options panel, the request popup, or Get help. (The bell's feed
 * is the strip's fourth surface but is local state, not a route.) A strip
 * switch REPLACES these routes' history entry rather than stacking on it, so
 * the surface being left is never one Back away under the new one. */
export function isTitlebarPopupRoutePath(path: string): boolean {
  return isWorkspaceOverlayPath(path) || path === "/inbox" || path === "/help";
}

/** The workspace kept mounted behind an app-overlay modal: the ?workspace= that
 * Get help, the request-review popup, the New workspace
 * template flow, and the AI-keys mint dialog forward, so those overlays float
 * over the live workspace they were opened from (kept mounted, no reload). The AI-keys dialog forwards
 * the machine's HOST id (the mint endpoint keys on it); the others forward the
 * agent id. Settings / Accounts are launched from Home and carry none, a popup
 * opened from Home carries none, and a template link with no machine open
 * redirects to the full create form -- so their overlay floats over Home /
 * never renders (returns null). */
const OVERLAY_BEHIND_WORKSPACE_PATHS = new Set([
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
    return workspaceContext(displayId, null);
  }
  // A popout wears its workspace's context (its accent above all); the
  // Shell draws the popout's own bar in place of the titlebar.
  const popout = popoutFromPath(path);
  if (popout !== null) return workspaceContext(popout.workspaceAnyId, null);
  let match = path.match(
    new RegExp(`^/workspace/${ID_SEGMENT}/settings$`, "i"),
  );
  if (match) return workspaceContext(match[1], "settings");
  match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/options$`, "i"));
  if (match) {
    // Same parse WorkspaceOptionsPage.requestedTab uses, so the highlighted
    // titlebar button is always the pane the page renders.
    return workspaceContext(
      match[1],
      toOptionsTab(new URLSearchParams(search).get("tab")),
    );
  }
  match = path.match(new RegExp(`^/workspace/${ID_SEGMENT}/backups$`, "i"));
  if (match) return workspaceContext(match[1], "settings");
  const destroyingId = destroyingWorkspaceIdFromPath(path);
  if (destroyingId !== null) return workspaceContext(destroyingId, null);
  const recoveryId = recoveryWorkspaceIdFromPath(path);
  if (recoveryId !== null) return workspaceContext(recoveryId, null);
  if (path === "/create/template") {
    // Over a machine the template stepper is a modal floating on that
    // machine's surface (its context + accent); with no machine it redirects to
    // the /create form, so it stays a plain New workspace page until that lands.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null
      ? workspaceContext(behind, null)
      : pageContext("New workspace");
  }
  const creatingAttemptId = creatingAttemptIdFromPath(path);
  if (creatingAttemptId !== null) {
    return { kind: "creating", workspaceAnyId: creatingAttemptId, activeTab: null, pageLabel: "" };
  }
  if (path === "/create" || path.startsWith("/creating/")) {
    return pageContext("New workspace");
  }
  if (isAppOverlayPath(path)) {
    // Imbue Studio settings / Accounts / Get help / the request popup / the AI-keys
    // mint dialog float as a centered modal over the surface they were opened
    // from; the titlebar keeps that surface's context (the workspace behind Get
    // help / the popup / AI-keys, else Home) rather than a standalone page.
    const behind = overlayBehindWorkspaceId(path, search);
    return behind !== null ? workspaceContext(behind, null) : HOME_CONTEXT;
  }
  if (path === "/workspaces/destroyed")
    return pageContext("Recently destroyed");
  if (path === "/workspaces/shared") return pageContext("Shared workspaces");
  if (path === "/consent") return pageContext("Consent");
  if (path === "/start") {
    return { kind: "start", workspaceAnyId: null, activeTab: null, pageLabel: "" };
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
