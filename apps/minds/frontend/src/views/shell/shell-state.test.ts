import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { workspacesMessage } from "../../testing";
import type { WaitingRequestList } from "./shell-state";
import { ShellState } from "./shell-state";

const AGENT = "agent-ab12";

/** A window whose content frame is armed on `armedAnyId`, or has no frame at
 * all when it is null. The frame counts its own reloads. */
function shellWithFrameOn(armedAnyId: string | null): {
  shell: ShellState;
  reloadCount: () => number;
} {
  const shell = new ShellState(createEmptyStores());
  // The mapping the workspace_refresh path depends on: the event names the
  // AGENT, while the frame may be armed on either spelling.
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
  let count = 0;
  if (armedAnyId !== null) {
    shell.workspaceFrame = {
      armedWorkspaceAnyId: () => armedAnyId,
      reload: () => (count += 1),
    };
  }
  return { shell, reloadCount: () => count };
}

/** These tests run without a DOM; handleRouteChanged repaints the accent on
 * its way through, which is all it needs of the document. */
function stubAccentPainting(): void {
  vi.stubGlobal("document", {
    documentElement: {
      style: { setProperty: () => undefined, removeProperty: () => undefined },
    },
    getElementById: () => null,
  });
}

/** Put the shell on the machine's own surface, so the route changes that
 * follow are judged against the machine an open card speaks for. */
function displaying(shell: ShellState, agentId: string): void {
  stubAccentPainting();
  shell.handleRouteChanged(`/workspace/${agentId}`);
}

// The ask follows the MOUNTED FRAME, not the routed content surface. Those
// diverge on the routes that float an app modal over a workspace (/help,
// /inbox, /settings/ai-keys, /create/template): the frame stays mounted and
// visible behind the card while `displayedWorkspaceAnyId` is null, and the
// Shell keeps it at a stable vtree position so dismissing the modal does not
// remount it either. Keying off the frame is what covers those windows.
const WORKSPACE_ID = "agent-ab12";

afterEach(() => {
  // restoreAllMocks does NOT undo vi.stubGlobal; only unstubAllGlobals does.
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function makeShell(): ShellState {
  // Accent painting writes to the document; these cases are about routing, so
  // the writes go to a stub rather than pulling a DOM into the suite.
  vi.stubGlobal("document", {
    documentElement: {
      style: { setProperty: () => undefined, removeProperty: () => undefined },
    },
    getElementById: () => null,
  });
  const shell = new ShellState(createEmptyStores());
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  return shell;
}

/** Put the shell on `route` the way the router does, so the state it derives
 * from a route (displayed workspace, the panel behind an overlay) is real. */
function land(shell: ShellState, route: string): void {
  vi.spyOn(m.route, "get").mockReturnValue(route);
  const [path, search = ""] = route.split("?");
  shell.handleRouteChanged(path, search);
}

describe("ShellState.openInbox", () => {
  it("floats the popup over the workspace on screen, which stays mounted", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(routeSet).toHaveBeenCalledWith(
      "/inbox",
      { selected: "evt-a", workspace: WORKSPACE_ID },
      undefined,
    );
  });

  it("never stacks a second /inbox push for the same request racing to open it", () => {
    // Regression test for the reported stale-duplicate-popup bug: two
    // callers racing to open the SAME request (a toast and a feed row, an
    // embed-contract message and a notification click, or simply a double
    // click) can both read the route as "not /inbox yet" and both push --
    // openInbox's own replace-in-place logic only kicks in once the FIRST
    // push has actually landed on /inbox, which does not happen
    // synchronously. The dedup guard closes that window regardless of the
    // exact source of the second call.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });
    // Still reads as /workspace/WORKSPACE_ID: the mocked route.set never
    // actually lands, exactly the race window this guards.
    shell.openInbox({ selected: "evt-a" });

    expect(routeSet).toHaveBeenCalledTimes(1);
  });

  it("does not dedup two DIFFERENT requests racing at once", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });
    shell.openInbox({ selected: "evt-b" });

    expect(routeSet).toHaveBeenCalledTimes(2);
  });

  it("remembers the Permissions pane it was opened from", () => {
    // The pane stays mounted underneath, and is what the popup goes back to.
    const shell = makeShell();
    const optionsRoute = `/workspace/${WORKSPACE_ID}/options?tab=permissions&section=waiting`;
    land(shell, optionsRoute);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.panelRouteBehindOverlay).toBe(optionsRoute);
  });

  it("names no pane when the popup was opened from the chat", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.panelRouteBehindOverlay).toBeNull();
  });
});

