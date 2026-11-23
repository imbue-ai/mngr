// Where Settings is, as its URL says it (specs/studio-top-bar-and-settings,
// 4.7): the route is the single source of truth for the tab, the entry of
// each tab's rail, and the workspace the Workspace & apps tab speaks for.
// Everything that opens Settings on a place, and every legacy URL of the
// docked options panel or the Manage Accounts page, goes through here.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { SETTINGS_PATH } from "../../shell/classify";

export type SettingsTab = "studio" | "accounts" | "workspace";
export const SETTINGS_TABS: readonly SettingsTab[] = ["studio", "accounts", "workspace"];

/** An entry of the Workspace & apps rail for one of the account's own workspaces. */
export type WorkspaceSettingsGroup = "general" | "permissions" | "sharing" | "backup" | "updates" | "suggestions";
export const WORKSPACE_SETTINGS_GROUPS: readonly WorkspaceSettingsGroup[] = [
  "general",
  "permissions",
  "sharing",
  "backup",
  "updates",
  "suggestions",
];

/** The rail's entries that are pages of a workspace's own settings (SettingsGroups.ts draws them), and so the
 * ones a workspace may ask for by name (the embed contract's `minds:open-workspace-settings`). Permissions and
 * Sharing are opened by messages of their own. */
export const WORKSPACE_OWN_SETTINGS_GROUPS = [
  "general",
  "backup",
  "updates",
  "suggestions",
] as const satisfies readonly WorkspaceSettingsGroup[];
export type WorkspaceOwnSettingsGroup = (typeof WORKSPACE_OWN_SETTINGS_GROUPS)[number];

/** The page a name spells, or null for a name that is no page of a workspace's own settings. */
export function findWorkspaceOwnSettingsGroup(raw: unknown): WorkspaceOwnSettingsGroup | null {
  return WORKSPACE_OWN_SETTINGS_GROUPS.find((group) => group === raw) ?? null;
}

/** The General page's section for the workspace's account. */
export const OWNER_SECTION_ID = "account-section";

/** What the URL says about where Settings is; null for whatever it leaves out. */
export interface SettingsPlace {
  tab: SettingsTab | null;
  /** Studio's section. */
  section: string | null;
  /** The account whose entry Accounts shows. */
  settingsAccount: string | null;
  /** The row the Workspace & apps tab speaks for, by its id in the workspace list. */
  settingsWorkspace: string | null;
  /** For a picked app share, the app the About page is about. */
  app: string | null;
  group: WorkspaceSettingsGroup | null;
  permSection: string | null;
  shareTarget: string | null;
  /** The control of Sharing the arrival asked a bubble for, as the URL spells it. One request, not a place:
   * Settings takes it out of the URL as it hands it to the page. */
  shareCoachMark: string | null;
  /** The workspace Settings floats over. */
  behindWorkspace: string | null;
  /** Whether the arrival asked for the workspace's account: General scrolls its Owner section into view. */
  isOwnerSectionAsked: boolean;
}

function nonEmpty(value: string | null): string | null {
  return value !== null && value !== "" ? value : null;
}

export function parseSettingsPlace(search: string): SettingsPlace {
  const params = new URLSearchParams(search);
  const tab = params.get("tab");
  const group = params.get("group");
  return {
    tab: SETTINGS_TABS.find((candidate) => candidate === tab) ?? null,
    section: nonEmpty(params.get("section")),
    settingsAccount: nonEmpty(params.get("settings_account")),
    settingsWorkspace: nonEmpty(params.get("settings_workspace")),
    app: nonEmpty(params.get("app")),
    group: WORKSPACE_SETTINGS_GROUPS.find((candidate) => candidate === group) ?? null,
    permSection: nonEmpty(params.get("perm_section")),
    shareTarget: nonEmpty(params.get("share_target")),
    shareCoachMark: nonEmpty(params.get("coach")),
    behindWorkspace: nonEmpty(params.get("workspace")),
    isOwnerSectionAsked: params.get("focus") === "owner",
  };
}

/** Whether Settings is the live route, rather than sitting hidden underneath the request popup opened from it. */
function isSettingsLiveRoute(): boolean {
  return (m.route.get() ?? "").split("?")[0] === SETTINGS_PATH;
}

/** The route Settings reads its place from. Normally the live one -- but while the request popup floats over
 * Settings, the live route is the popup's and carries none of Settings' params, so Settings reads the route it
 * was opened on and stays mounted underneath unchanged. */
export function settingsRoute(): string {
  const live = m.route.get() ?? "";
  if (isSettingsLiveRoute()) return live;
  return getAppContext().shell.settingsRouteBehindOverlay ?? live;
}

export function currentSettingsPlace(): SettingsPlace {
  return parseSettingsPlace(settingsRoute().split("?")[1] ?? "");
}

/**
 * Keep the URL pointing at what is on screen (replace, no history entry). A null value drops its param.
 *
 * Written against the same route the place is read from: a change can land while the request popup floats over
 * Settings (a connector sign-in resolving long after it was started), and writing that to the live route would
 * move the POPUP, leaving Settings to come back on its stale place. Every param in one write: two writes in a row
 * would each be computed from the route as it was, since m.route.set does not land synchronously.
 */
export function rememberSettingsPlace(changes: Record<string, string | null>): void {
  const [path, query = ""] = settingsRoute().split("?");
  const params = new URLSearchParams(query);
  let isChanged = false;
  for (const [param, value] of Object.entries(changes)) {
    if ((params.get(param) ?? null) === value) continue;
    isChanged = true;
    if (value === null) params.delete(param);
    else params.set(param, value);
  }
  if (!isChanged) return;
  const next = `${path}?${params.toString()}`;
  if (!isSettingsLiveRoute()) {
    getAppContext().shell.settingsRouteBehindOverlay = next;
    return;
  }
  m.route.set(next, undefined, { replace: true });
}

/** The Settings query a legacy options-panel URL (/workspace/<id>/options?tab=&group=&section=&target=) lands on.
 *
 * CLEANUP: remove this mapping, and the three redirects in router.ts that use it, once no supported Studio build,
 * saved window session, or workspace template can still produce an options-panel or /accounts URL. */
export function settingsQueryForLegacyOptions(agentId: string, legacySearch: string): Record<string, string> {
  const legacy = new URLSearchParams(legacySearch);
  const query: Record<string, string> = { workspace: agentId, tab: "workspace" };
  const legacyTab = legacy.get("tab");
  if (legacyTab === "permissions") {
    query.group = "permissions";
    const section = nonEmpty(legacy.get("section"));
    if (section !== null) query.perm_section = section;
    return query;
  }
  if (legacyTab === "settings") {
    const legacyGroup = legacy.get("group");
    if (legacyGroup === "backup" || legacyGroup === "updates") query.group = legacyGroup;
    else query.group = "general";
    // The legacy Account group is General's Owner section.
    if (legacyGroup === "account") query.focus = "owner";
    return query;
  }
  // The options panel opened on Share when its tab named nothing it knew.
  query.group = "sharing";
  const target = nonEmpty(legacy.get("target"));
  if (target !== null) query.share_target = target;
  return query;
}

/** The Settings query that opens a workspace's General page at its Owner section. */
export function ownerSectionQuery(agentId: string): Record<string, string> {
  return { tab: "workspace", group: "general", settings_workspace: agentId, focus: "owner" };
}
