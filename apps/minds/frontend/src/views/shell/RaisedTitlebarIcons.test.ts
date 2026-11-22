import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import { forgetWarmedPermissionsOverview, readWarmedPermissionsOverview } from "../../models/permissionsPrefetch";
import { ShellState } from "./shell-state";
import { Titlebar } from "./Titlebar";
import type { TitlebarPopupId } from "./RaisedTitlebarIcons";
import {
  RaisedTitlebarIcons,
  TITLEBAR_POPUP_ICONS,
  isTitlebarFlyoutOpen,
  openTitlebarPopup,
  readAnchorRect,
  titlebarAnchors,
} from "./RaisedTitlebarIcons";
import type { AnyVnode, StubRect } from "../../testing";
import {
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
  publishPendingRequests,
  publishUpdates,
  required,
  sharedWorkspaceEntry,
  stubTitlebarRects,
  thirdAccountEntry,
  withAttr,
  workspacesMessage,
} from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const OWNED: UiWorkspaceEntry = { ...workspacesMessage().workspaces[0], id: WORKSPACE_ID };
const ALICE = accountEntry({ display_name: "Alice Nguyen" });
const CAROL = thirdAccountEntry();

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const BUTTON_RECT: StubRect = { left: 100, top: 5, width: 28, height: 28 };

/** Every one of `ids` on screen, at the same rect. */
function showing(ids: readonly string[]): Record<string, StubRect> {
  return Object.fromEntries(ids.map((id) => [id, BUTTON_RECT]));
}

const RIGHT_HAND_IDS = ["notifications-toggle", "help-toggle", "settings-btn"] as const;
const WORKSPACE_BAR_IDS = ["account-switcher-btn", "workspace-switcher-btn", ...RIGHT_HAND_IDS] as const;

/** A shell on its account's own workspace "alpha". */
function makeShell(): ShellState {
  const shell = new ShellState(createEmptyStores());
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces: [OWNED] }));
  shell.displayedWorkspaceAnyId = WORKSPACE_ID;
  return shell;
}

function renderStrip(
  shell: ShellState,
  ids: readonly string[],
  selected: TitlebarPopupId,
  onSelect: (id: TitlebarPopupId) => void = () => undefined,
  onDismiss: () => void = () => undefined,
): AnyVnode {
  stubTitlebarRects(showing(ids));
  const instance = RaisedTitlebarIcons() as unknown as m.Component;
  return (instance.view as unknown as (v: unknown) => AnyVnode).call(instance, {
    attrs: { shell, anchors: titlebarAnchors(), selected, onDismiss, onSelect },
  });
}

function iconIds(strip: AnyVnode): unknown[] {
  return collectVnodes(strip)
    .map((vnode) => attrsOf(vnode)["data-titlebar-popup"])
    .filter((id) => id !== undefined);
}

function icon(strip: AnyVnode, id: TitlebarPopupId): AnyVnode {
  const found = collectVnodes(strip).find(
    (vnode) => attrsOf(vnode)["data-titlebar-popup"] === id,
  );
  expect(found, `no raised ${id} icon`).toBeDefined();
  return found as AnyVnode;
}

function glyphNames(node: AnyVnode): unknown[] {
  return withAttr(node.children, "name").map((vnode) => attrsOf(vnode).name);
}

describe("the bar's flyout buttons", () => {
  it("are the account, the workspace, the bell, Report a bug, and Settings, left to right", () => {
    expect(TITLEBAR_POPUP_ICONS.map((entry) => [entry.id, entry.buttonId])).toEqual([
      ["account", "account-switcher-btn"],
      ["workspace", "workspace-switcher-btn"],
      ["notifications", "notifications-toggle"],
      ["help", "help-toggle"],
      ["settings", "settings-btn"],
    ]);
  });
});