describe("ShellState.rememberPageBehindOverlay", () => {
  const RECOVERY_ROUTE = `/agents/${WORKSPACE_ID}/recovery?return_to=%2Fgoto%2Fhost-bb22%2F`;

  it("keeps the recovery page behind the modal opened from it", () => {
    // Get help forwards ?workspace=, which is what tells the Shell to paint a
    // machine behind the form -- and over this page that machine is the one
    // that would not load. The page the reader is reporting on stays instead.
    const shell = makeShell();
    land(shell, RECOVERY_ROUTE);

    shell.rememberPageBehindOverlay();

    expect(shell.pageRouteBehindOverlay).toBe(RECOVERY_ROUTE);
  });

  it("remembers nothing from a surface whose own backdrop is right", () => {
    // The same card as a modal over a live machine: that machine is already
    // painted underneath and is what the form should float on.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);

    shell.rememberPageBehindOverlay();

    expect(shell.pageRouteBehindOverlay).toBeNull();
  });

  it("forgets the page once the reader has left the modal", () => {
    const shell = makeShell();
    land(shell, RECOVERY_ROUTE);
    shell.rememberPageBehindOverlay();
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=0`);
    expect(shell.pageRouteBehindOverlay).toBe(RECOVERY_ROUTE);

    land(shell, RECOVERY_ROUTE);

    expect(shell.pageRouteBehindOverlay).toBeNull();
  });

  it("dismisses back to the page when there is no history to go back through", () => {
    // A window opened straight onto the form (session restore, a deeplink):
    // the fallback must not send the reader to the machine ?workspace= names,
    // which is the one destination this page exists because it does not work.
    const shell = makeShell();
    land(shell, RECOVERY_ROUTE);
    shell.rememberPageBehindOverlay();
    vi.stubGlobal("window", { history: { length: 1, back: () => undefined } });
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=0`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenCalledWith(RECOVERY_ROUTE);
  });
});

describe("ShellState.closeAppOverlay", () => {
  it("routes /inbox directly to its named workspace, never through history.back()", () => {
    // /inbox can be reached by a multi-push notification jump (workspace,
    // THEN the popup), which breaks history.back()'s "undo exactly one push"
    // assumption -- it would land on whatever was on screen BEFORE the jump,
    // not the workspace the popup was actually reviewing. Routing to the
    // ?workspace= the popup already names is correct regardless of how many
    // entries getting here pushed.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
  });

  it("does not fire a second dismissal when Escape is held down", () => {
    // The router re-runs handleRouteChanged on every redraw, and the
    // dismissal's own route.set does not land synchronously -- so the guard
    // must survive a redraw on the route still being left, or a held Escape
    // (repeating every ~30ms) would fire the dismissal a second time.
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 5, back: vi.fn() } });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);

    expect(shell.closeAppOverlay()).toBe(true);
    // A redraw lands before the navigation does: same path, not a navigation.
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    expect(shell.closeAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenCalledTimes(1);
  });

  it("closes again once the dismissal has actually landed", () => {
    // The guard is not a one-way latch: arriving somewhere new clears it, so a
    // later overlay still closes.
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 5, back: vi.fn() } });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    expect(shell.closeAppOverlay()).toBe(true);

    land(shell, `/workspace/${WORKSPACE_ID}`);
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    expect(shell.closeAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenCalledTimes(2);
  });

  it("still prefers history.back() for an app overlay with no workspace to name (e.g. Get help)", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, "/help");

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).toHaveBeenCalledTimes(1);
  });
});

