// Settings: one page, three tabs, and a URL that says where in it you are
// (specs/studio-top-bar-and-settings, 4.7 to 4.10 and 4.12). The pages it
// mounts have suites of their own; these pin which page is mounted, with what,
// and what every choice writes to the URL.

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { UiAccountEntry, UiWorkspaceEntry } from "../../channel/messages";
import type { UiSharedWorkspace } from "../../generated/ui";
import type { AccountPlanView } from "../../models/accountsDetail";
import { clearAccountPlanCacheForTests } from "../../models/accountsDetail";
import { createEmptyStores } from "../../models/boot";
import { getAppQueryClient } from "../../models/queryClient";
import { SETTINGS_SECTIONS } from "../../models/settings";
import type { SharePanelModel } from "../../models/sharePanel";
import { webLogin } from "../../models/webLogin";
import type { WorkspaceOptionsData } from "../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../models/workspaceOptions";
import { PermissionsModel } from "../../models/workspacePermissions";
import { permissionsView } from "../../models/workspacePermissions.testing";
import type { AnyVnode } from "../../testing";
import {
  OUT_OF_DATE_UPDATE,
  SHARED_WORKSPACE_DOMAIN,
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  classTokensOf,
  collectVnodes,
  jsonResponse,
  namedSharedOwner,
  renderDeep,
  required,
  secondAccountEntry,
  settingsOverview,
  settle,
  sharedAppTarget,
  sharedWorkspaceEntry,
  withAttr,
  workspaceOptionsData,
  workspacesMessage,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import type { PickableRow } from "./SettingsPage";
import { SettingsPage, pickableRows, pickedRow } from "./SettingsPage";
import { AccountCard } from "./settings/AccountCard";
import { KeyboardShortcutsSection } from "./settings/KeyboardShortcutsSection";
import { SharedWorkspaceAbout } from "./settings/SharedWorkspaceAbout";
import { PermissionsTab } from "./workspace/PermissionsTab";
import { WorkspaceSettingsGroup } from "./workspace/SettingsGroups";
import { ShareTab } from "./workspace/ShareTab";

const ALICE = accountEntry();
const BOB = secondAccountEntry();

const ALPHA = "agent-aa11";
const ALPHA_HOST = "host-bb22";
const BRAVO = "agent-cc33";

function ownedEntry(overrides: Partial<UiWorkspaceEntry> = {}): UiWorkspaceEntry {
  return { ...workspacesMessage().workspaces[0], ...overrides };
}

/** The account's own running workspace, alpha. */
const ALPHA_ENTRY = ownedEntry();
/** Its second, stopped. */
const BRAVO_ENTRY = ownedEntry({ id: BRAVO, name: "bravo", host_id: "host-dd44", liveness: "STOPPED" });

const SAM = namedSharedOwner();

const ORCHARD_KEY = "agent-" + "1".repeat(32);
const TOOLS_KEY = "agent-" + "2".repeat(32);

/** A whole workspace Sam shared and the account accepted. */
function orchard(shareOverrides: Partial<UiSharedWorkspace> = {}): UiWorkspaceEntry {
  return sharedWorkspaceEntry(
    { share_key: ORCHARD_KEY, host_id: "host-" + "1".repeat(32), status: "accepted", owner: SAM, ...shareOverrides },
    { name: "orchard" },
  );
}

/** Two apps Sam shared from another workspace, accepted. */
function tools(shareOverrides: Partial<UiSharedWorkspace> = {}): UiWorkspaceEntry {
  return sharedWorkspaceEntry(
    {
      share_key: TOOLS_KEY,
      host_id: "host-" + "2".repeat(32),
      status: "accepted",
      owner: SAM,
      targets: [sharedAppTarget("notes", "Notes"), sharedAppTarget("board", "Board")],
      ...shareOverrides,
    },
    { name: "tools" },
  );
}

function optionsData(agentId: string, overrides: Partial<WorkspaceOptionsData> = {}): WorkspaceOptionsData {
  return workspaceOptionsData({
    agent_id: agentId,
    host_id: `host-of-${agentId}`,
    name: agentId === ALPHA ? "alpha" : "bravo",
    app_services: ["web", "docs"],
    system_services: ["terminal"],
    ...overrides,
  });
}

/** The page with its hooks callable directly, since there is no DOM to mount into. */
interface Page {
  oninit: () => void;
  onbeforeupdate: () => void;
  onremove: () => void;
  view: () => AnyVnode[];
}

interface RouteWrite {
  route: string;
  params: unknown;
  options: unknown;
}

interface Harness {
  shell: ShellState;
  page: Page;
  /** The live route, which a route set moves as the router would. */
  route: { current: string };
  routeWrites: RouteWrite[];
  /** Every URL fetched, in order. */
  requests: string[];
  draw: () => AnyVnode[];
}

interface HarnessOptions {
  entries?: UiWorkspaceEntry[];
  accounts?: UiAccountEntry[];
  /** The window's account. */
  accountId?: string;
  /** The desktop bridge; null for a plain browser. */
  native?: Record<string, unknown> | null;
  /** Answer a request the defaults should not; undefined falls through to them. */
  respond?: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined;
}

/** Every page a test opened, so each one's models are let go when the test ends. */
const openPages: Page[] = [];

/** Settings opened on `route`, in a window on Alice's account with alpha and bravo listed unless told otherwise. */
function openSettings(route: string, options: HarnessOptions = {}): Harness {
  const {
    entries = [ALPHA_ENTRY, BRAVO_ENTRY],
    accounts = [ALICE, BOB],
    accountId = ALICE.user_id,
    native = {},
    respond,
  } = options;
  const shell = new ShellState(createEmptyStores());
  shell.accountId = accountId;
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces: entries }));
  shell.stores.accounts.applyAccountsMessage(accountsMessage(accounts, accountId));
  registerAppContext({ stores: shell.stores, shell });

  vi.stubGlobal("window", native === null ? {} : { mindsNative: native });
  const requests: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push(url);
    const answer = respond?.(url, init);
    if (answer !== undefined) return Promise.resolve(answer);
    if (url.startsWith("/ui/api/settings")) return Promise.resolve(jsonResponse(settingsOverview()));
    if (url.startsWith("/ui/api/accounts/"))
      return Promise.resolve(jsonResponse({ plan_view: null, trim_status: null }));
    if (url.startsWith("/api/v1/workspace-sharing/"))
      return Promise.resolve(jsonResponse({ enabled: false, url: null, grants: null }));
    const optionsOf = /^\/ui\/api\/workspaces\/([^/]+)\/options$/.exec(url);
    if (optionsOf !== null) return Promise.resolve(jsonResponse(optionsData(decodeURIComponent(optionsOf[1]))));
    if (url.endsWith("/permissions")) return Promise.resolve(jsonResponse(permissionsView()));
    return Promise.resolve(jsonResponse({}));
  });

  const live = { current: route };
  const routeWrites: RouteWrite[] = [];
  vi.spyOn(m.route, "get").mockImplementation(() => live.current);
  vi.spyOn(m.route, "set").mockImplementation(((next: string, params?: unknown, routeOptions?: unknown) => {
    routeWrites.push({ route: next, params, options: routeOptions });
    live.current = next;
  }) as typeof m.route.set);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);

  const page = SettingsPage() as unknown as Page;
  page.oninit();
  openPages.push(page);
  return { shell, page, route: live, routeWrites, requests, draw: () => page.view() };
}

/** The page once the reads its first draw started have landed. */
async function drawLoaded(harness: Harness): Promise<AnyVnode[]> {
  harness.draw();
  await settle();
  return harness.draw();
}

