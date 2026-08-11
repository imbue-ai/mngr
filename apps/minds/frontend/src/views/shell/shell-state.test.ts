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
// /inbox, /settings/ai-keys, /create/inspiration): the frame stays mounted and
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

  it("opens for the machine the user asked about", () => {
    shell.openRecoveryModal(AGENT);
    expect(shell.isRecoveryModalOpenFor(AGENT)).toBe(true);
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

  it("asks the frame to re-fetch when the card goes away", () => {
    // Dropping the card without reloading would uncover the dead page the
    // frame still holds, and report a recovery the window does not show.
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
});
