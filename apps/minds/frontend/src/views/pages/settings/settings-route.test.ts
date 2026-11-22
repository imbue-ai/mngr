import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import { ShellState } from "../../shell/shell-state";
import {
  OWNER_SECTION_ID,
  SETTINGS_TABS,
  WORKSPACE_SETTINGS_GROUPS,
  currentSettingsPlace,
  ownerSectionQuery,
  parseSettingsPlace,
  rememberSettingsPlace,
  settingsQueryForLegacyOptions,
  settingsRoute,
} from "./settings-route";
import type { SettingsPlace } from "./settings-route";

const AGENT = "agent-ab12";

/** What a URL that says nothing parses to. */
const NOWHERE: SettingsPlace = {
  tab: null,
  section: null,
  settingsAccount: null,
  settingsWorkspace: null,
  app: null,
  group: null,
  permSection: null,
  shareTarget: null,
  behindWorkspace: null,
  isOwnerSectionAsked: false,
};

afterEach(() => {
  clearAppContextForTests();
  vi.restoreAllMocks();
});

/** A window whose live route is `route`, with the shell the helpers read the remembered Settings from. */
function windowOn(route: string): { shell: ShellState; routeSet: ReturnType<typeof vi.spyOn> } {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  vi.spyOn(m.route, "get").mockReturnValue(route);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  return { shell, routeSet };
}

/** The route a write asked for, as its path and its query, whatever order the query was written in. */
function writtenRoute(route: unknown): { path: string; query: Record<string, string> } {
  const [path, query = ""] = String(route).split("?");
  return { path, query: Object.fromEntries(new URLSearchParams(query)) };
}

describe("parseSettingsPlace", () => {
  it("reads nothing from an empty query", () => {
    expect(parseSettingsPlace("")).toEqual(NOWHERE);
  });

  it("reads every param the URL contract names", () => {
    expect(
      parseSettingsPlace(
        "tab=workspace&section=display&settings_account=user-1&settings_workspace=agent-cd34&app=web" +
          `&group=sharing&perm_section=waiting&share_target=terminal&workspace=${AGENT}&focus=owner`,
      ),
    ).toEqual({
      tab: "workspace",
      section: "display",
      settingsAccount: "user-1",
      settingsWorkspace: "agent-cd34",
      app: "web",
      group: "sharing",
      permSection: "waiting",
      shareTarget: "terminal",
      behindWorkspace: AGENT,
      isOwnerSectionAsked: true,
    });
  });

  it("accepts each of the three tabs and each of the five groups, and no others", () => {
    expect(SETTINGS_TABS).toEqual(["studio", "accounts", "workspace"]);
    for (const tab of SETTINGS_TABS) expect(parseSettingsPlace(`tab=${tab}`).tab).toBe(tab);
    expect(WORKSPACE_SETTINGS_GROUPS).toEqual(["general", "permissions", "sharing", "backup", "updates"]);
    for (const group of WORKSPACE_SETTINGS_GROUPS) expect(parseSettingsPlace(`group=${group}`).group).toBe(group);
  });

  it("reads a tab it does not know as no tab, leaving the default to the page", () => {
    // The legacy options panel's tab names are not Settings' own.
    for (const tab of ["settings", "permissions", "share", "Studio", "nonsense", ""]) {
      expect(parseSettingsPlace(`tab=${tab}`).tab, tab).toBeNull();
    }
  });

  it("reads a group it does not know as no group, the legacy account group included", () => {
    for (const group of ["account", "share", "Backup", "nonsense", ""]) {
      expect(parseSettingsPlace(`group=${group}`).group, group).toBeNull();
    }
  });

  it("reads an empty value as an absent one", () => {
    expect(
      parseSettingsPlace(
        "tab=&section=&settings_account=&settings_workspace=&app=&group=&perm_section=&share_target=&workspace=&focus=",
      ),
    ).toEqual(NOWHERE);
  });

  it("asks for the Owner section only on focus=owner", () => {
    expect(parseSettingsPlace("focus=owner").isOwnerSectionAsked).toBe(true);
    expect(parseSettingsPlace("focus=account").isOwnerSectionAsked).toBe(false);
    expect(parseSettingsPlace("group=general").isOwnerSectionAsked).toBe(false);
  });

  it("keeps the overlay's own params out of Settings' place: the account is the window's, not the Accounts entry", () => {
    const place = parseSettingsPlace(`account=user-1&workspace=${AGENT}`);
    expect(place.settingsAccount).toBeNull();
    expect(place.settingsWorkspace).toBeNull();
    expect(place.behindWorkspace).toBe(AGENT);
  });

  it("decodes values as a query carries them", () => {
    expect(parseSettingsPlace("share_target=my%20app&perm_section=local-files").shareTarget).toBe("my app");
  });
});