describe("the raised titlebar icon strip", () => {
  it("raises a copy of every flyout button the bar shows, at that button's own rect", () => {
    const strip = renderStrip(makeShell(), WORKSPACE_BAR_IDS, "notifications");

    expect(iconIds(strip)).toEqual(["account", "workspace", "notifications", "help", "settings"]);
    for (const entry of TITLEBAR_POPUP_ICONS) {
      const copy = icon(strip, entry.id);
      expect(attrsOf(copy).id).toBe(`${entry.buttonId}-raised`);
      expect(attrsOf(copy).style).toBe("left: 100px; top: 5px; width: 28px; height: 28px");
    }
    expect(glyphNames(icon(strip, "notifications"))).toEqual(["bell"]);
    expect(glyphNames(icon(strip, "help"))).toEqual(["bug"]);
    expect(glyphNames(icon(strip, "settings"))).toEqual(["settings"]);
  });

  it("draws only the buttons the titlebar is actually showing", () => {
    // On a hub page other than Home the bar shows no workspace button, so the
    // strip has no copy of one -- not a ghost stacked at the origin.
    const strip = renderStrip(makeShell(), ["account-switcher-btn", ...RIGHT_HAND_IDS], "notifications");

    expect(iconIds(strip)).toEqual(["account", "notifications", "help", "settings"]);
  });

  it("marks the open flyout's button selected and joins it to the flyout", () => {
    const strip = renderStrip(makeShell(), RIGHT_HAND_IDS, "notifications");
    const bell = icon(strip, "notifications");
    const bug = icon(strip, "help");

    expect(attrsOf(bell)["aria-selected"]).toBe("true");
    // Filled with the card's own surface, square-bottomed so it reads as
    // physically joined to the flyout below it.
    expect(classTokensOf(bell)).toEqual(expect.arrayContaining(["bg-surface-primary", "rounded-b-none"]));
    expect(classTokensOf(bell)).not.toContain("titlebar-surface");
    expect(attrsOf(bug)["aria-selected"]).toBe("false");
    // The others stay titlebar-coloured and live, so they read as titlebar buttons.
    expect(classTokensOf(bug)).toEqual(expect.arrayContaining(["titlebar-surface", "hover:bg-fill-hover"]));
    expect(classTokensOf(bug)).not.toContain("rounded-b-none");
  });

  it("closes from the button you are on, and goes elsewhere from the others", () => {
    let dismissals = 0;
    const selections: TitlebarPopupId[] = [];
    const strip = renderStrip(
      makeShell(),
      WORKSPACE_BAR_IDS,
      "notifications",
      (id) => selections.push(id),
      () => (dismissals += 1),
    );

    (attrsOf(icon(strip, "notifications")).onclick as () => void)();
    expect(dismissals).toBe(1);
    expect(selections).toEqual([]);

    for (const id of ["account", "workspace", "help", "settings"] as const) {
      (attrsOf(icon(strip, id)).onclick as () => void)();
    }
    expect(dismissals).toBe(1);
    expect(selections).toEqual(["account", "workspace", "help", "settings"]);
  });

  it("labels the button you are on as the way out", () => {
    const strip = renderStrip(makeShell(), RIGHT_HAND_IDS, "help");

    expect(attrsOf(icon(strip, "help"))["aria-label"]).toBe("Close Report a bug");
    expect(attrsOf(icon(strip, "help"))["data-tooltip"]).toBe("Close");
    expect(attrsOf(icon(strip, "notifications"))["aria-label"]).toBe("Notifications");
    expect(attrsOf(icon(strip, "settings"))["aria-label"]).toBe("Settings");
    expect(attrsOf(icon(strip, "settings"))["data-tooltip"]).toBe("Settings");
  });

  it("starts reading the workspace's permissions when the raised gear is pointed at, as the bar's own does", () => {
    vi.stubGlobal("fetch", () => new Promise<Response>(() => undefined));
    forgetWarmedPermissionsOverview();
    const pointAtGear = (shell: ShellState): void =>
      (attrsOf(icon(renderStrip(shell, RIGHT_HAND_IDS, "help"), "settings")).onpointerenter as () => void)();

    pointAtGear(makeShell());
    expect(readWarmedPermissionsOverview(WORKSPACE_ID)).toBeNull();

    const waiting = makeShell();
    publishPendingRequests(waiting, [WORKSPACE_ID]);
    pointAtGear(waiting);
    expect(readWarmedPermissionsOverview(WORKSPACE_ID)).not.toBeNull();
    forgetWarmedPermissionsOverview();
  });

  it("carries the gear's waiting-on-you dot, so the cue survives an open flyout", () => {
    const waiting = makeShell();
    publishPendingRequests(waiting, [WORKSPACE_ID]);
    const withDot = renderStrip(waiting, RIGHT_HAND_IDS, "help");
    expect(byId(icon(withDot, "settings").children, "permissions-badge-raised"), "no raised waiting dot").toBeDefined();
    // The dot is the gear's alone.
    expect(byId(icon(withDot, "help").children, "permissions-badge-raised")).toBeUndefined();

    const withoutDot = renderStrip(makeShell(), RIGHT_HAND_IDS, "help");
    expect(byId(withoutDot, "permissions-badge-raised")).toBeUndefined();

    // A request from a workspace that is not on screen is not this bar's to flag.
    const elsewhere = makeShell();
    publishPendingRequests(elsewhere, ["agent-zz99"]);
    expect(byId(renderStrip(elsewhere, RIGHT_HAND_IDS, "help"), "permissions-badge-raised")).toBeUndefined();
  });

  it("carries the bell's unresolved count, and no badge at zero", () => {
    const shell = makeShell();
    shell.stores.notifications.applyNotificationsMessage({ entries: [], unresolved_count: 4 });

    const counted = renderStrip(shell, RIGHT_HAND_IDS, "settings");
    expect(attrsOf(required(byId(icon(counted, "notifications").children, "notifications-badge-raised"))).count).toBe(4);

    expect(byId(renderStrip(makeShell(), RIGHT_HAND_IDS, "settings"), "notifications-badge-raised")).toBeUndefined();
  });

  it("draws the account button's copy with the account's email, so it is as wide as the titlebar's", () => {
    const shell = makeShell();
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], ALICE.user_id));
    shell.accountId = ALICE.user_id;
    const strip = renderStrip(shell, ["account-switcher-btn", "notifications-toggle"], "notifications");
    const switcher = icon(strip, "account");

    expect(attrsOf(switcher).id).toBe("account-switcher-btn-raised");
    expect(attrsOf(switcher)["aria-label"]).toBe("Switch account");
    expect(allText(switcher)).toBe(ALICE.email);
    expect(glyphNames(switcher)).toEqual(["chevron-down-small"]);
    // The name is the account button's alone: the other icons stay bare.
    expect(allText(icon(strip, "notifications"))).toBe("");
  });

  it("marks the account button's copy signed out as the real button does, without taking the real name span's id", () => {
    const shell = makeShell();
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], CAROL.user_id, [CAROL]));
    shell.accountId = CAROL.user_id;
    const switcher = icon(renderStrip(shell, ["account-switcher-btn"], "notifications"), "account");

    expect(allText(switcher)).toContain(CAROL.email);
    expect(allText(switcher)).toContain("(signed out)");
    expect(byId(switcher.children, "account-switcher-name")).toBeUndefined();
  });

  it("draws the workspace button's copy with the workspace on screen, tinted for the bar until it is the selected one", () => {
    const swatchOf = (copy: AnyVnode): AnyVnode => required(withAttr(copy.children, "data-workspace-swatch")[0]);

    const onBar = icon(renderStrip(makeShell(), WORKSPACE_BAR_IDS, "help"), "workspace");
    expect(attrsOf(onBar)["aria-label"]).toBe("Switch workspace");
    expect(allText(onBar)).toBe("alpha");
    expect(byId(onBar.children, "workspace-switcher-name")).toBeUndefined();
    expect(classTokensOf(swatchOf(onBar))).toContain("workspace-swatch-on-bar");
    expect(glyphNames(onBar)).toEqual(["chevron-down-small"]);

    // The selected copy is drawn on the card's surface, where the plain colour reads.
    const selected = icon(renderStrip(makeShell(), WORKSPACE_BAR_IDS, "workspace"), "workspace");
    expect(classTokensOf(swatchOf(selected))).not.toContain("workspace-swatch-on-bar");
    expect(attrsOf(swatchOf(selected)).style).toBe("background-color: #aabbcc;");
    expect(glyphNames(selected)).toEqual(["chevron-up"]);
  });

  it("names a shared workspace's owner on the workspace button's copy, and invites opening one from Home", () => {
    const shared = sharedWorkspaceEntry({
      status: "accepted",
      owner: namedSharedOwner(),
    });
    const guest = makeShell();
    guest.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces: [OWNED, shared] }));
    guest.displayedWorkspaceAnyId = shared.id;
    expect(collectText(icon(renderStrip(guest, WORKSPACE_BAR_IDS, "help"), "workspace")).join("")).toBe(
      "Beta (Sam Okafor)",
    );

    const home = makeShell();
    home.displayedWorkspaceAnyId = null;
    expect(allText(icon(renderStrip(home, WORKSPACE_BAR_IDS, "help"), "workspace"))).toBe("Open workspace");
  });
});

