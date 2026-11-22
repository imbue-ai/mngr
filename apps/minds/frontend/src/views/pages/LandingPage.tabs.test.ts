import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import type { UiWorkspaceEntry } from "../../channel/messages";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  recordingFetch,
  required,
  stubSessionStorage,
  withAttr,
  workspacesMessage,
} from "../../testing";
import { CardMenu, type CardMenuItem } from "../components/CardMenu";
import { HomeBadge } from "../components/HomeBadge";
import { homeTabsFor } from "./LandingPage";
import {
  appIcons,
  initPageOnAlicesHub,
  menuItem,
  panelOf,
  selectTab,
  SETTLED_LANDING_EXTRAS,
  stripOf,
} from "./LandingPage.testing";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

beforeEach(() => {
  stubSessionStorage();
});

/** The page on Alice's hub over the given own rows (the fixture's one by default). */
function initPage(workspaces?: UiWorkspaceEntry[]) {
  return initPageOnAlicesHub({ workspaces });
}

/** Every status badge in the tree as its words and tone. */
function badgesOf(node: unknown): [string, unknown][] {
  return collectVnodes(node)
    .filter((v) => v.tag === HomeBadge)
    .map((v) => [allText(v), attrsOf(v).tone]);
}

describe("homeTabsFor", () => {
  it("shows Invitations only while any waits or was declined, counting the waiting ones", () => {
    expect(homeTabsFor(0, false).map((tab) => tab.id)).toEqual(["workspaces", "apps"]);
    expect(homeTabsFor(2, false).at(-1)).toEqual({ id: "invitations", label: "Invitations", count: 2 });
    expect(homeTabsFor(0, true).at(-1)).toEqual({ id: "invitations", label: "Invitations", count: 0 });
  });
});

describe("the home tabs", () => {
  it("opens on Workspaces, remembers the chosen tab per window, and falls back to Workspaces from a remembered tab that is gone", () => {
    const first = initPage();
    expect(panelOf(first.view())).toBe("workspaces");
    expect(attrsOf(stripOf(first.view())).selected).toBe("workspaces");

    selectTab(first.view(), "apps");
    expect(panelOf(first.view())).toBe("apps");
    expect(sessionStorage.getItem("home-tab")).toBe("apps");
    clearAppContextForTests();

    const second = initPage();
    expect(panelOf(second.view())).toBe("apps");
    clearAppContextForTests();

    sessionStorage.setItem("home-tab", "invitations");
    const third = initPage();
    expect(panelOf(third.view())).toBe("workspaces");
  });

  it("puts Studio settings in the active tab's first section head, keeping its id, and no launcher at the page's foot", () => {
    const { view } = initPage();

    const workspaces = view();
    const settings = required(byId(workspaces, "landing-minds-settings"));
    expect(allText(settings)).toContain("Studio settings");
    expect(byId(workspaces, "landing-account")).toBeUndefined();
    const ownSection = required(withAttr(workspaces, "data-home-section")[0]);
    expect(byId(ownSection, "landing-minds-settings")).toBeDefined();

    selectTab(workspaces, "apps");
    const apps = view();
    const appsSection = required(withAttr(apps, "data-home-section")[0]);
    expect(attrsOf(appsSection)["data-home-section"]).toBe("own-apps");
    expect(byId(appsSection, "landing-minds-settings")).toBeDefined();
  });

  it("keeps the destroyed link, under the account's own grid, and the providers panel under the Workspaces tab alone", () => {
    const { view } = initPage();
    const workspaces = view();
    // The link belongs to the account's own workspaces, so it sits in their section, above the shared ones.
    const ownSection = required(withAttr(workspaces, "data-home-section")[0]);
    expect(attrsOf(ownSection)["data-home-section"]).toBe("own-workspaces");
    expect(byId(ownSection, "landing-destroyed")).toBeDefined();
    expect(byId(workspaces, "landing-providers")).toBeDefined();

    selectTab(workspaces, "apps");
    const apps = view();
    expect(byId(apps, "landing-destroyed")).toBeUndefined();
    expect(byId(apps, "landing-providers")).toBeUndefined();
  });
});

