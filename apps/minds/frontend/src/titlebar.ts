// Pure titlebar-context classification (port of chrome.js's classifyContent):
// the left cluster's shape is a pure function of the content view's current
// URL. A workspace-scoped screen shows the "/ workspace-name" breadcrumb plus
// the Workspace / Workspace Settings icon-tabs; a non-workspace full page
// shows a "/ page-name" crumb (and, for pages that opted in, the contextual
// back arrow); the home screen shows just the home button.

export interface TitlebarContext {
  kind: "home" | "workspace" | "page" | "welcome";
  agentId?: string;
  activeTab?: "workspace" | "settings" | null;
  pageLabel?: string;
  showBack?: boolean;
}

export function classifyContent(urlString: string): TitlebarContext {
  let parsed: URL;
  try {
    parsed = new URL(urlString, window.location.origin);
  } catch {
    return { kind: "home" };
  }
  const host = parsed.hostname;
  const path = parsed.pathname;
  let match = host.match(/^(agent-[a-f0-9]+)\.localhost$/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: "workspace" };
  match = path.match(/^\/goto\/(agent-[a-f0-9]+)(?:\/|$)/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: "workspace" };
  match = path.match(/^\/workspace\/(agent-[a-f0-9]+)(?:\/|$)/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: "settings" };
  // Sharing is reached from workspace settings, so it gets the back arrow.
  match = path.match(/^\/sharing\/(agent-[a-f0-9]+)(?:\/|$)/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: null, showBack: true };
  match = path.match(/^\/destroying\/(agent-[a-f0-9]+)(?:\/|$)/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: null };
  match = path.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
  if (match !== null) return { kind: "workspace", agentId: match[1], activeTab: null };
  // No back arrow on the create form: the titlebar home button is the escape
  // (back to the workspace list / welcome splash).
  if (path === "/create") return { kind: "page", pageLabel: "New workspace" };
  if (/^\/creating\//.test(path)) return { kind: "page", pageLabel: "New workspace" };
  // Browser-mode full-page fallbacks (Electron shows these as modals).
  if (path === "/settings") return { kind: "page", pageLabel: "Settings", showBack: true };
  if (path === "/accounts") return { kind: "page", pageLabel: "Accounts", showBack: true };
  // No back arrow on the auth pages (browser-mode fallbacks; Electron opens
  // the sign-in modal instead): the titlebar home button is the escape, and
  // the home route bounces back to the splash until an account option is
  // chosen.
  if (/^\/auth(?:\/|$)/.test(path)) return { kind: "page", pageLabel: "Sign in" };
  // The welcome splash is the committed first screen: the user must pick
  // sign up / log in / continue without an account, so the home button is
  // hidden (there is nowhere else to go yet).
  if (path === "/welcome") return { kind: "welcome" };
  if (path === "/help") return { kind: "page", pageLabel: "Get help", showBack: true };
  return { kind: "home" };
}
