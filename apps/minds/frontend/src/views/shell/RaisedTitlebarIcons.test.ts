import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import type { TitlebarPopupId } from "./RaisedTitlebarIcons";
import {
  RaisedTitlebarIcons,
  openTitlebarPopup,
  titlebarAnchors,
} from "./RaisedTitlebarIcons";
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes } from "../../testing";

const WORKSPACE_ID = "agent-ab12";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A titlebar with `ids` on screen and everything else absent, the way a hub
 * page's machine tabs are absent. */
function stubTitlebar(ids: readonly string[]): void {
  vi.stubGlobal("document", {
    getElementById: (id: string) =>
      ids.includes(id)
        ? {
            getBoundingClientRect: () => ({
              left: 100,
              top: 5,
              width: 28,
              height: 28,
            }),
          }
        : null,
  });
}

function renderStrip(
  ids: readonly string[],
  selected: TitlebarPopupId,
  onSelect: (id: TitlebarPopupId) => void = () => undefined,
  onDismiss: () => void = () => undefined,
  hasWorkspaceRequestDot = false,
): AnyVnode {
  stubTitlebar(ids);
  const instance = RaisedTitlebarIcons() as unknown as m.Component;
  return (instance.view as unknown as (v: unknown) => AnyVnode).call(instance, {
    attrs: {
      anchors: titlebarAnchors(),
      selected,
      onDismiss,
      onSelect,
      unresolvedCount: 0,
      hasWorkspaceRequestDot,
      agentId: WORKSPACE_ID,
    },
  });
}

function iconIds(strip: AnyVnode): unknown[] {
  return collectVnodes(strip)
    .map((vnode) => attrsOf(vnode)["data-titlebar-popup"])
    .filter((id) => id !== undefined);
}

function icon(strip: AnyVnode, id: string): AnyVnode {
  const found = collectVnodes(strip).find(
    (vnode) => attrsOf(vnode)["data-titlebar-popup"] === id,
  );
  expect(found, `no raised ${id} icon`).toBeDefined();
  return found as AnyVnode;
}

/** A shell that records which of its surface transitions were asked for,
 * standing on `routePath` (the machine surface unless a test says otherwise,
 * i.e. the feed's non-route case). */
function makeShell(options: {
  routePath?: string;
  panelRouteBehindOverlay?: string;
} = {}): {
  shell: ShellState;
  calls: () => string[];
} {
  const calls: string[] = [];
  const shell = {
    displayedWorkspaceAgentId: () => WORKSPACE_ID,
    currentRoutePath: () => options.routePath ?? `/workspace/${WORKSPACE_ID}`,
    panelRouteBehindOverlay: options.panelRouteBehindOverlay ?? null,
    switchToNotifications: () => calls.push("switchToNotifications"),
    closeNotifications: () => calls.push("closeNotifications"),
    openHelp: () => calls.push("openHelp"),
  } as unknown as ShellState;
  return { shell, calls: () => calls };
}