describe("ShellState review deep link", () => {
  // consumeReviewParam runs from inside the router's render() (Mithril
  // resolving the ?review= navigation), so its m.route.set calls are queued
  // past this microtask tick rather than issued inline -- a route change
  // issued synchronously from there would be a NESTED one, reentering
  // Mithril's render while it is still committing the current one (this is
  // what produced a stale duplicate popup live). One microtask flush lets a
  // queued consumption run to completion.
  const flush = () => Promise.resolve();

  it("consumes ?review= once: strips it by replacement, then opens the popup for a pending request", async () => {
    const shell = makeShell();
    shell.stores.requests.requestIds = ["evt-1"];
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    await flush();

    expect(routeSet).toHaveBeenNthCalledWith(
      1,
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      {
        replace: true,
      },
    );
    expect(routeSet).toHaveBeenNthCalledWith(
      2,
      "/inbox",
      { selected: "evt-1", workspace: WORKSPACE_ID },
      undefined,
    );

    // The stripping set lands a tick later, so redraws still see the review
    // route; none of them may consume it again.
    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    await flush();
    expect(routeSet).toHaveBeenCalledTimes(2);
  });

  it("lands a still-creating machine's deep link on its creating page, popup deferred", async () => {
    const shell = makeShell();
    const base = workspacesMessage().workspaces[0];
    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({
        workspaces: [
          {
            ...base,
            id: WORKSPACE_ID,
            host_id: "host-cd34",
            create_attempt_state: "creating",
          },
        ],
      }),
    );
    shell.stores.requests.requestIds = ["evt-1"];
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    await flush();

    expect(routeSet).toHaveBeenCalledWith(
      `/creating/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    // Never the popup: /inbox over a creating machine would background to
    // Home; the ask stays in the bell until the machine is up.
    expect(routeSet).not.toHaveBeenCalledWith("/inbox", expect.anything());
    expect(routeSet.mock.calls.some(([path]) => path === "/inbox")).toBe(false);
  });

  it("strips a resolved or unknown review id without opening the popup", async () => {
    const shell = makeShell();
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-gone`);
    await flush();

    expect(routeSet).toHaveBeenCalledTimes(1);
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
  });

  it("consumes a later deep link afresh once the stripped route has landed", async () => {
    const shell = makeShell();
    shell.stores.requests.requestIds = ["evt-1"];
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    await flush();
    // Simulate the popup's own landing and dismissal (m.route.set is mocked
    // to a no-op above, so nothing gets here on its own): openInbox's dedup
    // guard is keyed on actually having left /inbox, not merely on time, so
    // a realistic close-then-reopen cycle has to walk through it to prove
    // the SECOND consumption is treated as fresh rather than a duplicate of
    // the first.
    land(shell, `/inbox?selected=evt-1&workspace=${WORKSPACE_ID}`);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    await flush();

    // Two full consumptions: strip + open, twice.
    expect(routeSet).toHaveBeenCalledTimes(4);
  });

  it("ignores ?review= off the workspace surface", async () => {
    const shell = makeShell();
    shell.stores.requests.requestIds = ["evt-1"];
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, "/create?review=evt-1");
    await flush();

    expect(routeSet).not.toHaveBeenCalled();
  });

  it("never leaves a second popup behind: a nested route.set during the landing render does not double-resolve", async () => {
    // Regression test for the reported "double popup, stale one below" bug:
    // consumeReviewParam must not call m.route.set synchronously from within
    // handleRouteChanged (itself invoked from the router's render()), since a
    // route change issued there reenters Mithril mid-render. Simulate that
    // reentrancy directly -- land() while still "inside" a route resolution
    // (i.e. before any microtask has flushed) -- and assert nothing routes
    // until the render this consumption belongs to has fully committed.
    const shell = makeShell();
    shell.stores.requests.requestIds = ["evt-1"];
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?review=evt-1`);
    // Still synchronous: nothing has routed yet, so nothing CAN double-render.
    expect(routeSet).not.toHaveBeenCalled();

    await flush();
    expect(routeSet).toHaveBeenCalledTimes(2);
  });
});

describe("ShellState notifications popover", () => {
  it("opens as local state over the current surface, never a navigation", () => {
    const shell = makeShell();
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, "/create");

    shell.openNotifications();

    expect(shell.isNotificationsOpen).toBe(true);
    // The surface underneath is untouched: no route change at all.
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("retires the floating toasts the moment it opens", () => {
    const shell = makeShell();
    let cleared = 0;
    shell.notificationsUi = {
      clearLiveToasts: () => (cleared += 1),
    } as unknown as NonNullable<ShellState["notificationsUi"]>;

    shell.openNotifications();

    expect(cleared).toBe(1);
  });

  it("closes on Escape first, without touching history", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);

    shell.openNotifications();
    expect(shell.isNotificationsOpen).toBe(true);
    // The popover sits on top, so Escape takes it -- not the app overlay it
    // was opened over, which would have fired history.back().
    expect(shell.handleEscape()).toBe(true);
    expect(shell.isNotificationsOpen).toBe(false);
    expect(back).not.toHaveBeenCalled();
  });

  it("closes on any navigation, like a dropdown would", () => {
    const shell = makeShell();
    land(shell, "/create");
    shell.openNotifications();

    // A redraw on the same route is not a navigation: still open.
    land(shell, "/create");
    expect(shell.isNotificationsOpen).toBe(true);

    // Leaving the surface (a feed row's jump to a machine) closes it.
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isNotificationsOpen).toBe(false);
  });
});

describe("ShellState.switchToNotifications", () => {
  it("puts an open app modal away and raises the feed in its place", () => {
    // Clicking the bell from Get help is a switch between two titlebar
    // popups, not a navigation away from a surface -- so the arrival at the
    // route the modal is dismissed to must not close the feed it opened.
    const shell = makeShell();
    // Real history depth, since Get help is a click away from the docked
    // options panel: history.back() would land back ON that panel, which would
    // then be standing under the feed this switch is opening.
    let backCount = 0;
    vi.stubGlobal("window", {
      history: { length: 5, back: () => (backCount += 1) },
    });
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.switchToNotifications();

    // Straight to the machine Get help named, replacing its entry rather than
    // walking back through whatever opened it.
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(backCount).toBe(0);
    expect(shell.isNotificationsOpen).toBe(true);
    // The dismissal navigation lands: the feed rides across it.
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isNotificationsOpen).toBe(true);
    // And an ORDINARY navigation after that still closes it.
    land(shell, "/create");
    expect(shell.isNotificationsOpen).toBe(false);
  });

  it("falls back to history for a Get help that names no machine", () => {
    // Opened from a hub page there is no machine to route to, and no titlebar
    // popup can be waiting back there to come up under the feed either.
    const shell = makeShell();
    let backCount = 0;
    vi.stubGlobal("window", {
      history: { length: 5, back: () => (backCount += 1) },
    });
    land(shell, "/help");

    shell.switchToNotifications();

    expect(backCount).toBe(1);
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("closes the docked options panel on its way to the feed, replacing its entry", () => {
    // Replaced like every other strip switch: pushed, the panel would sit one
    // Back away under the feed.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=share`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.switchToNotifications();

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("puts a centered app modal away first, so the feed never raises beneath its backdrop", () => {
    // The titlebar's real bell stays painted under Minds settings / Accounts
    // (no raised strip covers it there), and the feed's backdrop draws under
    // a later-DOM modal's at the same z -- so the bell's click must put the
    // modal away, not float the feed beneath it.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, "/settings");

    shell.switchToNotifications();

    expect(back).toHaveBeenCalledTimes(1);
    expect(shell.isNotificationsOpen).toBe(true);
    // The dismissal navigation lands: the feed rides across it.
    land(shell, "/");
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("just opens the feed when there is no popup to put away", () => {
    // Nothing was navigated, so nothing is armed: the next navigation closes
    // the feed exactly as it would have before.
    const shell = makeShell();
    land(shell, "/create");

    shell.switchToNotifications();
    expect(shell.isNotificationsOpen).toBe(true);

    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isNotificationsOpen).toBe(false);
  });
});

describe("ShellState.openHelp", () => {
  it("floats over the displayed machine as an ordinary push", () => {
    // No titlebar popup is up (the titlebar's own bug button), so dismissing
    // Get help must be able to go back to this machine entry.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelp();

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { workspace: WORKSPACE_ID, assist: "1" },
      undefined,
    );
  });

  it("replaces another titlebar popup's entry when switched to from its strip", () => {
    // Options panel -> bug icon is a switch between two of the five surfaces:
    // pushed, the panel would be left one Back away under Get help, and
    // dismissing the form would re-raise the panel that was just left.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=share`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelp();

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { workspace: WORKSPACE_ID, assist: "1" },
      { replace: true },
    );
  });

  it("lets go of the panel the request popup was remembering", () => {
    // Switching the popup for Get help leaves the panel like it leaves the
    // popup. Kept, the panel stayed painted under Get help with its own
    // backdrop and its own raised strip -- two of the five surfaces at once.
    const shell = makeShell();
    const optionsRoute = `/workspace/${WORKSPACE_ID}/options?tab=permissions&section=waiting`;
    land(shell, optionsRoute);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    expect(shell.panelRouteBehindOverlay).toBe(optionsRoute);

    shell.openHelp();

    expect(routeSet).toHaveBeenLastCalledWith(
      "/help",
      { workspace: WORKSPACE_ID, assist: "1" },
      { replace: true },
    );
    // The panel lives exactly as long as /inbox is the route: it is let go
    // the moment the /help route lands -- however /help was reached, the
    // strip or an Electron open-overlay ask alike.
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    expect(shell.panelRouteBehindOverlay).toBeNull();
  });
});