describe("a live workspace card", () => {
  it("carries its id, one run control, Open in new window, and a menu with the host fact and the owner actions", () => {
    const { view } = initPage([
      { ...workspacesMessage().workspaces[0], provider_label: "Local" },
    ]);

    const rendered = view();
    const card = required(withAttr(rendered, "data-row-kind").find((v) => attrsOf(v)["data-row-kind"] === "live"));
    expect(attrsOf(card)["data-agent-id"]).toBe("agent-aa11");
    expect(attrsOf(card).role).toBe("button");
    expect(allText(card)).toContain("alpha");
    const runControl = required(withAttr(card, "data-run-control")[0]);
    expect(attrsOf(runControl)["data-run-control"]).toBe("pause");
    expect(withAttr(card, "data-card-action").map((v) => attrsOf(v)["data-card-action"])).toEqual(["new-window"]);
    const menu = required(collectVnodes(card).find((v) => v.tag === CardMenu));
    expect(attrsOf(menu).facts).toEqual([{ label: "Host:", value: "Local" }]);
    expect((attrsOf(menu).items as CardMenuItem[]).map((item) => item.label)).toEqual([
      "Open in new window",
      "Stop workspace",
      "Workspace settings",
    ]);
    // Running and healthy: no badge speaks.
    expect(badgesOf(card)).toEqual([]);
  });

  it("opens Settings on the card's workspace from its menu, without entering the workspace", () => {
    const { shell, view, routeSet } = initPage();
    const enter = vi.spyOn(shell, "enterWorkspaceOrRecover").mockImplementation(() => true);
    const card = required(withAttr(view(), "data-row-kind")[0]);

    menuItem(card, "Workspace settings").onSelect();

    // The workspace is picked (`settings_workspace`), not floated over (`workspace`): Home stays behind Settings.
    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "workspace", settings_workspace: "agent-aa11", account: shell.accountId }, undefined],
    ]);
    expect(enter).not.toHaveBeenCalled();
  });

  it("opens on Enter or Space pressed on the card itself, never on a key bubbling up from a button it holds", () => {
    const { shell, view } = initPage();
    const enter = vi.spyOn(shell, "enterWorkspaceOrRecover").mockImplementation(() => true);
    const card = required(withAttr(view(), "data-row-kind")[0]);
    const onKeyDown = attrsOf(card).onkeydown as (event: unknown) => void;
    const cardElement = {};
    const buttonElement = {};

    const fromButton = { key: "Enter", target: buttonElement, currentTarget: cardElement, preventDefault: vi.fn() };
    onKeyDown(fromButton);
    expect(enter).not.toHaveBeenCalled();
    expect(fromButton.preventDefault).not.toHaveBeenCalled();

    const space = { key: " ", target: cardElement, currentTarget: cardElement, preventDefault: vi.fn() };
    onKeyDown(space);
    expect(enter).toHaveBeenCalledTimes(1);
    expect(space.preventDefault).toHaveBeenCalledTimes(1);
    onKeyDown({ key: "Tab", target: cardElement, currentTarget: cardElement, preventDefault: vi.fn() });
    expect(enter).toHaveBeenCalledTimes(1);
  });

  it("shows a stopped machine's state as a neutral badge with Start as its one control, and a key chip in the warn tone", () => {
    const { view } = initPage([
      { ...workspacesMessage().workspaces[0], liveness: "STOPPED", key_state: "locked" },
    ]);

    const card = required(withAttr(view(), "data-row-kind")[0]);
    const badges = badgesOf(card);
    expect(badges).toContainEqual(["Stopped", "neutral"]);
    expect(badges).toContainEqual(["Enter your master password to open", "warn"]);
    expect(attrsOf(required(withAttr(card, "data-run-control")[0]))["data-run-control"]).toBe("play");
    // A key this device lacks blocks the click.
    expect(attrsOf(card).role).toBeUndefined();
  });

  it("draws a create attempt and a remote record as cards with their badges, the remote one removable", () => {
    const base = workspacesMessage().workspaces[0];
    const { view } = initPage([
      { ...base, id: "create-attempt-1", name: "newbie", create_attempt_state: "interrupted" },
      {
        ...base,
        id: "agent-bb22",
        host_id: "host-cc33",
        name: "elsewhere",
        is_remote: true,
        remote_kind: "other_device",
        location: "Laptop",
        backup_access: "locked",
      },
    ]);

    const rendered = view();
    const kinds = withAttr(rendered, "data-row-kind").map((v) => attrsOf(v)["data-row-kind"]);
    expect(kinds).toEqual(["create-attempt", "remote"]);
    const [attempt, remote] = withAttr(rendered, "data-row-kind");
    expect(badgesOf(attempt)).toEqual([["Interrupted", "warn"]]);
    expect(allText(remote)).toContain("on Laptop");
    const remove = withAttr(remote, "aria-label").find((v) => attrsOf(v)["aria-label"] === "Remove from this list");
    expect(remove).toBeDefined();
    // Backups it cannot reach stays listed, inert, saying why on hover.
    const backups = menuItem(remote, "Backups");
    expect(backups.isDisabled).toBe(true);
    expect(backups.title).toContain("master password");
  });

  it.each([
    ["confirming", "Confirming removal…", "neutral", true],
    ["running", "Destroying…", "neutral", true],
    ["failed", "Removal failed", "alarm", false],
  ])("draws a workspace whose destroy is %s as a card with its one badge", async (status, label, tone, isSpinning) => {
    const { redraw, view } = initPageOnAlicesHub({
      fetchImpl: recordingFetch(() =>
        Promise.resolve(
          jsonResponse({ ...SETTLED_LANDING_EXTRAS, destroying_status_by_agent_id: { "agent-aa11": status } }),
        ),
      ).fetchImpl,
    });
    await vi.waitFor(() => expect(redraw).toHaveBeenCalled());

    const card = required(
      withAttr(view(), "data-row-kind").find((v) => attrsOf(v)["data-row-kind"] === "destroying"),
    );
    expect(attrsOf(card)["data-agent-id"]).toBe("agent-aa11");
    const badge = required(collectVnodes(card).find((v) => v.tag === HomeBadge));
    expect([allText(badge), attrsOf(badge).tone, attrsOf(badge).isSpinning]).toEqual([label, tone, isSpinning]);
  });
});

