import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { forgetWarmedPermissionsOverview, readWarmedPermissionsOverview } from "../../models/permissionsPrefetch";
import { MonogramTile } from "../components/MonogramTile";
import { DeviceResourceWarning } from "./ResourceWarning";
import { ShellState } from "./shell-state";
import type { ChromeAnchor } from "./shell-state";
import { SHARE_NOT_YET_CREATED_MESSAGE, Titlebar } from "./Titlebar";
import { TitlebarStatusPill } from "./titlebar-status";
import { TrafficLights } from "./TrafficLights";
import type { AnyVnode } from "../../testing";
import {
  CREATE_ATTEMPT_ID,
  OUT_OF_DATE_UPDATE,
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  classTokensOf,
  collectText,
  collectVnodes,
  createAttemptEntry,
  namedSharedOwner,
  notificationEntry,
  publishPendingRequests,
  publishUpdates,
  required,
  secondAccountEntry,
  sharedWorkspaceEntry,
  thirdAccountEntry,
  withAttr,
  workspacesMessage,
} from "../../testing";
import type { UiWorkspaceEntry } from "../../channel/messages";

const WORKSPACE_ID = "agent-ab12";
const WORKSPACE_PATH = `/workspace/${WORKSPACE_ID}`;
const HOME_PATH = "/account/local/";
const CREATING_PATH = `/creating/${CREATE_ATTEMPT_ID}`;

const OWNED: UiWorkspaceEntry = { ...workspacesMessage().workspaces[0], id: WORKSPACE_ID };
const ATTEMPT = createAttemptEntry();
const SAM = namedSharedOwner();
/** A workspace someone else shared with the account, which it accepted. */
const SHARED = sharedWorkspaceEntry({ status: "accepted", owner: SAM });
const SHARED_PATH = `/workspace/${SHARED.id}`;

const ALICE = accountEntry({ display_name: "Alice Nguyen" });
const BOB = secondAccountEntry();
const CAROL = thirdAccountEntry();

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A shell on a Mac whose account owns "alpha" and is making "gamma". `window` is stubbed because the Electron
 * bridge feature-detects `window.mindsNative` while deciding whether to draw the window controls. */
function makeShell(workspaces: UiWorkspaceEntry[] = [OWNED, ATTEMPT]): ShellState {
  vi.stubGlobal("window", {});
  const shell = new ShellState(createEmptyStores());
  shell.isMac = true;
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces }));
  return shell;
}

/** Render the bar as the Shell does on `route` (a path with its query). */
function renderBar(shell: ShellState, route: string): AnyVnode[] {
  vi.spyOn(m.route, "get").mockReturnValue(route);
  const instance = Titlebar() as unknown as m.Component;
  const routePath = route.split("?")[0];
  const root = (instance.view as unknown as (v: m.Vnode) => unknown).call(
    instance,
    m(instance, { shell, routePath } as unknown as m.Attributes) as m.Vnode,
  );
  return collectVnodes(root);
}

/** Expands one level of an unrendered component vnode by calling its own
 * view() -- TitlebarButton computes its final class string there, so an
 * unrendered `m(TitlebarButton, ...)` vnode's own attrs carry no class yet. */
function renderComponentVnode(vnode: AnyVnode): AnyVnode {
  const component = (vnode.tag as unknown as () => m.Component)();
  return (component.view as unknown as (v: unknown) => AnyVnode).call(
    component,
    { attrs: vnode.attrs, children: vnode.children },
  );
}

function barButton(nodes: AnyVnode[], id: string): AnyVnode {
  const button = byId(nodes, id);
  expect(button, `no titlebar button ${id}`).toBeDefined();
  return button as AnyVnode;
}

/** The classes a bar control is painted with, whether it is a TitlebarButton or a plain element. */
function paintedTokens(vnode: AnyVnode): string[] {
  return classTokensOf(typeof vnode.tag === "function" ? renderComponentVnode(vnode) : vnode);
}

function press(nodes: AnyVnode[], id: string): void {
  (attrsOf(barButton(nodes, id)).onclick as () => void)();
}

