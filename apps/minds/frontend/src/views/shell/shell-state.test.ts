import m from "mithril";
import type { WorkspaceMessage } from "../../models/workspaceMessage";
import type { SharedOpenPlan } from "../../models/sharedWithMe";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { recoveryRoute } from "../../models/create";
import { takePendingHelpLaunch } from "../../models/help";
import { ErrorDialogQueue } from "../../models/errorDialog";
import { WebLoginModel } from "../../models/webLogin";
import type { ClientPermissionAnswer } from "../../electron-bridge";
import { ClientPermissionPromptModel } from "../../models/clientPermissions";
import { clientPermissionAsk } from "../../models/clientPermissions.testing";
import type { UiChannelClient } from "../../channel/client";
import {
  SHARED_WORKSPACE_DOMAIN,
  accountEntry,
  accountsMessage,
  attrsOf,
  jsonResponse,
  publishPendingRequests,
  renderRoot,
  secondAccountEntry,
  settle,
  sharedWorkspace,
  sharedWorkspaceEntry,
  workspacesMessage,
} from "../../testing";
import type { AnyVnode } from "../../testing";
import { Modal, isAnyModalOpen } from "../components/Modal";
import type { WaitingRequestList } from "./shell-state";
import { ShellState, workspaceAppUrl } from "./shell-state";

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

/** The same for any route, given as the router hands it over. */
function displayingRoute(shell: ShellState, path: string, search: string): void {
  stubAccentPainting();
  shell.handleRouteChanged(path, search);
}

// The ask follows the MOUNTED FRAME, not the routed content surface. Those
// diverge on the routes that float an app modal over a workspace (/help,
// /inbox, /settings/ai-keys, /create/template): the frame stays mounted and
// visible behind the card while `displayedWorkspaceAnyId` is null, and the
// Shell keeps it at a stable vtree position so dismissing the modal does not
// remount it either. Keying off the frame is what covers those windows.
const WORKSPACE_ID = "agent-ab12";

/** The Modals a test left on screen, taken down after it: the count of them is the Modal module's own. */
const raisedModalRemovals: (() => void)[] = [];

/** Put a Modal on screen the way a mount would, by drawing it and running the hooks that register it; returns its
 * removal, which is what its owner's redraw does once `onClose` has flipped it shut. */
function raiseModal(onClose?: () => void): () => void {
  const backdrop = renderRoot(Modal, { isOpen: true, onClose }) as unknown as AnyVnode;
  const hooks = attrsOf(backdrop) as { oncreate: () => void; onremove: () => void };
  hooks.oncreate();
  let isRemoved = false;
  const remove = (): void => {
    if (isRemoved) return;
    isRemoved = true;
    hooks.onremove();
  };
  raisedModalRemovals.push(remove);
  return remove;
}