describe("the workspace button's copy where the bar names a workspace that is not on screen", () => {
  const ATTEMPT = createAttemptEntry();

  /** What the bar's own workspace button and its raised copy each say on `route`, a page that names a workspace
   * without showing its surface. */
  function buttonAndCopyOn(route: string): { button: string; copy: string } {
    vi.stubGlobal("window", {});
    vi.spyOn(m.route, "get").mockReturnValue(route);
    const shell = makeShell();
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces: [OWNED, ATTEMPT] }));
    // As handleRouteChanged leaves it on these routes: no workspace surface is displayed.
    shell.displayedWorkspaceAnyId = null;
    const bar = Titlebar() as unknown as m.Component;
    const barNodes = collectVnodes(
      (bar.view as unknown as (v: unknown) => unknown).call(bar, { attrs: { shell, routePath: route } }),
    );
    const strip = renderStrip(shell, WORKSPACE_BAR_IDS, "account");
    return {
      button: allText(required(byId(barNodes, "workspace-switcher-btn"))),
      copy: allText(icon(strip, "workspace")),
    };
  }

  it("names the workspace being made on its creation page, as the button it stands in for does", () => {
    const { button, copy } = buttonAndCopyOn(`/creating/${ATTEMPT.id}`);

    expect(button).toBe("gamma");
    expect(copy).toBe(button);
  });

  it("names the workspace on one of its own pages that replaces its surface, as the button it stands in for does", () => {
    const { button, copy } = buttonAndCopyOn(`/workspace/${WORKSPACE_ID}/backups`);

    expect(button).toBe("alpha");
    expect(copy).toBe(button);
  });
});