afterEach(() => {
  // Before the fetch stub goes: the page's models hold timers and reads of their own.
  for (const page of openPages.splice(0)) page.onremove();
  clearAppContextForTests();
  clearAccountPlanCacheForTests();
  // The share panels read through the app's own query client, whose answers would outlive the case.
  getAppQueryClient().clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function queryOf(route: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(route.split("?")[1] ?? ""));
}

function click(vnode: AnyVnode | undefined): void {
  (attrsOf(required(vnode)).onclick as () => void)();
}

function tabs(view: unknown): AnyVnode[] {
  return collectVnodes(view).filter((vnode) => attrsOf(vnode).role === "tab");
}

function selectedTab(view: AnyVnode[]): string {
  const selected = tabs(view).filter((tab) => attrsOf(tab)["aria-selected"] === "true");
  expect(selected).toHaveLength(1);
  return String(attrsOf(selected[0]).id);
}

/** The pane under the tabs: its rail and its content column. */
function pane(view: AnyVnode[]): { row: AnyVnode; rail: AnyVnode; content: AnyVnode } {
  const rail = required(collectVnodes(view).find((vnode) => vnode.tag === "nav"));
  const row = required(
    collectVnodes(view).find((vnode) => Array.isArray(vnode.children) && vnode.children.includes(rail)),
  );
  return { row, rail, content: (row.children as AnyVnode[])[1] };
}

/** The rail's entries carrying `attr`, as [value, whether it is the selected one]. */
function railEntries(view: AnyVnode[], attr: string): [string, boolean][] {
  return withAttr(pane(view).rail, attr).map((entry) => [
    String(attrsOf(entry)[attr]),
    attrsOf(entry)["aria-pressed"] === "true",
  ]);
}

function railEntry(view: AnyVnode[], attr: string, value: string): AnyVnode | undefined {
  return withAttr(pane(view).rail, attr).find((entry) => attrsOf(entry)[attr] === value);
}

/** The one vnode of `component` in the content column; undefined when it is not mounted. */
function mounted(view: AnyVnode[], component: unknown): AnyVnode | undefined {
  const found = collectVnodes(pane(view).content).filter((vnode) => vnode.tag === component);
  expect(found.length).toBeLessThanOrEqual(1);
  return found[0];
}

function picker(view: AnyVnode[]): AnyVnode {
  return required(byId(view, "settings-workspace-select"));
}

function pick(view: AnyVnode[], value: string): void {
  (attrsOf(picker(view)).onchange as (event: unknown) => void)({ target: { value } });
}

const OVER_ALPHA = `/settings?account=${ALICE.user_id}&workspace=${ALPHA}`;

describe("pickableRows", () => {
  function summary(rows: PickableRow[]): [string, string, string][] {
    return rows.map((row) => [row.group, row.value, row.label]);
  }

  it("offers the account's own workspaces, stopped ones included, and never a create attempt or a remote row", () => {
    const rows = pickableRows([
      ALPHA_ENTRY,
      BRAVO_ENTRY,
      ownedEntry({ id: "attempt-1", name: "charlie", host_id: "", create_attempt_state: "RUNNING" }),
      ownedEntry({ id: "agent-ee55", name: "delta", host_id: "host-ff66", is_remote: true }),
    ]);

    expect(summary(rows)).toEqual([
      ["owned", ALPHA, "alpha"],
      ["owned", BRAVO, "bravo"],
    ]);
    expect(rows.map((row) => row.app)).toEqual([null, null]);
    expect(rows[0].entry).toBe(ALPHA_ENTRY);
  });

  it("lists a share only once it is accepted or is a pasted link: no invitation, and nothing declined", () => {
    const invitation = sharedWorkspaceEntry({ share_key: "agent-" + "3".repeat(32), status: "invited" });
    const declined = sharedWorkspaceEntry({ share_key: "agent-" + "4".repeat(32), status: "declined" });
    const pasted = sharedWorkspaceEntry(
      {
        share_key: "f".repeat(16),
        is_manual: true,
        status: "invited",
        targets: [{ app: "system_interface", link: `https://system-interface-ab12cd34.${SHARED_WORKSPACE_DOMAIN}/` }],
      },
      { name: "pasted" },
    );

    const rows = pickableRows([ALPHA_ENTRY, invitation, declined, orchard(), pasted]);

    expect(summary(rows)).toEqual([
      ["owned", ALPHA, "alpha"],
      ["sharedWorkspace", ORCHARD_KEY, "orchard"],
      ["sharedWorkspace", "f".repeat(16), "pasted"],
    ]);
  });

  it("offers each app shared with the account, followed by its owner, after the workspaces", () => {
    // A workspace share's app is an app of its own only when a grant names it in its own right.
    const withGrantedApp = orchard({
      targets: [
        sharedAppTarget("system_interface", "orchard"),
        sharedAppTarget("notes", "Notes"),
        sharedAppTarget("ledger", "Ledger", true),
      ],
    });

    const rows = pickableRows([tools(), withGrantedApp, ALPHA_ENTRY]);

    expect(summary(rows)).toEqual([
      ["owned", ALPHA, "alpha"],
      ["sharedWorkspace", ORCHARD_KEY, "orchard"],
      ["sharedApp", `${TOOLS_KEY}#notes`, "Notes (Sam Okafor)"],
      ["sharedApp", `${TOOLS_KEY}#board`, "Board (Sam Okafor)"],
      ["sharedApp", `${ORCHARD_KEY}#ledger`, "Ledger (Sam Okafor)"],
    ]);
    expect(rows.map((row) => row.app)).toEqual([null, null, "notes", "board", "ledger"]);
    expect(rows[2].entry.id).toBe(TOOLS_KEY);
  });

  it("names no owner for an app that came as a pasted link, which names nobody", () => {
    const pastedApp = sharedWorkspaceEntry({
      share_key: "e".repeat(16),
      is_manual: true,
      targets: [{ app: "notes", link: `https://notes-ab12cd34.${SHARED_WORKSPACE_DOMAIN}/` }],
    });

    expect(summary(pickableRows([pastedApp]))).toEqual([["sharedApp", `${"e".repeat(16)}#notes`, "notes"]]);
  });

  it("falls back on a row's id when it has no name", () => {
    expect(pickableRows([ownedEntry({ name: "" })]).map((row) => row.label)).toEqual([ALPHA]);
  });
});

describe("pickedRow", () => {
  // A pasted link's share key is a bare digest, which no workspace route accepts: routes name it `shared-<key>`.
  const PASTED_KEY = "f".repeat(16);
  const pasted = sharedWorkspaceEntry(
    {
      share_key: PASTED_KEY,
      is_manual: true,
      targets: [{ app: "system_interface", link: `https://system-interface-ab12cd34.${SHARED_WORKSPACE_DOMAIN}/` }],
    },
    { name: "pasted" },
  );
  const entries = [ALPHA_ENTRY, BRAVO_ENTRY, orchard(), tools(), pasted];
  const rows = pickableRows(entries);
  // The workspace store's own resolver, which is what the page hands in.
  const store = createEmptyStores().workspaces;
  store.applyWorkspacesMessage(workspacesMessage({ workspaces: entries }));
  const entryByAnyId = (anyId: string): UiWorkspaceEntry | null => store.entryByAnyId(anyId);

  function picked(
    place: Partial<{ settingsWorkspace: string | null; app: string | null; behindWorkspace: string | null }>,
    offered: readonly PickableRow[] = rows,
  ): string | null {
    return (
      pickedRow(offered, { settingsWorkspace: null, app: null, behindWorkspace: null, ...place }, entryByAnyId)?.value ??
      null
    );
  }

  it("is the row the URL names, before the workspace Settings floats over", () => {
    expect(picked({ settingsWorkspace: BRAVO, behindWorkspace: ALPHA })).toBe(BRAVO);
  });

  it("finds one of the account's own workspaces by its agent id and by its host id", () => {
    expect(picked({ settingsWorkspace: BRAVO })).toBe(BRAVO);
    expect(picked({ settingsWorkspace: BRAVO_ENTRY.host_id })).toBe(BRAVO);
    expect(picked({ behindWorkspace: BRAVO_ENTRY.host_id })).toBe(BRAVO);
  });

  it("finds a shared row by its share key and by the alias a route names it with", () => {
    expect(picked({ settingsWorkspace: ORCHARD_KEY })).toBe(ORCHARD_KEY);
    expect(picked({ settingsWorkspace: PASTED_KEY })).toBe(PASTED_KEY);
    expect(picked({ settingsWorkspace: `shared-${PASTED_KEY}` })).toBe(PASTED_KEY);
    // A shared workspace on screen is the one Settings floats over.
    expect(picked({ behindWorkspace: `shared-${PASTED_KEY}` })).toBe(PASTED_KEY);
    expect(picked({ behindWorkspace: `shared-${ORCHARD_KEY}` })).toBe(ORCHARD_KEY);
  });

  it("picks the app the URL names within a share, and the share's first row when it names none it has", () => {
    expect(picked({ settingsWorkspace: TOOLS_KEY, app: "board" })).toBe(`${TOOLS_KEY}#board`);
    expect(picked({ settingsWorkspace: TOOLS_KEY })).toBe(`${TOOLS_KEY}#notes`);
    expect(picked({ settingsWorkspace: TOOLS_KEY, app: "gone" })).toBe(`${TOOLS_KEY}#notes`);
    // A whole workspace is its own row, and an app named beside one of the account's own is no app of it.
    expect(picked({ settingsWorkspace: ORCHARD_KEY })).toBe(ORCHARD_KEY);
    expect(picked({ settingsWorkspace: BRAVO, app: "board" })).toBe(BRAVO);
  });

  it("falls back from an id it cannot place to the workspace Settings floats over, then to the first row", () => {
    expect(picked({ settingsWorkspace: "agent-gone", behindWorkspace: BRAVO })).toBe(BRAVO);
    expect(picked({ settingsWorkspace: "agent-gone", behindWorkspace: "agent-gone" })).toBe(ALPHA);
    expect(picked({})).toBe(ALPHA);
  });

  it("does not pick a workspace that is listed but not on offer", () => {
    // The store still knows alpha, but the picker does not offer it (a remote row, a create attempt).
    const withoutAlpha = rows.filter((row) => row.entry.id !== ALPHA);

    expect(picked({ settingsWorkspace: ALPHA, behindWorkspace: BRAVO }, withoutAlpha)).toBe(BRAVO);
    expect(picked({ settingsWorkspace: ALPHA }, withoutAlpha)).toBe(BRAVO);
  });

  it("picks nothing when nothing is on offer", () => {
    expect(picked({ settingsWorkspace: ALPHA, behindWorkspace: ALPHA }, [])).toBeNull();
  });
});

describe("Settings' tabs", () => {
  it("draws a tab list of Studio, Accounts, and Workspace & apps under the title", () => {
    const view = openSettings(`/settings?account=${ALICE.user_id}`).draw();

    expect(allText(view[0])).toBe("Settings");
    const list = required(byId(view, "settings-tabs"));
    expect(attrsOf(list).role).toBe("tablist");
    expect(tabs(list).map((tab) => [attrsOf(tab).id, allText(tab), attrsOf(tab)["aria-selected"]])).toEqual([
      ["settings-tab-studio", "Studio", "true"],
      ["settings-tab-accounts", "Accounts", "false"],
      ["settings-tab-workspace", "Workspace & apps", "false"],
    ]);
  });

  it("opens on Studio from a hub page, and on Workspace & apps over a workspace", () => {
    expect(selectedTab(openSettings(`/settings?account=${ALICE.user_id}`).draw())).toBe("settings-tab-studio");
    expect(selectedTab(openSettings(OVER_ALPHA).draw())).toBe("settings-tab-workspace");
  });

  it("opens on the tab the URL names, wherever Settings was opened from", () => {
    expect(selectedTab(openSettings(`${OVER_ALPHA}&tab=accounts`).draw())).toBe("settings-tab-accounts");
    expect(selectedTab(openSettings(`${OVER_ALPHA}&tab=studio`).draw())).toBe("settings-tab-studio");
    expect(selectedTab(openSettings(`/settings?account=${ALICE.user_id}&tab=workspace`).draw())).toBe(
      "settings-tab-workspace",
    );
    // A tab it does not know is no tab at all.
    expect(selectedTab(openSettings(`${OVER_ALPHA}&tab=billing`).draw())).toBe("settings-tab-workspace");
  });

  it("hides Workspace & apps, and falls back to Studio, when the account has nothing to pick", () => {
    const attempt = ownedEntry({ id: "attempt-1", host_id: "", create_attempt_state: "RUNNING" });
    for (const route of [`${OVER_ALPHA}`, `/settings?account=${ALICE.user_id}&tab=workspace`]) {
      const view = openSettings(route, { entries: [attempt] }).draw();

      expect(tabs(view).map((tab) => attrsOf(tab).id)).toEqual(["settings-tab-studio", "settings-tab-accounts"]);
      expect(selectedTab(view)).toBe("settings-tab-studio");
      expect(railEntries(view, "data-settings-section").length).toBeGreaterThan(0);
    }
  });

  it("writes a clicked tab to the URL in place, keeping where the other tabs were left", () => {
    const harness = openSettings(`/settings?account=${ALICE.user_id}&section=updates`);

    click(byId(harness.draw(), "settings-tab-accounts"));

    expect(harness.routeWrites).toHaveLength(1);
    expect(harness.routeWrites[0].options).toEqual({ replace: true });
    expect(harness.routeWrites[0].params).toBeUndefined();
    expect(harness.route.current.split("?")[0]).toBe("/settings");
    expect(queryOf(harness.route.current)).toEqual({ account: ALICE.user_id, section: "updates", tab: "accounts" });
    expect(selectedTab(harness.draw())).toBe("settings-tab-accounts");

    // Back on Studio, the section it was left on is still the one shown.
    click(byId(harness.draw(), "settings-tab-studio"));
    expect(railEntries(harness.draw(), "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["updates", true],
    ]);
  });

  it("writes nothing for a click on what the URL already says", () => {
    const harness = openSettings(`/settings?account=${ALICE.user_id}&tab=accounts`);

    click(byId(harness.draw(), "settings-tab-accounts"));

    expect(harness.routeWrites).toEqual([]);
  });

  it("reads and writes the route it was opened on while the request popup floats over it", () => {
    // The live route is the popup's and carries none of Settings' params; writing a choice there would move the
    // popup and leave Settings to come back on its stale place.
    const behind = `${OVER_ALPHA}&tab=workspace&group=backup`;
    const harness = openSettings(`/inbox?account=${ALICE.user_id}&workspace=${ALPHA}&selected=evt-1`);
    harness.shell.settingsRouteBehindOverlay = behind;

    const view = harness.draw();
    expect(selectedTab(view)).toBe("settings-tab-workspace");
    expect(railEntries(view, "data-settings-group").filter(([, isSelected]) => isSelected)).toEqual([["backup", true]]);

    click(railEntry(view, "data-settings-group", "updates"));

    expect(harness.routeWrites).toEqual([]);
    expect(harness.route.current).toContain("/inbox");
    const remembered = required(harness.shell.settingsRouteBehindOverlay ?? undefined);
    expect(remembered.split("?")[0]).toBe("/settings");
    expect(queryOf(remembered)).toEqual({ ...queryOf(behind), group: "updates" });
  });

  it("pins the title and the tabs above a pane whose rail and page scroll on their own", () => {
    // One scroller around both columns would carry the rail off the top with a long page.
    const view = openSettings(`/settings?account=${ALICE.user_id}`).draw();
    const { row, rail, content } = pane(view);

    expect(classTokensOf(view[0])).toContain("shrink-0");
    expect(classTokensOf(required(byId(view, "settings-tabs")))).toContain("shrink-0");
    expect(attrsOf(rail)["aria-label"]).toBe("Settings");
    for (const column of [rail, content]) {
      expect(classTokensOf(column)).toEqual(expect.arrayContaining(["overflow-y-auto", "min-h-0"]));
    }
    expect(classTokensOf(row)).toEqual(expect.arrayContaining(["flex-1", "min-h-0"]));
    expect(classTokensOf(row)).not.toContain("items-start");
  });
});

describe("Settings › Studio", () => {
  const STUDIO = `/settings?account=${ALICE.user_id}`;

  function contentText(view: AnyVnode[]): string {
    return allText(renderDeep(pane(view).content));
  }

  /** The heading of the section's panel on screen. */
  function panelHeading(view: AnyVnode[]): string {
    return allText(required(collectVnodes(pane(view).content).find((vnode) => vnode.tag === "h2")));
  }

  it("lists the sections this build offers in the rail, on the first of them", async () => {
    const view = await drawLoaded(openSettings(STUDIO));

    const expected = SETTINGS_SECTIONS.filter((section) => section.name !== "keyboard-shortcuts");
    expect(railEntries(view, "data-settings-section")).toEqual(
      expected.map((section, index) => [section.name, index === 0]),
    );
    expect(withAttr(pane(view).rail, "data-settings-section").map((entry) => allText(entry))).toEqual(
      expected.map((section) => section.label),
    );
    expect(expected[0].name).toBe("notifications");
  });

  it("lists Keyboard shortcuts after Display in the desktop app that can rebind them", () => {
    const view = openSettings(STUDIO, { native: { getKeyboardShortcuts: () => Promise.resolve(null) } }).draw();

    const names = railEntries(view, "data-settings-section").map(([name]) => name);
    expect(names).toEqual(SETTINGS_SECTIONS.map((section) => section.name));
    expect(names.indexOf("keyboard-shortcuts")).toBe(names.indexOf("display") + 1);
  });

  it("keeps Updates in the rail in the browser build, for the machine-update window", () => {
    // There is no binary to update in a browser, but machines update the same way everywhere.
    const names = railEntries(openSettings(STUDIO, { native: null }).draw(), "data-settings-section").map(
      ([name]) => name,
    );

    expect(names).toContain("updates");
    expect(names).toContain("error-reporting");
    expect(names).not.toContain("keyboard-shortcuts");
  });

  it("styles its rail entries like every other pane's", () => {
    const entries = withAttr(pane(openSettings(STUDIO).draw()).rail, "data-settings-section");

    // Every section but Keyboard shortcuts, which this harness's build cannot rebind.
    expect(entries).toHaveLength(SETTINGS_SECTIONS.length - 1);
    for (const entry of entries) {
      expect(entry.tag).toBe("button");
      expect(classTokensOf(entry)).toEqual(expect.arrayContaining(["type-body", "rounded-md", "text-primary"]));
    }
  });

  it("writes a clicked section to the URL, and shows it", async () => {
    const harness = openSettings(STUDIO);
    const view = await drawLoaded(harness);

    click(railEntry(view, "data-settings-section", "error-reporting"));

    expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
    expect(queryOf(harness.route.current)).toEqual({ account: ALICE.user_id, section: "error-reporting" });
    const after = harness.draw();
    expect(railEntries(after, "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["error-reporting", true],
    ]);
    expect(panelHeading(after)).toBe("Error reporting");
  });

  it("opens the section the URL asks for even when the page is already on screen", async () => {
    // The menu bar's "Check for Updates..." navigates a window that may already be showing Settings. The router
    // returns the same component for `/settings`, so nothing is re-created: the ask has to be read on every draw.
    const harness = openSettings(STUDIO);
    const before = await drawLoaded(harness);
    expect(railEntries(before, "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["notifications", true],
    ]);

    harness.route.current = `${STUDIO}&section=updates`;

    const after = harness.draw();
    expect(railEntries(after, "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["updates", true],
    ]);
    expect(panelHeading(after)).toBe("Updates");
  });

  it("ignores a section name it does not know", async () => {
    // A stale or mistyped deep link would otherwise land on a panel that renders nothing, with no rail entry
    // selected to leave it by.
    const view = await drawLoaded(openSettings(`${STUDIO}&section=machine-updates`, { native: null }));

    expect(railEntries(view, "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["notifications", true],
    ]);
  });

  it("ignores Keyboard shortcuts where there is no main process to rebind them", async () => {
    const view = await drawLoaded(openSettings(`${STUDIO}&section=keyboard-shortcuts`, { native: null }));

    expect(railEntries(view, "data-settings-section").filter(([, isSelected]) => isSelected)).toEqual([
      ["notifications", true],
    ]);
    expect(mounted(view, KeyboardShortcutsSection)).toBeUndefined();
  });

  it("says the settings are loading until the payload lands", async () => {
    const harness = openSettings(STUDIO, {
      respond: (url) => (url.startsWith("/ui/api/settings") ? new Promise<Response>(() => undefined) : undefined),
    });

    const view = await drawLoaded(harness);

    expect(contentText(view)).toContain("Loading settings…");
    expect(harness.requests).toContain(`/ui/api/settings?account=${ALICE.user_id}`);
  });

  describe("when the settings payload could not be loaded", () => {
    function unreachable(route: string, native: Record<string, unknown> = {}): Harness {
      return openSettings(route, {
        native,
        respond: (url) => (url.startsWith("/ui/api/settings") ? jsonResponse({ error: "boom" }, 500) : undefined),
      });
    }

    it("says so in place of a section that needs it, with a way to try again", async () => {
      const harness = unreachable(STUDIO);
      const view = await drawLoaded(harness);

      const notice = required(byId(pane(view).content, "settings-load-error"));
      const actions = attrsOf(notice).actions as { label: string; onclick: () => void }[];
      expect(actions.map((action) => action.label)).toEqual(["Try again"]);

      const readsBefore = harness.requests.filter((url) => url.startsWith("/ui/api/settings")).length;
      actions[0].onclick();
      expect(harness.requests.filter((url) => url.startsWith("/ui/api/settings"))).toHaveLength(readsBefore + 1);
    });

    it("still opens Updates, the one remedy a broken backend leaves the user", async () => {
      // The panel is the only surface that reports an update check, and the menu bar's "Check for Updates..."
      // lands on it. It reads none of the payload, so a backend that is down must not hide it.
      const harness = unreachable(`${STUDIO}&section=updates`);
      const view = await drawLoaded(harness);

      expect(byId(pane(view).content, "settings-load-error")).toBeUndefined();
      expect(panelHeading(view)).toBe("Updates");

      // Every other section still says the payload is missing.
      harness.route.current = `${STUDIO}&section=notifications`;
      expect(byId(pane(harness.draw()).content, "settings-load-error")).toBeDefined();
    });

    it("still opens Display and Keyboard shortcuts, which the main process holds", async () => {
      const native = { getKeyboardShortcuts: () => Promise.resolve(null) };

      const display = await drawLoaded(unreachable(`${STUDIO}&section=display`, native));
      expect(byId(pane(display).content, "settings-load-error")).toBeUndefined();
      expect(panelHeading(display)).toBe("Display");

      const shortcuts = await drawLoaded(unreachable(`${STUDIO}&section=keyboard-shortcuts`, native));
      expect(byId(pane(shortcuts).content, "settings-load-error")).toBeUndefined();
      expect(mounted(shortcuts, KeyboardShortcutsSection)).toBeDefined();
    });
  });
});

describe("Settings › Accounts", () => {
  const ACCOUNTS = `/settings?account=${ALICE.user_id}&tab=accounts`;

  function shownAccount(view: AnyVnode[]): string | null {
    const card = mounted(view, AccountCard);
    return card === undefined ? null : (attrsOf(card).account as UiAccountEntry).user_id;
  }

  it("lists every signed-in account by email, marks the window's as Current, and ends with Add account", () => {
    const named = accountEntry({ display_name: "Alice Liddell" });
    const view = openSettings(ACCOUNTS, { accounts: [named, BOB] }).draw();
    const { rail } = pane(view);

    expect(withAttr(rail, "data-settings-account").map((entry) => allText(entry))).toEqual([
      `${ALICE.email} Current`,
      BOB.email,
    ]);
    expect(allText(rail)).not.toContain("Alice Liddell");
    const buttons = collectVnodes(rail).filter((vnode) => vnode.tag === "button");
    expect(attrsOf(buttons[buttons.length - 1]).id).toBe("settings-add-account");
    expect(allText(buttons[buttons.length - 1])).toBe("Add account");
  });

  it("shows the window's account's card first, with the page's model", () => {
    const view = openSettings(ACCOUNTS, { accounts: [BOB, ALICE] }).draw();

    expect(railEntries(view, "data-settings-account")).toEqual([
      [BOB.user_id, false],
      [ALICE.user_id, true],
    ]);
    const card = required(mounted(view, AccountCard));
    expect(attrsOf(card).account).toEqual(ALICE);
    expect(attrsOf(card).model).toBeDefined();
  });

  it("shows the first signed-in account in a window that is the device's", () => {
    const view = openSettings("/settings?account=local&tab=accounts", { accountId: "local" }).draw();

    expect(shownAccount(view)).toBe(ALICE.user_id);
    expect(allText(pane(view).rail)).not.toContain("Current");
  });

  it("shows the account the URL names, and writes a clicked one to it", () => {
    const harness = openSettings(`${ACCOUNTS}&settings_account=${BOB.user_id}`);
    const view = harness.draw();
    expect(shownAccount(view)).toBe(BOB.user_id);

    click(railEntry(view, "data-settings-account", ALICE.user_id));

    expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
    expect(queryOf(harness.route.current)).toEqual({
      account: ALICE.user_id,
      tab: "accounts",
      settings_account: ALICE.user_id,
    });
    expect(shownAccount(harness.draw())).toBe(ALICE.user_id);
  });

  it("falls back to the window's account when the selected one is logged out, then to the empty state", () => {
    const harness = openSettings(`${ACCOUNTS}&settings_account=${BOB.user_id}`);
    expect(shownAccount(harness.draw())).toBe(BOB.user_id);

    harness.shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], ALICE.user_id));
    const afterBob = harness.draw();
    expect(shownAccount(afterBob)).toBe(ALICE.user_id);
    expect(railEntries(afterBob, "data-settings-account")).toEqual([[ALICE.user_id, true]]);

    harness.shell.stores.accounts.applyAccountsMessage(accountsMessage([], ALICE.user_id));
    const afterAll = harness.draw();
    expect(shownAccount(afterAll)).toBeNull();
    expect(allText(required(byId(afterAll, "settings-no-accounts")))).toBe("No accounts logged in.");
  });

  it("falls back to another account when the window's own is the one logged out", () => {
    const harness = openSettings(ACCOUNTS);
    expect(shownAccount(harness.draw())).toBe(ALICE.user_id);

    harness.shell.stores.accounts.applyAccountsMessage(accountsMessage([BOB], ALICE.user_id));

    expect(shownAccount(harness.draw())).toBe(BOB.user_id);
  });

  it("says no account is logged in, with only Add account in the rail", () => {
    const view = openSettings("/settings?account=local&tab=accounts", { accounts: [], accountId: "local" }).draw();

    expect(allText(pane(view).content)).toBe("No accounts logged in.");
    expect(mounted(view, AccountCard)).toBeUndefined();
    expect(collectVnodes(pane(view).rail).filter((vnode) => vnode.tag === "button").map((button) => allText(button))).toEqual([
      "Add account",
    ]);
  });

  it("starts an added account's sign-in to close on landing, and holds the button while the browser opens", () => {
    const start = vi.spyOn(webLogin, "start").mockImplementation(() => Promise.resolve());
    const harness = openSettings(ACCOUNTS);

    const idle = required(byId(harness.draw(), "settings-add-account"));
    expect(attrsOf(idle).disabled).toBe(false);
    click(idle);

    expect(start.mock.calls).toEqual([["", { isClosedOnSignIn: true }]]);
    // The sign-in leaves Settings where it is.
    expect(harness.routeWrites).toEqual([]);

    vi.spyOn(webLogin, "isOpen", "get").mockReturnValue(true);
    const opening = required(byId(harness.draw(), "settings-add-account"));
    expect(attrsOf(opening).disabled).toBe(true);
    expect(allText(opening)).toBe("Opening browser…");
  });

  it("lists an account signed in while it is open, and reads its plan", async () => {
    const harness = openSettings(ACCOUNTS, { accounts: [ALICE] });
    await settle();
    expect(railEntries(harness.draw(), "data-settings-account").map(([userId]) => userId)).toEqual([ALICE.user_id]);

    harness.shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], ALICE.user_id));
    harness.page.onbeforeupdate();
    await settle();

    expect(railEntries(harness.draw(), "data-settings-account").map(([userId]) => userId)).toEqual([
      ALICE.user_id,
      BOB.user_id,
    ]);
    expect(harness.requests.filter((url) => url.endsWith("/plan"))).toEqual([
      `/ui/api/accounts/${ALICE.user_id}/plan`,
      `/ui/api/accounts/${BOB.user_id}/plan`,
    ]);
  });

  it("reads no account's details while another tab is shown, and reads them once Accounts is", async () => {
    const accountReads = (requests: string[]): string[] =>
      requests.filter((url) => url.startsWith("/ui/api/accounts/"));
    const harness = openSettings(`/settings?workspace=${ALPHA}&tab=workspace&group=sharing`);
    await drawLoaded(harness);
    harness.page.onbeforeupdate();
    await settle();

    expect(accountReads(harness.requests)).toEqual([]);

    harness.route.current = ACCOUNTS;
    harness.page.onbeforeupdate();
    harness.draw();
    await settle();

    expect(accountReads(harness.requests).filter((url) => url.endsWith("/plan"))).toEqual([
      `/ui/api/accounts/${ALICE.user_id}/plan`,
      `/ui/api/accounts/${BOB.user_id}/plan`,
    ]);
  });

  it("shows the plan an earlier visit read as soon as Accounts is turned to, while it is read again", async () => {
    const explorer: AccountPlanView = {
      plan_name: "explorer",
      plan_display_name: "Explorer",
      available_plans: ["free", "explorer"],
      usage_rows: [],
      is_over_storage_quota: false,
      is_at_bucket_quota: false,
      is_sharing_workspaces: false,
    };
    const earlier = openSettings(ACCOUNTS, {
      respond: (url) => (url.endsWith("/plan") ? jsonResponse({ plan_view: explorer, trim_status: null }) : undefined),
    });
    await drawLoaded(earlier);
    // The next visit's reads of the accounts never land, so only what the window kept can be on screen.
    const harness = openSettings(`/settings?workspace=${ALPHA}&tab=workspace&group=sharing`, {
      respond: (url) => (url.startsWith("/ui/api/accounts/") ? new Promise<Response>(() => undefined) : undefined),
    });
    await drawLoaded(harness);

    // A redraw as mithril makes it: the hook, then the view.
    harness.route.current = ACCOUNTS;
    harness.page.onbeforeupdate();
    const shown = allText(renderDeep(pane(harness.draw()).content));

    expect(shown).toContain("Explorer");
    expect(shown).not.toContain("Loading plan and usage");
  });

  describe("the action-error notice above the card", () => {
    /** The notice's text once Alice's Log out is pressed and `logout` answers it. */
    async function pressLogOut(logout: () => Response): Promise<string> {
      const harness = openSettings(ACCOUNTS, {
        accounts: [ALICE],
        respond: (url) => (url.endsWith("/logout") ? logout() : undefined),
      });
      await settle();
      expect(byId(harness.draw(), "accounts-action-error")).toBeUndefined();
      const logOut = collectVnodes(renderDeep(harness.draw())).find(
        (node) => node.tag === "button" && allText(node) === "Log out",
      );
      click(logOut);
      await settle();
      const content = pane(harness.draw()).content;
      // Above the card, not in place of it.
      const order = collectVnodes(content)
        .filter((vnode) => attrsOf(vnode).id === "accounts-action-error" || vnode.tag === AccountCard)
        .map((vnode) => (vnode.tag === AccountCard ? "card" : "notice"));
      expect(order).toEqual(["notice", "card"]);
      return allText(byId(renderDeep(content), "accounts-action-error"));
    }

    it("shows the route's reason for a refused log out", async () => {
      const inFlight =
        "A workspace is still being created on this account's providers; wait for it to finish, then sign out.";

      const text = await pressLogOut(() => jsonResponse({ error: inFlight }, 409));

      expect(text).toContain(inFlight);
      expect(text).not.toContain("Couldn't log out");
      expect(text).toContain("Report a bug");
    });

    it("names the log out that failed and asks for a bug report when the route gives no reason", async () => {
      const text = await pressLogOut(() =>
        jsonResponse({ detail: "The account isn't in this computer's account list." }, 409),
      );

      expect(text).toContain(`Couldn't log out ${ALICE.email}. Try again. If this persists, report a bug.`);
      expect(text).toContain("Report a bug");
    });
  });
});