afterEach(() => {
  for (const remove of raisedModalRemovals.splice(0)) remove();
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
 * from a route (displayed workspace, the Settings behind an overlay) is real. */
function land(shell: ShellState, route: string): void {
  vi.spyOn(m.route, "get").mockReturnValue(route);
  const [path, search = ""] = route.split("?");
  shell.handleRouteChanged(path, search);
}

/** Settings on the workspace's Permissions, floating over that workspace: where a "Waiting on you" row is pressed. */
const SETTINGS_ON_WAITING = `/settings?tab=workspace&group=permissions&perm_section=waiting&workspace=${WORKSPACE_ID}&account=local`;

/** The query of a route as a plain record, so an assertion on it does not depend on the order it was written in. */
function queryOf(route: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(route.split("?")[1] ?? ""));
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

  it("remembers the Settings route it was opened from, and still floats over the workspace behind Settings", () => {
    // Settings stays mounted underneath, and is what the popup goes back to.
    const shell = makeShell();
    land(shell, SETTINGS_ON_WAITING);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.settingsRouteBehindOverlay).toBe(SETTINGS_ON_WAITING);
    // A push, not a replace: Settings is not swapped for the popup, it sits under it.
    expect(routeSet).toHaveBeenCalledWith(
      "/inbox",
      { selected: "evt-a", workspace: WORKSPACE_ID, account: "local" },
      undefined,
    );
  });

  it("remembers Settings whichever tab it was on, so a request arriving from a notification returns there", () => {
    const shell = makeShell();
    const studioRoute = "/settings?tab=studio&section=display&account=local";
    land(shell, studioRoute);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.settingsRouteBehindOverlay).toBe(studioRoute);
  });

  it("keeps the remembered Settings when a second request swings the open popup", () => {
    // The live route is /inbox by then, which names no Settings: only the
    // first open may write what is underneath.
    const shell = makeShell();
    land(shell, SETTINGS_ON_WAITING);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?selected=evt-a&workspace=${WORKSPACE_ID}&account=local`);

    shell.openInbox({ selected: "evt-b" });

    expect(shell.settingsRouteBehindOverlay).toBe(SETTINGS_ON_WAITING);
    expect(routeSet).toHaveBeenLastCalledWith(
      "/inbox",
      { selected: "evt-b", workspace: WORKSPACE_ID, account: "local" },
      { replace: true },
    );
  });

  it("keeps the remembered Settings across the redraw that runs before the popup's route lands", () => {
    // The router calls handleRouteChanged on every redraw, and one happens
    // with Settings' own route still current right after the open.
    const shell = makeShell();
    land(shell, SETTINGS_ON_WAITING);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });

    land(shell, SETTINGS_ON_WAITING);

    expect(shell.settingsRouteBehindOverlay).toBe(SETTINGS_ON_WAITING);
  });

  it("names no Settings when the popup was opened from the chat", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.settingsRouteBehindOverlay).toBeNull();
  });

  it("names no Settings when the popup was opened from Get help, the other routed flyout", () => {
    const shell = makeShell();
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(shell.settingsRouteBehindOverlay).toBeNull();
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

  it("closes again after stepping back onto an earlier entry of the same overlay", () => {
    // A link from outside the page (the menu bar's Check for Updates, a notification) can push a second
    // Settings entry over the first. Stepping back from it lands on the same path, with another query.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    land(shell, `/settings?workspace=${WORKSPACE_ID}&tab=workspace`);
    land(shell, "/settings?section=updates");

    expect(shell.closeAppOverlay()).toBe(true);
    // A redraw on the entry still being left keeps the guard: a held Escape steps back once.
    land(shell, "/settings?section=updates");
    expect(shell.closeAppOverlay()).toBe(true);
    expect(back).toHaveBeenCalledTimes(1);

    land(shell, `/settings?workspace=${WORKSPACE_ID}&tab=workspace`);
    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).toHaveBeenCalledTimes(1);
    expect(routeSet.mock.calls).toEqual([[`/workspace/${WORKSPACE_ID}`, undefined, { replace: true }]]);
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

  it("routes Settings directly to the machine it floats over, never through history.back()", () => {
    // The entry behind Settings can be a request popup since answered, or the
    // flyout it was switched to from; the machine it names is where it goes.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    land(shell, `/settings?tab=workspace&group=sharing&workspace=${WORKSPACE_ID}&settings_workspace=agent-cd34`);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    // The workspace on screen, not the one Settings had picked.
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
  });

  it("goes back through history for a Settings opened over Home, which names no machine", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    // The picked workspace is not one the window shows, so it is nowhere to dismiss to.
    land(shell, "/settings?tab=workspace&settings_workspace=agent-cd34&account=local");

    expect(shell.closeAppOverlay()).toBe(true);

    expect(back).toHaveBeenCalledTimes(1);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("lands a cold-started Settings on its account's hub when there is no history to go back through", () => {
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 1, back: vi.fn() } });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    land(shell, "/settings?tab=accounts&account=user-1");

    expect(shell.closeAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenCalledWith("/account/user-1/");
  });

  it("dismisses a Settings opened from the recovery page back to that page, not to the machine that would not load", () => {
    const recoveryPage = `/agents/${WORKSPACE_ID}/recovery`;
    const shell = makeShell();
    land(shell, recoveryPage);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openSettingsFromBar();
    vi.stubGlobal("window", { history: { length: 1, back: vi.fn() } });
    land(shell, `/settings?tab=workspace&settings_workspace=${WORKSPACE_ID}&account=local`);

    expect(shell.closeAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenLastCalledWith(recoveryPage);
  });

  it("leaves the legacy options and accounts URLs alone: they are redirects, not surfaces to close", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}/options?tab=share`);
    expect(shell.closeAppOverlay()).toBe(false);
    expect(shell.handleEscape()).toBe(false);
    land(shell, "/accounts");
    expect(shell.closeAppOverlay()).toBe(false);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).not.toHaveBeenCalled();
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

  it("opens a chat of the workspace on screen in place, putting what floats over it away first", () => {
    const shell = makeShell();
    const sent: [string, string][] = [];
    recordFocusChats(shell, sent);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openWorkspaceChat(WORKSPACE_ID, "agent-cc33");
    expect(routeSet).not.toHaveBeenCalled();

    land(shell, `/settings?tab=workspace&group=updates&workspace=${WORKSPACE_ID}`);
    shell.openWorkspaceChat(WORKSPACE_ID, "agent-dd44");
    expect(routeSet).toHaveBeenCalledTimes(1);
    expect(routeSet).toHaveBeenLastCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });

    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    shell.openWorkspaceChat(WORKSPACE_ID, "agent-ee55");
    expect(routeSet).toHaveBeenCalledTimes(2);
    expect(routeSet).toHaveBeenLastCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });

    expect(sent).toEqual([
      [WORKSPACE_ID, "agent-cc33"],
      [WORKSPACE_ID, "agent-dd44"],
      [WORKSPACE_ID, "agent-ee55"],
    ]);
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
    const claims = stubClaim(false);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.routeTo(`/workspace/${OTHER_ID}`, { chat: "agent-ee55" });

    await vi.waitFor(() =>
      expect(routeSet).toHaveBeenCalledWith(`/workspace/${OTHER_ID}`, { chat: "agent-ee55" }),
    );
    expect(claims).toEqual([[OTHER_ID, `/workspace/${OTHER_ID}?chat=agent-ee55`]]);
  });

  it("asks nothing for a route within the workspace this window shows, or off any workspace", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const claims = stubClaim(true);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.routeTo(`/workspace/${WORKSPACE_ID}?chat=agent-ee55`);
    // Settings on another workspace is not that workspace's surface: only the
    // surface route is one window's to hold.
    shell.routeTo(`/settings?tab=workspace&settings_workspace=${OTHER_ID}`);
    shell.routeTo(`/workspace/${OTHER_ID}/backups`);

    expect(claims).toEqual([]);
    expect(routeSet.mock.calls).toEqual([
      [`/workspace/${WORKSPACE_ID}?chat=agent-ee55`],
      [`/settings?tab=workspace&settings_workspace=${OTHER_ID}`],
      [`/workspace/${OTHER_ID}/backups`],
    ]);
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

  it("strips a resolved or unknown review id without opening the popup, and says it is gone", async () => {
    const shell = makeShell();
    const toasts: string[] = [];
    shell.notificationsUi = {
      showTransientToast: (title: string) => toasts.push(title),
    } as unknown as NonNullable<ShellState["notificationsUi"]>;
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
    expect(toasts).toEqual(["That request is no longer waiting for you."]);
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
    // Real history depth, since Get help is a click away from Settings:
    // history.back() could land back ON a flyout's entry, which would then be
    // standing under the feed this switch is opening.
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

  it("puts Settings away on its way to the feed, replacing its entry", () => {
    // Replaced like every other strip switch: pushed, Settings would sit one
    // Back away under the feed.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/settings?tab=workspace&group=sharing&workspace=${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.switchToNotifications();

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}`,
      undefined,
      { replace: true },
    );
    expect(shell.isNotificationsOpen).toBe(true);
    // The dismissal navigation lands: the feed rides across it.
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("puts a Settings opened over Home away through history, and rides across that landing too", () => {
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

  it("puts a centered app modal away first, so the feed never raises beneath its backdrop", () => {
    // The bar's real bell stays painted under a centered card (no raised strip
    // covers it there), and the feed's backdrop draws under a later-DOM
    // modal's at the same z -- so the bell's click must put the modal away,
    // not float the feed beneath it.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, "/settings/ai-keys?workspace=host-99aa");

    shell.switchToNotifications();

    expect(back).toHaveBeenCalledTimes(1);
    expect(shell.isNotificationsOpen).toBe(true);
    land(shell, "/workspace/host-99aa");
    expect(shell.isNotificationsOpen).toBe(true);
  });

  it("closes whichever menu was open, so the feed is the one flyout up", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openWorkspaceSwitcher();
    shell.switchToNotifications();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.closeNotifications();
    shell.openAccountSwitcher();
    shell.switchToNotifications();
    expect(shell.isAccountSwitcherOpen).toBe(false);
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

  it("puts Settings away on its way to the account menu, replacing its entry", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=accounts&workspace=${WORKSPACE_ID}`);
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
    // Armed for that one arrival only.
    land(shell, "/create");
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("takes the workspace menu's place when switched to from it", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openWorkspaceSwitcher();

    shell.switchToAccountSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
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

  it("does not arm the other menu: only the menu that was switched to rides the landing", () => {
    const shell = makeShell();
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.switchToAccountSwitcher();

    // The workspace menu, opened by its own button before the dismissal lands, has no claim on that landing.
    shell.openWorkspaceSwitcher();
    land(shell, `/workspace/${WORKSPACE_ID}`);

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });
});

describe("ShellState.switchToWorkspaceSwitcher", () => {
  it("puts Get help away and keeps the menu up across that navigation's landing", () => {
    // The workspace button on the raised strip over Get help is a switch
    // between two flyouts: the arrival at the machine Get help is dismissed
    // to must not close the menu it opened.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.switchToWorkspaceSwitcher();

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    // An ORDINARY navigation after that still closes it.
    land(shell, "/create");
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("puts Settings away on its way to the menu, replacing its entry", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.switchToWorkspaceSwitcher();

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("rides across the history step that puts away a Settings opened over Home", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, "/settings?tab=studio&account=local");

    shell.switchToWorkspaceSwitcher();

    expect(back).toHaveBeenCalledTimes(1);
    land(shell, "/account/local/");
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    land(shell, "/account/local/create");
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("just opens the menu when there is no flyout route to put away, and the next navigation closes it", () => {
    const shell = makeShell();
    land(shell, "/create");
    shell.openNotifications();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.switchToWorkspaceSwitcher();

    expect(routeSet).not.toHaveBeenCalled();
    // The feed is the flyout being left.
    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    // A redraw on the same route is not a navigation: still open.
    land(shell, "/create");
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("takes the account menu's place when switched to from it", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openAccountSwitcher();

    shell.switchToWorkspaceSwitcher();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does not arm the other menu: only the menu that was switched to rides the landing", () => {
    const shell = makeShell();
    land(shell, `/help?workspace=${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.switchToWorkspaceSwitcher();

    // The account menu, opened by its own button before the dismissal lands, has no claim on that landing.
    shell.openAccountSwitcher();
    land(shell, `/workspace/${WORKSPACE_ID}`);

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });
});

describe("ShellState's two menus", () => {
  it("opens each menu as local state, never a navigation, and closes the other", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    expect(shell.isAccountSwitcherOpen).toBe(false);

    shell.openAccountSwitcher();
    expect(shell.isAccountSwitcherOpen).toBe(true);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    expect(shell.isAccountSwitcherOpen).toBe(false);

    shell.closeWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("closes either menu on a navigation it was not armed for, and keeps it across a redraw", () => {
    const shell = makeShell();
    land(shell, "/account/local/");

    shell.openWorkspaceSwitcher();
    land(shell, "/account/local/");
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    // Choosing a row routes to it, and the arrival closes the menu.
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openAccountSwitcher();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    expect(shell.isAccountSwitcherOpen).toBe(true);
    land(shell, "/account/local/");
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("closes either menu when Get help is opened from the bar", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openWorkspaceSwitcher();
    shell.openHelp();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openAccountSwitcher();
    shell.openHelp();
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });
});

describe("ShellState.openSettings", () => {
  const RECOVERY_PAGE = `/agents/${WORKSPACE_ID}/recovery?return_to=%2Fgoto%2Fhost-bb22%2F`;

  it("opens plainly over Home, carrying the window's account so a reload keeps it", () => {
    const shell = makeShell();
    land(shell, "/account/user-1/");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings();

    expect(routeSet.mock.calls).toEqual([["/settings", { account: "user-1" }, undefined]]);
  });

  it("names the workspace on screen, by its agent id, so Settings floats over it", () => {
    const shell = makeShell();
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    // A cold-start restore lands on the host coordinate.
    land(shell, "/workspace/host-bb22");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings({ tab: "studio", section: "this-computer" });

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "studio", section: "this-computer", workspace: "agent-aa11", account: "local" }, undefined],
    ]);
  });

  it("picks a workspace without floating over it: a home card's settings control does not open the workspace", () => {
    const shell = makeShell();
    land(shell, "/account/local/");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings({ tab: "workspace", settings_workspace: WORKSPACE_ID });

    // No `workspace`: Home stays behind, and the window displays nothing new.
    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "workspace", settings_workspace: WORKSPACE_ID, account: "local" }, undefined],
    ]);
  });

  it("keeps floating over the workspace on screen when another workspace is picked", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings({ tab: "workspace", settings_workspace: "agent-cd34", workspace: "agent-cd34" });

    // The workspace on screen is the window's to say, whatever the caller passed.
    expect(routeSet).toHaveBeenCalledWith(
      "/settings",
      { tab: "workspace", settings_workspace: "agent-cd34", workspace: WORKSPACE_ID, account: "local" },
      undefined,
    );
  });

  it("replaces Get help's entry when switched to from it: a switch, not a stack", () => {
    const shell = makeShell();
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings();

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { workspace: WORKSPACE_ID, account: "local" }, { replace: true }],
    ]);
  });

  it("replaces its own entry when asked for another place while it is open", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=studio&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings({ tab: "accounts" });

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "accounts", workspace: WORKSPACE_ID, account: "local" }, { replace: true }],
    ]);
  });

  it("pushes over a page or a workspace, so dismissing it can go back", () => {
    const shell = makeShell();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    land(shell, "/account/local/create");
    shell.openSettings({ tab: "accounts" });
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openSettings({ tab: "accounts" });

    expect(routeSet.mock.calls.map((call) => call[2])).toEqual([undefined, undefined]);
  });

  it("closes whichever menu it was chosen from", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openWorkspaceSwitcher();
    shell.openSettings({ tab: "workspace", settings_workspace: WORKSPACE_ID });
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openAccountSwitcher();
    shell.openSettings({ tab: "accounts" });
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("remembers the recovery page it was opened from, which stays painted behind it", () => {
    // Without it the backdrop would be Home -- or, through the gear, the very
    // machine the recovery page is there because nobody could load.
    const shell = makeShell();
    land(shell, RECOVERY_PAGE);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettings({ tab: "studio" });

    expect(shell.pageRouteBehindOverlay).toBe(RECOVERY_PAGE);
    // The recovery page displays no workspace, so Settings names none to float over.
    expect(routeSet).toHaveBeenCalledWith("/settings", { tab: "studio", account: "local" }, undefined);
  });

  it("keeps the remembered page while moving around inside Settings", () => {
    const shell = makeShell();
    land(shell, RECOVERY_PAGE);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openSettings({ tab: "studio" });
    land(shell, "/settings?tab=studio&account=local");

    shell.openSettings({ tab: "accounts" });
    land(shell, "/settings?tab=accounts&account=local");

    expect(shell.pageRouteBehindOverlay).toBe(RECOVERY_PAGE);
  });

  it("remembers no page from a surface whose own backdrop is right", () => {
    const shell = makeShell();
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openSettings();
    expect(shell.pageRouteBehindOverlay).toBeNull();

    land(shell, "/account/local/");
    shell.openSettings();
    expect(shell.pageRouteBehindOverlay).toBeNull();
  });
});

describe("ShellState.openSettingsFromBar", () => {
  it("opens Settings plainly on Home", () => {
    const shell = makeShell();
    land(shell, "/account/local/");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([["/settings", { account: "local" }, undefined]]);
  });

  it("opens Settings plainly on a hub page that names no workspace, even while some workspace has a request", () => {
    const shell = makeShell();
    publishPendingRequests(shell, [WORKSPACE_ID]);
    land(shell, "/account/local/create");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([["/settings", { account: "local" }, undefined]]);
  });

  it("opens Workspace & apps on the workspace on screen, floating over it", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    // No `group`: Settings lands on General.
    expect(routeSet.mock.calls).toEqual([
      [
        "/settings",
        { tab: "workspace", settings_workspace: WORKSPACE_ID, workspace: WORKSPACE_ID, account: "local" },
        undefined,
      ],
    ]);
  });

  it("opens on Permissions while that workspace has a request waiting on an answer", () => {
    const shell = makeShell();
    publishPendingRequests(shell, [WORKSPACE_ID]);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([
      [
        "/settings",
        {
          tab: "workspace",
          settings_workspace: WORKSPACE_ID,
          group: "permissions",
          workspace: WORKSPACE_ID,
          account: "local",
        },
        undefined,
      ],
    ]);
  });

  it("does not open on Permissions for a request that is some other workspace's", () => {
    const shell = makeShell();
    publishPendingRequests(shell, ["agent-cd34"]);
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet).toHaveBeenCalledWith(
      "/settings",
      { tab: "workspace", settings_workspace: WORKSPACE_ID, workspace: WORKSPACE_ID, account: "local" },
      undefined,
    );
  });

  it("reads the pending request under the agent id when the route names the workspace by its host", () => {
    const shell = makeShell();
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    publishPendingRequests(shell, ["agent-aa11"]);
    land(shell, "/workspace/host-bb22");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet).toHaveBeenCalledWith(
      "/settings",
      {
        tab: "workspace",
        settings_workspace: "agent-aa11",
        group: "permissions",
        workspace: "agent-aa11",
        account: "local",
      },
      undefined,
    );
  });

  it("picks the workspace a sub-page names, without floating over a surface that is not on screen", () => {
    // The backups page replaces the workspace's surface: the bar still speaks
    // for that workspace, but there is no frame to keep mounted behind Settings.
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}/backups`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "workspace", settings_workspace: WORKSPACE_ID, account: "local" }, undefined],
    ]);
  });

  it("keeps speaking for the workspace behind Get help when switched to from its strip", () => {
    const shell = makeShell();
    publishPendingRequests(shell, [WORKSPACE_ID]);
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([
      [
        "/settings",
        {
          tab: "workspace",
          settings_workspace: WORKSPACE_ID,
          group: "permissions",
          workspace: WORKSPACE_ID,
          account: "local",
        },
        { replace: true },
      ],
    ]);
  });

  it("does not take a creation page's attempt for a workspace to open Settings on", () => {
    const shell = makeShell();
    land(shell, "/creating/create-attempt-ff00");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openSettingsFromBar();

    expect(routeSet.mock.calls).toEqual([["/settings", { account: "local" }, undefined]]);
  });
});

describe("ShellState.pressShare", () => {
  const SHARING = { tab: "workspace", group: "sharing", settings_workspace: WORKSPACE_ID };

  it("opens Settings at Sharing on the whole workspace on screen", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    // No `share_target`: the Full workspace page.
    expect(routeSet.mock.calls).toEqual([
      ["/settings", { ...SHARING, workspace: WORKSPACE_ID, account: "local" }, undefined],
    ]);
    expect(shell.isShareNotYetCreatedDialogOpen).toBe(false);
  });

  it("moves an open Settings to Sharing, replacing its entry", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=studio&workspace=${WORKSPACE_ID}&account=local`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { ...SHARING, workspace: WORKSPACE_ID, account: "local" }, { replace: true }],
    ]);
  });

  it("moves Settings from one app's Sharing page to the whole workspace's", () => {
    const shell = makeShell();
    land(
      shell,
      `/settings?tab=workspace&group=sharing&settings_workspace=${WORKSPACE_ID}&share_target=web&workspace=${WORKSPACE_ID}`,
    );
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { ...SHARING, workspace: WORKSPACE_ID, account: "local" }, { replace: true }],
    ]);
  });

  it("moves Settings from another workspace's Sharing to the one on screen", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&group=sharing&settings_workspace=agent-cd34&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(routeSet.mock.calls).toEqual([
      ["/settings", { ...SHARING, workspace: WORKSPACE_ID, account: "local" }, { replace: true }],
    ]);
  });

  it("puts Settings away when it is already on that workspace's whole-workspace Sharing", () => {
    // A second press of Share, like a second press of any flyout's button.
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&group=sharing&settings_workspace=${WORKSPACE_ID}&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(routeSet.mock.calls).toEqual([[`/workspace/${WORKSPACE_ID}`, undefined, { replace: true }]]);
  });

  it("reads the picked workspace as the one behind Settings when the URL leaves it out", () => {
    // `settings_workspace` absent means the workspace Settings floats over (spec 4.7).
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&group=sharing&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(routeSet.mock.calls).toEqual([[`/workspace/${WORKSPACE_ID}`, undefined, { replace: true }]]);
  });

  it("raises the not-yet-created dialog on a creation page, and opens nothing", () => {
    const shell = makeShell();
    land(shell, "/creating/create-attempt-ff00");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.pressShare();

    expect(shell.isShareNotYetCreatedDialogOpen).toBe(true);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("does nothing on Home or a hub page, where the bar is about no workspace", () => {
    const shell = makeShell();
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    land(shell, "/account/local/");
    shell.pressShare();
    land(shell, "/account/local/create");
    shell.pressShare();
    // Settings over Home has picked a workspace, but the bar is still Home's.
    land(shell, `/settings?tab=workspace&group=general&settings_workspace=${WORKSPACE_ID}&account=local`);
    shell.pressShare();

    expect(routeSet).not.toHaveBeenCalled();
    expect(shell.isShareNotYetCreatedDialogOpen).toBe(false);
  });

  it("closes whichever menu was open", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openWorkspaceSwitcher();
    shell.pressShare();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openAccountSwitcher();
    shell.pressShare();
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });
});

describe("ShellState.toggleWorkspaceSwitcher", () => {
  it("opens the menu, and a second press puts it away", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.mocked(m.redraw).mockClear();

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    // Local state on a surface with nothing floating: no navigation either way.
    expect(routeSet).not.toHaveBeenCalled();
    // Pressed outside any click handler, so each press asks for its own redraw.
    expect(m.redraw).toHaveBeenCalledTimes(2);
  });

  it("opens on Home and on a hub page, where the bar has no workspace button of its own", () => {
    const shell = makeShell();

    land(shell, "/account/local/");
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);

    shell.toggleWorkspaceSwitcher();
    land(shell, "/account/local/workspaces/destroyed");
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("takes the account menu's place and the feed's", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openAccountSwitcher();
    shell.openNotifications();

    shell.toggleWorkspaceSwitcher();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("switches away from an open Settings or Get help like the strip's own button, and rides that landing", () => {
    for (const flyout of ["/settings?tab=studio", "/help?assist=1"]) {
      const shell = makeShell();
      land(shell, `${flyout}&workspace=${WORKSPACE_ID}`);
      const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

      shell.toggleWorkspaceSwitcher();

      expect(routeSet.mock.calls).toEqual([[`/workspace/${WORKSPACE_ID}`, undefined, { replace: true }]]);
      expect(shell.isWorkspaceSwitcherOpen).toBe(true);
      land(shell, `/workspace/${WORKSPACE_ID}`);
      expect(shell.isWorkspaceSwitcherOpen).toBe(true);
      routeSet.mockRestore();
    }
  });

  it("closes an open menu even where it would not open one", () => {
    // Closing is never refused: a menu left up under a dialog that appeared
    // after it could not be put away by the key that raised it.
    const queue = new ErrorDialogQueue();
    queue.attachHost();
    const shell = new ShellState(createEmptyStores(), undefined, undefined, queue);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    stubAccentPainting();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openWorkspaceSwitcher();
    queue.show({ error: { message: "Couldn't do that.", details: "" } });

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("does nothing in the start flow, which has no bar to hang the menu from", () => {
    const shell = makeShell();
    land(shell, "/start");
    vi.mocked(m.redraw).mockClear();

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(m.redraw).not.toHaveBeenCalled();
  });

  it("does nothing in a pulled-out window", () => {
    const shell = makeShell();
    land(shell, `/popout/${WORKSPACE_ID}/win-0123`);

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("does nothing under the error dialog", () => {
    const queue = new ErrorDialogQueue();
    queue.attachHost();
    const shell = new ShellState(createEmptyStores(), undefined, undefined, queue);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    stubAccentPainting();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    queue.show({ error: { message: "Couldn't do that.", details: "" } });

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    // Answered, the dialog no longer holds the window.
    queue.dismiss();
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under the browser sign-in", () => {
    const webLoginModel = new WebLoginModel(
      undefined,
      () => {},
      () => {},
    );
    webLoginModel.state = "waiting";
    const shell = new ShellState(createEmptyStores(), webLoginModel);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    stubAccentPainting();
    land(shell, "/account/local/");

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    webLoginModel.dismiss();
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under the client-permission prompt", () => {
    const prompt = new ClientPermissionPromptModel({ redraw: () => undefined, answer: () => undefined });
    prompt.show(clientPermissionAsk());
    const shell = new ShellState(createEmptyStores(), undefined, undefined, undefined, prompt);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    stubAccentPainting();
    land(shell, `/workspace/${WORKSPACE_ID}`);

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    prompt.dismiss();
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under the recovery card", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openRecoveryModal(WORKSPACE_ID);

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.finishRecovery();
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under Share's not-yet-created dialog", () => {
    const shell = makeShell();
    land(shell, "/creating/create-attempt-ff00");
    shell.pressShare();

    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.isShareNotYetCreatedDialogOpen = false;
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under a centered app modal, and leaves that modal where it is", () => {
    for (const modalRoute of [
      `/inbox?selected=evt-a&workspace=${WORKSPACE_ID}`,
      "/inbox",
      "/settings/ai-keys?workspace=host-99aa",
    ]) {
      const shell = makeShell();
      const back = vi.fn();
      vi.stubGlobal("window", { history: { length: 5, back } });
      land(shell, modalRoute);
      const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

      shell.toggleWorkspaceSwitcher();

      expect(shell.isWorkspaceSwitcherOpen, modalRoute).toBe(false);
      expect(routeSet, modalRoute).not.toHaveBeenCalled();
      expect(back, modalRoute).not.toHaveBeenCalled();
      routeSet.mockRestore();
    }
  });

  it("stays available while the workspace's update is landing, whose cover leaves the bar clear for it", () => {
    const shell = makeShell();
    shell.stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: {
        [WORKSPACE_ID]: {
          availability: "OUT_OF_DATE",
          current_version: "minds-v0.3.9",
          supported_version: "minds-v0.4.1",
          activity: "APPLYING",
        },
      },
      update_window: "2:00 AM-5:00 AM",
    });
    expect(shell.stores.updates.isApplying(WORKSPACE_ID)).toBe(true);
    land(shell, `/workspace/${WORKSPACE_ID}`);

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("does nothing under the template stepper floating over a workspace, and leaves the stepper where it is", () => {
    // A centered card like the request popup, though its route is a hub
    // page's rather than one of the app-overlay paths.
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/account/local/create/template?workspace=${WORKSPACE_ID}&git_url=https%3A%2F%2Fexample.com%2Fr.git`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(back).not.toHaveBeenCalled();
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("opens on the template page with no workspace behind it, which is a page and not a modal", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, "/account/local/create/template?git_url=https%3A%2F%2Fexample.com%2Fr.git");

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
    // Nothing floats there to put away first.
    expect(back).not.toHaveBeenCalled();
  });

  it("does nothing while any Modal is on screen, such as a confirmation raised from inside Settings", () => {
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const removeModal = raiseModal();

    shell.toggleWorkspaceSwitcher();

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    // Settings is not put away from under its own confirmation.
    expect(routeSet).not.toHaveBeenCalled();

    // The confirmation answered, the shortcut works again.
    removeModal();
    shell.toggleWorkspaceSwitcher();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });
});

describe("ShellState.openHelp", () => {
  it("names a shared workspace without offering assist, since it has no agent of the account's to ask", () => {
    const shell = makeShell();
    const shareKey = sharedWorkspace().share_key;
    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({ workspaces: [sharedWorkspaceEntry({ status: "accepted" })] }),
    );
    land(shell, `/workspace/${shareKey}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelp();

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { workspace: shareKey, assist: "0", account: "local" },
      undefined,
    );
  });

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

  it("replaces Settings' entry when switched to from its strip", () => {
    // Settings -> bug icon is a switch between two of the strip's flyouts:
    // pushed, Settings would be left one Back away under Get help, and
    // dismissing the form would re-raise the Settings that was just left.
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&group=sharing&workspace=${WORKSPACE_ID}`);
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

  it("lets go of the Settings the request popup was remembering", () => {
    // Leaving the popup for Get help leaves Settings like it leaves the
    // popup. Kept, Settings would stay painted under Get help with its own
    // backdrop and its own raised strip -- two flyouts at once.
    const shell = makeShell();
    land(shell, SETTINGS_ON_WAITING);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    expect(shell.settingsRouteBehindOverlay).toBe(SETTINGS_ON_WAITING);

    shell.openHelp();

    expect(routeSet.mock.lastCall?.slice(0, 2)).toEqual([
      "/help",
      { workspace: WORKSPACE_ID, assist: "1", account: "local" },
    ]);
    // Settings lives exactly as long as /inbox is the route: it is let go
    // the moment the /help route lands -- however /help was reached, the
    // bar or an Electron open-overlay ask alike.
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=1`);
    expect(shell.settingsRouteBehindOverlay).toBeNull();
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

  it("replaces a routed flyout's entry rather than stacking on it", () => {
    // Same reasoning as openHelp: pushed, Settings would be left one Back
    // away under the report form it was covered by.
    const shell = makeShell();
    land(shell, `/settings?tab=workspace&group=sharing&workspace=${WORKSPACE_ID}`);
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

describe("ShellState.openHelpForError", () => {
  const ERROR = { message: "Couldn't rename this workspace.", details: "HTTP 500" };

  it("scopes the report to the workspace on screen and attaches the error", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForError(ERROR, WORKSPACE_ID);

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      expect.objectContaining({ workspace: WORKSPACE_ID }),
      undefined,
    );
    expect(takePendingHelpLaunch()).toMatchObject({
      workspaceAgentId: WORKSPACE_ID,
      errorDetails: "Couldn't rename this workspace.\n\nHTTP 500",
    });
  });

  it("leaves the workspace out of the route, still collecting from it, when it is not on screen", () => {
    // Scoping the route would mount that workspace's frame in place of Home,
    // where the error was shown.
    const shell = makeShell();
    land(shell, "/");
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForError(ERROR, WORKSPACE_ID);

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      { account: "local" },
      undefined,
    );
    const launch = takePendingHelpLaunch();
    expect(launch?.workspaceAgentId).toBe("");
    expect(launch?.errorWorkspaceAgentId).toBe(WORKSPACE_ID);
  });

  it("scopes the report from the workspace's recovery page, which stays painted behind the form", () => {
    const shell = makeShell();
    land(shell, `/agents/${WORKSPACE_ID}/recovery`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForError(ERROR, WORKSPACE_ID);

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      expect.objectContaining({ workspace: WORKSPACE_ID }),
      undefined,
    );
    expect(shell.pageRouteBehindOverlay).toBe(`/agents/${WORKSPACE_ID}/recovery`);
  });

  it("keeps the recovery page behind the form when raised over the Get help that page opened", () => {
    const recoveryRoute = `/agents/${WORKSPACE_ID}/recovery`;
    const shell = makeShell();
    land(shell, recoveryRoute);
    shell.rememberPageBehindOverlay();
    land(shell, `/help?workspace=${WORKSPACE_ID}&assist=0`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    shell.openHelpForError(ERROR, WORKSPACE_ID);

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      expect.objectContaining({ workspace: WORKSPACE_ID }),
      { replace: true },
    );
    expect(shell.pageRouteBehindOverlay).toBe(recoveryRoute);
  });
});

describe("ShellState.dismissAppOverlay", () => {
  /** A shell whose request popup was opened from Settings and has landed. */
  function popupOverSettings(): { shell: ShellState; routeSet: ReturnType<typeof vi.spyOn>; back: ReturnType<typeof vi.fn> } {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, SETTINGS_ON_WAITING);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?selected=evt-a&workspace=${WORKSPACE_ID}&account=local`);
    routeSet.mockClear();
    return { shell, routeSet, back };
  }

  it("returns a popup opened from Settings to that Settings route, replacing the popup's entry", () => {
    // Back to where the request was picked, with the tab and section it was
    // left on -- and replaced, so Back from Settings does not re-raise the
    // popup that was just dismissed.
    const { shell, routeSet, back } = popupOverSettings();

    expect(shell.dismissAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet.mock.calls).toEqual([[SETTINGS_ON_WAITING, undefined, { replace: true }]]);
  });

  it("keeps Settings remembered until the return lands, then lets it go", () => {
    // Dropped early, the Settings underneath would blink out for the frames
    // before its own route arrives.
    const { shell } = popupOverSettings();

    shell.dismissAppOverlay();
    expect(shell.settingsRouteBehindOverlay).toBe(SETTINGS_ON_WAITING);

    land(shell, SETTINGS_ON_WAITING);
    expect(shell.settingsRouteBehindOverlay).toBeNull();
  });

  it("does not return twice when one dismissal arrives twice", () => {
    // The in-document Escape plus Electron's forward of it.
    const { shell, routeSet } = popupOverSettings();

    expect(shell.dismissAppOverlay()).toBe(true);
    land(shell, `/inbox?selected=evt-a&workspace=${WORKSPACE_ID}&account=local`);
    expect(shell.dismissAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenCalledTimes(1);
  });

  it("gives Escape on the popup the same way back", () => {
    const { shell, routeSet } = popupOverSettings();

    expect(shell.handleEscape()).toBe(true);

    expect(routeSet.mock.calls).toEqual([[SETTINGS_ON_WAITING, undefined, { replace: true }]]);
  });

  it("closes the Settings it returned to as an ordinary flyout, out to the workspace", () => {
    const { shell, routeSet } = popupOverSettings();
    shell.dismissAppOverlay();
    land(shell, SETTINGS_ON_WAITING);

    expect(shell.dismissAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenLastCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });
  });

  it("leaves a popup opened from the chat for the workspace, with no Settings to go back to", () => {
    const shell = makeShell();
    const back = vi.fn();
    vi.stubGlobal("window", { history: { length: 5, back } });
    land(shell, `/workspace/${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?selected=evt-a&workspace=${WORKSPACE_ID}&account=local`);

    expect(shell.dismissAppOverlay()).toBe(true);

    expect(back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenLastCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });
  });

  it("does not go back to a Settings the reader has since left", () => {
    // Remembered only while /inbox is the route: Settings that was closed and
    // a popup opened afresh over the workspace has nothing underneath.
    const { shell, routeSet } = popupOverSettings();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    shell.openInbox({ selected: "evt-b" });
    land(shell, `/inbox?selected=evt-b&workspace=${WORKSPACE_ID}&account=local`);

    expect(shell.dismissAppOverlay()).toBe(true);

    expect(routeSet).toHaveBeenLastCalledWith(`/workspace/${WORKSPACE_ID}`, undefined, { replace: true });
  });
});

describe("ShellState.returnToSettingsAfterRequest", () => {
  function listWith(count: number): WaitingRequestList {
    return {
      forgetWaitingRequest: () => undefined,
      hasWaitingRequests: () => count > 0,
    };
  }

  it("hands Settings back on its own list while other requests are still waiting", () => {
    // The reader picked this request off that list and the rest of it is still
    // theirs to work through, so they land back on it, on the section they left.
    const shell = makeShell();
    shell.settingsRouteBehindOverlay = SETTINGS_ON_WAITING;
    shell.registerWaitingRequestList(listWith(1));
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToSettingsAfterRequest()).toBe(true);

    expect(routeSet.mock.calls).toEqual([[SETTINGS_ON_WAITING, undefined, { replace: true }]]);
  });

  it("hands Settings back on Add connection once nothing is left to answer", () => {
    // The list that led here has just emptied, so returning to it would be
    // returning to nothing.
    const shell = makeShell();
    shell.settingsRouteBehindOverlay = SETTINGS_ON_WAITING;
    shell.registerWaitingRequestList(listWith(0));
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToSettingsAfterRequest()).toBe(true);

    expect(routeSet).toHaveBeenCalledTimes(1);
    const [route, params, options] = routeSet.mock.calls[0];
    expect(String(route).split("?")[0]).toBe("/settings");
    // Still floating over the same workspace, on the same account.
    expect(queryOf(String(route))).toEqual({
      tab: "workspace",
      group: "permissions",
      perm_section: "add-connection",
      workspace: WORKSPACE_ID,
      account: "local",
    });
    expect(params).toBeUndefined();
    // Replaced, so Back does not re-raise the answered request.
    expect(options).toEqual({ replace: true });
  });

  it("hands Settings back on Add connection when no pane list ever registered", () => {
    // A Settings that never reached its Permissions page (a request that
    // arrived from a notification while it showed Studio) has no list to speak
    // for it; Add connection is the safe landing, never a section that is gone.
    const shell = makeShell();
    shell.settingsRouteBehindOverlay = `/settings?tab=studio&section=display&settings_workspace=agent-cd34&workspace=${WORKSPACE_ID}&account=local`;
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);

    expect(shell.returnToSettingsAfterRequest()).toBe(true);

    const [route, , options] = routeSet.mock.calls[0];
    expect(queryOf(String(route))).toEqual({
      tab: "workspace",
      group: "permissions",
      perm_section: "add-connection",
      // What Settings had picked and floated over is not the answer's to change.
      section: "display",
      settings_workspace: "agent-cd34",
      workspace: WORKSPACE_ID,
      account: "local",
    });
    expect(options).toEqual({ replace: true });
  });

  it("reports no Settings for a page opened from the chat", () => {
    // Nothing to hand back to: that page simply closes.
    const shell = makeShell();
    shell.registerWaitingRequestList(listWith(3));
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    expect(shell.returnToSettingsAfterRequest()).toBe(false);
    expect(shell.returnToSettingsBehindOverlay()).toBe(false);

    expect(routeSet).not.toHaveBeenCalled();
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

  it("gives the key to the workspace menu over an open card", () => {
    // The menu sits over every other surface, the card included, so it has to
    // be asked first -- and the card must survive the keypress.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openWorkspaceSwitcher();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("gives the key to an error dialog over the workspace menu", () => {
    const queue = new ErrorDialogQueue();
    queue.attachHost();
    shell = new ShellState(createEmptyStores(), undefined, undefined, queue);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    shell.openWorkspaceSwitcher();
    queue.show({ error: { message: "Couldn't do that.", details: "" } });

    expect(shell.handleEscape()).toBe(true);

    expect(queue.current).toBeNull();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("closes the card once the menu above it is gone", () => {
    // The keypress after the one the menu took: the card is next in line,
    // not skipped over because the menu was there first.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openWorkspaceSwitcher();
    shell.handleEscape();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("gives the key to the account menu over an open card, as it does the workspace menu", () => {
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openAccountSwitcher();

    expect(shell.handleEscape()).toBe(true);

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("gives the key to the workspace menu over the feed and the prompts beneath it, one surface a press", () => {
    const webLoginModel = new WebLoginModel(
      undefined,
      () => {},
      () => {},
    );
    webLoginModel.state = "waiting";
    shell = new ShellState(createEmptyStores(), webLoginModel);
    shell.openNotifications();
    shell.openWorkspaceSwitcher();

    expect(shell.handleEscape()).toBe(true);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(webLoginModel.isOpen).toBe(true);
    expect(shell.isNotificationsOpen).toBe(true);

    expect(shell.handleEscape()).toBe(true);
    expect(webLoginModel.isOpen).toBe(false);
    expect(shell.isNotificationsOpen).toBe(true);

    expect(shell.handleEscape()).toBe(true);
    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.handleEscape()).toBe(false);
  });

  it("closes the workspace menu first and the Settings flyout under it second", () => {
    // The shortcut opens the menu over Settings in the beat before Settings'
    // dismissal lands; the menu is on top, so it takes the first press and
    // Settings is left standing for the second.
    vi.spyOn(m.route, "get").mockReturnValue(`/settings?tab=studio&workspace=${AGENT}`);
    displayingRoute(shell, "/settings", `tab=studio&workspace=${AGENT}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openWorkspaceSwitcher();

    expect(shell.handleEscape()).toBe(true);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(routeSet).not.toHaveBeenCalled();

    expect(shell.handleEscape()).toBe(true);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, undefined, { replace: true });
  });

  it("closes a confirmation raised inside Settings first, and Settings second", () => {
    vi.spyOn(m.route, "get").mockReturnValue(`/settings?tab=workspace&group=general&workspace=${AGENT}`);
    displayingRoute(shell, "/settings", `tab=workspace&group=general&workspace=${AGENT}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    let closes = 0;
    const removeModal = raiseModal(() => {
      closes += 1;
      removeModal();
    });

    expect(shell.handleEscape()).toBe(true);
    expect(closes).toBe(1);
    // Settings stays where it was under the confirmation that just closed.
    expect(routeSet).not.toHaveBeenCalled();

    expect(shell.handleEscape()).toBe(true);
    expect(closes).toBe(1);
    expect(routeSet.mock.calls).toEqual([[`/workspace/${AGENT}`, undefined, { replace: true }]]);
  });

  it("lets a Modal that cannot be dismissed swallow the key, leaving what is under it alone", () => {
    // The key is the Modal's to ignore, not the surface underneath's to take.
    vi.spyOn(m.route, "get").mockReturnValue(`/settings?tab=workspace&workspace=${AGENT}`);
    displayingRoute(shell, "/settings", `tab=workspace&workspace=${AGENT}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    raiseModal();

    expect(shell.handleEscape()).toBe(true);
    expect(shell.handleEscape()).toBe(true);

    expect(isAnyModalOpen()).toBe(true);
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("closes a Modal before the feed and the recovery card it is drawn over", () => {
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.openNotifications();
    let closes = 0;
    const removeModal = raiseModal(() => {
      closes += 1;
      removeModal();
    });

    expect(shell.handleEscape()).toBe(true);

    expect(closes).toBe(1);
    expect(shell.isNotificationsOpen).toBe(true);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    // The feed is next, then the card.
    expect(shell.handleEscape()).toBe(true);
    expect(shell.isNotificationsOpen).toBe(false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("closes only the topmost of two Modals a press", () => {
    displaying(shell, AGENT);
    const closed: string[] = [];
    const removeFirst = raiseModal(() => {
      closed.push("first");
      removeFirst();
    });
    const removeSecond = raiseModal(() => {
      closed.push("second");
      removeSecond();
    });

    expect(shell.handleEscape()).toBe(true);
    expect(closed).toEqual(["second"]);
    expect(shell.handleEscape()).toBe(true);
    expect(closed).toEqual(["second", "first"]);
    expect(shell.handleEscape()).toBe(false);
  });

  it("gives the key to a menu, the client-permission prompt and the sign-in before a Modal under them", () => {
    const webLoginModel = new WebLoginModel(
      undefined,
      () => {},
      () => {},
    );
    webLoginModel.state = "waiting";
    const prompt = new ClientPermissionPromptModel({ redraw: () => undefined, answer: () => undefined });
    prompt.show(clientPermissionAsk());
    shell = new ShellState(createEmptyStores(), webLoginModel, undefined, undefined, prompt);
    shell.openWorkspaceSwitcher();
    let closes = 0;
    raiseModal(() => (closes += 1));

    expect(shell.handleEscape()).toBe(true);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
    expect(shell.handleEscape()).toBe(true);
    expect(prompt.isOpen).toBe(false);
    expect(shell.handleEscape()).toBe(true);
    expect(webLoginModel.isOpen).toBe(false);
    expect(closes).toBe(0);

    expect(shell.handleEscape()).toBe(true);
    expect(closes).toBe(1);
  });

  it("gives the key to the sign-in modal over the z-[110] surface beneath it", () => {
    // The Shell paints the sign-in modal over every other z-[110] surface
    // (Settings' Accounts tab, the feed), so Escape must close it and leave
    // the surface that opened it standing.
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

  it("gives the key to the client-permission prompt over the sign-in modal, answering a one-time Don't allow", () => {
    // The Shell paints the prompt last of all the z-[110] surfaces, so it is
    // closed first, and closing it answers main rather than leaving the
    // page's request waiting.
    const webLoginModel = new WebLoginModel(
      undefined,
      () => {},
      () => {},
    );
    webLoginModel.state = "waiting";
    const answers: ClientPermissionAnswer[] = [];
    const prompt = new ClientPermissionPromptModel({
      redraw: () => undefined,
      answer: (answer) => answers.push(answer),
    });
    prompt.show(clientPermissionAsk());
    shell = new ShellState(createEmptyStores(), webLoginModel, undefined, undefined, prompt);

    expect(shell.handleEscape()).toBe(true);

    expect(prompt.isOpen).toBe(false);
    expect(answers).toEqual([{ promptId: "prompt-1", decision: "denied", isRemembered: false }]);
    expect(webLoginModel.isOpen).toBe(true);
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

describe("ShellState updates", () => {
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

  it("opens Settings on the machine's Updates from Home without entering it, flagging one arrival to flash", () => {
    const shell = outOfDateShell();
    stubAccentPainting();
    land(shell, "/");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openUpdatesTab(AGENT);

    // The machine is picked, not floated over: no `workspace`, so Home stays behind Settings.
    expect(routeSet.mock.calls).toEqual([
      ["/settings", { tab: "workspace", group: "updates", settings_workspace: AGENT, account: "local" }, undefined],
    ]);
    expect(shell.takeUpdatesTabArrival(AGENT)).toMatchObject({ agentId: AGENT, failure: null });
    // Taken once: a later draw of the page does not flash again.
    expect(shell.takeUpdatesTabArrival(AGENT)).toBeNull();
  });

  it("names the machine by its agent id whichever coordinate asked, floating over it when it is on screen", () => {
    const shell = outOfDateShell();
    stubAccentPainting();
    land(shell, "/workspace/host-bb22");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openUpdatesTab("host-bb22");

    // The listed workspace's host coordinate resolves to its agent id.
    expect(routeSet).toHaveBeenCalledWith(
      "/settings",
      {
        tab: "workspace",
        group: "updates",
        settings_workspace: "agent-aa11",
        workspace: "agent-aa11",
        account: "local",
      },
      undefined,
    );
    // Held for the machine it names, and for no other.
    expect(shell.takeUpdatesTabArrival(AGENT)).toBeNull();
    expect(shell.takeUpdatesTabArrival("agent-aa11")?.agentId).toBe("agent-aa11");
  });

  it("replaces the open Settings' history entry, and flags a fresh arrival on each request", () => {
    // Asked from the page itself (a pill click while Settings is up) it must
    // still flash, without stacking a Back entry per click.
    const shell = outOfDateShell();
    stubAccentPainting();
    land(shell, `/settings?tab=workspace&group=general&settings_workspace=${AGENT}&workspace=${AGENT}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openUpdatesTab(AGENT);
    const first = shell.takeUpdatesTabArrival(AGENT)?.serial ?? 0;
    const failure = { isOk: false, status: 502, error: "Couldn't reach this workspace.", detail: "", code: "" };
    shell.openUpdatesTab(AGENT, { failure });

    expect(routeSet).toHaveBeenLastCalledWith(
      "/settings",
      { tab: "workspace", group: "updates", settings_workspace: AGENT, workspace: AGENT, account: "local" },
      { replace: true },
    );
    const second = shell.takeUpdatesTabArrival(AGENT);
    expect(second?.serial).toBe(first + 1);
    expect(second?.failure).toBe(failure);
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
      recoveryRoute("agent-aa11", "/goto/agent-aa11/", "start"),
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
      recoveryRoute("agent-aa11", "/goto/agent-aa11/", null),
    );
  });
});

describe("pulled-out windows (the popout route)", () => {
  const POPOUT_PATH = `/popout/${AGENT}/win-0123`;

  /** A shell on the popout route, its frame's reattach sender recording what
   * it is asked to send. */
  function popoutShell(): { shell: ShellState; sends: string[] } {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    const sends: string[] = [];
    shell.registerReattachWindowSender((windowId) => sends.push(windowId));
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
      const settled = shell.returnPopoutToDesktop().then(() => {
        isSettled = true;
      });
      expect(sends).toEqual(["win-0123"]);
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
      const settled = shell.returnPopoutToDesktop().then(() => {
        isSettled = true;
      });
      expect(sends).toEqual(["win-0123"]);
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
    await shell.returnPopoutToDesktop();
    expect(sends).toEqual(["win-0123"]);
    stubAccentPainting();
    const plain = new ShellState(createEmptyStores());
    const plainSends: unknown[] = [];
    plain.registerReattachWindowSender((windowId) => plainSends.push(windowId));
    vi.spyOn(m.route, "get").mockImplementation(() => `/workspace/${AGENT}`);
    plain.handleRouteChanged(`/workspace/${AGENT}`);
    await plain.returnPopoutToDesktop();
    expect(plainSends).toEqual([]);
  });

  it("returns a popout's window through the mounted frame only when this main window shows its workspace", () => {
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    const sends: unknown[] = [];
    shell.registerReattachWindowSender((windowId) => sends.push(windowId));
    const ask = { workspaceId: AGENT, windowId: "win-0123" };
    // A hub page shows no workspace; another workspace's surface is not this one's desktop.
    vi.spyOn(m.route, "get").mockImplementation(() => "/");
    shell.handleRouteChanged("/");
    shell.handleReattachPopoutWindow(ask);
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/agent-ff00");
    shell.handleRouteChanged("/workspace/agent-ff00");
    shell.handleReattachPopoutWindow(ask);
    expect(sends).toEqual([]);
    // The workspace's own surface takes it.
    vi.spyOn(m.route, "get").mockImplementation(() => `/workspace/${AGENT}`);
    shell.handleRouteChanged(`/workspace/${AGENT}`);
    shell.handleReattachPopoutWindow(ask);
    expect(sends).toEqual(["win-0123"]);
    // A popout of the workspace is a view of one window, never the desktop that takes one back.
    vi.spyOn(m.route, "get").mockImplementation(() => POPOUT_PATH);
    shell.handleRouteChanged(POPOUT_PATH);
    shell.handleReattachPopoutWindow(ask);
    expect(sends).toHaveLength(1);
  });

  it("takes a reattach ask naming the workspace by either coordinate", () => {
    // A popout restored from the saved session names its workspace by the
    // host-scoped id its persisted route was written with; a cold-started
    // main window may display the workspace under that spelling too.
    stubAccentPainting();
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    const sends: unknown[] = [];
    shell.registerReattachWindowSender((windowId) => sends.push(windowId));
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/agent-aa11");
    shell.handleRouteChanged("/workspace/agent-aa11");
    shell.handleReattachPopoutWindow({ workspaceId: "host-bb22", windowId: "win-0123" });
    vi.spyOn(m.route, "get").mockImplementation(() => "/workspace/host-bb22");
    shell.handleRouteChanged("/workspace/host-bb22");
    shell.handleReattachPopoutWindow({ workspaceId: "agent-aa11", windowId: "win-4567" });
    shell.handleReattachPopoutWindow({ workspaceId: "host-0000", windowId: "win-8888" });
    expect(sends).toEqual(["win-0123", "win-4567"]);
  });

  it("answers main once the desktop reports the window it was asked to take back, so the popout can go", async () => {
    vi.useFakeTimers();
    try {
      const shell = workspaceShell();
      const ignored = { count: 0 };
      report(shell, [{ windowId: "win-0123", title: "PRs" }], [], ignored);
      let answer: boolean | null = null;
      const answered = shell
        .handleReattachPopoutWindow({ workspaceId: AGENT, windowId: "win-0123" })
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
      const ask = { workspaceId: AGENT, windowId: "win-0123" };
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
    land(shell, `/settings?tab=workspace&workspace=${BOBS_WORKSPACE}&settings_workspace=${BOBS_WORKSPACE}`);
    land(shell, "/settings?tab=accounts");
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

describe("opening a shared workspace", () => {
  const SHARE_DOMAIN = SHARED_WORKSPACE_DOMAIN;
  const SHARE_KEY = sharedWorkspace().share_key;
  const SHELL_LINK = `https://shell-1.${SHARE_DOMAIN}/`;

  /** A shell whose account's rows hold one accepted share, with the frame armed on `armedAnyId` (none when null). */
  function shellWithShare(armedAnyId: string | null): { shell: ShellState; reloadCount: () => number } {
    const { shell, reloadCount } = shellWithFrameOn(armedAnyId);
    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({ workspaces: [sharedWorkspaceEntry({ status: "accepted" })] }),
    );
    return { shell, reloadCount };
  }

  /** The desktop's answer to the open route: the accepted share's plan onto the shell link, with no redeem unless
   * `overrides` say otherwise. */
  function openPlan(overrides: Partial<SharedOpenPlan> = {}): SharedOpenPlan {
    return {
      share_key: SHARE_KEY,
      host_id: "host-" + "9".repeat(32),
      link: SHELL_LINK,
      redeem_url: null,
      status: "accepted",
      ...overrides,
    };
  }

  /** A fetch answering the open route with the plan; counts the opens. */
  function stubOpen(redeemUrl: string | null): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (url: string) => {
      if (!url.includes("/open")) throw new Error(`unexpected fetch ${url}`);
      return jsonResponse(openPlan({ redeem_url: redeemUrl }));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    return fetchMock;
  }

  it("reloads the frame when the share is the one the window already shows, instead of re-setting the route", async () => {
    const { shell, reloadCount } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    stubOpen(null);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.reopenDisplayedSharedWorkspace();

    expect(reloadCount()).toBe(1);
    expect(routeSet).not.toHaveBeenCalled();
    expect(shell.stores.workspaces.sharedFrameLink(SHARE_KEY)).toBe(SHELL_LINK);
  });

  it("reopens the target the frame was on, not the shell, after a session lapse", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    shell.stores.workspaces.chooseSharedLink(SHARE_KEY, `https://notes-1.${SHARE_DOMAIN}/`, null);
    const opened = stubOpen(null);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.reopenDisplayedSharedWorkspace();

    expect(opened).toHaveBeenCalledTimes(1);
    expect(String(opened.mock.calls[0][1]?.body)).toBe(JSON.stringify({ app: "notes" }));
  });

  it("reads a login-page landing within half a minute of the frame's load as that open's own, and signs in afresh after that", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const opened = stubOpen(null);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-04T09:00:00Z"));
      // The desktop takes 25 s to answer the open (a slow partition sign-in would take as long).
      opened.mockImplementationOnce(async () => {
        vi.setSystemTime(new Date("2026-11-04T09:00:25Z"));
        return jsonResponse(openPlan());
      });
      await shell.openSharedWorkspace(SHARE_KEY, null);
      expect(opened).toHaveBeenCalledTimes(1);

      // The frame landed on the login page right after it started loading: this open's redeem did not carry it.
      vi.setSystemTime(new Date("2026-11-04T09:00:35Z"));
      await expect(shell.reopenDisplayedSharedWorkspace()).resolves.toBe("not_due");
      expect(opened).toHaveBeenCalledTimes(1);

      // A landing long after the frame loaded is a session that went away.
      vi.setSystemTime(new Date("2026-11-04T09:01:25Z"));
      await expect(shell.reopenDisplayedSharedWorkspace()).resolves.toBe("reopened");
      expect(opened).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stamps the share before the open's round trip, so a second landing during it does not open again", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const opened = stubOpen(null);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await Promise.all([shell.reopenDisplayedSharedWorkspace(), shell.reopenDisplayedSharedWorkspace()]);

    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("leaves the window where the user went during a reopen instead of pulling it back onto the share", async () => {
    const { shell, reloadCount } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const fetchMock = vi.fn(async (url: string) => {
      if (!url.includes("/open")) throw new Error(`unexpected fetch ${url}`);
      // The user went Home while the desktop was answering.
      shell.displayedWorkspaceAnyId = null;
      return jsonResponse(openPlan());
    });
    vi.stubGlobal("fetch", fetchMock);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.reopenDisplayedSharedWorkspace();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(routeSet).not.toHaveBeenCalled();
    expect(reloadCount()).toBe(0);
  });

  /** A fetch answering the open route with the plan and the verdict route with its row; counts the opens and
   * records every verdict. */
  function stubOpenAndVerdict(redeemUrl: string | null): { openCount: () => number; verdicts: string[] } {
    const verdicts: string[] = [];
    let openCount = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/verdict")) {
        verdicts.push(String((JSON.parse(String(init?.body)) as { verdict: string }).verdict));
        return jsonResponse({ share_key: SHARE_KEY, row: null });
      }
      if (url.includes("/retry")) return jsonResponse({ share_key: SHARE_KEY, row: null });
      if (!url.includes("/open")) throw new Error(`unexpected fetch ${url}`);
      openCount += 1;
      return jsonResponse(openPlan({ redeem_url: redeemUrl }));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    return { openCount: () => openCount, verdicts };
  }

  it("opens once more with a fresh code when the redeem refused the partition's other session, then gives up", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const { openCount, verdicts } = stubOpenAndVerdict("https://accounts.example/accounts/session?code=c0de&next=x");
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await shell.openSharedWorkspace(SHARE_KEY, null);
    expect(shell.sharedFrameProgress).toMatchObject({ shareKey: SHARE_KEY, phase: "connecting", isSignInRetried: false });

    await shell.handleSharedFrameRedeemRefused("session_mismatch");
    expect(openCount()).toBe(2);
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "connecting", isSignInRetried: true });
    expect(verdicts).toEqual([]);

    await shell.handleSharedFrameRedeemRefused("session_mismatch");
    expect(openCount()).toBe(2);
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: "sign_in_failed" });
    expect(verdicts).toEqual(["sign_in_failed"]);
  });

  it("drops the open's progress when the user left during the retry after a session mismatch", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const redeem = "https://accounts.example/accounts/session?code=c0de&next=x";
    let openCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (!url.includes("/open")) throw new Error(`unexpected fetch ${url}`);
        openCount += 1;
        // The user went Home while the desktop was answering the retry.
        if (openCount === 2) shell.displayedWorkspaceAnyId = null;
        return jsonResponse(openPlan({ redeem_url: redeem }));
      }),
    );
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await shell.openSharedWorkspace(SHARE_KEY, null);
    expect(shell.sharedFrameProgress).toMatchObject({ shareKey: SHARE_KEY, phase: "connecting" });

    await shell.handleSharedFrameRedeemRefused("session_mismatch");

    expect(openCount).toBe(2);
    expect(routeSet).not.toHaveBeenCalled();
    expect(shell.sharedFrameProgress).toBeNull();
  });

  it("fails the open outright on any other refusal of the redeem", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const { openCount, verdicts } = stubOpenAndVerdict("https://accounts.example/accounts/session?code=c0de&next=x");
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await shell.openSharedWorkspace(SHARE_KEY, null);

    await shell.handleSharedFrameRedeemRefused("refused");

    expect(openCount()).toBe(1);
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: "sign_in_failed" });
    expect(verdicts).toEqual(["sign_in_failed"]);
  });

  it("fails the open when the frame lands on the login page with an error, or without one right after the open", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const { openCount, verdicts } = stubOpenAndVerdict(null);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await shell.openSharedWorkspace(SHARE_KEY, null);

    await shell.handleSharedFrameLoginLanding(true);
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: "sign_in_failed" });

    await shell.openSharedWorkspace(SHARE_KEY, null);
    await shell.handleSharedFrameLoginLanding(false);
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: "sign_in_failed" });
    expect(openCount()).toBe(2);
    expect(verdicts).toEqual(["sign_in_failed", "sign_in_failed"]);
  });

  it("takes the overlay down when the workspace's page is up or its document admitted the account", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    stubOpenAndVerdict(null);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.openSharedWorkspace(SHARE_KEY, null);
    shell.sharedFrameReady("some-other-share");
    expect(shell.sharedFrameProgress).not.toBeNull();
    shell.sharedFrameReady(SHARE_KEY);
    expect(shell.sharedFrameProgress).toBeNull();

    await shell.openSharedWorkspace(SHARE_KEY, null);
    await shell.recordSharedFrameVerdict(`shell-1.${SHARE_DOMAIN}`, "refused");
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: "refused" });
    await shell.recordSharedFrameVerdict(`shell-1.${SHARE_DOMAIN}`, "admitted");
    expect(shell.sharedFrameProgress).toBeNull();
  });

  it("logs a refused sign-in again instead of rejecting, since the bridge handler cannot catch it, and shows the refusal", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const fetched: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        fetched.push(url);
        return jsonResponse({ error: "The desktop is away", code: "not_shared" }, 404);
      }),
    );
    const warned = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await expect(shell.reopenDisplayedSharedWorkspace()).resolves.toBe("refused");

    expect(warned.mock.calls.some(([line]) => String(line).includes("could not sign"))).toBe(true);
    // The desktop's refusal is the overlay's words; no sign-in ran, so no verdict is recorded.
    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: null });
    expect(shell.sharedFrameProgress?.refusalMessage).toBe("The desktop is away");
    expect(fetched.some((url) => url.includes("/verdict"))).toBe(false);
  });

  it("shows the desktop's refusal of a Retry from the overlay in its words, recording no verdict", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const fetched: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        fetched.push(url);
        if (url.includes("/retry")) return jsonResponse({ share_key: SHARE_KEY, row: null });
        return jsonResponse({ error: "The workspace has not registered that address yet", code: "no_link" }, 409);
      }),
    );
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.retryDisplayedSharedOpen();

    expect(shell.sharedFrameProgress).toMatchObject({ phase: "failed", verdict: null });
    expect(shell.sharedFrameProgress?.refusalMessage).toBe("The workspace has not registered that address yet");
    expect(fetched.some((url) => url.includes("/verdict"))).toBe(false);
  });

  it("records a verdict only from the frame's own document, not from a nested app frame under the domain", async () => {
    const { shell } = shellWithShare(SHARE_KEY);
    const recorded: string[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      recorded.push(`${url} ${String(init?.body)}`);
      return jsonResponse({ share_key: SHARE_KEY, row: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    await shell.recordSharedFrameVerdict(`notes-1.${SHARE_DOMAIN}`, "refused");
    await shell.recordSharedFrameVerdict("agent-abc.localhost", "refused");
    await shell.recordSharedFrameVerdict(`SHELL-1.${SHARE_DOMAIN}`, "admitted");

    expect(recorded).toEqual([
      `/ui/api/shared-with-me/${SHARE_KEY}/verdict?account=${shell.accountId} {"verdict":"admitted"}`,
    ]);
  });

  it("has the frame go through the redeem URL, while the share link stays what the frame is on", async () => {
    const { shell } = shellWithShare(AGENT);
    const redeem = "https://accounts.example/accounts/session?code=c0de&next=x";
    stubOpen(redeem);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.openSharedWorkspace(SHARE_KEY, null);

    expect(shell.stores.workspaces.workspaceFrameUrl(SHARE_KEY)).toBe(redeem);
    expect(shell.stores.workspaces.sharedFrameLink(SHARE_KEY)).toBe(SHELL_LINK);
    expect(shell.sharedFrameProgress).toMatchObject({ shareKey: SHARE_KEY, phase: "connecting", verdict: null });
  });

  it("re-arms a frame already on the share onto the fresh redeem through the store, without a reload", async () => {
    const { shell, reloadCount } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const redeem = "https://accounts.example/accounts/session?code=c0de&next=x";
    stubOpen(redeem);
    const changed = vi.fn();
    shell.stores.workspaces.onChanged(changed);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.openSharedWorkspace(SHARE_KEY, null);

    expect(changed).toHaveBeenCalled();
    expect(reloadCount()).toBe(0);
    expect(routeSet).not.toHaveBeenCalled();
    expect(shell.stores.workspaces.workspaceFrameUrl(SHARE_KEY)).toBe(redeem);
  });

  it("re-arms a frame on the share onto another target's link through the store, without a reload, when nothing is redeemed", async () => {
    const { shell, reloadCount } = shellWithShare(SHARE_KEY);
    shell.displayedWorkspaceAnyId = SHARE_KEY;
    const notesLink = `https://notes-1.${SHARE_DOMAIN}/`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (!url.includes("/open")) throw new Error(`unexpected fetch ${url}`);
        return jsonResponse(openPlan({ link: notesLink }));
      }),
    );
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.openSharedWorkspace(SHARE_KEY, "notes");

    expect(shell.stores.workspaces.workspaceFrameUrl(SHARE_KEY)).toBe(notesLink);
    expect(reloadCount()).toBe(0);
    expect(routeSet).not.toHaveBeenCalled();

    // The same target again, with nothing to redeem, has nothing for the store to move: the frame is reloaded.
    await shell.openSharedWorkspace(SHARE_KEY, "notes");
    expect(reloadCount()).toBe(1);
  });

  it("answers the desktop's refusal code and routes nowhere", async () => {
    const { shell } = shellWithShare(AGENT);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: "no address yet", code: "no_link" }, 409)));
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await expect(shell.openSharedWorkspace(SHARE_KEY, "notes")).rejects.toMatchObject({
      code: "no_link",
      message: "no address yet",
      status: 409,
    });

    expect(routeSet).not.toHaveBeenCalled();
  });

  it("routes onto the share's surface when the window shows something else", async () => {
    const { shell, reloadCount } = shellWithShare(AGENT);
    stubOpen(null);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    await shell.openSharedWorkspace(SHARE_KEY, null);

    expect(reloadCount()).toBe(0);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${SHARE_KEY}`);
  });
});

describe("opening an own app from the home page", () => {
  it("names the app's address under the workspace's own origin, and nothing without a label or a forward", () => {
    expect(workspaceAppUrl("https://localhost:8421", "agent-aa11", "reading-list-k3j9x2ab")).toBe(
      "https://reading-list-k3j9x2ab.agent-aa11.localhost:8421/",
    );
    expect(workspaceAppUrl("https://localhost:8421", "agent-aa11", "")).toBeNull();
    expect(workspaceAppUrl("", "agent-aa11", "reading-list-k3j9x2ab")).toBeNull();
  });

  it("enters the workspace as its card does, then hands it the app's address once its page listens", () => {
    const shell = makeShell();
    shell.mngrForwardOrigin = "https://localhost:8421";
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.spyOn(shell, "routeTo").mockImplementation((route) => m.route.set(route));
    const entry = { id: WORKSPACE_ID, name: "alpha", accent: "#aabbcc", supports_shutdown: true, liveness: "RUNNING" };
    const sent: WorkspaceMessage[] = [];
    let isReady = false;
    shell.registerWorkspaceMessageSender((_workspaceAgentId, message) => {
      if (!isReady) return false;
      sent.push(message);
      return true;
    });

    shell.openWorkspaceApp(entry, { name: "reading-list", display_name: "Reading List", label: "reading-list-k3j9x2ab" }, "RUNNING");

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`);
    expect(sent).toEqual([]);
    isReady = true;
    shell.flushPendingWorkspaceMessage();
    expect(sent).toEqual([{ kind: "open-link", url: `https://reading-list-k3j9x2ab.${WORKSPACE_ID}.localhost:8421/` }]);
  });

  it("goes nowhere and holds nothing for a workspace whose card is blocked", () => {
    const shell = makeShell();
    shell.mngrForwardOrigin = "https://localhost:8421";
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const routeTo = vi.spyOn(shell, "routeTo").mockImplementation(() => undefined);
    const sent: WorkspaceMessage[] = [];
    shell.registerWorkspaceMessageSender((_workspaceAgentId, message) => {
      sent.push(message);
      return true;
    });

    shell.openWorkspaceApp(
      { id: WORKSPACE_ID, name: "alpha", accent: "#aabbcc", supports_shutdown: true, liveness: "RUNNING", key_state: "locked" },
      { name: "reading-list", display_name: "Reading List", label: "reading-list-k3j9x2ab" },
      "RUNNING",
    );
    shell.flushPendingWorkspaceMessage();

    expect(routeSet).not.toHaveBeenCalled();
    expect(routeTo).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it("opens the workspace alone for an app with no address yet", () => {
    const shell = makeShell();
    shell.mngrForwardOrigin = "https://localhost:8421";
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.spyOn(shell, "routeTo").mockImplementation(() => undefined);
    const sent: WorkspaceMessage[] = [];
    shell.registerWorkspaceMessageSender((_workspaceAgentId, message) => {
      sent.push(message);
      return true;
    });

    shell.openWorkspaceApp(
      { id: WORKSPACE_ID, name: "alpha", accent: "#aabbcc", supports_shutdown: true, liveness: "RUNNING" },
      { name: "zen", display_name: "Zen", label: "" },
      "RUNNING",
    );

    expect(sent).toEqual([]);
  });
});