describe("ShellState.dismissAppOverlay", () => {
  it("leaves the popup's window for the machine, replacing the popup's entry", () => {
    // The popup took the panel's window over, so dismissing it leaves the
    // window rather than uncovering the panel -- and replaces, so Back from
    // the machine does not re-raise the popup that was just dismissed.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=permissions`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);

    expect(shell.dismissAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenLastCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
  });
});

describe("ShellState.returnToPanelAfterRequest", () => {
  const PANEL_ROUTE = `/workspace/${WORKSPACE_ID}/options?tab=permissions&section=waiting`;

  function listWith(count: number): WaitingRequestList {
    return {
      forgetWaitingRequest: () => undefined,
      hasWaitingRequests: () => count > 0,
    };
  }

  it("hands the pane back on its own list while other requests are still waiting", () => {
    // The reader picked this request off that list and the rest of it is still
    // theirs to work through, so they land back on it, on the section they left.
    const shell = makeShell();
    shell.panelRouteBehindOverlay = PANEL_ROUTE;
    shell.registerWaitingRequestList(listWith(1));
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToPanelAfterRequest()).toBe(true);

    expect(routeSet.mock.calls[0][0]).toBe(PANEL_ROUTE);
  });

  it("hands the pane back on Add connection once nothing is left to answer", () => {
    // The list that led here has just emptied, so returning to it would be
    // returning to nothing.
    const shell = makeShell();
    shell.panelRouteBehindOverlay = PANEL_ROUTE;
    shell.registerWaitingRequestList(listWith(0));
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToPanelAfterRequest()).toBe(true);

    expect(routeSet.mock.calls[0][0]).toContain("section=add-connection");
    expect(routeSet.mock.calls[0][0]).toContain("tab=permissions");
  });

  it("hands the pane back on Add connection when no pane list ever registered", () => {
    // A panel that never reached its Permissions tab has no list to speak for
    // it; Add connection is the safe landing, never a section that is gone.
    const shell = makeShell();
    shell.panelRouteBehindOverlay = PANEL_ROUTE;
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToPanelAfterRequest()).toBe(true);

    expect(routeSet.mock.calls[0][0]).toContain("section=add-connection");
  });

  it("reports no pane for a page opened from the chat", () => {
    // Nothing to hand back to: that page simply closes.
    const shell = makeShell();
    shell.registerWaitingRequestList(listWith(3));

    expect(shell.returnToPanelAfterRequest()).toBe(false);
  });
});