describe("the strip's live copy of Share", () => {
  function raisedShare(strip: AnyVnode): AnyVnode | undefined {
    return byId(strip, "share-btn-raised");
  }

  it("is drawn over the bar's Share, and presses Share through the shell", () => {
    const shell = makeShell();
    const pressShare = vi.spyOn(shell, "pressShare").mockImplementation(() => undefined);
    const share = required(raisedShare(renderStrip(shell, [...WORKSPACE_BAR_IDS, "share-btn"], "settings")));

    expect(attrsOf(share).style).toBe("left: 100px; top: 5px; width: 28px; height: 28px");
    expect(attrsOf(share)["aria-label"]).toBe("Share");
    expect(allText(share)).toBe("Share");
    expect(glyphNames(share)).toEqual(["share"]);
    // Drawn outside the titlebar, it re-bases its text on the workspace accent itself, as the icons do.
    expect(classTokensOf(share)).toEqual(expect.arrayContaining(["titlebar-surface", "pointer-events-auto"]));
    // Not one of the flyout buttons: it is never the selected tab.
    expect(attrsOf(share)["data-titlebar-popup"]).toBeUndefined();
    expect(attrsOf(share).role).toBeUndefined();
    (attrsOf(share).onclick as () => void)();

    expect(pressShare).toHaveBeenCalledTimes(1);
  });

  it("is absent where the bar shows no Share", () => {
    expect(raisedShare(renderStrip(makeShell(), WORKSPACE_BAR_IDS, "settings"))).toBeUndefined();
  });
});

