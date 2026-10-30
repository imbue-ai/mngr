import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import { OUT_OF_DATE_UPDATE, updatesShell } from "../../testing";
import { titlebarStatusFor } from "./workspace-notices";

const BEHIND = "agent-" + "a".repeat(8);
const PREPARING = "agent-" + "b".repeat(8);
const FAILED = "agent-" + "c".repeat(8);
const NOTED = "agent-" + "d".repeat(8);
const UNCHATTED_NOTE = "agent-" + "e".repeat(8);
const DECIDING = "agent-" + "f".repeat(8);
const CHAT = "agent-update-chat";

afterEach(() => {
  vi.restoreAllMocks();
  clearAppContextForTests();
});

const NOTE = {
  ...OUT_OF_DATE_UPDATE,
  availability: "UP_TO_DATE",
  verdict: "UPDATED_WITH_REBUILD_ITEMS",
} as const;

function shellWithUpdates() {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  return updatesShell(
    {
      [BEHIND]: OUT_OF_DATE_UPDATE,
      [PREPARING]: { ...OUT_OF_DATE_UPDATE, activity: "RUNNING", chat_agent_id: CHAT },
      [FAILED]: { ...OUT_OF_DATE_UPDATE, verdict: "STUCK", chat_agent_id: CHAT },
      [NOTED]: { ...NOTE, chat_agent_id: CHAT },
      [UNCHATTED_NOTE]: NOTE,
      [DECIDING]: { ...OUT_OF_DATE_UPDATE, verdict: "NEEDS_DECISION", is_decision_pending: true, chat_agent_id: CHAT },
    },
    false,
  );
}

describe("the titlebar status for the workspace on screen", () => {
  it("opens the Updates tab for an update on offer, and stands down while the band speaks", () => {
    const shell = shellWithUpdates();
    const openUpdatesTab = vi.spyOn(shell, "openUpdatesTab").mockImplementation(() => undefined);

    const status = titlebarStatusFor(shell, BEHIND);
    expect(status).toMatchObject({ key: "update-out-of-date", label: "Update available" });
    status?.onClick();
    expect(openUpdatesTab).toHaveBeenCalledWith(BEHIND);

    vi.spyOn(shell.stores.health, "statusFor").mockReturnValue("stuck");
    expect(titlebarStatusFor(shell, BEHIND)).toBeNull();
  });

  it("opens a run's chat once the chat is known", () => {
    const shell = shellWithUpdates();
    const openWorkspaceChat = vi.spyOn(shell, "openWorkspaceChat").mockImplementation(() => undefined);

    titlebarStatusFor(shell, PREPARING)?.onClick();

    expect(openWorkspaceChat).toHaveBeenCalledWith(PREPARING, CHAT);
  });

  it("dismisses how a run ended once its chat is opened from the status, and only then", () => {
    const shell = shellWithUpdates();
    const openWorkspaceChat = vi.spyOn(shell, "openWorkspaceChat").mockImplementation(() => undefined);
    const openUpdatesTab = vi.spyOn(shell, "openUpdatesTab").mockImplementation(() => undefined);
    const dismissOutcome = vi
      .spyOn(shell.stores.updates, "dismissOutcome")
      .mockResolvedValue({ isOk: true, error: "", detail: "" });

    titlebarStatusFor(shell, NOTED)?.onClick();
    expect(openWorkspaceChat).toHaveBeenCalledWith(NOTED, CHAT);
    expect(dismissOutcome).toHaveBeenCalledWith(NOTED);
    expect(titlebarStatusFor(shell, FAILED)?.label).toBe("Update didn't finish");
    titlebarStatusFor(shell, FAILED)?.onClick();
    expect(openWorkspaceChat).toHaveBeenCalledWith(FAILED, CHAT);
    expect(dismissOutcome).toHaveBeenCalledWith(FAILED);

    // With no chat to read it in, the status opens the Updates tab, which settles it once left.
    titlebarStatusFor(shell, UNCHATTED_NOTE)?.onClick();
    expect(openUpdatesTab).toHaveBeenCalledWith(UNCHATTED_NOTE);
    // A run's chat opened mid-run settles nothing, and neither does a decision's: dismissing it skips the release.
    titlebarStatusFor(shell, PREPARING)?.onClick();
    expect(titlebarStatusFor(shell, DECIDING)?.label).toBe("Update needs a decision");
    titlebarStatusFor(shell, DECIDING)?.onClick();
    expect(openWorkspaceChat).toHaveBeenCalledWith(DECIDING, CHAT);
    expect(dismissOutcome).toHaveBeenCalledTimes(2);
  });
});