describe("ShellState waiting-request list", () => {
  it("drops an answered request from the pane behind the popup", () => {
    // The popup answers it; the pane behind is what shows the list it was in.
    const shell = makeShell();
    const forgotten: string[] = [];
    shell.registerWaitingRequestList({
      forgetWaitingRequest: (id) => forgotten.push(id),
      hasWaitingRequests: () => false,
    });

    shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: "agent-other",
      verdict: "granted",
    });

    // Unconditional, unlike the relay: the list is this window's own, whichever
    // machine the request belongs to.
    expect(forgotten).toEqual(["evt-a"]);
  });

  it("keeps the live list when a torn-down pane unregisters its own", () => {
    const shell = makeShell();
    const forgotten: string[] = [];
    const live = {
      forgetWaitingRequest: (id: string) => forgotten.push(id),
      hasWaitingRequests: () => false,
    };
    shell.registerWaitingRequestList(live);

    shell.unregisterWaitingRequestList({
      forgetWaitingRequest: () => undefined,
      hasWaitingRequests: () => false,
    });
    shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: null,
      verdict: "denied",
    });

    expect(forgotten).toEqual(["evt-a"]);
  });
});

describe("ShellState displayed workspace", () => {
  it("still counts the workspace a modal floats over as displayed", () => {
    // What addresses a verdict to the machine that asked: the popup is the
    // current route, but that machine's frame is mounted right behind it.
    const shell = makeShell();
    land(shell, `/inbox?workspace=${WORKSPACE_ID}&selected=evt-a`);
    expect(shell.displayedWorkspaceAnyId).toBe(WORKSPACE_ID);

    land(shell, "/inbox");
    expect(shell.displayedWorkspaceAnyId).toBeNull();
  });
});