describe("the strip's live copy of the status pill", () => {
  function shellWithUpdateOnOffer(): ShellState {
    const shell = makeShell();
    publishUpdates(shell, { [WORKSPACE_ID]: OUT_OF_DATE_UPDATE }, false);
    return shell;
  }

  function raisedPill(strip: AnyVnode): AnyVnode | undefined {
    return byId(strip, "titlebar-status-pill-raised");
  }

  it("raises the pill over the real one, still clickable, and only while the titlebar shows one", () => {
    const shell = shellWithUpdateOnOffer();
    const openUpdatesTab = vi.spyOn(shell, "openUpdatesTab").mockImplementation(() => undefined);

    const withPill = required(raisedPill(renderStrip(shell, ["titlebar-status-pill", "help-toggle"], "help")));
    expect(String(attrsOf(withPill).style)).toContain("left: 100px; top: 5px");
    expect(attrsOf(withPill)["data-status"]).toBe("update-out-of-date");
    expect(allText(withPill)).toContain("Update available");
    // Drawn outside the titlebar, it re-bases its text on the workspace accent itself, as the icons do.
    expect(classTokensOf(withPill)).toContain("titlebar-surface");
    (attrsOf(withPill).onclick as () => void)();
    expect(openUpdatesTab).toHaveBeenCalledWith(WORKSPACE_ID);

    expect(raisedPill(renderStrip(shell, ["help-toggle"], "help"))).toBeUndefined();
  });

  it("draws none for a workspace with nothing to say, even over a pill the bar has not yet taken down", () => {
    expect(raisedPill(renderStrip(makeShell(), ["titlebar-status-pill", "help-toggle"], "help"))).toBeUndefined();
  });
});

describe("titlebarAnchors", () => {
  it("reads each flyout button's rect, Share's and the status pill's, and nothing for what the bar is not showing", () => {
    stubTitlebarRects({
      "help-toggle": { left: 700, top: 8, width: 28, height: 28 },
      "share-btn": { left: 780, top: 8, width: 72, height: 28 },
      "titlebar-status-pill": { left: 300, top: 10, width: 120, height: 24 },
      // A button in a hidden crumb is in the document and measures at nothing.
      "workspace-switcher-btn": { left: 0, top: 0, width: 0, height: 0 },
    });

    const anchors = titlebarAnchors();

    expect(anchors.rectOf("help")).toEqual({ x: 700, y: 8, width: 28, height: 28 });
    expect(anchors.shareRect()).toEqual({ x: 780, y: 8, width: 72, height: 28 });
    expect(anchors.statusPillRect()).toEqual({ x: 300, y: 10, width: 120, height: 24 });
    expect(anchors.rectOf("workspace")).toBeNull();
    expect(anchors.rectOf("account")).toBeNull();
    expect(readAnchorRect("workspace-switcher-btn")).toBeNull();
  });

  it("redraws on a re-measure only when a rect moved, appeared, or left", () => {
    const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const rects: Record<string, StubRect> = {
      "workspace-switcher-btn": { left: 200, top: 8, width: 90, height: 28 },
      "help-toggle": { left: 700, top: 8, width: 28, height: 28 },
    };
    stubTitlebarRects(rects);
    const anchors = titlebarAnchors();

    anchors.remeasure();
    expect(redraw).not.toHaveBeenCalled();

    // A late-loading workspace name widens the button.
    rects["workspace-switcher-btn"] = { left: 200, top: 8, width: 140, height: 28 };
    anchors.remeasure();
    expect(redraw).toHaveBeenCalledTimes(1);
    expect(anchors.rectOf("workspace")?.width).toBe(140);

    // The window moves to a hub page: the workspace button leaves the bar, and its copy with it.
    delete rects["workspace-switcher-btn"];
    anchors.remeasure();
    expect(redraw).toHaveBeenCalledTimes(2);
    expect(anchors.rectOf("workspace")).toBeNull();

    rects["share-btn"] = { left: 780, top: 8, width: 72, height: 28 };
    anchors.remeasure();
    expect(redraw).toHaveBeenCalledTimes(3);
    delete rects["share-btn"];
    anchors.remeasure();
    expect(redraw).toHaveBeenCalledTimes(4);
    expect(anchors.shareRect()).toBeNull();

    anchors.remeasure();
    expect(redraw).toHaveBeenCalledTimes(4);
  });
});