describe("settingsQueryForLegacyOptions", () => {
  const ON_WORKSPACE = { workspace: AGENT, tab: "workspace" };

  it("lands the settings tab's groups on General, Backup and Updates", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=general")).toEqual({
      ...ON_WORKSPACE,
      group: "general",
    });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=backup")).toEqual({
      ...ON_WORKSPACE,
      group: "backup",
    });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=updates")).toEqual({
      ...ON_WORKSPACE,
      group: "updates",
    });
  });

  it("lands the settings tab on General when it names no group, or one that is gone", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings")).toEqual({ ...ON_WORKSPACE, group: "general" });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=nonsense")).toEqual({
      ...ON_WORKSPACE,
      group: "general",
    });
    // Permissions and Sharing were tabs of their own, never groups of the settings tab.
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=permissions")).toEqual({
      ...ON_WORKSPACE,
      group: "general",
    });
  });

  it("lands the Account group on General, asking for its Owner section", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=account")).toEqual({
      ...ON_WORKSPACE,
      group: "general",
      focus: "owner",
    });
  });

  it("asks for the Owner section for the Account group alone", () => {
    for (const group of ["general", "backup", "updates"]) {
      expect(settingsQueryForLegacyOptions(AGENT, `tab=settings&group=${group}`), group).not.toHaveProperty("focus");
    }
  });

  it("lands the permissions tab on Permissions, carrying its section as perm_section", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=permissions&section=waiting")).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
      perm_section: "waiting",
    });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=permissions&section=local-files")).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
      perm_section: "local-files",
    });
  });

  it("lands the permissions tab on Permissions alone when it names no section", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=permissions")).toEqual({ ...ON_WORKSPACE, group: "permissions" });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=permissions&section=")).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
    });
  });

  it("lands the share tab on Sharing, carrying its target as share_target", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=share&target=web")).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
      share_target: "web",
    });
  });

  it("lands the share tab on the whole workspace's Sharing when it names no target", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=share")).toEqual({ ...ON_WORKSPACE, group: "sharing" });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=share&target=")).toEqual({ ...ON_WORKSPACE, group: "sharing" });
  });

  it("falls back to Sharing for no tab or one it does not know, as the options panel opened on Share", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "")).toEqual({ ...ON_WORKSPACE, group: "sharing" });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=nonsense")).toEqual({ ...ON_WORKSPACE, group: "sharing" });
    // A target still rides along: the panel's default tab was the one that took it.
    expect(settingsQueryForLegacyOptions(AGENT, "target=web")).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
      share_target: "web",
    });
  });

  it("keeps each tab's param to that tab: a section is not a share target, nor a target a section", () => {
    expect(settingsQueryForLegacyOptions(AGENT, "tab=share&section=waiting&group=backup")).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
    });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=permissions&target=web&group=backup")).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
    });
    expect(settingsQueryForLegacyOptions(AGENT, "tab=settings&group=backup&section=waiting&target=web")).toEqual({
      ...ON_WORKSPACE,
      group: "backup",
    });
  });

  it("floats Settings over the workspace the legacy URL named, by the id it was named with", () => {
    expect(settingsQueryForLegacyOptions("host-99aa", "tab=share").workspace).toBe("host-99aa");
  });

  it("lands on a place Settings parses back as the same place", () => {
    const query = settingsQueryForLegacyOptions(AGENT, "tab=settings&group=account");
    expect(parseSettingsPlace(new URLSearchParams(query).toString())).toEqual({
      ...NOWHERE,
      tab: "workspace",
      group: "general",
      behindWorkspace: AGENT,
      isOwnerSectionAsked: true,
    });
  });
});

describe("ownerSectionQuery", () => {
  it("opens the workspace's General page at its Owner section", () => {
    expect(ownerSectionQuery(AGENT)).toEqual({
      tab: "workspace",
      group: "general",
      settings_workspace: AGENT,
      focus: "owner",
    });
  });

  it("parses back as the Owner section of that picked workspace", () => {
    const place = parseSettingsPlace(new URLSearchParams(ownerSectionQuery(AGENT)).toString());
    expect(place).toEqual({
      ...NOWHERE,
      tab: "workspace",
      group: "general",
      settingsWorkspace: AGENT,
      isOwnerSectionAsked: true,
    });
  });

  it("keeps the id the General page's Owner section has always carried", () => {
    expect(OWNER_SECTION_ID).toBe("account-section");
  });
});

