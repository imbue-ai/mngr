// The SPA route table: real hub paths (mirroring the legacy Flask routes so
// deep links, Electron session restore, and muscle memory keep working) plus
// /workspace/<id>, the workspace content surface. Every route renders inside the
// persistent Shell; a route change updates the titlebar context, accent, and
// the channel's client_state registration.

import m from "mithril";
import { consumeWebLoginParams, webLogin } from "./models/webLogin";
import { Shell } from "./views/shell/Shell";
import {
  isWorkspaceOverlayPath,
  recoveryWorkspaceIdFromPath,
  workspaceDisplayIdFromPath,
  workspaceSurfaceIdFromPath,
} from "./views/shell/classify";
import type { ShellState } from "./views/shell/shell-state";
import { DevStyleguide } from "./views/pages/DevStyleguide";
import { AccountsPage } from "./views/pages/AccountsPage";
import { AiKeysPage } from "./views/pages/AiKeysPage";
import { ConsentPage } from "./views/pages/ConsentPage";
import { CreateTemplatePage } from "./views/pages/CreateTemplatePage";
import { CreatePage } from "./views/pages/CreatePage";
import { CreatingPage } from "./views/pages/CreatingPage";
import { DestroyedWorkspacesPage } from "./views/pages/DestroyedWorkspacesPage";
import { DestroyingPage } from "./views/pages/DestroyingPage";
import { HelpPage } from "./views/pages/HelpPage";
import { InboxPage } from "./views/pages/InboxPage";
import { LandingPage } from "./views/pages/LandingPage";
import { RecoveryPage } from "./views/pages/RecoveryPage";
import { SettingsPage } from "./views/pages/SettingsPage";
import { WelcomePage } from "./views/pages/WelcomePage";
import { WorkspaceBackupsPage } from "./views/pages/WorkspaceBackupsPage";
import { WorkspaceOptionsPage } from "./views/pages/WorkspaceOptionsPage";
import { WorkspaceSettingsRedirect } from "./views/pages/WorkspaceSettingsRedirect";
import { RouteError } from "./views/pages/RouteError";

interface RouteEntry {
  path: string;
  component: m.ComponentTypes;
}

const ROUTE_ENTRIES: RouteEntry[] = [
  { path: "/", component: LandingPage },
  { path: "/create", component: CreatePage },
  { path: "/create/template", component: CreateTemplatePage },
  { path: "/creating/:agentId", component: CreatingPage },
  { path: "/settings", component: SettingsPage },
  { path: "/settings/ai-keys", component: AiKeysPage },
  { path: "/accounts", component: AccountsPage },
  { path: "/workspaces/destroyed", component: DestroyedWorkspacesPage },
  // The workspace content surface (SPA twin of the deleted /_chrome wrapper).
  // Accepts agent- OR host-scoped ids: cold-start restore navigates with a
  // host-scoped id before the snapshot provides the alias mapping.
  { path: "/workspace/:workspaceId", component: LandingPage },
  // Legacy URL: redirects into the options overlay's settings tab.
  {
    path: "/workspace/:agentId/settings",
    component: WorkspaceSettingsRedirect,
  },
  // The options overlay: rendered by the Shell OVER the workspace surface.
  { path: "/workspace/:agentId/options", component: WorkspaceOptionsPage },
  { path: "/workspace/:agentId/backups", component: WorkspaceBackupsPage },
  // The request-review popup: an app-overlay route the Shell floats as a
  // centered card, showing one request at a time and never a list.
  { path: "/inbox", component: InboxPage },
  { path: "/destroying/:agentId", component: DestroyingPage },
  { path: "/agents/:agentId/recovery", component: RecoveryPage },
  { path: "/help", component: HelpPage },
  { path: "/welcome", component: WelcomePage },
  { path: "/consent", component: ConsentPage },
  { path: "/_dev/styleguide", component: DevStyleguide },
];

export function mountRouter(root: Element, shell: ShellState): void {
  const resolvers: m.RouteDefs = {};

  const wrap = (component: m.ComponentTypes): m.RouteResolver => ({
    onmatch: () => component,
    render(vnode) {
      const path = shell.currentRoutePath();
      const search = shell.currentRouteSearch();
      // The surface id (bare workspace OR its options overlay) keeps the
      // WorkspaceFrame mounted across overlay open/close, so opening Share /
      // Settings never tears down and reloads the workspace iframe.
      const workspaceParam = workspaceSurfaceIdFromPath(path);
      shell.handleRouteChanged(path, search);
      // The options panel an app-level modal was opened over: the same page as
      // the routed one on the options route, so the Shell's single slot for it
      // keeps one component instance (and its loaded models) across the open.
      const panelPath = (shell.panelRouteBehindOverlay ?? "").split("?")[0];
      // The hub page an app-level modal was opened over, for the pages that
      // must stay painted behind it rather than be replaced by Home or by the
      // machine ?workspace= names (see ShellState.pageRouteBehindOverlay). It
      // renders in the same slot the routed page holds, so the page keeps one
      // component instance -- and its polling models -- across the open.
      const pagePath = (shell.pageRouteBehindOverlay ?? "").split("?")[0];
      return m(Shell, {
        shell,
        routePath: path,
        workspaceParam,
        content: vnode,
        // The Home surface an app-level modal (settings/accounts/help) floats
        // over when no workspace is behind it; the router owns page identity.
        homeContent: m(LandingPage),
        behindContent:
          recoveryWorkspaceIdFromPath(pagePath) !== null
            ? m(RecoveryPage)
            : null,
        optionsContent: isWorkspaceOverlayPath(panelPath)
          ? m(WorkspaceOptionsPage)
          : null,
      });
    },
  });

  for (const entry of ROUTE_ENTRIES) {
    resolvers[entry.path] = wrap(entry.component);
  }
  // Genuinely unknown in-app paths render the friendly RouteError surface
  // rather than dead-ending (the SPA twin of the legacy 404 page).
  resolvers["/:notFound..."] = wrap(RouteError);

  m.route.prefix = "";
  m.route(root as HTMLElement, "/", resolvers);
}

