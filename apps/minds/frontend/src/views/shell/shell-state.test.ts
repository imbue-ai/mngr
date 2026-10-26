import m from "mithril";
import type { WorkspaceMessage } from "../../models/workspaceMessage";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { takePendingHelpLaunch } from "../../models/help";
import { WebLoginModel } from "../../models/webLogin";
import type { UiChannelClient } from "../../channel/client";
import {
  accountEntry,
  accountsMessage,
  jsonResponse,
  secondAccountEntry,
  settle,
  workspacesMessage,
} from "../../testing";
import type { WaitingRequestList } from "./shell-state";
import { ShellState } from "./shell-state";

const AGENT = "agent-ab12";

/** A window whose content frame is armed on `armedAnyId`, or has no frame at
 * all when it is null. The frame counts its own reloads. */
function shellWithFrameOn(
  armedAnyId: string | null,
  lastLoadedAtMs: number | null = null,
): {
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
      lastLoadedAtMs: () => lastLoadedAtMs,
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
  // Module-level, and openHelpForAgentAsk reads it: a launch left staged would
  // turn the next test's push into a replace.
  takePendingHelpLaunch();
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
      { selected: "evt-a", workspace: WORKSPACE_ID, account: "local" },
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
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

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

  it("routes Get help directly to the machine it names, never through history.back()", () => {
    // history.back() can land on another /help entry, a dismissal that never
    // visibly lands.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, `/help?workspace=${WORKSPACE_ID}`);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
  });

  it("closes the template stepper floating over a machine on its account's hub route", () => {
    // The stepper is opened on /account/<id>/create/template?workspace=: the
    // hub prefix must not hide the modal from the dismissal.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/account/local/create/template?workspace=${WORKSPACE_ID}`);
    expect(shell.displayedWorkspaceAnyId).toBe(WORKSPACE_ID);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).toHaveBeenCalledTimes(1);
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

describe("ShellState chat deep link and workspace-message relay", () => {
  const flush = () => Promise.resolve();
  /** Register a sender that records focus-chat asks as [workspace, chat] pairs. */
  const recordFocusChats = (
    shell: ShellState,
    sent: [string, string][],
    isReady: () => boolean = () => true,
  ): void => {
    shell.registerWorkspaceMessageSender((workspaceAgentId, message) => {
      if (!isReady()) return false;
      if (message.kind !== "focus-chat") throw new Error(message.kind);
      sent.push([workspaceAgentId, message.chatAgentId]);
      return true;
    });
  };

  it("consumes ?chat= once: strips it by replacement and asks the frame for the chat", async () => {
    const shell = makeShell();
    const sent: [string, string][] = [];
    recordFocusChats(shell, sent);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?chat=agent-cc33`);
    await flush();

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(sent).toEqual([[WORKSPACE_ID, "agent-cc33"]]);

    land(shell, `/workspace/${WORKSPACE_ID}?chat=agent-cc33`);
    await flush();
    expect(sent).toHaveLength(1);
  });

  it("holds the ask while the frame is not ready and sends it on the frame's load", async () => {
    const shell = makeShell();
    const sent: [string, string][] = [];
    let isFrameReady = false;
    recordFocusChats(shell, sent, () => isFrameReady);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}?chat=agent-cc33`);
    await flush();
    expect(sent).toEqual([]);

    isFrameReady = true;
    shell.flushPendingWorkspaceMessage();
    expect(sent).toEqual([[WORKSPACE_ID, "agent-cc33"]]);
    // Sent once: a second load does not repeat it.
    shell.flushPendingWorkspaceMessage();
    expect(sent).toHaveLength(1);
  });

  it("puts the feed overlay away on an in-place ask, which changes no route", () => {
    const shell = makeShell();
    shell.registerWorkspaceMessageSender(() => true);
    shell.openNotifications();

    shell.requestFocusChat(WORKSPACE_ID, "agent-cc33");

    expect(shell.isNotificationsOpen).toBe(false);
  });

  it("sends an app message (a notification's destination) as the frame posts it, and holds it until the frame is ready", () => {
    const shell = makeShell();
    const sent: [string, WorkspaceMessage][] = [];
    let isFrameReady = false;
    shell.registerWorkspaceMessageSender((workspaceAgentId, message) => {
      if (!isFrameReady) return false;
      sent.push([workspaceAgentId, message]);
      return true;
    });
    const message: WorkspaceMessage = {
      kind: "app-message",
      type: "minds:show-routine",
      fields: { routine_id: "inbox-digest" },
    };

    shell.requestWorkspaceMessage(WORKSPACE_ID, message);
    expect(sent).toEqual([]);

    isFrameReady = true;
    shell.flushPendingWorkspaceMessage();
    expect(sent).toEqual([[WORKSPACE_ID, message]]);
    shell.flushPendingWorkspaceMessage();
    expect(sent).toHaveLength(1);
  });

  it("holds only the newest unsent message: a later ask replaces an earlier one", () => {
    const shell = makeShell();
    shell.requestFocusChat(WORKSPACE_ID, "agent-cc33");
    shell.requestWorkspaceMessage(WORKSPACE_ID, {
      kind: "app-message",
      type: "minds:show-routine",
      fields: {},
    });
    const sent: WorkspaceMessage[] = [];
    shell.registerWorkspaceMessageSender((_workspaceAgentId, message) => {
      sent.push(message);
      return true;
    });

    shell.flushPendingWorkspaceMessage();

    expect(sent).toEqual([
      { kind: "app-message", type: "minds:show-routine", fields: {} },
    ]);
  });

  it("drops a frame's sender on unregister only when it is still the registered one", () => {
    const shell = makeShell();
    const first = (): boolean => true;
    const second = (): boolean => true;
    shell.registerWorkspaceMessageSender(first);
    shell.registerWorkspaceMessageSender(second);
    shell.unregisterWorkspaceMessageSender(first);
    // The successor's sender still takes the ask.
    shell.requestFocusChat(WORKSPACE_ID, "agent-cc33");
    shell.unregisterWorkspaceMessageSender(second);
    // With nothing registered the ask is held for the next frame.
    shell.requestFocusChat(WORKSPACE_ID, "agent-dd44");
    const sent: WorkspaceMessage[] = [];
    shell.registerWorkspaceMessageSender((_workspaceAgentId, message) => {
      sent.push(message);
      return true;
    });
    shell.flushPendingWorkspaceMessage();
    expect(sent).toEqual([{ kind: "focus-chat", chatAgentId: "agent-dd44" }]);
  });
});

describe("ShellState.routeTo (one main window per workspace)", () => {
  const OTHER_ID = "agent-cd34";

  function stubClaim(isOpenedElsewhere: boolean): string[][] {
    const claims: string[][] = [];
    vi.stubGlobal("window", {
      mindsNative: {
        platform: "darwin",
        claimWorkspaceWindow: async (workspaceId: string, route: string) => {
          claims.push([workspaceId, route]);
          return { opened_elsewhere: isOpenedElsewhere };
        },
      },
    });
    return claims;
  }

  it("stays put when another window already shows the workspace", async () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const claims = stubClaim(true);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.enterWorkspace(OTHER_ID, { chat: "agent-ee55" });
    await vi.waitFor(() => expect(claims).toHaveLength(1));
    await Promise.resolve();

    // Main raised the other window and handed it the route, chat included.
    expect(claims).toEqual([[OTHER_ID, `/workspace/${OTHER_ID}?chat=agent-ee55`]]);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("navigates here once the workspace has no other window", async () => {
    const shell = makeShell();
    land(shell, "/");
    stubClaim(false);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.routeTo(`/workspace/${OTHER_ID}/options?tab=settings`);

    await vi.waitFor(() =>
      expect(routeSet).toHaveBeenCalledWith(`/workspace/${OTHER_ID}/options?tab=settings`),
    );
  });

  it("asks nothing for a route within the workspace this window shows, or off any workspace", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const claims = stubClaim(true);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.routeTo(`/workspace/${WORKSPACE_ID}/options?tab=share`);
    shell.routeTo("/accounts");

    expect(claims).toEqual([]);
    expect(routeSet.mock.calls).toEqual([[`/workspace/${WORKSPACE_ID}/options?tab=share`], ["/accounts"]]);
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
      { selected: "evt-1", workspace: WORKSPACE_ID, account: "local" },
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
    // The titlebar's real bell stays painted under Imbue Studio settings / Accounts
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

describe("ShellState.switchToAccountSwitcher", () => {
  it("puts Get help away and keeps the switcher up across that navigation's landing", () => {
    // The account button on the raised strip over Get help is a switch between
    // two titlebar popups: the arrival at the machine Get help is dismissed to
    // must not close the switcher it opened.
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 5, back: vi.fn() } });
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.switchToAccountSwitcher();

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(shell.isAccountSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isAccountSwitcherOpen).toBe(true);
    // An ORDINARY navigation after that still closes it.
    land(shell, "/create");
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("closes the docked options panel on its way to the switcher, replacing its entry", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=share`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.switchToAccountSwitcher();

    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(shell.isAccountSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isAccountSwitcherOpen).toBe(true);
  });

  it("just opens the switcher when there is no popup to put away, and the next navigation closes it", () => {
    const shell = makeShell();
    land(shell, "/create");
    shell.openNotifications();

    shell.switchToAccountSwitcher();

    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.isAccountSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isAccountSwitcherOpen).toBe(false);
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
      { workspace: WORKSPACE_ID, assist: "1", account: "local" },
      undefined,
    );
  });

  it("replaces another titlebar popup's entry when switched to from its strip", () => {
    // Options panel -> bug icon is a switch between two of the strip's surfaces:
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
      { workspace: WORKSPACE_ID, assist: "1", account: "local" },
      { replace: true },
    );
  });

  it("lets go of the panel the request popup was remembering", () => {
    // Switching the popup for Get help leaves the panel like it leaves the
    // popup. Kept, the panel stayed painted under Get help with its own
    // backdrop and its own raised strip -- two of the strip's surfaces at once.
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
      { workspace: WORKSPACE_ID, assist: "1", account: "local" },
      { replace: true },
    );
    // The panel lives exactly as long as /inbox is the route: it is let go
    // the moment the /help route lands -- however /help was reached, the
    // strip or an Electron open-overlay ask alike.
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    expect(shell.panelRouteBehindOverlay).toBeNull();
  });
});

describe("ShellState.openHelpForAgentAsk", () => {
  it("floats the pre-filled report over the machine that asked, which stays mounted", () => {
    // Without ?workspace= the Shell has no machine to keep behind the
    // backdrop, and a report ABOUT a machine floats over Home instead, with
    // that machine's frame torn down behind it.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForAgentAsk(WORKSPACE_ID, "the diagnosis");

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { workspace: WORKSPACE_ID, account: "local" },
      undefined,
    );
    // The route names the machine, so the Shell reads it as displayed.
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    expect(shell.displayedWorkspaceAnyId).toBe(WORKSPACE_ID);
  });

  it("hands the page the diagnosis the route cannot carry", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openHelpForAgentAsk(WORKSPACE_ID, "the diagnosis");

    expect(takePendingHelpLaunch()).toMatchObject({
      workspaceAgentId: WORKSPACE_ID,
      description: "the diagnosis",
      isAgentReport: true,
    });
  });

  it("replaces a titlebar popup's entry rather than stacking on it", () => {
    // Same reasoning as openHelp: pushed, the options panel would be left one
    // Back away under the report form it was covered by.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=share`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForAgentAsk(WORKSPACE_ID, "the diagnosis");

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { workspace: WORKSPACE_ID, account: "local" },
      { replace: true },
    );
  });
});

describe("ShellState.openHelpForAgentAsk arriving twice", () => {
  it("replaces the first ask's /help when the second arrives before it lands", () => {
    // The route still reads as the machine (m.route.get() trails route.set),
    // so only the launch still staged says /help is already on its way.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForAgentAsk(WORKSPACE_ID, "first diagnosis");
    shell.openHelpForAgentAsk(WORKSPACE_ID, "second diagnosis");

    expect(routeSet).toHaveBeenNthCalledWith(
      1,
      "/help",
      { workspace: WORKSPACE_ID, account: "local" },
      undefined,
    );
    expect(routeSet).toHaveBeenNthCalledWith(
      2,
      "/help",
      { workspace: WORKSPACE_ID, account: "local" },
      { replace: true },
    );
    expect(takePendingHelpLaunch()).toMatchObject({
      description: "second diagnosis",
    });
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

    shell.reloadWorkspaceFrame("agent-aa11", null);

    expect(reloadCount()).toBe(1);
  });

  it("reloads a host-scoped surface named by its agent id", () => {
    const { shell, reloadCount } = shellWithFrameOn("host-bb22");

    shell.reloadWorkspaceFrame("agent-aa11", null);

    expect(reloadCount()).toBe(1);
  });

  it("leaves a window whose frame shows a different workspace alone", () => {
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11");

    shell.reloadWorkspaceFrame("agent-cc33", null);

    expect(reloadCount()).toBe(0);
  });

  it("leaves a frameless window alone, so recovery keeps ownership of its screen", () => {
    // Hub pages, recovery, destroying and the workspace sub-pages mount no
    // frame at all. The recovery page is the case that matters: its own
    // machinery navigates it home once the workspace is healthy, and a refresh
    // must not reach in and re-navigate that screen.
    const { shell, reloadCount } = shellWithFrameOn(null);

    shell.reloadWorkspaceFrame("agent-aa11", null);

    expect(reloadCount()).toBe(0);
  });

  // An update's verdict carries the moment the run ended: only a page loaded
  // before it is still the previous build.
  const RUN_ENDED_AT_MS = Date.parse("2026-09-28T20:14:49.432Z");

  it("reloads a page that last loaded before the update ended", () => {
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11", RUN_ENDED_AT_MS - 3_600_000);

    shell.reloadWorkspaceFrame("agent-aa11", RUN_ENDED_AT_MS);

    expect(reloadCount()).toBe(1);
  });

  it("leaves a page loaded since the update ended alone", () => {
    // The user reloaded it, or the window was opened after the update.
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11", RUN_ENDED_AT_MS + 17_000);

    shell.reloadWorkspaceFrame("agent-aa11", RUN_ENDED_AT_MS);

    expect(reloadCount()).toBe(0);
  });

  it("reloads a frame whose first page has not finished loading", () => {
    // Nothing says which build that page will be, so it is treated as stale.
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11", null);

    shell.reloadWorkspaceFrame("agent-aa11", RUN_ENDED_AT_MS);

    expect(reloadCount()).toBe(1);
  });

  it("reloads a freshly loaded page when the refresh names no cutoff", () => {
    const { shell, reloadCount } = shellWithFrameOn("agent-aa11", RUN_ENDED_AT_MS + 17_000);

    shell.reloadWorkspaceFrame("agent-aa11", null);

    expect(reloadCount()).toBe(1);
  });
});

describe("ShellState.isWorkspaceFrameOn", () => {
  it("is true only for the workspace this window's frame shows, by either spelling", () => {
    const { shell } = shellWithFrameOn("host-bb22");

    expect(shell.isWorkspaceFrameOn("agent-aa11")).toBe(true);
    expect(shell.isWorkspaceFrameOn("agent-cc33")).toBe(false);
  });

  it("is false in a window with no workspace frame", () => {
    const { shell } = shellWithFrameOn(null);

    expect(shell.isWorkspaceFrameOn("agent-aa11")).toBe(false);
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
    // "Restart workspace" over a band correctly saying only the app restart helps.
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
    // Imbue Studio settings opened from Home carries no ?workspace, so nothing of the
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
      lastLoadedAtMs: () => null,
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
      lastLoadedAtMs: () => null,
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
    // The popover (z-[200]) sits over every other surface, the card included,
    // so it has to be asked first -- and the card must survive the keypress.
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

  it("gives the key to the sign-in modal over the z-[110] surface beneath it", () => {
    // The Shell paints the sign-in modal over every other z-[110] surface
    // (Manage Accounts, the feed), so Escape must close it and leave the
    // surface that opened it standing.
    const webLoginModel = new WebLoginModel(
      undefined,
      () => {},
      () => {},
    );
    webLoginModel.state = "waiting";
    shell = new ShellState(createEmptyStores(), webLoginModel);
    shell.openNotifications();

    expect(shell.handleEscape()).toBe(true);

    expect(webLoginModel.isOpen).toBe(false);
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("gives the key to the notification feed over an open card", () => {
    // The feed is another surface that can open ON TOP of the card (see
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

  function applyUpdateActivity(
    shell: ShellState,
    activity: UpdateActivity,
  ): void {
    shell.stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: {
        [AGENT]: {
          availability: "OUT_OF_DATE",
          current_version: "minds-v0.3.9",
          supported_version: "minds-v0.4.1",
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
  function shellWithMachine(liveness: string): {
    shell: ShellState;
    routeSet: ReturnType<typeof vi.fn>;
  } {
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
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
    shell.stores.health.applyHealthMessage({
      agent_id: "agent-aa11",
      status: "stuck",
    });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const entry = shell.stores.workspaces.entryByAnyId("agent-aa11");
    if (entry === null) throw new Error("fixture machine missing");
    shell.enterWorkspaceOrRecover(entry, "RUNNING");
    expect(routeSet).toHaveBeenCalledWith(
      `/agents/agent-aa11/recovery?return_to=${encodeURIComponent("/goto/agent-aa11/")}`,
    );
  });
});

describe("pulled-out windows (the popout route)", () => {
  const POPOUT_PATH = `/popout/${AGENT}/win-0123`;

  /** A shell on the popout route, its frame's reattach sender recording what
   * it is asked to send. */
  function popoutShell(): {
    shell: ShellState;
    sends: { windowId: string; frame: unknown }[];
  } {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    const sends: { windowId: string; frame: unknown }[] = [];
    shell.registerReattachWindowSender((windowId, frame) => sends.push({ windowId, frame }));
    vi.spyOn(m.route, "get").mockImplementation(() => POPOUT_PATH);
    shell.handleRouteChanged(POPOUT_PATH);
    return { shell, sends };
  }

  /** A main window's shell showing the workspace, its frame's reattach sender a no-op. */
  function workspaceShell(): ShellState {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    shell.registerReattachWindowSender(() => undefined);
    vi.spyOn(m.route, "get").mockImplementation(() => `/workspace/${AGENT}`);
    shell.handleRouteChanged(`/workspace/${AGENT}`);
    return shell;
  }

  function report(
    shell: ShellState,
    windows: { windowId: string; title: string }[],
    titles: string[],
    gone: { count: number },
  ): void {
    shell.handleDetachedWindows(
      windows,
      (title) => titles.push(title),
      () => {
        gone.count += 1;
      },
    );
  }

  it("displays the popout's workspace, so its accent and its channel state follow the machine", () => {
    const { shell } = popoutShell();
    expect(shell.displayedWorkspaceAnyId).toBe(AGENT);
    expect(shell.popoutRoute()).toEqual({ workspaceAnyId: AGENT, windowId: "win-0123" });
  });

  it("takes the window's title from the shell's detached set, and closes when the set lacks it", () => {
    const { shell } = popoutShell();
    const titles: string[] = [];
    const gone = { count: 0 };
    expect(shell.popoutWindowTitle("win-0123")).toBe("…");
    report(shell, [{ windowId: "win-0123", title: "Notes" }], titles, gone);
    expect(shell.popoutWindowTitle("win-0123")).toBe("Notes");
    expect(titles).toEqual(["Notes"]);
    // The same title again is not announced again; a new one is.
    report(shell, [{ windowId: "win-0123", title: "Notes" }], titles, gone);
    report(shell, [{ windowId: "win-0123", title: "Notes (2)" }], titles, gone);
    expect(titles).toEqual(["Notes", "Notes (2)"]);
    expect(gone.count).toBe(0);
    // Closed for everyone, or brought back from the desktop: the popout goes.
    report(shell, [{ windowId: "win-9999", title: "Other" }], titles, gone);
    expect(gone.count).toBe(1);
  });

  it("returns the window to the desktop through the frame and settles once the shell reports it back", async () => {
    vi.useFakeTimers();
    try {
      const { shell, sends } = popoutShell();
      const titles: string[] = [];
      const gone = { count: 0 };
      report(shell, [{ windowId: "win-0123", title: "Notes" }], titles, gone);
      let isSettled = false;
      const settled = shell.returnPopoutToDesktop({ x: 0.1, y: 0.2, width: 0.5, height: 0.5 }).then(() => {
        isSettled = true;
      });
      expect(sends).toEqual([{ windowId: "win-0123", frame: { x: 0.1, y: 0.2, width: 0.5, height: 0.5 } }]);
      await Promise.resolve();
      expect(isSettled).toBe(false);
      report(shell, [], titles, gone);
      await settled;
      expect(isSettled).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("settles a reattach the shell never confirms after a short wait", async () => {
    vi.useFakeTimers();
    try {
      const { shell, sends } = popoutShell();
      const titles: string[] = [];
      const gone = { count: 0 };
      report(shell, [{ windowId: "win-0123", title: "Notes" }], titles, gone);
      let isSettled = false;
      const settled = shell.returnPopoutToDesktop(null).then(() => {
        isSettled = true;
      });
      expect(sends).toEqual([{ windowId: "win-0123", frame: null }]);
      await vi.advanceTimersByTimeAsync(1500);
      await settled;
      expect(isSettled).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("settles at once when the window is not out any more, and does nothing off the popout route", async () => {
    const { shell, sends } = popoutShell();
    // Never reported as detached: nothing to wait for.
    await shell.returnPopoutToDesktop(null);
    expect(sends).toEqual([{ windowId: "win-0123", frame: null }]);
    stubAccentPainting();
    const plain = new ShellState(createEmptyStores());
    const plainSends: unknown[] = [];
    plain.registerReattachWindowSender((windowId) => plainSends.push(windowId));
    vi.spyOn(m.route, "get").mockImplementation(() => `/workspace/${AGENT}`);
    plain.handleRouteChanged(`/workspace/${AGENT}`);
    await plain.returnPopoutToDesktop(null);
    expect(plainSends).toEqual([]);
  });

  it("returns a popout's window through the mounted frame only when this main window shows its workspace", () => {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    const sends: unknown[] = [];
    shell.registerReattachWindowSender((windowId, frame) => sends.push({ windowId, frame }));
    const ask = { workspaceId: AGENT, windowId: "win-0123", frame: { x: 0.1, y: 0.2, width: 0.5, height: 0.5 } };
    // A hub page shows no workspace; another workspace's surface is not this one's desktop.
    vi.spyOn(m.route, "get").mockImplementation(() => "/");
    shell.handleRouteChanged("/");
    shell.handleReattachPopoutWindow(ask);
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/agent-ff00");
    shell.handleRouteChanged("/workspace/agent-ff00");
    shell.handleReattachPopoutWindow(ask);
    expect(sends).toEqual([]);
    // The workspace's own surface takes it, frame and all, or at the kept frame when the popout is closing.
    vi.spyOn(m.route, "get").mockImplementation(() => `/workspace/${AGENT}`);
    shell.handleRouteChanged(`/workspace/${AGENT}`);
    shell.handleReattachPopoutWindow(ask);
    shell.handleReattachPopoutWindow({ ...ask, frame: null });
    expect(sends).toEqual([
      { windowId: "win-0123", frame: { x: 0.1, y: 0.2, width: 0.5, height: 0.5 } },
      { windowId: "win-0123", frame: null },
    ]);
    // A popout of the workspace is a view of one window, never the desktop that takes one back.
    vi.spyOn(m.route, "get").mockImplementation(() => POPOUT_PATH);
    shell.handleRouteChanged(POPOUT_PATH);
    shell.handleReattachPopoutWindow(ask);
    expect(sends).toHaveLength(2);
  });

  it("takes a reattach ask naming the workspace by either coordinate", () => {
    // A popout restored from the saved session names its workspace by the
    // host-scoped id its persisted route was written with; a cold-started
    // main window may display the workspace under that spelling too.
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    const sends: unknown[] = [];
    shell.registerReattachWindowSender((windowId, frame) => sends.push({ windowId, frame }));
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/agent-aa11");
    shell.handleRouteChanged("/workspace/agent-aa11");
    shell.handleReattachPopoutWindow({ workspaceId: "host-bb22", windowId: "win-0123", frame: null });
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/host-bb22");
    shell.handleRouteChanged("/workspace/host-bb22");
    shell.handleReattachPopoutWindow({ workspaceId: "agent-aa11", windowId: "win-4567", frame: null });
    shell.handleReattachPopoutWindow({ workspaceId: "host-0000", windowId: "win-8888", frame: null });
    expect(sends).toEqual([
      { windowId: "win-0123", frame: null },
      { windowId: "win-4567", frame: null },
    ]);
  });

  it("answers main once the desktop reports the window it was asked to take back, so the popout can go", async () => {
    vi.useFakeTimers();
    try {
      const shell = workspaceShell();
      const ignored = { count: 0 };
      report(shell, [{ windowId: "win-0123", title: "PRs" }], [], ignored);
      let answer: boolean | null = null;
      const answered = shell
        .handleReattachPopoutWindow({ workspaceId: AGENT, windowId: "win-0123", frame: null })
        .then((isReportedBack) => {
          answer = isReportedBack;
        });
      // Another window's change is not this window's return.
      report(shell, [{ windowId: "win-0123", title: "PRs" }, { windowId: "win-4567", title: "Chat" }], [], ignored);
      await Promise.resolve();
      expect(answer).toBeNull();
      report(shell, [{ windowId: "win-4567", title: "Chat" }], [], ignored);
      await answered;
      expect(answer).toBe(true);
      // A main window never closes itself on its own report.
      expect(ignored.count).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not answer main that the window is back when the desktop never confirms it", async () => {
    vi.useFakeTimers();
    try {
      const shell = workspaceShell();
      const ignored = { count: 0 };
      // Never reported out: nothing seen to come back.
      const ask = { workspaceId: AGENT, windowId: "win-0123", frame: null };
      expect(await shell.handleReattachPopoutWindow(ask)).toBe(false);
      report(shell, [{ windowId: "win-0123", title: "PRs" }], [], ignored);
      const answered = shell.handleReattachPopoutWindow(ask);
      await vi.advanceTimersByTimeAsync(1500);
      expect(await answered).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("forwards main's report of a watched title-bar drag to the mounted frame, and nothing without one", () => {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    const report = { workspaceId: AGENT, windowId: "win-0123", phase: "out" as const };
    shell.handleTearOut(report);
    const forwarded: unknown[] = [];
    const sender = (received: unknown): number => forwarded.push(received);
    shell.registerTearOutSender(sender);
    shell.handleTearOut(report);
    shell.unregisterTearOutSender(sender);
    shell.handleTearOut({ ...report, phase: "released" });
    expect(forwarded).toEqual([report]);
  });

  it("hands a link from main to the mounted frame, and not to a frame torn down after its successor mounted", () => {
    stubAccentPainting();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const shell = new ShellState(createEmptyStores());
    shell.handleOpenLink("http://localhost:3000/dropped");
    const oldFrame: string[] = [];
    const newFrame: string[] = [];
    const oldSender = (url: string): number => oldFrame.push(url);
    const newSender = (url: string): number => newFrame.push(url);
    shell.registerOpenLinkSender(oldSender);
    shell.registerOpenLinkSender(newSender);
    shell.unregisterOpenLinkSender(oldSender);
    shell.handleOpenLink("http://localhost:3000/preview");
    shell.unregisterOpenLinkSender(newSender);
    shell.handleOpenLink("http://localhost:3000/after");
    expect(oldFrame).toEqual([]);
    expect(newFrame).toEqual(["http://localhost:3000/preview"]);
    expect(warn.mock.calls.map((call) => String(call[0]))).toEqual([
      "[link-routing] dropped a link main handed back, since no workspace frame is mounted: http://localhost:3000/dropped",
      "[link-routing] dropped a link main handed back, since no workspace frame is mounted: http://localhost:3000/after",
    ]);
    warn.mockRestore();
  });
});

describe("ShellState's account", () => {
  const ALICE = accountEntry();
  const BOB = secondAccountEntry();
  const BOBS_WORKSPACE = "agent-bb01";

  /** A shell on Alice's hub, whose list holds her workspace and the device's, with a channel listening. */
  function shellOnAlice(): {
    shell: ShellState;
    clientStates: [string, string | null, string][];
  } {
    const shell = makeShell();
    shell.stores.accounts.applyAccountsMessage(
      accountsMessage([ALICE, BOB], ALICE.user_id),
    );
    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({
        workspaces: [
          { ...workspacesMessage().workspaces[0], account_user_id: ALICE.user_id },
          { ...workspacesMessage().workspaces[0], id: "agent-dd01", host_id: "host-dd02", name: "device" },
        ],
      }),
    );
    const clientStates: [string, string | null, string][] = [];
    shell.channel = {
      clientId: "win-asking",
      setClientState: (route: string, workspaceAgentId: string | null, accountId: string) => {
        clientStates.push([route, workspaceAgentId, accountId]);
      },
    } as unknown as UiChannelClient;
    land(shell, `/account/${ALICE.user_id}/`);
    clientStates.length = 0;
    return { shell, clientStates };
  }

  /** A fetch that answers the switch-account route with `route`, or a refusal / failure. */
  function stubSwitchAnswer(answer: { route: string } | number | Error): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => {
      if (answer instanceof Error) throw answer;
      if (typeof answer === "number") return jsonResponse({ error: "no" }, answer);
      return jsonResponse(answer);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    return fetchMock;
  }

  it("reads the account off the route: the hub segment, a listed workspace's association, ?account=, the start flow", () => {
    const { shell } = shellOnAlice();
    expect(shell.accountIdForRoute(`/account/${BOB.user_id}/create`, "")).toBe(BOB.user_id);
    expect(shell.accountIdForRoute("/account/local/", "")).toBe("local");
    expect(shell.accountIdForRoute("/workspace/agent-aa11", "")).toBe(ALICE.user_id);
    // By either coordinate, and a workspace associated with no account is the device's.
    expect(shell.accountIdForRoute("/workspace/host-bb22/options", "")).toBe(ALICE.user_id);
    // The sub-pages that replace the surface are the workspace's pages too.
    expect(shell.accountIdForRoute("/workspace/agent-aa11/backups", "")).toBe(ALICE.user_id);
    expect(shell.accountIdForRoute("/workspace/agent-aa11/settings", "")).toBe(ALICE.user_id);
    expect(shell.accountIdForRoute("/workspace/agent-dd01", "")).toBe("local");
    // A workspace the list does not know says nothing: the window keeps its account.
    expect(shell.accountIdForRoute(`/workspace/${BOBS_WORKSPACE}`, "")).toBeNull();
    expect(shell.accountIdForRoute("/settings", `account=${BOB.user_id}`)).toBe(BOB.user_id);
    // A value that is not an account id names none, as the backend reads the same parameter.
    expect(shell.accountIdForRoute("/settings", "account=..%2Fetc")).toBeNull();
    expect(shell.accountIdForRoute("/settings", "account=")).toBeNull();
    expect(shell.accountIdForRoute("/start", "")).toBe("local");
    expect(shell.accountIdForRoute("/accounts", "")).toBeNull();
  });

  it("follows the route's account on a route change and builds its hub routes and overlay query from it", () => {
    const { shell } = shellOnAlice();
    expect(shell.accountId).toBe(ALICE.user_id);
    expect(shell.hubRoute("/create")).toBe(`/account/${ALICE.user_id}/create`);
    expect(shell.overlayQuery({ selected: "evt-a" })).toEqual({ selected: "evt-a", account: ALICE.user_id });

    land(shell, "/workspace/agent-dd01");
    expect(shell.accountId).toBe("local");
    expect(shell.hubRoute()).toBe("/account/local/");
  });

  it("lands a switch on the answered workspace as that account's window, so the channel brings its rows", async () => {
    const { shell, clientStates } = shellOnAlice();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const fetchMock = stubSwitchAnswer({ route: `/workspace/${BOBS_WORKSPACE}` });

    await shell.switchAccount(BOB.user_id);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      account_id: BOB.user_id,
      client_id: "win-asking",
      is_desktop: false,
    });
    expect(shell.accountId).toBe(BOB.user_id);
    expect(clientStates).toEqual([[`/account/${ALICE.user_id}/`, null, BOB.user_id]]);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${BOBS_WORKSPACE}`);
    // The landing commits before Bob's list arrives: the still-unknown workspace does not undo the switch,
    // and whose it is was settled by the switch, so the backend is not asked.
    land(shell, `/workspace/${BOBS_WORKSPACE}`);
    expect(shell.accountId).toBe(BOB.user_id);
    expect(shell.isAccountSwitchPending).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["refuses", 503],
    ["cannot be asked", new Error("offline")],
  ] as const)("lands a switch on the account's hub when the backend %s", async (_name, answer) => {
    const { shell } = shellOnAlice();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    stubSwitchAnswer(answer);

    await shell.switchAccount(BOB.user_id);

    expect(shell.accountId).toBe(BOB.user_id);
    expect(routeSet.mock.calls.map((call) => call[0])).toEqual([`/account/${BOB.user_id}/`]);
  });

  it("does not switch to the account the window already shows, nor twice while one switch is out", async () => {
    const { shell } = shellOnAlice();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const fetchMock = stubSwitchAnswer({ route: `/account/${BOB.user_id}/` });

    await shell.switchAccount(ALICE.user_id);
    expect(fetchMock).not.toHaveBeenCalled();

    const first = shell.switchAccount(BOB.user_id);
    const second = shell.switchAccount("local");
    await Promise.all([first, second]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(shell.accountId).toBe(BOB.user_id);
    expect(routeSet).toHaveBeenCalledTimes(1);
  });

  it("lands on the hub when another desktop window claims the workspace the switch answered", async () => {
    const { shell } = shellOnAlice();
    vi.stubGlobal("window", {
      mindsNative: {
        claimWorkspaceWindow: () => Promise.resolve({ opened_elsewhere: true }),
      },
    });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    stubSwitchAnswer({ route: `/workspace/${BOBS_WORKSPACE}` });

    await shell.switchAccount(BOB.user_id);
    await settle();

    expect(routeSet.mock.calls.map((call) => call[0])).toEqual([`/account/${BOB.user_id}/`]);
    expect(shell.accountId).toBe(BOB.user_id);
  });

  it("adopts a landed account without navigating: the hub URL is rewritten in place and the channel told", () => {
    const { shell, clientStates } = shellOnAlice();
    const replaceState = vi.fn();
    vi.stubGlobal("window", { history: { replaceState } });
    vi.spyOn(m.route, "get").mockReturnValue(`/account/${ALICE.user_id}/create?git_url=x`);

    shell.adoptAccount(ALICE.user_id);
    expect(clientStates).toEqual([]);

    shell.adoptAccount(BOB.user_id);
    expect(shell.accountId).toBe(BOB.user_id);
    expect(replaceState).toHaveBeenCalledWith(null, "", `/account/${BOB.user_id}/create?git_url=x`);
    expect(clientStates).toEqual([[`/account/${ALICE.user_id}/create`, null, BOB.user_id]]);
  });

  it("keeps an adopted account across a redraw of the unchanged route, and the start flow's too", () => {
    // The router runs handleRouteChanged on every draw; mithril still reports
    // the pre-rewrite hub path, and /start reads as the device's.
    const { shell, clientStates } = shellOnAlice();
    vi.stubGlobal("window", { history: { replaceState: vi.fn() } });
    const hubPath = `/account/${ALICE.user_id}/create`;
    land(shell, hubPath);
    clientStates.length = 0;

    shell.adoptAccount(BOB.user_id);
    shell.handleRouteChanged(hubPath, "");
    expect(shell.accountId).toBe(BOB.user_id);
    // The channel heard Bob (once from the adoption, once more from the draw,
    // which the real client dedupes) and never Alice again.
    const reported = clientStates.map(([, , accountId]) => accountId);
    expect(reported).toContain(BOB.user_id);
    expect(reported).not.toContain(ALICE.user_id);

    land(shell, "/start");
    expect(shell.accountId).toBe("local");
    shell.adoptAccount(ALICE.user_id);
    shell.handleRouteChanged("/start", "");
    expect(shell.accountId).toBe(ALICE.user_id);
    // The next navigation reads its own account as ever.
    land(shell, "/account/local/");
    expect(shell.accountId).toBe("local");
  });

  it("asks the backend whose a workspace route is when the list cannot say, and becomes that account's window", async () => {
    const { shell, clientStates } = shellOnAlice();
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ account_id: BOB.user_id }));
    vi.stubGlobal("fetch", fetchMock);

    land(shell, `/workspace/${BOBS_WORKSPACE}`);

    expect(shell.accountId).toBe(ALICE.user_id);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `/ui/api/windows/route-account?path=%2Fworkspace%2F${BOBS_WORKSPACE}&query=`,
    ]);
    await settle();
    expect(shell.accountId).toBe(BOB.user_id);
    // The channel is told (after the landing's own report), so Bob's rows (the workspace's among them) follow.
    expect(clientStates.map(([route, , accountId]) => [route, accountId])).toEqual([
      [`/workspace/${BOBS_WORKSPACE}`, ALICE.user_id],
      [`/workspace/${BOBS_WORKSPACE}`, BOB.user_id],
    ]);
    // A redraw on the same route asks the list, not the backend again.
    shell.handleRouteChanged(`/workspace/${BOBS_WORKSPACE}`, "");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("drops the backend's answer once the window has moved on, and asks nothing for a route naming no workspace", async () => {
    const { shell } = shellOnAlice();
    let answer: (response: Response) => void = () => undefined;
    const fetchMock = vi.fn(
      (_url: string, _init?: RequestInit) =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    land(shell, `/workspace/${BOBS_WORKSPACE}`);
    land(shell, "/account/local/");
    answer(jsonResponse({ account_id: BOB.user_id }));
    await settle();

    expect(shell.accountId).toBe("local");
    // An overlay's ?workspace= and a bare overlay say nothing the backend should be asked about.
    land(shell, `/help?workspace=${BOBS_WORKSPACE}`);
    land(shell, "/accounts");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps a switch made while the backend's answer for the route it left was still out", async () => {
    const { shell } = shellOnAlice();
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    let answer: (response: Response) => void = () => undefined;
    const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
      url.startsWith("/ui/api/windows/route-account")
        ? new Promise<Response>((resolve) => {
            answer = resolve;
          })
        : Promise.resolve(jsonResponse({ route: `/account/${BOB.user_id}/` })),
    );
    vi.stubGlobal("fetch", fetchMock);

    land(shell, `/workspace/${BOBS_WORKSPACE}`);
    await shell.switchAccount(BOB.user_id);
    // The landing has not committed yet when the ask answers for the old route.
    answer(jsonResponse({ account_id: ALICE.user_id }));
    await settle();

    expect(shell.accountId).toBe(BOB.user_id);
  });

  it("asks a workspace route again on a redraw until its row has arrived", () => {
    const { shell } = shellOnAlice();
    const path = `/workspace/${BOBS_WORKSPACE}`;
    land(shell, path);
    expect(shell.accountId).toBe(ALICE.user_id);

    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({
        workspaces: [
          {
            ...workspacesMessage().workspaces[0],
            id: BOBS_WORKSPACE,
            host_id: "host-bb02",
            account_user_id: BOB.user_id,
          },
        ],
      }),
    );
    shell.handleRouteChanged(path, "");
    expect(shell.accountId).toBe(BOB.user_id);
  });

  it("knows a signed-out account's window from the device's and a signed-in one's", () => {
    const { shell } = shellOnAlice();
    expect(shell.isAccountSignedOut()).toBe(false);
    land(shell, "/account/user-gone/");
    expect(shell.isAccountSignedOut()).toBe(true);
    land(shell, "/account/local/");
    expect(shell.isAccountSignedOut()).toBe(false);
  });
});