function ids(nodes: AnyVnode[]): unknown[] {
  return nodes.map((vnode) => attrsOf(vnode).id).filter((id) => id !== undefined);
}

function iconNames(node: AnyVnode): unknown[] {
  return withAttr(node.children, "name").map((vnode) => attrsOf(vnode).name);
}

function statusPill(nodes: AnyVnode[]): AnyVnode | undefined {
  return nodes.find((vnode) => vnode.tag === TitlebarStatusPill);
}

function publishUpdateOnOffer(shell: ShellState): void {
  publishUpdates(shell, { [WORKSPACE_ID]: OUT_OF_DATE_UPDATE }, false);
}

describe("Titlebar trail", () => {
  it("leads with Home, the account, and the workspace on screen, and no page crumb", () => {
    const nodes = renderBar(makeShell(), WORKSPACE_PATH);

    const trail = ids(nodes).filter((id) =>
      ["home-btn", "account-switcher-btn", "workspace-switcher-btn", "notifications-toggle"].includes(String(id)),
    );
    expect(trail).toEqual(["home-btn", "account-switcher-btn", "workspace-switcher-btn", "notifications-toggle"]);
    expect(attrsOf(barButton(nodes, "ws-crumb")).hidden).toBe(false);
    expect(attrsOf(barButton(nodes, "page-crumb")).hidden).toBe(true);
    expect(ids(nodes)).not.toContain("page-crumb-name");

    const workspace = barButton(nodes, "workspace-switcher-btn");
    expect(attrsOf(workspace)["aria-label"]).toBe("Switch workspace");
    expect(attrsOf(workspace)["aria-haspopup"]).toBe("menu");
    expect(allText(workspace)).toBe("alpha");
    // The dot is tinted for the bar, which is painted in the workspace's own colour.
    const swatch = required(withAttr(workspace.children, "data-workspace-swatch")[0]);
    expect(attrsOf(swatch)["data-workspace-swatch"]).toBe("#aabbcc");
    expect(classTokensOf(swatch)).toContain("workspace-swatch-on-bar");
  });

  it("offers 'Open workspace' on Home, with no dot, no Share and no status", () => {
    const shell = makeShell();
    publishUpdateOnOffer(shell);
    const nodes = renderBar(shell, HOME_PATH);

    const workspace = barButton(nodes, "workspace-switcher-btn");
    expect(allText(workspace)).toBe("Open workspace");
    expect(attrsOf(workspace)["aria-label"]).toBe("Open a workspace");
    expect(attrsOf(workspace)["data-tooltip"]).toBe("Open a workspace");
    expect(withAttr(workspace.children, "data-workspace-swatch")).toEqual([]);
    expect(classTokensOf(required(byId(workspace.children, "workspace-switcher-name")))).toContain("text-secondary");
    expect(attrsOf(barButton(nodes, "ws-crumb")).hidden).toBe(false);
    expect(ids(nodes)).not.toContain("share-btn");
    expect(statusPill(nodes)).toBeUndefined();
  });

  it("keeps the page crumb in the workspace button's place on the other hub pages", () => {
    const PAGES: [string, string][] = [
      ["/account/local/workspaces/destroyed", "Recently destroyed"],
      ["/account/local/workspaces/shared", "Shared workspaces"],
      ["/account/local/create", "New workspace"],
    ];
    for (const [route, label] of PAGES) {
      const nodes = renderBar(makeShell(), route);
      expect(ids(nodes), route).not.toContain("workspace-switcher-btn");
      expect(attrsOf(barButton(nodes, "ws-crumb")).hidden, route).toBe(true);
      expect(attrsOf(barButton(nodes, "page-crumb")).hidden, route).toBe(false);
      expect(allText(barButton(nodes, "page-crumb-name")), route).toBe(label);
      // The account stays named, and Share has no workspace to speak for.
      expect(ids(nodes), route).toContain("account-switcher-btn");
      expect(ids(nodes), route).not.toContain("share-btn");
    }
  });

  it("names the workspace being made on its creation page, from the attempt's own row", () => {
    const shell = makeShell();
    publishUpdateOnOffer(shell);
    const nodes = renderBar(shell, CREATING_PATH);

    const workspace = barButton(nodes, "workspace-switcher-btn");
    expect(allText(workspace)).toBe("gamma");
    expect(attrsOf(workspace)["aria-label"]).toBe("Switch workspace");
    expect(attrsOf(barButton(nodes, "page-crumb")).hidden).toBe(true);
    // The workspace a status would speak for does not exist yet.
    expect(statusPill(nodes)).toBeUndefined();
  });

  it("shows the workspace's status after its button, in a workspace context", () => {
    const shell = makeShell();
    publishUpdateOnOffer(shell);

    const pill = required(statusPill(renderBar(shell, WORKSPACE_PATH)));

    expect((attrsOf(pill).status as { key: string }).key).toBe("update-out-of-date");
  });

  it("draws the wordmark as the way to the account's hub", () => {
    const shell = makeShell();
    shell.accountId = ALICE.user_id;
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const nodes = renderBar(shell, WORKSPACE_PATH);

    const home = barButton(nodes, "home-btn");
    expect(attrsOf(home)["aria-label"]).toBe("Home");
    expect(withAttr(home.children, "role").map((vnode) => attrsOf(vnode)["aria-label"])).toEqual(["Imbue Studio"]);
    press(nodes, "home-btn");

    expect(routeSet).toHaveBeenCalledWith(`/account/${ALICE.user_id}/`, undefined, undefined);
  });

  it("renders no back button, on any route", () => {
    for (const route of ["/account/local/create", "/account/local/workspaces/destroyed", WORKSPACE_PATH, HOME_PATH]) {
      expect(ids(renderBar(makeShell(), route)), route).not.toContain("back-btn");
    }
  });
});

