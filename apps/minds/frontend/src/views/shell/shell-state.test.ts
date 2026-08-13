import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { workspacesMessage } from "../../testing";
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
    documentElement: { style: { setProperty: () => undefined, removeProperty: () => undefined } },
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
const OPTIONS_ROUTE = `/workspace/${WORKSPACE_ID}/options?tab=permissions&section=local-files`;

afterEach(() => {
  // restoreAllMocks does NOT undo vi.stubGlobal; only unstubAllGlobals does.
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function makeShell(): ShellState {
  // Accent painting writes to the document; these cases are about routing, so
  // the writes go to a stub rather than pulling a DOM into the suite.
  vi.stubGlobal("document", {
    documentElement: { style: { setProperty: () => undefined, removeProperty: () => undefined } },
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
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });

    expect(routeSet).toHaveBeenCalledWith("/inbox", { selected: "evt-a", workspace: WORKSPACE_ID }, undefined);
  });

  it("carries no workspace when it was opened from Home", () => {
    const shell = makeShell();
    land(shell, "/");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox();

    expect(routeSet).toHaveBeenCalledWith("/inbox", {}, undefined);
  });

  it("remembers the options panel it was opened over, and forgets it on the way out", () => {
    const shell = makeShell();
    land(shell, OPTIONS_ROUTE);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox({ selected: "evt-a" });
    expect(shell.panelRouteBehindOverlay).toBe(OPTIONS_ROUTE);

    // Still remembered while the popup is up, so the panel keeps rendering...
    land(shell, `/inbox?workspace=${WORKSPACE_ID}&selected=evt-a`);
    expect(shell.panelRouteBehindOverlay).toBe(OPTIONS_ROUTE);

    // ...and dropped once the route is no longer a modal's.
    land(shell, OPTIONS_ROUTE);
    expect(shell.panelRouteBehindOverlay).toBeNull();
  });

  it("keeps the panel underneath when a second request opens over the popup", () => {
    const shell = makeShell();
    land(shell, OPTIONS_ROUTE);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}&selected=evt-a`);

    shell.openInbox({ selected: "evt-b" });

    expect(shell.panelRouteBehindOverlay).toBe(OPTIONS_ROUTE);
    // ...and it swings the open popup over rather than stacking a second
    // history entry, so one dismissal still lands back on the panel.
    expect(routeSet).toHaveBeenLastCalledWith(
      "/inbox",
      { selected: "evt-b", workspace: WORKSPACE_ID },
      { replace: true },
    );
  });

  it("names no panel when the popup was opened over a bare workspace", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    shell.openInbox();

    expect(shell.panelRouteBehindOverlay).toBeNull();
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
  function relayOver(displayed: string | null): { shell: ShellState; sent: [string, string][] } {
    const shell = makeShell();
    const sent: [string, string][] = [];
    shell.registerPermissionResolvedSender((requestId, verdict) => sent.push([requestId, verdict]));
    shell.displayedWorkspaceAnyId = displayed;
    return { shell, sent };
  }

  it("tells the workspace the resolved request belongs to", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.notifyRequestResolved({ requestId: "evt-a", agentId: WORKSPACE_ID, verdict: "granted" });

    expect(sent).toEqual([["evt-a", "granted"]]);
  });

  it("says nothing to a workspace that did not ask", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.notifyRequestResolved({ requestId: "evt-a", agentId: "agent-other", verdict: "denied" });

    expect(sent).toEqual([]);
  });

  it("says nothing with no workspace on screen, or no workspace on the card", () => {
    const offScreen = relayOver(null);
    offScreen.shell.notifyRequestResolved({ requestId: "evt-a", agentId: WORKSPACE_ID, verdict: "denied" });
    expect(offScreen.sent).toEqual([]);

    const unresolved = relayOver(WORKSPACE_ID);
    unresolved.shell.notifyRequestResolved({ requestId: "evt-a", agentId: null, verdict: "denied" });
    expect(unresolved.sent).toEqual([]);
  });

  it("keeps the live sender when a torn-down frame unregisters its own", () => {
    const { shell, sent } = relayOver(WORKSPACE_ID);

    shell.unregisterPermissionResolvedSender(() => undefined);
    shell.notifyRequestResolved({ requestId: "evt-a", agentId: WORKSPACE_ID, verdict: "denied" });

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
    for (const health of ["stuck", "restarting", "healthy"] as const) {
      shell.handleHealthChanged(AGENT, health, false);
      expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
    }
  });

  it("raises itself on the edge into restart_failed for the displayed machine", () => {
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "restart_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
    expect(shell.isRecoveryModalAutoRaised(AGENT)).toBe(true);
  });

  it("does not raise itself for a failure that predates the window", () => {
    // The connect snapshot replays the state the machine is already in. That
    // is not a transition, so the band reports it and "Open recovery" is one
    // click away -- a card taking over a window the user just opened is not.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "restart_failed", true);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("drops the card it raised once the machine answers again", () => {
    // Nothing raised it but the failure, and the failure is over -- a machine
    // that came back on its own would otherwise leave a window reading
    // "unresponsive" over a working machine.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "restart_failed", false);
    shell.handleHealthChanged(AGENT, "healthy", false);

    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("gives up a card it raised when a fresh snapshot starts", () => {
    // A machine that recovered while the socket was down replays no frame at
    // all, so nothing else would ever drop the card.
    displaying(shell, AGENT);
    shell.handleHealthChanged(AGENT, "restart_failed", false);
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
    shell.handleHealthChanged(AGENT, "restart_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("does not raise itself while the discovery consumer is dead", () => {
    // Every machine reads unhealthy then, and the card's own actions route
    // through the forward the dead consumer feeds -- so it would offer
    // "Restart Machine" over a band correctly saying only the app restart helps.
    displaying(shell, AGENT);
    shell.stores.health.applyDiscoveryHealthMessage({ type: "discovery_health", state: "blocked" });
    shell.handleHealthChanged(AGENT, "restart_failed", false);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(false);
  });

  it("still opens on request while the consumer is dead", () => {
    shell.stores.health.applyDiscoveryHealthMessage({ type: "discovery_health", state: "blocked" });
    shell.openRecoveryModal(AGENT);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
  });

  it("does not re-raise itself over a card the user is already looking at", () => {
    // A second restart_failed frame -- a fresh failure reason on the same
    // machine -- must not convert a deliberately opened card into one that
    // will dismiss itself under the user.
    displaying(shell, AGENT);
    shell.openRecoveryModal(AGENT);
    shell.handleHealthChanged(AGENT, "restart_failed", false);
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

    shell.handleHealthChanged(AGENT, "restart_failed", false);
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
});

describe("ShellState.handleEscape over the request popup", () => {
  it("closes the popup first and leaves the options panel standing", () => {
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 4, back: vi.fn() } });
    land(shell, OPTIONS_ROUTE);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openInbox({ selected: "evt-a" });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}&selected=evt-a`);

    // The popup goes back to the panel it was opened over, and nothing else.
    expect(shell.handleEscape()).toBe(true);
    expect(window.history.back).toHaveBeenCalledTimes(1);

    // Only the NEXT Escape, once that navigation has landed, takes the panel down.
    land(shell, OPTIONS_ROUTE);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    expect(shell.handleEscape()).toBe(true);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`);
  });

  it("dismisses the popup once, however many times a single Escape reaches it", () => {
    // Under Electron one keypress arrives twice (in-document listener + the
    // main-process forward), and history.back() is not idempotent.
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 4, back: vi.fn() } });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);

    expect(shell.handleEscape()).toBe(true);
    expect(shell.handleEscape()).toBe(true);

    expect(window.history.back).toHaveBeenCalledTimes(1);
  });

  it("falls back to the workspace behind the popup on a cold-start deep link", () => {
    const shell = makeShell();
    vi.stubGlobal("window", { history: { length: 1, back: vi.fn() } });
    land(shell, `/inbox?workspace=${WORKSPACE_ID}`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    expect(shell.handleEscape()).toBe(true);

    expect(window.history.back).not.toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`);
  });

  it("takes the switcher popover before the panel, and reports nothing left", () => {
    const shell = makeShell();
    land(shell, `/workspace/${WORKSPACE_ID}`);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    shell.openSidebar({ x: 0, y: 0, width: 10, height: 10 });

    expect(shell.handleEscape()).toBe(true);
    expect(shell.isSidebarOpen).toBe(false);
    // Bare workspace surface: no panel to close either.
    expect(shell.handleEscape()).toBe(false);
  });

  it("reports nothing dismissed on a plain hub page", () => {
    const shell = makeShell();
    land(shell, "/workspaces/destroyed");
    expect(shell.handleEscape()).toBe(false);
  });
});