describe("Settings › Workspace & apps: the picker", () => {
  const WORKSPACE_TAB = `/settings?account=${ALICE.user_id}&tab=workspace`;

  function options(view: AnyVnode[]): [string, string][] {
    return collectVnodes(picker(view))
      .filter((vnode) => vnode.tag === "option")
      .map((option) => [String(attrsOf(option).value), allText(option)]);
  }

  function groups(view: AnyVnode[]): [string, string[]][] {
    return collectVnodes(picker(view))
      .filter((vnode) => vnode.tag === "optgroup")
      .map((group) => [
        String(attrsOf(group).label),
        collectVnodes(group.children)
          .filter((vnode) => vnode.tag === "option")
          .map((option) => String(attrsOf(option).value)),
      ]);
  }

  it("sits at the top of the rail under its label, on the picked workspace", () => {
    const view = openSettings(`${WORKSPACE_TAB}&settings_workspace=${BRAVO}`).draw();
    const { rail } = pane(view);

    const label = required(collectVnodes(rail).find((vnode) => vnode.tag === "label"));
    expect(allText(label)).toBe("Workspace/App");
    expect(attrsOf(label).for).toBe("settings-workspace-select");
    expect(attrsOf(picker(view)).value).toBe(BRAVO);
    const inRail = collectVnodes(rail);
    expect(inRail.indexOf(picker(view))).toBeLessThan(inRail.indexOf(required(railEntry(view, "data-settings-group", "general"))));
  });

  it("offers plain options while nothing is shared with the account", () => {
    const view = openSettings(WORKSPACE_TAB).draw();

    expect(groups(view)).toEqual([]);
    expect(options(view)).toEqual([
      [ALPHA, "alpha"],
      [BRAVO, "bravo"],
    ]);
  });

  it("groups its options once something is shared, leaving out a group with nothing in it", () => {
    const all = openSettings(WORKSPACE_TAB, { entries: [ALPHA_ENTRY, orchard(), tools()] }).draw();
    expect(groups(all)).toEqual([
      ["Your workspaces", [ALPHA]],
      ["Workspaces shared with you", [ORCHARD_KEY]],
      ["Apps shared with you", [`${TOOLS_KEY}#notes`, `${TOOLS_KEY}#board`]],
    ]);
    expect(options(all).map(([, text]) => text)).toEqual(["alpha", "orchard", "Notes (Sam Okafor)", "Board (Sam Okafor)"]);

    const appsOnly = openSettings(WORKSPACE_TAB, { entries: [ALPHA_ENTRY, tools()] }).draw();
    expect(groups(appsOnly).map(([label]) => label)).toEqual(["Your workspaces", "Apps shared with you"]);

    const sharedOnly = openSettings(WORKSPACE_TAB, { entries: [orchard()] }).draw();
    expect(groups(sharedOnly).map(([label]) => label)).toEqual(["Workspaces shared with you"]);
  });

  it("starts on the workspace Settings was opened over, by either of its coordinates", () => {
    expect(attrsOf(picker(openSettings(`/settings?account=${ALICE.user_id}&workspace=${BRAVO}`).draw())).value).toBe(
      BRAVO,
    );
    const byHost = openSettings(`/settings?account=${ALICE.user_id}&workspace=${ALPHA_HOST}`, {
      entries: [BRAVO_ENTRY, ALPHA_ENTRY],
    });
    expect(attrsOf(picker(byHost.draw())).value).toBe(ALPHA);
  });

  it("writes a pick in one go, starting it on its own Sharing target, Permissions section, and scroll", () => {
    // Each of these belongs to the workspace being left; written one at a time, each write would be computed
    // from the route as it was, and put back what the last one removed.
    const harness = openSettings(
      `${OVER_ALPHA}&tab=workspace&group=sharing&share_target=web&perm_section=conn:slack:&focus=owner`,
    );

    pick(harness.draw(), BRAVO);

    expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
    expect(queryOf(harness.route.current)).toEqual({
      account: ALICE.user_id,
      // What the window shows has not changed; only what Settings speaks for.
      workspace: ALPHA,
      tab: "workspace",
      group: "sharing",
      settings_workspace: BRAVO,
    });
    expect(attrsOf(picker(harness.draw())).value).toBe(BRAVO);
  });

  it("writes a picked app as its share and its name, and a workspace picked after it without one", () => {
    const harness = openSettings(WORKSPACE_TAB, { entries: [ALPHA_ENTRY, tools()] });

    pick(harness.draw(), `${TOOLS_KEY}#board`);
    expect(queryOf(harness.route.current)).toEqual({
      account: ALICE.user_id,
      tab: "workspace",
      settings_workspace: TOOLS_KEY,
      app: "board",
    });
    expect(attrsOf(picker(harness.draw())).value).toBe(`${TOOLS_KEY}#board`);

    pick(harness.draw(), ALPHA);
    expect(queryOf(harness.route.current)).toEqual({
      account: ALICE.user_id,
      tab: "workspace",
      settings_workspace: ALPHA,
    });
  });

  it("writes nothing for a value it did not offer", () => {
    const harness = openSettings(WORKSPACE_TAB);

    pick(harness.draw(), "agent-gone");

    expect(harness.routeWrites).toEqual([]);
  });
});