describe("the bar's account button", () => {
  function shellOn(accountId: string): ShellState {
    const shell = makeShell();
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], accountId, [CAROL]));
    shell.accountId = accountId;
    return shell;
  }

  it("names the window's account by its email rather than its display name, and the device as 'This device'", () => {
    const account = barButton(renderBar(shellOn(ALICE.user_id), WORKSPACE_PATH), "account-switcher-btn");
    expect(allText(required(byId(account.children, "account-switcher-name")))).toBe(ALICE.email);
    expect(allText(account)).not.toContain("Alice Nguyen");
    expect(attrsOf(account)["data-account-id"]).toBe(ALICE.user_id);
    expect(attrsOf(account)["aria-label"]).toBe("Switch account");
    expect(withAttr(account.children, "data-account-signed-out")).toEqual([]);

    const device = barButton(renderBar(shellOn("local"), HOME_PATH), "account-switcher-btn");
    expect(allText(device)).toBe("This device");
  });

  it("carries the signed-out mark while the account has no session here", () => {
    const account = barButton(renderBar(shellOn(CAROL.user_id), WORKSPACE_PATH), "account-switcher-btn");

    expect(allText(required(byId(account.children, "account-switcher-name")))).toBe(CAROL.email);
    expect(withAttr(account.children, "data-account-signed-out").map(allText)).toEqual(["(signed out)"]);
  });

  it("opens the account menu, and puts it away on a second press", () => {
    const shell = shellOn(ALICE.user_id);
    const closed = renderBar(shell, WORKSPACE_PATH);
    expect(attrsOf(barButton(closed, "account-switcher-btn"))["aria-expanded"]).toBe("false");

    press(closed, "account-switcher-btn");
    expect(shell.isAccountSwitcherOpen).toBe(true);
    const open = renderBar(shell, WORKSPACE_PATH);
    expect(attrsOf(barButton(open, "account-switcher-btn"))["aria-expanded"]).toBe("true");

    press(open, "account-switcher-btn");
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });
});