describe("your apps", () => {
  it("lists each live workspace's apps as icons named by the workspace, opening through the shell", () => {
    const base = workspacesMessage().workspaces[0];
    const { shell, view } = initPage([
      {
        ...base,
        apps: [
          { name: "reading-list", display_name: "Reading List", label: "reading-list-k3j9x2ab", icon_version: "abc123" },
          { name: "zenbox", display_name: "zenbox", label: "", icon_version: "" },
        ],
      },
    ]);
    const open = vi.spyOn(shell, "openWorkspaceApp").mockImplementation(() => undefined);

    selectTab(view(), "apps");
    const rendered = view();
    const icons = appIcons(rendered);
    expect(icons.map((icon) => [attrsOf(icon).name, attrsOf(icon).subtitle, attrsOf(icon).iconUrl])).toEqual([
      ["Reading List", "alpha", "/ui/api/workspaces/agent-aa11/apps/reading-list/icon.svg?v=abc123"],
      ["zenbox", "alpha", null],
    ]);
    (attrsOf(icons[0]).onOpen as () => void)();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][1].name).toBe("reading-list");
    // With nothing shared, the section offers the pasted-link input in place of an empty state.
    expect(byId(rendered, "landing-no-shared-apps")).toBeUndefined();
    expect(attrsOf(required(byId(rendered, "shared-with-me-app-link"))).placeholder).toBe(
      "Paste an app link you were given",
    );
    expect(byId(rendered, "shared-with-me-add-app-link")).toBeDefined();
  });

  it("dims a blocked workspace's app icons as its card is not clickable, saying so in the tooltip", () => {
    const base = workspacesMessage().workspaces[0];
    const app = { name: "reading-list", display_name: "Reading List", label: "reading-list-k3j9x2ab", icon_version: "" };
    const { view } = initPage([
      { ...base, apps: [app] },
      { ...base, id: "agent-bb22", name: "beta", key_state: "locked", apps: [app] },
    ]);

    selectTab(view(), "apps");
    const icons = appIcons(view());
    expect(icons.map((icon) => [attrsOf(icon).subtitle, attrsOf(icon).isDisabled])).toEqual([
      ["alpha", false],
      ["beta", true],
    ]);
    expect(attrsOf(icons[0]).title).toBeUndefined();
    expect(String(attrsOf(icons[1]).title)).toContain("cannot be opened");
  });

  it("lists no app of a workspace whose destroy is in flight, as its card cannot be entered either", async () => {
    const base = workspacesMessage().workspaces[0];
    const app = { name: "reading-list", display_name: "Reading List", label: "reading-list-k3j9x2ab", icon_version: "" };
    const { redraw, view } = initPageOnAlicesHub({
      workspaces: [
        { ...base, apps: [app] },
        { ...base, id: "agent-bb22", name: "beta", apps: [app] },
      ],
      fetchImpl: recordingFetch(() =>
        Promise.resolve(
          jsonResponse({ ...SETTLED_LANDING_EXTRAS, destroying_status_by_agent_id: { "agent-bb22": "running" } }),
        ),
      ).fetchImpl,
    });
    await vi.waitFor(() => expect(redraw).toHaveBeenCalled());

    selectTab(view(), "apps");
    expect(appIcons(view()).map((icon) => attrsOf(icon).subtitle)).toEqual(["alpha"]);
  });

  it("says so when no workspace has an app yet", () => {
    const { view } = initPage();
    selectTab(view(), "apps");
    expect(byId(view(), "landing-no-own-apps")).toBeDefined();
  });
});