describe("ShellState permission-resolution relay", () => {
  /** A shell showing `displayed`, with a mounted frame's sender registered. */
  function relayOver(displayed: string | null): {
    shell: ShellState;
    sent: [string, string][];
  } {
    const shell = makeShell();
    const sent: [string, string][] = [];
    shell.registerPermissionResolvedSender((requestId, verdict) =>
      sent.push([requestId, verdict]),
    );
    shell.displayedWorkspaceAnyId = displayed;
    return { shell, sent };
  }

  it("tells the workspace the resolved request belongs to", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: WORKSPACE_ID,
      verdict: "granted",
    });

    expect(sent).toEqual([["evt-a", "granted"]]);
  });

  it("says nothing to a workspace that did not ask", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: "agent-other",
      verdict: "denied",
    });

    expect(sent).toEqual([]);
  });

  it("says nothing with no workspace on screen, or no workspace on the card", () => {
    const offScreen = relayOver(null);
    offScreen.shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: WORKSPACE_ID,
      verdict: "denied",
    });
    expect(offScreen.sent).toEqual([]);

    const unresolved = relayOver(WORKSPACE_ID);
    unresolved.shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: null,
      verdict: "denied",
    });
    expect(unresolved.sent).toEqual([]);
  });

  it("keeps the live sender when a torn-down frame unregisters its own", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.unregisterPermissionResolvedSender(() => undefined);
    shell.notifyRequestResolved({
      requestId: "evt-a",
      agentId: WORKSPACE_ID,
      verdict: "denied",
    });

    expect(sent).toEqual([["evt-a", "denied"]]);
  });
});

// The ask follows the MOUNTED FRAME, not the routed content surface. Those
// diverge on the routes that float an app modal over a workspace (/help,
// /inbox, /settings/ai-keys, /create/template): the frame stays mounted and
// visible behind the card while `displayedWorkspaceAnyId` is null, and the
// Shell keeps it at a stable vtree position so dismissing the modal does not
// remount it either. Keying off the frame is what covers those windows.

describe("ShellState.reloadWorkspaceFrame", () => {
  it("reloads the frame when the named workspace is the one on screen", () => {
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11");

    shell.reloadWorkspaceFrame("agent-aa11");

    expect(reloadCount()).toBe(1);
  });

  it("reloads a host-scoped surface named by its agent id", () => {
    const { shell, reloadCount } = shellWithFrameOn("host-bb22");

    shell.reloadWorkspaceFrame("agent-aa11");

    expect(reloadCount()).toBe(1);
  });

  it("leaves a window whose frame shows a different workspace alone", () => {
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11");

    shell.reloadWorkspaceFrame("agent-cc33");

    expect(reloadCount()).toBe(0);
  });

  it("leaves a frameless window alone, so recovery keeps ownership of its screen", () => {
    // Hub pages, recovery, destroying and the workspace sub-pages mount no
    // frame at all. The recovery page is the case that matters: its own
    // machinery navigates it home once the workspace is healthy, and a refresh
    // must not reach in and re-navigate that screen.
    const { shell, reloadCount } = shellWithFrameOn(null);

    shell.reloadWorkspaceFrame("agent-aa11");

    expect(reloadCount()).toBe(0);
  });
});