describe("the bar's workspace button", () => {
  it("names one of the account's own workspaces with no owner beside it", () => {
    const workspace = barButton(renderBar(makeShell(), WORKSPACE_PATH), "workspace-switcher-btn");

    expect(allText(required(byId(workspace.children, "workspace-switcher-name")))).toBe("alpha");
  });

  it("follows a shared workspace's name with its owner", () => {
    const nodes = renderBar(makeShell([OWNED, SHARED]), SHARED_PATH);

    const name = required(byId(barButton(nodes, "workspace-switcher-btn").children, "workspace-switcher-name"));
    expect(collectText(name).join("")).toBe("Beta (Sam Okafor)");
    // The owner is an aside in the muted colour, not part of the name.
    const aside = required(collectVnodes(name.children).find((vnode) => allText(vnode) === " (Sam Okafor)"));
    expect(classTokensOf(aside)).toContain("text-secondary");
    const workspace = barButton(nodes, "workspace-switcher-btn");
    expect(withAttr(workspace.children, "data-workspace-swatch").map((vnode) => attrsOf(vnode)["data-workspace-swatch"])).toEqual([
      SHARED.accent,
    ]);
  });

  it("names a shared app open on its own by its tile and display name, followed by its owner", () => {
    const notes = required(SHARED.share?.targets.find((target) => target.app === "notes"));
    const appShare = sharedWorkspaceEntry({ status: "accepted", owner: SAM, targets: [notes] });
    const nodes = renderBar(makeShell([OWNED, appShare]), `/workspace/${appShare.id}`);

    const workspace = barButton(nodes, "workspace-switcher-btn");
    expect(collectText(required(byId(workspace.children, "workspace-switcher-name"))).join("")).toBe(
      "Notes (Sam Okafor)",
    );
    const tiles = collectVnodes(workspace.children).filter((vnode) => vnode.tag === MonogramTile);
    expect(tiles.map((tile) => attrsOf(tile).name)).toEqual(["Notes"]);
    expect(withAttr(workspace.children, "data-workspace-swatch")).toEqual([]);
  });

  it("opens the workspace menu, and puts it away on a second press", () => {
    const shell = makeShell();
    const closed = renderBar(shell, WORKSPACE_PATH);
    expect(attrsOf(barButton(closed, "workspace-switcher-btn"))["aria-expanded"]).toBe("false");

    press(closed, "workspace-switcher-btn");
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    const open = renderBar(shell, WORKSPACE_PATH);
    expect(attrsOf(barButton(open, "workspace-switcher-btn"))["aria-expanded"]).toBe("true");

    press(open, "workspace-switcher-btn");
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("opens the menu from Home as well", () => {
    const shell = makeShell();

    press(renderBar(shell, HOME_PATH), "workspace-switcher-btn");

    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });
});

describe("Titlebar right cluster", () => {
  it("offers the notification bell as the one 'something needs you' entry", () => {
    // The bell's resolution-based badge is the chrome's single such signal.
    // Requests themselves never got a titlebar entry, so that id stays
    // forbidden below.
    const shell = makeShell();
    shell.stores.notifications.applyNotificationsMessage({ entries: [], unresolved_count: 3 });
    const nodes = renderBar(shell, WORKSPACE_PATH);
    expect(ids(nodes)).not.toContain("requests-toggle");
    expect(ids(nodes)).toContain("help-toggle");

    const bell = barButton(nodes, "notifications-toggle");
    expect(attrsOf(bell)["aria-label"]).toBe("Notifications");
    expect(attrsOf(bell)["data-tooltip"]).toBe("Notifications");
    expect(attrsOf(bell)["aria-expanded"]).toBe("false");
    expect(iconNames(bell)).toEqual(["bell"]);

    // The badge renders the unresolved count (its component caps at 99+).
    expect(attrsOf(barButton(nodes, "notifications-badge")).count).toBe(3);
  });

  it("hides the badge entirely while nothing is unresolved", () => {
    const nodes = renderBar(makeShell(), WORKSPACE_PATH);
    expect(ids(nodes)).toContain("notifications-toggle");
    expect(ids(nodes)).not.toContain("notifications-badge");
  });

  it("opens the feed through the shell's switch (no navigation of its own), and reads expanded while open", () => {
    // The switch, not a bare open: it puts a centered app modal away first,
    // so the feed never raises beneath that modal's backdrop.
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const shell = makeShell();
    const switchToNotifications = vi.spyOn(shell, "switchToNotifications").mockImplementation(() => undefined);
    const closed = renderBar(shell, WORKSPACE_PATH);

    press(closed, "notifications-toggle");
    expect(switchToNotifications).toHaveBeenCalledTimes(1);
    // The feed is a popover over the current surface, never a route.
    expect(routeSet).not.toHaveBeenCalled();
    expect(attrsOf(barButton(closed, "notifications-toggle"))["aria-expanded"]).toBe("false");

    shell.isNotificationsOpen = true;
    expect(attrsOf(barButton(renderBar(shell, WORKSPACE_PATH), "notifications-toggle"))["aria-expanded"]).toBe("true");
  });

  it("opens Report a bug through the shell", () => {
    const shell = makeShell();
    const openHelp = vi.spyOn(shell, "openHelp").mockImplementation(() => undefined);
    const nodes = renderBar(shell, WORKSPACE_PATH);

    expect(attrsOf(barButton(nodes, "help-toggle"))["aria-label"]).toBe("Report a bug");
    press(nodes, "help-toggle");

    expect(openHelp).toHaveBeenCalledTimes(1);
  });

  it("orders the right group bell, Report a bug, Settings, Share", () => {
    const right = ids(renderBar(makeShell(), WORKSPACE_PATH)).filter((id) =>
      ["notifications-toggle", "help-toggle", "settings-btn", "share-btn"].includes(String(id)),
    );

    expect(right).toEqual(["notifications-toggle", "help-toggle", "settings-btn", "share-btn"]);
  });
});

describe("the bar's device resource warning", () => {
  it("opens Settings on Studio's This computer section, and hides with the buttons under a flyout", () => {
    const shell = makeShell();
    const openSettings = vi.spyOn(shell, "openSettings").mockImplementation(() => undefined);

    const warning = required(renderBar(shell, WORKSPACE_PATH).find((vnode) => vnode.tag === DeviceResourceWarning));
    expect(attrsOf(warning).extra).toBe("");
    (attrsOf(warning).onOpenSettings as () => void)();
    expect(openSettings).toHaveBeenCalledWith({ tab: "studio", section: "this-computer" });

    shell.openAccountSwitcher();
    const hidden = required(renderBar(shell, WORKSPACE_PATH).find((vnode) => vnode.tag === DeviceResourceWarning));
    expect(attrsOf(hidden).extra).toBe("invisible");
  });
});

describe("the bar's Settings gear", () => {
  it("is a gear, tooltipped like its neighbours, that opens Settings through the shell", () => {
    const shell = makeShell();
    const openSettingsFromBar = vi.spyOn(shell, "openSettingsFromBar").mockImplementation(() => undefined);
    const nodes = renderBar(shell, WORKSPACE_PATH);

    const gear = barButton(nodes, "settings-btn");
    expect(attrsOf(gear)["aria-label"]).toBe("Settings");
    // The delegated [data-tooltip] chrome, not a native title: the two would
    // otherwise show two different tooltips on the same bar.
    expect(attrsOf(gear)["data-tooltip"]).toBe("Settings");
    expect(attrsOf(gear).title).toBeUndefined();
    expect(iconNames(gear)).toEqual(["settings"]);
    press(nodes, "settings-btn");

    expect(openSettingsFromBar).toHaveBeenCalledTimes(1);
  });

  it("is on Home as well, where it is the one way into Settings", () => {
    const shell = makeShell();
    const openSettingsFromBar = vi.spyOn(shell, "openSettingsFromBar").mockImplementation(() => undefined);

    press(renderBar(shell, HOME_PATH), "settings-btn");

    expect(openSettingsFromBar).toHaveBeenCalledTimes(1);
  });

  it("keeps the pending-request dot after the request's notification has been dismissed", () => {
    const shell = makeShell();
    publishPendingRequests(shell, [WORKSPACE_ID]);
    shell.stores.notifications.applyNotificationsMessage({ entries: [], unresolved_count: 0 });

    const gear = barButton(renderBar(shell, WORKSPACE_PATH), "settings-btn");

    expect(byId(gear.children, "permissions-badge")).toBeDefined();
  });

  it("starts reading the workspace's permissions when the gear is pointed at, only while a request waits there", () => {
    vi.stubGlobal("fetch", () => new Promise<Response>(() => undefined));
    forgetWarmedPermissionsOverview();
    const pointAtGear = (shell: ShellState): void =>
      (attrsOf(barButton(renderBar(shell, WORKSPACE_PATH), "settings-btn")).onpointerenter as () => void)();

    pointAtGear(makeShell());
    expect(readWarmedPermissionsOverview(WORKSPACE_ID)).toBeNull();

    const waiting = makeShell();
    publishPendingRequests(waiting, [WORKSPACE_ID]);
    pointAtGear(waiting);
    expect(readWarmedPermissionsOverview(WORKSPACE_ID)).not.toBeNull();
    forgetWarmedPermissionsOverview();
  });

  it("stays undotted for a resolved request, one from a different workspace, and on Home", () => {
    const resolved = makeShell();
    resolved.stores.notifications.applyNotificationsMessage({
      entries: [notificationEntry("n1", { workspace_agent_id: WORKSPACE_ID, is_resolved: true, outcome: "approved" })],
      unresolved_count: 0,
    });
    expect(ids(renderBar(resolved, WORKSPACE_PATH))).not.toContain("permissions-badge");

    const elsewhere = makeShell();
    publishPendingRequests(elsewhere, ["agent-zz99"]);
    expect(ids(renderBar(elsewhere, WORKSPACE_PATH))).not.toContain("permissions-badge");

    const home = makeShell();
    publishPendingRequests(home, [WORKSPACE_ID]);
    expect(ids(renderBar(home, HOME_PATH))).not.toContain("permissions-badge");
  });
});

describe("the bar's Share", () => {
  it("is offered for one of the account's own workspaces and on a creation page, and nowhere else", () => {
    const shell = makeShell([OWNED, ATTEMPT, SHARED]);

    expect(ids(renderBar(shell, WORKSPACE_PATH))).toContain("share-btn");
    expect(ids(renderBar(shell, CREATING_PATH))).toContain("share-btn");
    // A guest cannot share someone else's workspace on.
    expect(ids(renderBar(shell, SHARED_PATH))).not.toContain("share-btn");
    expect(ids(renderBar(shell, HOME_PATH))).not.toContain("share-btn");
    expect(ids(renderBar(shell, "/account/local/workspaces/destroyed"))).not.toContain("share-btn");
  });

  it("is the share glyph and the word, and goes through the shell's Share", () => {
    const shell = makeShell();
    const pressShare = vi.spyOn(shell, "pressShare").mockImplementation(() => undefined);
    const nodes = renderBar(shell, WORKSPACE_PATH);

    const share = barButton(nodes, "share-btn");
    expect(attrsOf(share)["aria-label"]).toBe("Share");
    expect(allText(share)).toBe("Share");
    expect(iconNames(share)).toEqual(["share"]);
    press(nodes, "share-btn");

    expect(pressShare).toHaveBeenCalledTimes(1);
  });
});

describe("Titlebar on a creation page", () => {
  it("answers Share with the check-back dialog, rendered beside the bar rather than inside it", () => {
    const shell = makeShell();
    press(renderBar(shell, CREATING_PATH), "share-btn");
    const nodes = renderBar(shell, CREATING_PATH);

    const dialog = barButton(nodes, "creating-not-yet-dialog");
    expect(allText(dialog)).toContain(SHARE_NOT_YET_CREATED_MESSAGE);
    // The bar re-bases its text tokens for the accent (.titlebar-surface), so
    // a dialog nested in it would paint white on white.
    expect(collectVnodes(barButton(nodes, "minds-titlebar"))).not.toContain(dialog);

    (attrsOf(dialog).onClose as () => void)();
    expect(ids(renderBar(shell, CREATING_PATH))).not.toContain("creating-not-yet-dialog");
  });

  it("shows no dialog until Share is pressed", () => {
    expect(ids(renderBar(makeShell(), CREATING_PATH))).not.toContain("creating-not-yet-dialog");
  });

  it("drops an open dialog once the route leaves the creation page", () => {
    // The creation page enters the finished workspace by itself; a dialog left
    // up on the creation page must not follow it onto the workspace.
    const shell = makeShell();
    press(renderBar(shell, CREATING_PATH), "share-btn");

    expect(ids(renderBar(shell, WORKSPACE_PATH))).not.toContain("creating-not-yet-dialog");
    // Nor does it come back with the page.
    expect(ids(renderBar(shell, CREATING_PATH))).not.toContain("creating-not-yet-dialog");
  });
});

describe("Titlebar under an open flyout", () => {
  const RAISED_BUTTON_IDS = [
    "account-switcher-btn",
    "workspace-switcher-btn",
    "notifications-toggle",
    "help-toggle",
    "settings-btn",
    "share-btn",
  ];
  const OVER_WORKSPACE = `workspace=${WORKSPACE_ID}`;

  function shellWithStatus(): ShellState {
    const shell = makeShell();
    publishUpdateOnOffer(shell);
    return shell;
  }

  it("hides every button the strip raises a copy of, whichever flyout is open", () => {
    // Every flyout raises a copy of all of these over the dimmed bar, so the
    // real ones have to get out of the way together: hiding only the one whose
    // flyout is up would leave the rest ghosting through their raised copies.
    // By visibility, which keeps their boxes and so the rects the copies are drawn at.
    const FLYOUTS: { label: string; route: string; open: (shell: ShellState) => void }[] = [
      { label: "Settings", route: `/settings?${OVER_WORKSPACE}`, open: () => undefined },
      { label: "Report a bug", route: `/help?${OVER_WORKSPACE}`, open: () => undefined },
      { label: "the bell's feed", route: WORKSPACE_PATH, open: (shell) => (shell.isNotificationsOpen = true) },
      { label: "the account menu", route: WORKSPACE_PATH, open: (shell) => shell.openAccountSwitcher() },
      { label: "the workspace menu", route: WORKSPACE_PATH, open: (shell) => shell.openWorkspaceSwitcher() },
    ];

    for (const flyout of FLYOUTS) {
      const shell = shellWithStatus();
      flyout.open(shell);
      const nodes = renderBar(shell, flyout.route);
      for (const id of RAISED_BUTTON_IDS) {
        expect(paintedTokens(barButton(nodes, id)), `${id} stayed painted under ${flyout.label}`).toContain("invisible");
      }
      expect(attrsOf(required(statusPill(nodes))).extra, `the status pill under ${flyout.label}`).toBe("invisible");
    }

    // With nothing open they are all on screen, of course.
    const closed = renderBar(shellWithStatus(), WORKSPACE_PATH);
    for (const id of RAISED_BUTTON_IDS) {
      expect(paintedTokens(barButton(closed, id)), id).not.toContain("invisible");
    }
    expect(attrsOf(required(statusPill(closed))).extra).toBe("");
  });

  it("keeps them painted under the request popup, a centered card that raises no strip", () => {
    const nodes = renderBar(shellWithStatus(), `/inbox?${OVER_WORKSPACE}`);

    for (const id of RAISED_BUTTON_IDS) {
      expect(paintedTokens(barButton(nodes, id)), `${id} went missing under the request popup`).not.toContain(
        "invisible",
      );
    }
  });
});

describe("Titlebar chrome ring", () => {
  const ANCHOR_BUTTON_IDS: Record<ChromeAnchor, string> = {
    "workspace-switcher": "workspace-switcher-btn",
    permissions: "settings-btn",
    settings: "settings-btn",
    share: "share-btn",
    help: "help-toggle",
  };
  // The anchored controls, and the two a tour can never name.
  const BUTTON_IDS = [...new Set(Object.values(ANCHOR_BUTTON_IDS)), "account-switcher-btn", "notifications-toggle"];

  function ringedIds(shell: ShellState): string[] {
    const nodes = renderBar(shell, WORKSPACE_PATH);
    return BUTTON_IDS.filter((id) => paintedTokens(barButton(nodes, id)).includes("animate-pulse"));
  }

  it("rings exactly the control a workspace's tour named, and none otherwise", () => {
    for (const [anchor, buttonId] of Object.entries(ANCHOR_BUTTON_IDS)) {
      const shell = makeShell();
      shell.highlightedChromeAnchor = anchor as ChromeAnchor;

      expect(ringedIds(shell), anchor).toEqual([buttonId]);
      expect(paintedTokens(barButton(renderBar(shell, WORKSPACE_PATH), buttonId)), anchor).toEqual(
        expect.arrayContaining(["ring-2", "ring-accent"]),
      );
    }

    expect(ringedIds(makeShell())).toEqual([]);
  });
});

/** The div holding the bar's own minimize / maximize / close buttons as its direct children. */
function windowButtonCluster(nodes: AnyVnode[]): AnyVnode {
  const cluster = nodes.find(
    (vnode) =>
      Array.isArray(vnode.children) &&
      (vnode.children as AnyVnode[]).some((child) => child !== null && attrsOf(child).id === "close-btn"),
  );
  expect(cluster, "no window button cluster").toBeDefined();
  return cluster as AnyVnode;
}

describe("Titlebar window controls", () => {
  function shellOffMac(flags: { isTrafficLightsDrawn?: boolean; isNativeFrame?: boolean } = {}): ShellState {
    const shell = makeShell();
    shell.isMac = false;
    shell.isTrafficLightsDrawn = flags.isTrafficLightsDrawn ?? false;
    shell.isNativeFrame = flags.isNativeFrame ?? false;
    return shell;
  }

  it("draws its own traffic lights, and none of its buttons, when the platform has no native ones but asks for them", () => {
    const nodes = renderBar(shellOffMac({ isTrafficLightsDrawn: true }), WORKSPACE_PATH);
    const lights = nodes.find((vnode) => vnode.tag === TrafficLights);
    expect(lights).toBeDefined();
    const lightIds = collectVnodes(renderComponentVnode(lights as AnyVnode).children).map((vnode) => attrsOf(vnode).id);
    expect(lightIds).toEqual(["traffic-light-close", "traffic-light-minimize", "traffic-light-maximize"]);
    expect(classTokensOf(windowButtonCluster(nodes))).toContain("hidden");
    // No spacer either: the lights take that room themselves.
    expect(nodes.find((vnode) => classTokensOf(vnode).includes("w-[72px]") && vnode.tag === "div" && attrsOf(vnode).id === undefined)).toBeUndefined();
  });

  it("draws neither its own buttons nor lights when the native frame's title bar carries the controls", () => {
    const nodes = renderBar(shellOffMac({ isNativeFrame: true }), WORKSPACE_PATH);
    expect(nodes.find((vnode) => vnode.tag === TrafficLights)).toBeUndefined();
    expect(classTokensOf(windowButtonCluster(nodes))).toContain("hidden");
  });

  it("keeps its own buttons, and draws no lights, off macOS by default", () => {
    const nodes = renderBar(shellOffMac(), WORKSPACE_PATH);
    expect(nodes.find((vnode) => vnode.tag === TrafficLights)).toBeUndefined();
    expect(classTokensOf(windowButtonCluster(nodes))).not.toContain("hidden");
  });

  it("leaves room under the native traffic lights on macOS, before the wordmark", () => {
    const nodes = renderBar(makeShell(), WORKSPACE_PATH);

    const spacerAt = nodes.findIndex((vnode) => vnode.tag === "div" && classTokensOf(vnode).includes("w-[72px]"));
    expect(spacerAt).toBeGreaterThan(-1);
    expect(spacerAt).toBeLessThan(nodes.indexOf(barButton(nodes, "home-btn")));
  });
});

describe("the first run's titlebar", () => {
  it("draws the lockup alone: no log-in, no trail, and none of the right-hand buttons", () => {
    const nodes = renderBar(makeShell(), "/start");

    expect(attrsOf(barButton(nodes, "minds-titlebar"))["data-titlebar-kind"]).toBe("start");
    expect(ids(nodes)).toContain("start-mark");
    for (const id of [
      "start-log-in",
      "home-btn",
      "account-switcher-btn",
      "workspace-switcher-btn",
      "notifications-toggle",
      "help-toggle",
      "settings-btn",
      "share-btn",
    ]) {
      expect(ids(nodes), id).not.toContain(id);
    }
  });
});