describe("openTitlebarPopup", () => {
  /** A shell that records which of its flyout transitions were asked for. */
  function recordingShell(): { shell: ShellState; calls: () => string[] } {
    const calls: string[] = [];
    const shell = makeShell();
    for (const method of [
      "switchToNotifications",
      "switchToAccountSwitcher",
      "switchToWorkspaceSwitcher",
      "closeNotifications",
      "openHelp",
      "openSettings",
      "openSettingsFromBar",
    ] as const) {
      vi.spyOn(shell, method).mockImplementation(() => {
        calls.push(method);
      });
    }
    return { shell, calls: () => calls };
  }

  it("hands the bell its own switch, which survives the dismissal navigation", () => {
    const { shell, calls } = recordingShell();

    openTitlebarPopup(shell, "notifications");

    expect(calls()).toEqual(["switchToNotifications"]);
  });

  it("hands the account menu its own switch, like the bell", () => {
    const { shell, calls } = recordingShell();

    openTitlebarPopup(shell, "account");

    expect(calls()).toEqual(["switchToAccountSwitcher"]);
  });

  it("hands the workspace menu its own switch, like the bell", () => {
    const { shell, calls } = recordingShell();

    openTitlebarPopup(shell, "workspace");

    expect(calls()).toEqual(["switchToWorkspaceSwitcher"]);
  });

  it("leaves the feed to the arrival, so the overlay slot is never empty mid-switch", () => {
    // The feed is local state; the navigation closes it when it lands
    // (handleRouteChanged). Closed eagerly here, the Shell's one overlay slot
    // would render empty for the frames before the landing -- an un-dim /
    // re-dim flash between two flyouts that should hand off in place.
    const help = recordingShell();
    openTitlebarPopup(help.shell, "help");
    expect(help.calls()).toEqual(["openHelp"]);

    const settings = recordingShell();
    openTitlebarPopup(settings.shell, "settings");
    expect(settings.calls()).toEqual(["openSettingsFromBar"]);
  });
});

describe("isTitlebarFlyoutOpen", () => {
  const WORKSPACE_PATH = `/workspace/${WORKSPACE_ID}`;

  it("is true under each of the five flyouts", () => {
    expect(isTitlebarFlyoutOpen(makeShell(), "/settings")).toBe(true);
    expect(isTitlebarFlyoutOpen(makeShell(), "/help")).toBe(true);

    const feed = makeShell();
    feed.isNotificationsOpen = true;
    expect(isTitlebarFlyoutOpen(feed, WORKSPACE_PATH)).toBe(true);

    const accountMenu = makeShell();
    accountMenu.openAccountSwitcher();
    expect(isTitlebarFlyoutOpen(accountMenu, WORKSPACE_PATH)).toBe(true);

    const workspaceMenu = makeShell();
    workspaceMenu.openWorkspaceSwitcher();
    expect(isTitlebarFlyoutOpen(workspaceMenu, "/account/local/")).toBe(true);
  });

  it("is false with nothing open, and under the centered cards, which raise no strip", () => {
    for (const path of [WORKSPACE_PATH, "/account/local/", "/inbox", "/settings/ai-keys", "/account/local/create/template"]) {
      expect(isTitlebarFlyoutOpen(makeShell(), path), path).toBe(false);
    }
  });
});