describe("recovery card openness", () => {
  let shell: ShellState;

  beforeEach(() => {
    shell = new ShellState(createEmptyStores());
  });

  afterEach(() => {
    // restoreAllMocks does NOT undo vi.stubGlobal; only this does.
    vi.unstubAllGlobals();
  });

  it("stays shut for every state but the one that means a restart is worth offering", () => {
    displaying(shell, AGENT);
    for (const health of ["stuck", "recovering", "healthy"] as const) {
      shell.handleHealthChanged(AGENT, health, false);
      expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
    }
  });

  it("raises itself on the edge into recovery_failed for the displayed machine", () => {
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    expect(shell.isRecoveryModalAutoRaised(AGENT)).toBe(true);
  });

  it("does not raise itself for a failure that predates the window", () => {
    // The connect snapshot replays the state the machine is already in. That
    // is not a transition, so the band reports it and "Open recovery" is one
    // click away -- a card taking over a window the user just opened is not.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", true);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("drops the card it raised once the machine answers again", () => {
    // Nothing raised it but the failure, and the failure is over -- a machine
    // that came back on its own would otherwise leave a window reading
    // "unresponsive" over a working machine.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    shell.handleHealthChanged(AGENT, "healthy", false);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("gives up a card it raised when a fresh snapshot starts", () => {
    // A machine that recovered while the socket was down replays no frame at
    // all, so nothing else would ever drop the card.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);

    shell.handleSnapshotStart();

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("keeps a card the user opened across a snapshot", () => {
    // Theirs to close. A reconnect is not an answer to why they opened it.
    shell.openRecoveryModal(AGENT);

    shell.handleSnapshotStart();

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("leaves a card the user opened up when the machine answers again", () => {
    // They asked to be there, so the card gets to tell them how it ended.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.handleHealthChanged(AGENT, "healthy", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("does not raise itself for a machine the window is not showing", () => {
    displaying(shell, "agent-cd34");
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("does not raise itself while the discovery consumer is dead", () => {
    // Every machine reads unhealthy then, and the card's own actions route
    // through the forward the dead consumer feeds -- so it would offer
    // "Restart Machine" over a band correctly saying only the app restart helps.
    displaying(shell, AGENT);
    shell.stores.health.applyDiscoveryHealthMessage({
      type: "discovery_health",
      state: "blocked",
    });
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("still opens on request while the consumer is dead", () => {
    shell.stores.health.applyDiscoveryHealthMessage({
      type: "discovery_health",
      state: "blocked",
    });
    shell.openRecoveryModal(AGENT);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("does not re-raise itself over a card the user is already looking at", () => {
    // A second recovery_failed frame -- a fresh failure reason on the same
    // machine -- must not convert a deliberately opened card into one that
    // will dismiss itself under the user.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalAutoRaised(AGENT)).toBe(false);
  });

  it("marks a card the user asked for as theirs to close", () => {
    shell.openRecoveryModal(AGENT);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    expect(shell.isRecoveryModalAutoRaised(AGENT)).toBe(false);
  });

  it("does not follow the user to a different machine", () => {
    shell.openRecoveryModal(AGENT);
    displaying(shell, "agent-cd34");
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("survives an app modal opened over the machine it speaks for", () => {
    // The card's own "Report a problem" routes to /help?workspace=<id>, which
    // keeps the machine's surface mounted behind the backdrop. The Shell
    // renders no card while an app modal is up but expects it back on the way
    // out, so dropping it there would mean the card's own action discarded it.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.handleRouteChanged("/help", `workspace=${AGENT}`);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    displaying(shell, AGENT);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("drops the card for an app modal that left the machine behind", () => {
    // Minds settings opened from Home carries no ?workspace, so nothing of the
    // machine is on screen and the card has nothing to sit over.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.handleRouteChanged("/settings");
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("asks the frame to re-fetch when the user closes the card", () => {
    // A card can be closed while the machine is still down, so no recovery is
    // coming to refresh the frame. Dropping the card without reloading would
    // uncover the dead page the frame still holds.
    let reloadCount = 0;
    shell.workspaceFrame = {
      armedWorkspaceAnyId: () => AGENT,
      reload: () => (reloadCount += 1),
    };

    shell.openRecoveryModal(AGENT);
    shell.closeRecoveryModal();

    expect(reloadCount).toBe(1);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("leaves the reload to the server when the machine recovers under the card", () => {
    // The recovery edge broadcasts a workspace_refresh that every window
    // applies to its own frame. Reloading here too would be a second owner of
    // one behavior, on the same edge, covering only the windows with a card up.
    // Driven through the health edges, since that is the only way production
    // reaches the drop: the card has to be one the shell raised itself.
    let reloadCount = 0;
    displaying(shell, AGENT);
    shell.workspaceFrame = {
      armedWorkspaceAnyId: () => AGENT,
      reload: () => (reloadCount += 1),
    };

    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    shell.handleHealthChanged(AGENT, "healthy", false);

    expect(reloadCount).toBe(0);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });
});

describe("ShellState.handleEscape", () => {
  let shell: ShellState;

  beforeEach(() => {
    shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports the key untaken when nothing is open", () => {
    // The listener only consumes an Escape it used, so one nobody wanted still
    // reaches whatever is focused.
    displaying(shell, AGENT);

    expect(shell.handleEscape()).toBe(false);
  });

  it("gives the key to the switcher popover over an open card", () => {
    // The popover is the one surface that can open ON TOP of the card, so it
    // has to be asked first -- and the card must survive the keypress.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openSidebar({ x: 0, y: 0, width: 10, height: 10 });

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isSidebarOpen).toBe(false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("closes the card once the popover above it is gone", () => {
    // The keypress after the one the popover took: the card is next in line,
    // not skipped over because the popover was there first.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openSidebar({ x: 0, y: 0, width: 10, height: 10 });
    shell.handleEscape();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("gives the key to the notification feed over an open card", () => {
    // The feed is the other surface that can open ON TOP of the card (see
    // handleEscape's own doc comment), so it takes the keypress first and the
    // card must survive it.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openNotifications();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("closes the card once the feed above it is gone", () => {
    // The keypress after the one the feed took: the card is next in line, not
    // skipped over because the feed was there first.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openNotifications();
    shell.handleEscape();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });
});

describe("ShellState update modal", () => {
  type UpdateActivity = "IDLE" | "RUNNING" | "APPLYING";

  function applyUpdateActivity(shell: ShellState, activity: UpdateActivity): void {
    shell.stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: {
        [AGENT]: {
          availability: "OUT_OF_DATE",
          current_version: "minds-v0.3.9",
          supported_version: "minds-v0.4.1",
          is_version_from_label: false,
          activity,
        },
      },
      update_window: "2:00 AM-5:00 AM",
    });
  }

  function outOfDateShell(activity: UpdateActivity = "IDLE"): ShellState {
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    applyUpdateActivity(shell, activity);
    return shell;
  }

  it("opens for a machine with no machine displayed, which is how the machines list opens it", () => {
    // From a row on Home there is no displayed machine to key on.
    const shell = outOfDateShell();
    shell.openUpdateModal(AGENT);
    expect(shell.openUpdateModalAgentId()).toBe(AGENT);
  });

  it("is dismissed by Escape wherever it was opened from", () => {
    const shell = outOfDateShell();
    shell.openUpdateModal(AGENT);
    expect(shell.handleEscape()).toBe(true);
    expect(shell.openUpdateModalAgentId()).toBeNull();
  });

  it("is dropped by a navigation to anywhere but its own machine", () => {
    const shell = outOfDateShell();
    stubAccentPainting();
    shell.handleRouteChanged("/");
    shell.openUpdateModal(AGENT);
    shell.handleRouteChanged("/settings");
    expect(shell.openUpdateModalAgentId()).toBeNull();
  });

  it("rides the navigation into its own machine, which Update now makes before dispatching", () => {
    // The press enters the machine before dispatching (the chat tab opens only
    // for connected clients), so the modal must survive that navigation.
    const shell = outOfDateShell();
    stubAccentPainting();
    shell.handleRouteChanged("/");
    shell.openUpdateModal(AGENT);
    shell.handleRouteChanged(`/workspace/${AGENT}`);
    expect(shell.openUpdateModalAgentId()).toBe(AGENT);
    // Not pinned there: leaving the machine afterwards drops it like any other.
    shell.handleRouteChanged("/");
    expect(shell.openUpdateModalAgentId()).toBeNull();
  });

  it("leaves the recovery card alone while the machine is mid-apply", () => {
    // The app took the services down itself; a recovery card would blame the
    // machine for it.
    const shell = outOfDateShell();
    applyUpdateActivity(shell, "APPLYING");
    displaying(shell, AGENT);

    shell.handleHealthChanged(AGENT, "recovery_failed", false);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("still retires an auto-raised recovery card once a mid-apply machine answers", () => {
    // Health frames are edge-published: a healthy frame skipped mid-apply is
    // the only one the machine sends.
    const shell = outOfDateShell();
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "recovery_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    applyUpdateActivity(shell, "APPLYING");

    shell.handleHealthChanged(AGENT, "healthy", false);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("raises the recovery card for a machine that dies mid-prepare, as for any other outage", () => {
    // Only the apply is an expected outage.
    const shell = outOfDateShell();
    applyUpdateActivity(shell, "RUNNING");
    displaying(shell, AGENT);

    shell.handleHealthChanged(AGENT, "recovery_failed", false);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });
});

describe("enterWorkspaceOrRecover", () => {
  function shellWithMachine(liveness: string): { shell: ShellState; routeSet: ReturnType<typeof vi.fn> } {
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const entry = shell.stores.workspaces.entryByAnyId("agent-aa11");
    if (entry === null) throw new Error("fixture machine missing");
    shell.enterWorkspaceOrRecover(entry, liveness);
    return { shell, routeSet };
  }

  it("lands a healthy running machine on its surface", () => {
    const { routeSet } = shellWithMachine("RUNNING");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {});
  });

  it("sends a stopped machine through Recovery with a start, returning to the surface", () => {
    // Entering a stopped container directly would strand the user on the
    // loader; Recovery dispatches the start and comes back via /goto.
    const { routeSet } = shellWithMachine("STOPPED");
    expect(routeSet).toHaveBeenCalledWith(
      `/agents/agent-aa11/recovery?return_to=${encodeURIComponent("/goto/agent-aa11/")}&intent=start`,
    );
  });

  it("sends an unhealthy machine to Recovery without a start", () => {
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    shell.stores.health.applyHealthMessage({ agent_id: "agent-aa11", status: "stuck" });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const entry = shell.stores.workspaces.entryByAnyId("agent-aa11");
    if (entry === null) throw new Error("fixture machine missing");
    shell.enterWorkspaceOrRecover(entry, "RUNNING");
    expect(routeSet).toHaveBeenCalledWith(
      `/agents/agent-aa11/recovery?return_to=${encodeURIComponent("/goto/agent-aa11/")}`,
    );
  });
});