describe("Settings' place while it is the live route", () => {
  const LIVE = `/settings?tab=workspace&group=permissions&perm_section=waiting&workspace=${AGENT}&account=local`;

  it("reads its place from the live route", () => {
    windowOn(LIVE);

    expect(settingsRoute()).toBe(LIVE);
    expect(currentSettingsPlace()).toEqual({
      ...NOWHERE,
      tab: "workspace",
      group: "permissions",
      permSection: "waiting",
      behindWorkspace: AGENT,
    });
  });

  it("reads the live route even when the shell still remembers an older Settings", () => {
    // The remembered route outlives the popup by a beat (it is dropped when
    // the return to Settings lands); the live one is where Settings is now.
    const { shell } = windowOn(LIVE);
    shell.settingsRouteBehindOverlay = "/settings?tab=studio&account=local";

    expect(settingsRoute()).toBe(LIVE);
    expect(currentSettingsPlace().tab).toBe("workspace");
  });

  it("writes a change to the live route by replacing its entry, keeping every other param", () => {
    const { shell, routeSet } = windowOn(LIVE);

    rememberSettingsPlace({ perm_section: "add-connection" });

    expect(routeSet).toHaveBeenCalledTimes(1);
    const [route, params, options] = routeSet.mock.calls[0];
    expect(writtenRoute(route)).toEqual({
      path: "/settings",
      query: {
        tab: "workspace",
        group: "permissions",
        perm_section: "add-connection",
        workspace: AGENT,
        account: "local",
      },
    });
    expect(params).toBeUndefined();
    // Moving around inside Settings adds no history entries.
    expect(options).toEqual({ replace: true });
    expect(shell.settingsRouteBehindOverlay).toBeNull();
  });

  it("writes several params in one navigation, dropping the ones set to null", () => {
    const { routeSet } = windowOn(LIVE);

    rememberSettingsPlace({ group: "sharing", perm_section: null, share_target: "web" });

    expect(routeSet).toHaveBeenCalledTimes(1);
    expect(writtenRoute(routeSet.mock.calls[0][0]).query).toEqual({
      tab: "workspace",
      group: "sharing",
      share_target: "web",
      workspace: AGENT,
      account: "local",
    });
  });

  it("navigates nowhere for a change that changes nothing", () => {
    const { routeSet } = windowOn(LIVE);

    rememberSettingsPlace({ group: "permissions", perm_section: "waiting", share_target: null, focus: null });

    expect(routeSet).not.toHaveBeenCalled();
  });

  it("writes the first param onto a Settings route that has no query yet", () => {
    const { routeSet } = windowOn("/settings");

    rememberSettingsPlace({ tab: "accounts" });

    expect(writtenRoute(routeSet.mock.calls[0][0])).toEqual({ path: "/settings", query: { tab: "accounts" } });
  });
});

describe("Settings' place while it sits behind the request popup", () => {
  const REMEMBERED = `/settings?tab=workspace&group=permissions&perm_section=waiting&workspace=${AGENT}&account=local`;
  const POPUP = `/inbox?selected=evt-a&workspace=${AGENT}&account=local`;

  it("reads its place from the route it was opened on, not from the popup's", () => {
    const { shell } = windowOn(POPUP);
    shell.settingsRouteBehindOverlay = REMEMBERED;

    expect(settingsRoute()).toBe(REMEMBERED);
    expect(currentSettingsPlace()).toEqual({
      ...NOWHERE,
      tab: "workspace",
      group: "permissions",
      permSection: "waiting",
      behindWorkspace: AGENT,
    });
  });

  it("writes a change to the remembered route and leaves the popup's route alone", () => {
    // A change landing while the popup is up (a sign-in resolving late) must
    // not move the POPUP: Settings comes back on the place it was left on.
    const { shell, routeSet } = windowOn(POPUP);
    shell.settingsRouteBehindOverlay = REMEMBERED;

    rememberSettingsPlace({ perm_section: "add-connection" });

    expect(routeSet).not.toHaveBeenCalled();
    expect(writtenRoute(shell.settingsRouteBehindOverlay)).toEqual({
      path: "/settings",
      query: {
        tab: "workspace",
        group: "permissions",
        perm_section: "add-connection",
        workspace: AGENT,
        account: "local",
      },
    });
    // And the place reads back from what was just written.
    expect(currentSettingsPlace().permSection).toBe("add-connection");
  });

  it("leaves the remembered route untouched for a change that changes nothing", () => {
    const { shell, routeSet } = windowOn(POPUP);
    shell.settingsRouteBehindOverlay = REMEMBERED;

    rememberSettingsPlace({ perm_section: "waiting" });

    expect(shell.settingsRouteBehindOverlay).toBe(REMEMBERED);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("returns the popup to the place that was written while it was up", () => {
    const { shell, routeSet } = windowOn(POPUP);
    shell.settingsRouteBehindOverlay = REMEMBERED;
    rememberSettingsPlace({ perm_section: "add-connection" });

    expect(shell.returnToSettingsBehindOverlay()).toBe(true);

    const [route, , options] = routeSet.mock.calls[0];
    expect(writtenRoute(route).query.perm_section).toBe("add-connection");
    expect(options).toEqual({ replace: true });
  });
});