describe("Settings › Workspace & apps: one of the account's own workspaces", () => {
  const ON_ALPHA = `${OVER_ALPHA}&tab=workspace`;

  function selectedGroup(view: AnyVnode[]): string[] {
    return railEntries(view, "data-settings-group")
      .filter(([, isSelected]) => isSelected)
      .map(([group]) => group);
  }

  function optionsReads(harness: Harness): string[] {
    return harness.requests.filter((url) => url.endsWith("/options"));
  }

  function permissionsReads(harness: Harness): string[] {
    return harness.requests.filter((url) => url.endsWith("/permissions"));
  }

  describe("the rail", () => {
    it("lists General, Permissions, Sharing, Backup, Updates, and Suggestions, on General", () => {
      const view = openSettings(ON_ALPHA).draw();

      expect(railEntries(view, "data-settings-group")).toEqual([
        ["general", true],
        ["permissions", false],
        ["sharing", false],
        ["backup", false],
        ["updates", false],
        ["suggestions", false],
      ]);
      expect(withAttr(pane(view).rail, "data-settings-group").map((entry) => allText(entry))).toEqual([
        "General",
        "Permissions",
        "Sharing",
        "Backup",
        "Updates",
        "Suggestions",
      ]);
    });

    it("opens on the entry the URL names, and on General for one it does not know", () => {
      expect(selectedGroup(openSettings(`${ON_ALPHA}&group=backup`).draw())).toEqual(["backup"]);
      // The legacy `account` group is General's Owner section, so its name selects General.
      expect(selectedGroup(openSettings(`${ON_ALPHA}&group=account`).draw())).toEqual(["general"]);
    });

    it("writes a clicked entry to the URL, and drops an ask for the Owner section with it", () => {
      const harness = openSettings(`${ON_ALPHA}&group=general&focus=owner`);

      click(railEntry(harness.draw(), "data-settings-group", "updates"));

      expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
      expect(queryOf(harness.route.current)).toEqual({
        account: ALICE.user_id,
        workspace: ALPHA,
        tab: "workspace",
        group: "updates",
      });
      expect(selectedGroup(harness.draw())).toEqual(["updates"]);
    });

    it("badges Permissions while the picked workspace's agents wait on an answer", () => {
      const harness = openSettings(ON_ALPHA);
      expect(byId(harness.draw(), "settings-permissions-badge")).toBeUndefined();

      // Another workspace's request is not this one's.
      harness.shell.stores.requests.applyRequestsMessage({
        type: "requests",
        count: 1,
        request_ids: ["evt-b"],
        workspace_agent_ids: [BRAVO],
      });
      expect(byId(harness.draw(), "settings-permissions-badge")).toBeUndefined();

      harness.shell.stores.requests.applyRequestsMessage({
        type: "requests",
        count: 2,
        request_ids: ["evt-a", "evt-b"],
        workspace_agent_ids: [ALPHA, BRAVO],
      });
      const view = harness.draw();
      const badge = required(byId(view, "settings-permissions-badge"));
      expect(collectVnodes(railEntry(view, "data-settings-group", "permissions"))).toContain(badge);
    });

    it("dots Updates while the picked workspace is out of date", () => {
      const harness = openSettings(ON_ALPHA);
      expect(byId(harness.draw(), "updates-nav-dot")).toBeUndefined();

      harness.shell.stores.updates.applyUpdatesMessage({
        type: "workspace_updates",
        updates: { [ALPHA]: OUT_OF_DATE_UPDATE },
        update_window: "2:00 AM-5:00 AM",
      });

      const view = harness.draw();
      const dot = required(byId(view, "updates-nav-dot"));
      expect(collectVnodes(railEntry(view, "data-settings-group", "updates"))).toContain(dot);
      // Bravo is not behind.
      harness.route.current = `${ON_ALPHA}&settings_workspace=${BRAVO}`;
      expect(byId(harness.draw(), "updates-nav-dot")).toBeUndefined();
    });

    it("nests the permission sections under Permissions only while it is the selected entry", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=permissions&perm_section=local-files`);
      const before = harness.draw();
      // The sections are the read's to list; the page itself says what is loading.
      expect(withAttr(before, "data-perm-nav")).toEqual([]);

      // The Permissions page reads on its first mount.
      (attrsOf(required(mounted(before, PermissionsTab))).model as PermissionsModel).ensureLoaded();
      await settle();

      const view = harness.draw();
      const nested = required(byId(view, "settings-permission-sections"));
      const sections = withAttr(nested, "data-perm-nav").map((entry) => [
        attrsOf(entry)["data-perm-nav"],
        attrsOf(entry)["aria-pressed"],
      ]);
      expect(sections).toContainEqual(["conn:slack:", "false"]);
      expect(sections).toContainEqual(["local-files", "true"]);
      // Directly under the Permissions entry, before Sharing.
      const inRail = collectVnodes(pane(view).rail);
      expect(inRail.indexOf(nested)).toBeGreaterThan(
        inRail.indexOf(required(railEntry(view, "data-settings-group", "permissions"))),
      );
      expect(inRail.indexOf(nested)).toBeLessThan(
        inRail.indexOf(required(railEntry(view, "data-settings-group", "sharing"))),
      );

      click(withAttr(nested, "data-perm-nav").find((entry) => attrsOf(entry)["data-perm-nav"] === "conn:slack:"));
      expect(queryOf(harness.route.current).perm_section).toBe("conn:slack:");
      expect(queryOf(harness.route.current).group).toBe("permissions");

      click(railEntry(harness.draw(), "data-settings-group", "general"));
      const general = harness.draw();
      expect(byId(general, "settings-permission-sections")).toBeUndefined();
      expect(withAttr(general, "data-perm-nav")).toEqual([]);
    });

    it("nests the sharing targets under Sharing only while it is the selected entry", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`);
      // The targets are the options load's to name.
      expect(byId(harness.draw(), "settings-sharing-targets")).toBeUndefined();
      await settle();

      const view = harness.draw();
      const nested = required(byId(view, "settings-sharing-targets"));
      expect(withAttr(nested, "data-share-target").map((entry) => attrsOf(entry)["data-share-target"])).toEqual([
        "system_interface",
        "web",
        "docs",
        "terminal",
      ]);
      expect(allText(nested)).toContain("Full workspace");
      expect(allText(nested)).toContain("System apps");
      const inRail = collectVnodes(pane(view).rail);
      expect(inRail.indexOf(nested)).toBeGreaterThan(
        inRail.indexOf(required(railEntry(view, "data-settings-group", "sharing"))),
      );
      expect(inRail.indexOf(nested)).toBeLessThan(
        inRail.indexOf(required(railEntry(view, "data-settings-group", "backup"))),
      );

      click(railEntry(view, "data-settings-group", "backup"));
      expect(byId(harness.draw(), "settings-sharing-targets")).toBeUndefined();
    });
  });

  describe("General, Backup, Updates, and Suggestions", () => {
    it("mounts the group's page on the workspace's options, once they have loaded", async () => {
      for (const group of ["general", "backup", "updates", "suggestions"] as const) {
        const harness = openSettings(`${ON_ALPHA}&group=${group}`);

        const loading = harness.draw();
        expect(allText(pane(loading).content)).toContain("Loading workspace settings...");
        expect(mounted(loading, WorkspaceSettingsGroup)).toBeUndefined();
        await settle();

        const view = harness.draw();
        const page = required(mounted(view, WorkspaceSettingsGroup));
        expect(attrsOf(page).group).toBe(group);
        expect(attrsOf(page).isOwnerSectionAsked).toBe(false);
        const model = attrsOf(page).model as WorkspaceOptionsModel;
        expect(model).toBeInstanceOf(WorkspaceOptionsModel);
        expect(model.agentId).toBe(ALPHA);
        expect(model.data?.name).toBe("alpha");
        expect(allText(pane(view).content)).not.toContain("Loading workspace settings...");
        // A block page, not the flex column Permissions and Sharing are.
        expect(classTokensOf(pane(view).content)).not.toContain("flex-col");
      }
    });

    it("passes on an arrival's ask for the Owner section", async () => {
      const view = await drawLoaded(openSettings(`${ON_ALPHA}&group=general&focus=owner`));

      expect(attrsOf(required(mounted(view, WorkspaceSettingsGroup))).isOwnerSectionAsked).toBe(true);
    });

    it("reads the options once, however often it draws and whichever entry is shown, and never the gateway", async () => {
      const harness = openSettings(ON_ALPHA);
      const view = await drawLoaded(harness);
      harness.draw();
      click(railEntry(view, "data-settings-group", "updates"));
      await drawLoaded(harness);

      expect(optionsReads(harness)).toEqual([`/ui/api/workspaces/${ALPHA}/options`]);
      // Permissions has its own read, which opening General must not start.
      expect(permissionsReads(harness)).toEqual([]);
    });
  });

  describe("when the workspace's options cannot be loaded", () => {
    function failing(route: string): { harness: Harness; heal: () => void } {
      let isFailing = true;
      const harness = openSettings(route, {
        respond: (url) =>
          url.endsWith("/options") && isFailing ? jsonResponse({ error: "The host did not answer." }, 502) : undefined,
      });
      return { harness, heal: () => (isFailing = false) };
    }

    it("shows the load error with Try again on General, Backup, Updates, Suggestions, and Sharing", async () => {
      for (const group of ["general", "backup", "updates", "suggestions", "sharing"]) {
        const { harness } = failing(`${ON_ALPHA}&group=${group}`);
        const view = await drawLoaded(harness);

        const notice = required(byId(pane(view).content, "options-load-error"));
        expect(attrsOf(notice).workspaceAgentId).toBe(ALPHA);
        expect((attrsOf(notice).actions as { label: string }[]).map((action) => action.label)).toEqual(["Try again"]);
        expect(mounted(view, WorkspaceSettingsGroup)).toBeUndefined();
        expect(mounted(view, ShareTab)).toBeUndefined();
      }
    });

    it("reads again from Try again, and shows the page once the read lands", async () => {
      const { harness, heal } = failing(`${ON_ALPHA}&group=backup`);
      const view = await drawLoaded(harness);
      heal();

      (attrsOf(required(byId(pane(view).content, "options-load-error"))).actions as { onclick: () => void }[])[0].onclick();
      await settle();

      expect(optionsReads(harness)).toHaveLength(2);
      const healed = harness.draw();
      expect(byId(pane(healed).content, "options-load-error")).toBeUndefined();
      expect(attrsOf(required(mounted(healed, WorkspaceSettingsGroup))).group).toBe("backup");
    });

    it("asks for a restart, not another try, when this window's login lapsed", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=general`, {
        respond: (url) => (url.endsWith("/options") ? jsonResponse({ error: "Unauthorized" }, 401) : undefined),
      });
      const view = await drawLoaded(harness);

      const notice = required(byId(pane(view).content, "options-load-error"));
      expect((attrsOf(notice).actions as { label: string }[]).map((action) => action.label)).toEqual([
        "Restart Imbue Studio",
      ]);
      expect(attrsOf(notice).isBugReportOffered).toBe(false);
    });

    it("still opens Permissions, which has a read and a failure state of its own", async () => {
      const { harness } = failing(`${ON_ALPHA}&group=permissions`);
      const view = await drawLoaded(harness);

      expect(byId(pane(view).content, "options-load-error")).toBeUndefined();
      const page = required(mounted(view, PermissionsTab));
      expect((attrsOf(page).model as PermissionsModel).agentId).toBe(ALPHA);
      // The name is the options load's; the page stands without it.
      expect(attrsOf(page).workspaceName).toBe("");
    });
  });

  describe("Permissions", () => {
    it("mounts the Permissions page without waiting for the options, on the section the URL names", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=permissions&perm_section=conn:slack:`);

      const loading = harness.draw();
      const page = required(mounted(loading, PermissionsTab));
      expect(attrsOf(page).model).toBeInstanceOf(PermissionsModel);
      expect((attrsOf(page).model as PermissionsModel).agentId).toBe(ALPHA);
      expect(attrsOf(page).requestedSection).toBe("conn:slack:");
      expect(attrsOf(page).workspaceName).toBe("");
      expect(allText(pane(loading).content)).not.toContain("Loading workspace settings...");
      expect(classTokensOf(pane(loading).content)).toEqual(expect.arrayContaining(["flex", "flex-col"]));
      await settle();

      const loaded = required(mounted(harness.draw(), PermissionsTab));
      expect(attrsOf(loaded).workspaceName).toBe("alpha");
      expect(attrsOf(loaded).model).toBe(attrsOf(page).model);
    });

    it("leaves the section to the page when the URL names none", () => {
      const page = required(mounted(openSettings(`${ON_ALPHA}&group=permissions`).draw(), PermissionsTab));

      expect(attrsOf(page).requestedSection).toBeNull();
    });

    it("writes the section the page moves to, and opens a request to review over Settings", () => {
      const harness = openSettings(`${ON_ALPHA}&group=permissions`);
      const openInbox = vi.spyOn(harness.shell, "openInbox").mockImplementation(() => undefined);
      const page = required(mounted(harness.draw(), PermissionsTab));

      (attrsOf(page).onSelectSection as (section: string) => void)("add-connection");
      expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
      expect(queryOf(harness.route.current)).toEqual({
        account: ALICE.user_id,
        workspace: ALPHA,
        tab: "workspace",
        group: "permissions",
        perm_section: "add-connection",
      });

      (attrsOf(page).onReviewRequest as (requestId: string) => void)("evt-1");
      expect(openInbox.mock.calls).toEqual([[{ selected: "evt-1" }]]);
    });

    it("re-reads the permissions when the pending requests change, since nothing re-creates the page", async () => {
      // Settings stays mounted under the request popup, so the way back from answering a request never mounts
      // the page again: the waiting list and the grant it made would stay as they were.
      const harness = openSettings(`${ON_ALPHA}&group=permissions`);
      (attrsOf(required(mounted(harness.draw(), PermissionsTab))).model as PermissionsModel).ensureLoaded();
      await settle();
      harness.draw();
      expect(permissionsReads(harness)).toHaveLength(1);

      harness.draw();
      await settle();
      expect(permissionsReads(harness)).toHaveLength(1);

      harness.shell.stores.requests.applyRequestsMessage({
        type: "requests",
        count: 1,
        request_ids: ["evt-new"],
        workspace_agent_ids: [ALPHA],
      });
      harness.draw();
      await settle();

      expect(permissionsReads(harness)).toHaveLength(2);
    });
  });

  describe("Sharing", () => {
    function sharePage(view: AnyVnode[]): AnyVnode {
      return required(mounted(view, ShareTab));
    }

    it("asks for the workspace's connections only after the sharing reads, which the page is waiting on", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`);
      harness.draw();
      await settle();
      harness.draw();
      await settle();
      harness.draw();

      const sharingReads = harness.requests
        .map((url, index) => (url.startsWith("/api/v1/workspace-sharing/") ? index : -1))
        .filter((index) => index >= 0);
      const permissionsRead = harness.requests.findIndex((url) => url.endsWith("/permissions"));
      expect(sharingReads.length).toBeGreaterThan(0);
      expect(permissionsReads(harness)).toHaveLength(1);
      expect(permissionsRead).toBeGreaterThan(Math.max(...sharingReads.slice(0, 2)));
    });

    it("still asks for the workspace's connections when the sharing reads fail with nothing to draw", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`, {
        respond: (url) =>
          url.startsWith("/api/v1/workspace-sharing/") ? jsonResponse({ error: "relay down" }, 502) : undefined,
      });
      harness.draw();
      await settle();
      const model = attrsOf(sharePage(harness.draw())).share as SharePanelModel;
      await settle();
      harness.draw();

      expect(model.isSharingDocumentKnown).toBe(false);
      expect(permissionsReads(harness)).toHaveLength(1);
    });

    it("mounts the Sharing page on the workspace's share model, name, and colour", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`);
      expect(allText(pane(harness.draw()).content)).toContain("Loading workspace settings...");
      expect(mounted(harness.draw(), ShareTab)).toBeUndefined();
      await settle();

      const view = harness.draw();
      const page = sharePage(view);
      expect(attrsOf(page).workspaceName).toBe("alpha");
      expect(attrsOf(page).accent).toBe("#aabbcc");
      expect(classTokensOf(pane(view).content)).toEqual(expect.arrayContaining(["flex", "flex-col"]));

      // The share model is the one the workspace's options made, which General reads from too.
      click(railEntry(view, "data-settings-group", "general"));
      const model = attrsOf(required(mounted(harness.draw(), WorkspaceSettingsGroup))).model as WorkspaceOptionsModel;
      expect(attrsOf(page).share).toBe(model.share);
      expect(model.share?.appTargets).toEqual(["web", "docs"]);
    });

    it("leaves the connections out until the permissions are known, and reads them without being asked", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`);
      harness.draw();
      await settle();

      // The first draw with the options in hand starts the read; the page does not wait for it.
      expect(attrsOf(sharePage(harness.draw())).connections).toBeNull();
      expect(permissionsReads(harness)).toEqual([`/ui/api/workspaces/${ALPHA}/permissions`]);
      await settle();

      const connections = attrsOf(sharePage(harness.draw())).connections as { service_name: string }[];
      expect(connections.map((connection) => connection.service_name)).toEqual(["slack"]);
      expect(permissionsReads(harness)).toHaveLength(1);
    });

    it("leaves the connections out when the permissions cannot be read", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`, {
        respond: (url) => (url.endsWith("/permissions") ? jsonResponse({ error: "no gateway" }, 502) : undefined),
      });
      harness.draw();
      await settle();
      harness.draw();
      await settle();

      expect(attrsOf(sharePage(harness.draw())).connections).toBeNull();
    });

    it("opens a connection's own page in Permissions from its tile", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing`);
      const view = await drawLoaded(harness);

      (attrsOf(sharePage(view)).onOpenConnection as (sectionId: string) => void)("conn:slack:");

      expect(queryOf(harness.route.current)).toEqual({
        account: ALICE.user_id,
        workspace: ALPHA,
        tab: "workspace",
        group: "permissions",
        perm_section: "conn:slack:",
      });
      expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
    });

    describe("the target and the URL's share_target", () => {
      function currentTarget(harness: Harness): string {
        return (attrsOf(sharePage(harness.draw())).share as SharePanelModel).currentTarget;
      }

      it("holds the URL's target until the share model exists, then selects it", async () => {
        // The share model is only made once the options load, so a deep link's target has to survive the draws
        // before that.
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        harness.draw();
        harness.draw();
        await settle();

        expect(currentTarget(harness)).toBe("web");
        expect(queryOf(harness.route.current).share_target).toBe("web");
        expect(harness.routeWrites).toEqual([]);
        expect(railEntries(harness.draw(), "data-share-target").filter(([, isSelected]) => isSelected)).toEqual([
          ["web", true],
        ]);
      });

      it("starts on the whole workspace when the URL names no target", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing`);
        await drawLoaded(harness);

        expect(currentTarget(harness)).toBe("system_interface");
        expect(harness.routeWrites).toEqual([]);
      });

      it("writes back a target the page moved to itself, and does not fight it", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        const view = await drawLoaded(harness);

        click(railEntry(view, "data-share-target", "docs"));
        harness.draw();

        expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
        expect(queryOf(harness.route.current).share_target).toBe("docs");
        expect(currentTarget(harness)).toBe("docs");
        harness.draw();
        expect(harness.routeWrites).toHaveLength(1);
      });

      it("ends on the page's move even when a draw lands before the route write does", async () => {
        // A route set does not land synchronously: a draw in between still reads the target the page left.
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        const view = await drawLoaded(harness);
        const stale = harness.route.current;

        click(railEntry(view, "data-share-target", "docs"));
        harness.draw();
        const written = harness.route.current;
        harness.route.current = stale;
        harness.draw();
        harness.route.current = written;

        expect(currentTarget(harness)).toBe("docs");
        expect(queryOf(harness.route.current).share_target).toBe("docs");
        expect(harness.routeWrites).toHaveLength(1);
      });

      it("clears the param when the page moves back to the whole workspace", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        const view = await drawLoaded(harness);

        click(railEntry(view, "data-share-target", "system_interface"));
        harness.draw();

        expect(queryOf(harness.route.current)).toEqual({
          account: ALICE.user_id,
          workspace: ALPHA,
          tab: "workspace",
          group: "sharing",
        });
      });

      it("selects the target a changed URL names, as a workspace's own Share button changes it", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        await drawLoaded(harness);

        harness.route.current = `${ON_ALPHA}&group=sharing&share_target=docs`;

        expect(currentTarget(harness)).toBe("docs");
        expect(harness.routeWrites).toEqual([]);
      });

      it("goes back to the whole workspace when the URL drops its target, as the bar's Share does", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
        await drawLoaded(harness);

        harness.route.current = `${ON_ALPHA}&group=sharing`;

        expect(currentTarget(harness)).toBe("system_interface");
        expect(harness.routeWrites).toEqual([]);
      });

      it("lands on the whole workspace for a target it does not know, and clears the param", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=retired-app`);
        await drawLoaded(harness);

        expect(currentTarget(harness)).toBe("system_interface");
        expect(queryOf(harness.route.current)).toEqual({
          account: ALICE.user_id,
          workspace: ALPHA,
          tab: "workspace",
          group: "sharing",
        });
        expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
      });

      it("applies the target on whichever entry is shown, so Sharing opens on it later", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=general&share_target=docs`);
        const view = await drawLoaded(harness);

        click(railEntry(view, "data-settings-group", "sharing"));

        expect(currentTarget(harness)).toBe("docs");
      });
    });

    describe("the bubble an arrival asks for with the URL's coach", () => {
      function shareModel(harness: Harness): SharePanelModel {
        return attrsOf(sharePage(harness.draw())).share as SharePanelModel;
      }

      it("holds the param until the share model exists, then hands the mark over and takes it out of the URL", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&coach=mobile-access`);
        harness.draw();
        harness.draw();
        expect(queryOf(harness.route.current).coach).toBe("mobile-access");
        expect(harness.routeWrites).toEqual([]);
        await settle();

        expect(shareModel(harness).coachMark).toBe("mobile-access");
        expect(queryOf(harness.route.current)).toEqual({
          account: ALICE.user_id,
          workspace: ALPHA,
          tab: "workspace",
          group: "sharing",
        });
        expect(harness.routeWrites.map((write) => write.options)).toEqual([{ replace: true }]);
      });

      it("asks once per request, however many draws pass before the removal lands", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&coach=mobile-access`);
        const stale = harness.route.current;
        const share = attrsOf(sharePage(await drawLoaded(harness))).share as SharePanelModel;
        share.dismissCoachMark();
        // A route set does not land synchronously: draws in between still read the param.
        const written = harness.route.current;
        harness.route.current = stale;
        harness.draw();
        harness.draw();
        harness.route.current = written;
        harness.draw();

        expect(share.coachMark).toBeNull();
        expect(harness.routeWrites).toHaveLength(1);
      });

      it("asks again when the same link lands again", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&coach=mobile-access`);
        await drawLoaded(harness);
        const share = shareModel(harness);
        share.dismissCoachMark();
        harness.draw();

        harness.route.current = `${ON_ALPHA}&group=sharing&coach=mobile-access`;

        expect(shareModel(harness)).toBe(share);
        expect(share.coachMark).toBe("mobile-access");
        expect(queryOf(harness.route.current).coach).toBeUndefined();
      });

      it("draws none when Settings is opened without the param", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing`);
        await drawLoaded(harness);

        expect(shareModel(harness).coachMark).toBeNull();
        expect(harness.routeWrites).toEqual([]);
      });

      it("drops a mark Sharing does not know, still taking it out of the URL", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&coach=a-mark-from-a-newer-workspace`);
        await drawLoaded(harness);

        expect(shareModel(harness).coachMark).toBeNull();
        expect(queryOf(harness.route.current).coach).toBeUndefined();
      });

      it("takes the mark out and clears a target it does not know in one write", async () => {
        // Two writes in a row would each be computed from the route as it was, and one would undo the other.
        const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=retired-app&coach=mobile-access`);
        await drawLoaded(harness);

        expect(shareModel(harness).coachMark).toBe("mobile-access");
        expect(queryOf(harness.route.current)).toEqual({
          account: ALICE.user_id,
          workspace: ALPHA,
          tab: "workspace",
          group: "sharing",
        });
        expect(harness.routeWrites).toHaveLength(1);
      });

      it("lets go of the mark with the workspace's models, as a change of pick or a close of Settings drops them", async () => {
        const harness = openSettings(`${ON_ALPHA}&group=sharing&coach=mobile-access`);
        const view = await drawLoaded(harness);
        const alphaShare = shareModel(harness);
        expect(alphaShare.coachMark).toBe("mobile-access");

        pick(view, BRAVO);
        await drawLoaded(harness);
        pick(harness.draw(), ALPHA);
        await drawLoaded(harness);

        expect(shareModel(harness)).not.toBe(alphaShare);
        expect(shareModel(harness).coachMark).toBeNull();
      });
    });
  });

  describe("the models of the picked workspace", () => {
    it("are swapped when the pick changes: the old options model disposed, the permissions list re-registered", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=permissions`);
      const register = vi.spyOn(harness.shell, "registerWaitingRequestList");
      const unregister = vi.spyOn(harness.shell, "unregisterWaitingRequestList");
      const alphaPermissions = attrsOf(required(mounted(await drawLoaded(harness), PermissionsTab)))
        .model as PermissionsModel;
      click(railEntry(harness.draw(), "data-settings-group", "general"));
      const alphaModel = attrsOf(required(mounted(harness.draw(), WorkspaceSettingsGroup)))
        .model as WorkspaceOptionsModel;
      const dispose = vi.spyOn(alphaModel, "dispose");
      // The request popup reaches the waiting list through the shell.
      expect(register.mock.calls).toEqual([[alphaPermissions]]);
      expect(unregister).not.toHaveBeenCalled();

      pick(harness.draw(), BRAVO);
      const view = await drawLoaded(harness);

      expect(dispose).toHaveBeenCalledTimes(1);
      const bravoModel = attrsOf(required(mounted(view, WorkspaceSettingsGroup))).model as WorkspaceOptionsModel;
      expect(bravoModel).not.toBe(alphaModel);
      expect(bravoModel.agentId).toBe(BRAVO);
      expect(bravoModel.data?.name).toBe("bravo");
      expect(harness.requests.filter((url) => url.endsWith("/options"))).toEqual([
        `/ui/api/workspaces/${ALPHA}/options`,
        `/ui/api/workspaces/${BRAVO}/options`,
      ]);

      click(railEntry(view, "data-settings-group", "permissions"));
      const bravoPermissions = attrsOf(required(mounted(harness.draw(), PermissionsTab))).model as PermissionsModel;
      expect(bravoPermissions).not.toBe(alphaPermissions);
      expect(bravoPermissions.agentId).toBe(BRAVO);
      expect(unregister.mock.calls).toEqual([[alphaPermissions]]);
      expect(register.mock.calls).toEqual([[alphaPermissions], [bravoPermissions]]);
    });

    it("start the new pick on the whole workspace, not the last one's Sharing target", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=sharing&share_target=web`);
      const view = await drawLoaded(harness);

      pick(view, BRAVO);
      const bravo = await drawLoaded(harness);

      expect((attrsOf(required(mounted(bravo, ShareTab))).share as SharePanelModel).currentTarget).toBe(
        "system_interface",
      );
      expect(queryOf(harness.route.current).share_target).toBeUndefined();
    });

    it("are kept across the other tabs, so coming back reads nothing again", async () => {
      const harness = openSettings(ON_ALPHA);
      const model = attrsOf(required(mounted(await drawLoaded(harness), WorkspaceSettingsGroup))).model;

      click(byId(harness.draw(), "settings-tab-accounts"));
      harness.draw();
      click(byId(harness.draw(), "settings-tab-workspace"));

      expect(attrsOf(required(mounted(harness.draw(), WorkspaceSettingsGroup))).model).toBe(model);
      expect(harness.requests.filter((url) => url.endsWith("/options"))).toHaveLength(1);
    });

    it("are dropped when Settings closes", async () => {
      const harness = openSettings(`${ON_ALPHA}&group=permissions`);
      const unregister = vi.spyOn(harness.shell, "unregisterWaitingRequestList");
      const permissions = attrsOf(required(mounted(await drawLoaded(harness), PermissionsTab))).model;
      click(railEntry(harness.draw(), "data-settings-group", "general"));
      const model = attrsOf(required(mounted(harness.draw(), WorkspaceSettingsGroup))).model as WorkspaceOptionsModel;
      const dispose = vi.spyOn(model, "dispose");

      harness.page.onremove();

      expect(dispose).toHaveBeenCalledTimes(1);
      expect(unregister.mock.calls).toEqual([[permissions]]);
    });

    it("are not made at all while another tab is the one shown", async () => {
      const harness = openSettings(`${OVER_ALPHA}&tab=studio`);
      const register = vi.spyOn(harness.shell, "registerWaitingRequestList");

      await drawLoaded(harness);

      expect(harness.requests.filter((url) => url.endsWith("/options"))).toEqual([]);
      expect(register).not.toHaveBeenCalled();
    });
  });
});

describe("Settings › Workspace & apps: a workspace or app shared with the account", () => {
  const WORKSPACE_TAB = `/settings?account=${ALICE.user_id}&tab=workspace`;

  it("shows only About, on the share, and reads none of an owner's settings", async () => {
    const harness = openSettings(`${WORKSPACE_TAB}&settings_workspace=${ORCHARD_KEY}&group=sharing`, {
      entries: [ALPHA_ENTRY, orchard()],
    });
    const view = await drawLoaded(harness);
    const { rail } = pane(view);

    expect(attrsOf(picker(view)).value).toBe(ORCHARD_KEY);
    expect(railEntries(view, "data-settings-group")).toEqual([]);
    const entries = collectVnodes(rail).filter((vnode) => vnode.tag === "button");
    expect(entries.map((entry) => [allText(entry), attrsOf(entry)["aria-pressed"]])).toEqual([["About", "true"]]);

    const about = required(mounted(view, SharedWorkspaceAbout));
    expect((attrsOf(about).entry as UiWorkspaceEntry).id).toBe(ORCHARD_KEY);
    expect(mounted(view, ShareTab)).toBeUndefined();
    expect(mounted(view, WorkspaceSettingsGroup)).toBeUndefined();
    expect(harness.requests.filter((url) => url.includes("/ui/api/workspaces/"))).toEqual([]);
  });

  it("shows the About page of the share a picked app came from", () => {
    const view = openSettings(`${WORKSPACE_TAB}&settings_workspace=${TOOLS_KEY}&app=board`, {
      entries: [ALPHA_ENTRY, tools()],
    }).draw();

    expect(attrsOf(picker(view)).value).toBe(`${TOOLS_KEY}#board`);
    expect((attrsOf(required(mounted(view, SharedWorkspaceAbout))).entry as UiWorkspaceEntry).id).toBe(TOOLS_KEY);
  });

  it("drops the models of the workspace it was showing before", async () => {
    const harness = openSettings(`${OVER_ALPHA}&tab=workspace`, { entries: [ALPHA_ENTRY, orchard()] });
    const unregister = vi.spyOn(harness.shell, "unregisterWaitingRequestList");
    const model = attrsOf(required(mounted(await drawLoaded(harness), WorkspaceSettingsGroup)))
      .model as WorkspaceOptionsModel;
    const dispose = vi.spyOn(model, "dispose");

    pick(harness.draw(), ORCHARD_KEY);
    const view = harness.draw();

    expect(mounted(view, SharedWorkspaceAbout)).toBeDefined();
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(unregister).toHaveBeenCalledTimes(1);
    // What the window shows is still alpha; only Settings moved.
    expect(queryOf(harness.route.current).workspace).toBe(ALPHA);
  });
});