/** Port of chrome.js's navigateContent for URLs arriving from outside the
 * router (Electron shell-navigate asks, notifications, deeplinks). */
export function navigateExternalUrl(shell: ShellState, url: string): void {
  const workspaceAnyId = parseWorkspaceIdFromUrl(url);
  if (workspaceAnyId !== null) {
    // An OS-notification click deep-links /workspace/<id>?review=<request-id>,
    // and enterWorkspace routes with no query -- so the review param must be
    // forwarded here or the click could never open the request it names (the
    // shell's route-changed handling consumes it).
    shell.enterWorkspace(workspaceAnyId, reviewQueryOf(url));
    return;
  }
  try {
    const parsed = new URL(url, window.location.origin);
    // The Electron shell's auth_required nudge arrives here (not as a page
    // load) when the window is a live SPA: its ``?web-login=1`` must start
    // the sign-in flow now and be stripped from the routed URL, or no modal
    // would open and the leftover param would spuriously restart the flow on
    // the window's next full reload. Mirrors the boot-time consumption in
    // index.ts.
    const webLoginMessage = consumeWebLoginParams(parsed.searchParams);
    if (webLoginMessage !== null) {
      void webLogin.start(webLoginMessage);
    }
    // A template deeplink (minds://create?git_url= -> /create/template?
    // git_url=) that lands while a machine is displayed floats the stepper as a
    // modal over that machine (create new OR add to it), the SPA heir of the
    // legacy /create/template/modal; with no machine it passes through to the
    // full-page stepper. (Electron decided this via mru.currentWorkspaceId; here
    // the displayed machine is the same signal, captured before we navigate.)
    const gitUrl = parsed.searchParams.get("git_url");
    const displayed = shell.displayedWorkspaceAnyId;
    if (
      parsed.pathname === "/create/template" &&
      gitUrl &&
      displayed !== null
    ) {
      const query: Record<string, string> = {
        workspace: shell.stores.workspaces.toAgentScopedId(displayed),
        git_url: gitUrl,
      };
      const branch = parsed.searchParams.get("branch");
      if (branch) query.branch = branch;
      m.route.set("/create/template", query);
      return;
    }
    m.route.set(parsed.pathname + parsed.search);
  } catch {
    m.route.set("/");
  }
}

/** The ``?review=`` param to carry into the workspace route, when `urlString`
 * is a workspace-display deep link that names one; empty otherwise. Scoped to
 * the display shape on purpose: the other URL shapes parseWorkspaceIdFromUrl
 * accepts (workspace origins, /goto bridges) are not SPA routes and a review
 * param on them means nothing here. */
function reviewQueryOf(urlString: string): Record<string, string> {
  try {
    const base =
      typeof window !== "undefined"
        ? window.location.origin
        : "http://localhost";
    const parsed = new URL(urlString, base);
    if (workspaceDisplayIdFromPath(parsed.pathname) === null) return {};
    const review = parsed.searchParams.get("review");
    return review === null || review === "" ? {} : { review };
  } catch {
    return {};
  }
}

export function parseWorkspaceIdFromUrl(urlString: string): string | null {
  if (!urlString) return null;
  try {
    const base =
      typeof window !== "undefined"
        ? window.location.origin
        : "http://localhost";
    const parsed = new URL(urlString, base);
    const hostMatch = parsed.hostname.match(
      /^(?:[a-z0-9_-]+\.)*(host-[a-f0-9]+)\.localhost$/i,
    );
    if (hostMatch) return hostMatch[1];
    const pathMatch = (parsed.pathname + parsed.search).match(
      /^\/(?:goto|forward-bridge)(?:[/?]\S*?)?\/?(host-[a-f0-9]+)(?:\/|$|%2F)/i,
    );
    if (pathMatch) return pathMatch[1];
    const plainGoto = parsed.pathname.match(
      /^\/goto\/((?:agent|host)-[a-f0-9]+)(?:\/|$)/i,
    );
    if (plainGoto) return plainGoto[1];
    const displayMatch = workspaceDisplayIdFromPath(parsed.pathname);
    if (displayMatch !== null) return displayMatch;
    // Stale persisted URLs from pre-SPA sessions still carry the old wrapper
    // shape; accept them so restore never drops a window on upgrade.
    const chromeMatch =
      parsed.pathname === "/_chrome" ? parsed.searchParams.get("agent") : null;
    return chromeMatch;
  } catch {
    return null;
  }
}