describe("the raised titlebar icon strip", () => {
  it("draws only the icons the titlebar is actually showing", () => {
    // On a hub page the three machine tabs sit in a hidden crumb and measure
    // at nothing, so the strip is the right-hand pair alone -- not three
    // stacked ghosts at the origin.
    const strip = renderStrip(
      ["notifications-toggle", "help-toggle"],
      "notifications",
    );

    expect(iconIds(strip)).toEqual(["notifications", "help"]);
  });

  it("marks the open surface's icon selected and joins it to the panel", () => {
    const strip = renderStrip(
      ["notifications-toggle", "help-toggle"],
      "notifications",
    );
    const bell = icon(strip, "notifications");
    const bug = icon(strip, "help");

    expect(attrsOf(bell)["aria-selected"]).toBe("true");
    // Filled with the card's own surface, square-bottomed so it reads as
    // physically joined to the panel below it.
    expect(String(attrsOf(bell).className)).toContain("bg-surface-primary");
    expect(String(attrsOf(bell).className)).toContain("rounded-b-none");
    expect(attrsOf(bug)["aria-selected"]).toBe("false");
    // The others stay titlebar-coloured, so they read as titlebar buttons.
    expect(String(attrsOf(bug).className)).toContain("titlebar-surface");
  });

  it("closes from the icon you are on, and goes elsewhere from the others", () => {
    let dismissals = 0;
    const selections: TitlebarPopupId[] = [];
    const strip = renderStrip(
      ["notifications-toggle", "help-toggle"],
      "notifications",
      (id) => selections.push(id),
      () => (dismissals += 1),
    );

    (attrsOf(icon(strip, "notifications")).onclick as () => void)();
    expect(dismissals).toBe(1);
    expect(selections).toEqual([]);

    (attrsOf(icon(strip, "help")).onclick as () => void)();
    expect(dismissals).toBe(1);
    expect(selections).toEqual(["help"]);
  });

  it("carries the key tab's waiting-on-you dot, so the cue survives an open surface", () => {
    const ALL_IDS = [
      "ws-tab-permissions",
      "ws-tab-settings",
      "ws-tab-share",
      "notifications-toggle",
      "help-toggle",
    ] as const;
    const withDot = renderStrip(ALL_IDS, "help", undefined, undefined, true);
    const dotted = collectVnodes(icon(withDot, "permissions")).find(
      (vnode) => attrsOf(vnode).id === "permissions-badge-raised",
    );
    expect(dotted, "no raised waiting dot").toBeDefined();

    const withoutDot = renderStrip(ALL_IDS, "help");
    const undotted = collectVnodes(icon(withoutDot, "permissions")).find(
      (vnode) => attrsOf(vnode).id === "permissions-badge-raised",
    );
    expect(undotted).toBeUndefined();
  });

  it("labels the icon you are on as the way out", () => {
    const strip = renderStrip(
      ["notifications-toggle", "help-toggle"],
      "help",
    );

    expect(attrsOf(icon(strip, "help"))["aria-label"]).toBe(
      "Close Report a bug",
    );
    expect(attrsOf(icon(strip, "notifications"))["aria-label"]).toBe(
      "Notifications",
    );
  });
});

describe("openTitlebarPopup", () => {
  it("hands the bell its own switch, which survives the dismissal navigation", () => {
    const { shell, calls } = makeShell();

    openTitlebarPopup(shell, "notifications");

    expect(calls()).toEqual(["switchToNotifications"]);
  });

  it("leaves the feed to the arrival, so the overlay slot is never empty mid-switch", () => {
    // The feed is local state; the navigation closes it when it lands
    // (handleRouteChanged). Closed eagerly here, the Shell's one overlay slot
    // would render empty for the frames before the landing -- an un-dim /
    // re-dim flash between two surfaces that should hand off in place.
    const { shell, calls } = makeShell();

    openTitlebarPopup(shell, "help");

    expect(calls()).toEqual(["openHelp"]);
  });

  it("opens a machine tab on the displayed machine", () => {
    // From the machine surface itself (the feed's case: it is not a route),
    // this is an ordinary push -- dismissing the panel can go back here.
    const { shell, calls } = makeShell();
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    openTitlebarPopup(shell, "share");

    expect(calls()).toEqual([]);
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}/options`,
      { tab: "share" },
      undefined,
    );
  });

  it("replaces another popup's entry on the way to a machine tab", () => {
    // Get help -> the key icon is a switch between two of the five surfaces:
    // pushed, Get help would be left one Back away under the panel.
    const { shell } = makeShell({ routePath: "/help" });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    openTitlebarPopup(shell, "permissions");

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}/options`,
      { tab: "permissions" },
      { replace: true },
    );
  });

  it("hands the taken-over panel its window back on the asked tab", () => {
    // The request popup took the options panel's window over; a tab click
    // returns to the panel's own remembered route -- keeping the group and
    // section its tabs were left on -- rather than a fresh ?tab= open.
    const { shell } = makeShell({
      routePath: "/inbox",
      panelRouteBehindOverlay: `/workspace/${WORKSPACE_ID}/options?tab=permissions&section=waiting&group=backup`,
    });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    openTitlebarPopup(shell, "settings");

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}/options?tab=settings&section=waiting&group=backup`,
      undefined,
      { replace: true },
    );
  });

  it("has nowhere to open a machine tab with no machine on screen", () => {
    const { shell } = makeShell();
    (
      shell as unknown as { displayedWorkspaceAgentId: () => string | null }
    ).displayedWorkspaceAgentId = () => null;
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    openTitlebarPopup(shell, "permissions");

    expect(routeSet).not.toHaveBeenCalled();
  });
});
