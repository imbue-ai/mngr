import { describe, expect, it } from "vitest";
import type { UiWorkspaceUpdate } from "../channel/messages";
import { OUT_OF_DATE_UPDATE } from "../testing";
import {
  updateNavDotFor,
  updatePillFor,
  updateTabActionsFor,
  updateTabStatusFor,
} from "./updateSurfaces";
import { updateBadgeFor } from "./updates";

const BEHIND: UiWorkspaceUpdate = {
  ...OUT_OF_DATE_UPDATE,
  is_workspace_readable: true,
};
const SECURITY: UiWorkspaceUpdate = {
  ...BEHIND,
  availability: "SECURITY_UPDATE",
  current_version: "minds-v0.4.0",
};
const CURRENT: UiWorkspaceUpdate = {
  ...BEHIND,
  availability: "UP_TO_DATE",
  current_version: "minds-v0.4.1",
};
const CHAT = "agent-update-chat";

/** Each state the mockup draws, as the pushed update state reads in it. */
const STATES: Record<
  string,
  { update: UiWorkspaceUpdate; isUpdating?: boolean }
> = {
  "up to date": { update: CURRENT },
  "out of date": { update: BEHIND },
  scheduled: { update: { ...BEHIND, is_scheduled: true } },
  "scheduled, last window missed": {
    update: {
      ...BEHIND,
      is_scheduled: true,
      last_skip_reason:
        "The last update window closed before the update could start.",
    },
  },
  starting: { update: { ...BEHIND, activity: "STARTING" }, isUpdating: true },
  "a press with no frame yet": { update: BEHIND, isUpdating: true },
  preparing: {
    update: { ...BEHIND, activity: "RUNNING", chat_agent_id: CHAT },
    isUpdating: true,
  },
  applying: {
    update: { ...BEHIND, activity: "APPLYING", chat_agent_id: CHAT },
    isUpdating: true,
  },
  finishing: {
    update: { ...CURRENT, activity: "FINISHING", chat_agent_id: CHAT },
    isUpdating: true,
  },
  waiting: {
    update: { ...BEHIND, activity: "WAITING", chat_agent_id: CHAT },
    isUpdating: true,
  },
  "waiting for a go-ahead": {
    update: {
      ...BEHIND,
      activity: "WAITING",
      is_hold_recorded: true,
      hold_detail: "v0.4.1 restarts every running chat.",
      chat_agent_id: CHAT,
    },
    isUpdating: true,
  },
  "didn't start": {
    update: {
      ...BEHIND,
      dispatch_failure: "Couldn't start the update agent in this workspace.",
    },
  },
  "needs a decision": {
    update: {
      ...BEHIND,
      verdict: "NEEDS_DECISION",
      is_decision_pending: true,
      verdict_detail: "It can't keep your Recipes app.",
      chat_agent_id: CHAT,
    },
  },
  failed: { update: { ...BEHIND, verdict: "STUCK", chat_agent_id: CHAT } },
  stalled: { update: { ...BEHIND, activity: "STALLED", chat_agent_id: CHAT } },
  "updated, with a note": {
    update: {
      ...CURRENT,
      verdict: "UPDATED_WITH_REBUILD_ITEMS",
      verdict_detail: "Rebuild your digest app.",
      success_note_version: "minds-v0.4.1",
      chat_agent_id: CHAT,
    },
  },
  updated: {
    update: {
      ...CURRENT,
      verdict: "UPDATED",
      success_note_version: "minds-v0.4.1",
      chat_agent_id: CHAT,
    },
  },
  "too old": {
    update: {
      ...BEHIND,
      availability: "NEEDS_RECREATION",
      current_version: "minds-v0.3.4",
    },
  },
  "ahead of Studio": {
    update: {
      ...BEHIND,
      availability: "APP_BEHIND",
      current_version: "minds-v0.4.2",
    },
  },
  "version unknown": {
    update: { ...BEHIND, availability: "UNKNOWN", current_version: "" },
  },
  "needs sign-in": { update: { ...BEHIND, needs_sign_in: true } },
  skipped: { update: { ...BEHIND, is_update_skipped: true } },
  "security update": { update: SECURITY },
  "security update, scheduled": { update: { ...SECURITY, is_scheduled: true } },
};

type PillRow = [
  label: string | null,
  tone?: "attention" | "quiet",
  target?: "chat" | "tab",
];

describe("the titlebar pill, for every update state", () => {
  const expected: Record<string, PillRow> = {
    "up to date": [null],
    "out of date": ["Update available", "quiet", "tab"],
    scheduled: [null],
    "scheduled, last window missed": [null],
    starting: ["Updating…", "quiet", "tab"],
    "a press with no frame yet": ["Updating…", "quiet", "tab"],
    preparing: ["Updating…", "quiet", "chat"],
    applying: [null],
    finishing: ["Finishing update…", "quiet", "chat"],
    waiting: ["Update needs a decision", "attention", "chat"],
    "waiting for a go-ahead": ["Update needs a decision", "attention", "chat"],
    "didn't start": ["Update didn't start", "attention", "tab"],
    "needs a decision": ["Update needs a decision", "attention", "chat"],
    failed: ["Update didn't finish", "attention", "chat"],
    stalled: ["Update didn't finish", "attention", "chat"],
    "updated, with a note": ["Updated, with a note", "quiet", "chat"],
    updated: [null],
    "too old": ["Workspace too old to update", "attention", "tab"],
    "ahead of Studio": [null],
    "version unknown": [null],
    "needs sign-in": ["Update needs sign-in", "attention", "tab"],
    skipped: [null],
    "security update": ["Security update available", "attention", "tab"],
    "security update, scheduled": [null],
  };

  it.each(Object.keys(STATES))("%s", (state) => {
    const { update, isUpdating = false } = STATES[state];
    const pill = updatePillFor(
      update,
      isUpdating,
      false,
      (update.chat_agent_id ?? "") !== "",
    );
    const [label, tone, target] = expected[state];
    if (label === null) {
      expect(pill).toBeNull();
      expect(updateNavDotFor(pill)).toBeNull();
      return;
    }
    expect(pill).toMatchObject({ label, tone, target });
    // The Updates tab's nav entry wears the pill's colour.
    expect(updateNavDotFor(pill)).toBe(
      pill?.tone === "attention" ? "warning" : pill?.dot,
    );
  });

  it("breathes only while a run is in flight, and is green only for a landed update's note", () => {
    const pulsing = Object.keys(STATES).filter(
      (state) =>
        updatePillFor(
          STATES[state].update,
          STATES[state].isUpdating ?? false,
          false,
          true,
        )?.isDotPulsing,
    );
    expect(pulsing).toEqual([
      "starting",
      "a press with no frame yet",
      "preparing",
      "finishing",
    ]);
    expect(
      updatePillFor(STATES["updated, with a note"].update, false, false, true)
        ?.dot,
    ).toBe("success");
  });

  it("opens the Updates tab instead of a chat it does not know yet", () => {
    for (const state of [
      "preparing",
      "finishing",
      "waiting",
      "needs a decision",
      "failed",
      "updated, with a note",
    ]) {
      expect(
        updatePillFor(
          STATES[state].update,
          STATES[state].isUpdating ?? false,
          false,
          false,
        )?.target,
      ).toBe("tab");
    }
  });

  it("says nothing of an offer auto-update will take care of, or of a scheduled security fix", () => {
    expect(updatePillFor(BEHIND, false, true, false)).toBeNull();
    expect(updatePillFor(SECURITY, false, true, false)).toBeNull();
    expect(
      updatePillFor(
        { ...SECURITY, is_scheduled: true, is_schedule_auto_armed: true },
        false,
        true,
        false,
      ),
    ).toBeNull();
    // One auto-update cannot arm still says it is behind.
    expect(
      updatePillFor(
        { ...BEHIND, is_workspace_readable: false },
        false,
        true,
        false,
      )?.label,
    ).toBe("Update available");
  });

  it("keeps the band's sentence as its hover text, and the hold's own line for a go-ahead", () => {
    expect(updatePillFor(BEHIND, false, false, false)?.tooltip).toBe(
      "This workspace is running an older version of Imbue Studio.",
    );
    expect(updatePillFor(SECURITY, false, false, false)?.tooltip).toBe(
      "An update for this workspace is available, which includes a critical security fix.",
    );
    expect(
      updatePillFor(STATES["waiting for a go-ahead"].update, true, false, true)
        ?.tooltip,
    ).toBe(
      "v0.4.1 restarts every running chat. This workspace's update is waiting for your go-ahead in its chat.",
    );
    expect(
      updatePillFor(STATES["too old"].update, false, false, false)?.tooltip,
    ).toBe("This workspace is too old to update in place.");
  });

  it("ranks a run in flight over how the last one ended, and that over a standing condition", () => {
    const failedAndBehind = {
      ...BEHIND,
      verdict: "STUCK" as const,
      needs_sign_in: true,
    };
    expect(updatePillFor(failedAndBehind, false, false, false)?.label).toBe(
      "Update didn't finish",
    );
    expect(
      updatePillFor(
        { ...failedAndBehind, activity: "RUNNING" },
        true,
        false,
        true,
      )?.label,
    ).toBe("Updating…");
  });
});

describe("the Updates tab, for every update state", () => {
  const expected: Record<
    string,
    { title: string; actions: string[]; costs?: boolean }
  > = {
    "up to date": { title: "Up to date", actions: [] },
    "out of date": {
      title: "Update available",
      actions: ["Schedule update", "Update now"],
      costs: true,
    },
    scheduled: {
      title: "Update scheduled",
      actions: ["Update now instead", "Cancel schedule"],
      costs: true,
    },
    "scheduled, last window missed": {
      title: "Update scheduled",
      actions: ["Update now instead", "Cancel schedule"],
      costs: true,
    },
    starting: { title: "Starting the update", actions: [] },
    "a press with no frame yet": { title: "Starting the update", actions: [] },
    preparing: { title: "Preparing the update", actions: ["Open chat"] },
    applying: { title: "Updating", actions: ["Open chat"] },
    finishing: { title: "Finishing the update", actions: ["Open chat"] },
    waiting: { title: "Waiting on you", actions: ["Open chat"] },
    "waiting for a go-ahead": {
      title: "Waiting for your go-ahead",
      actions: ["Open chat"],
    },
    "didn't start": {
      title: "Update didn't start",
      actions: ["Try again"],
    },
    "needs a decision": {
      title: "Needs your decision",
      actions: ["Open chat", "Dismiss"],
    },
    failed: { title: "Update didn't finish", actions: ["Open chat"] },
    stalled: { title: "Update didn't finish", actions: ["Open chat"] },
    "updated, with a note": { title: "Updated to v0.4.1", actions: ["Open chat"] },
    updated: { title: "Updated to v0.4.1", actions: ["Open chat"] },
    "too old": {
      title: "Too old to update in place",
      actions: ["Create a new workspace"],
    },
    "ahead of Studio": { title: "Ahead of Imbue Studio", actions: [] },
    "version unknown": {
      title: "Version unknown",
      actions: ["Schedule update", "Update now"],
      costs: true,
    },
    "needs sign-in": {
      title: "Sign in to update",
      actions: ["Sign in"],
      costs: true,
    },
    skipped: {
      title: "Skipped v0.4.1",
      actions: ["Schedule update", "Update now"],
      costs: true,
    },
    "security update": {
      title: "Update available",
      actions: ["Update now", "Schedule update"],
      costs: true,
    },
    "security update, scheduled": {
      title: "Update scheduled",
      actions: ["Update now instead", "Cancel schedule"],
      costs: true,
    },
  };

  it.each(Object.keys(STATES))("%s", (state) => {
    const { update, isUpdating = false } = STATES[state];
    const status = updateTabStatusFor(
      update,
      isUpdating,
      false,
      false,
      "2:00 AM-5:00 AM",
    );
    const actions = updateTabActionsFor(
      update,
      status,
      false,
      update.chat_agent_id ?? "",
    );
    expect(status.title).toBe(expected[state].title);
    expect(status.line).not.toBe("");
    expect(actions.map((action) => action.label)).toEqual(
      expected[state].actions,
    );
    expect(status.isCostShown).toBe(expected[state].costs ?? false);
  });

  it("quotes the update agent's own words where it left any", () => {
    const quoteOf = (state: string): string =>
      updateTabStatusFor(STATES[state].update, false, false, false, "").quote;
    expect(quoteOf("needs a decision")).toBe("It can't keep your Recipes app.");
    expect(quoteOf("updated, with a note")).toBe("Rebuild your digest app.");
    expect(quoteOf("out of date")).toBe("");
  });

  it("cards the security fix only while that release is still on offer", () => {
    const isShown = (update: UiWorkspaceUpdate, isUpdating = false): boolean =>
      updateTabStatusFor(update, isUpdating, false, false, "").isSecurityShown;
    expect(isShown(SECURITY)).toBe(true);
    expect(isShown({ ...SECURITY, is_scheduled: true })).toBe(true);
    expect(isShown({ ...SECURITY, needs_sign_in: true })).toBe(true);
    expect(isShown({ ...SECURITY, activity: "RUNNING" }, true)).toBe(false);
    expect(isShown(BEHIND)).toBe(false);
  });

  it("offers a skip, not a cancel, for a schedule auto-update armed, and says nothing is needed", () => {
    const armed = {
      ...BEHIND,
      is_scheduled: true,
      is_schedule_auto_armed: true,
    };
    const status = updateTabStatusFor(
      armed,
      false,
      false,
      true,
      "1:00 AM-4:00 AM",
    );
    expect(status.line).toBe(
      "Studio will update this workspace in the next update window (1:00 AM-4:00 AM). No action is needed.",
    );
    expect(
      updateTabActionsFor(armed, status, true, "").map(
        (action) => action.label,
      ),
    ).toEqual(["Update now instead", "Skip v0.4.1"]);
    // The same schedule under a manual setting is the user's to cancel.
    const manual = updateTabStatusFor(
      armed,
      false,
      false,
      false,
      "1:00 AM-4:00 AM",
    );
    expect(
      updateTabActionsFor(armed, manual, false, "").map(
        (action) => action.label,
      ),
    ).toEqual(["Update now instead", "Cancel schedule"]);
  });

  it("says a stopped workspace's update waits for it to start, with nothing to do and no versions", () => {
    const status = updateTabStatusFor(BEHIND, false, true, false, "");
    expect(status).toMatchObject({
      title: "Workspace stopped",
      line: "Start this workspace to see its updates.",
      versions: null,
    });
    expect(updateTabActionsFor(BEHIND, status, false, "")).toEqual([]);
  });
});

describe("the Updates tab, in the states the mockup does not draw", () => {
  const lineOf = (update: UiWorkspaceUpdate, isUpdating = false): string =>
    updateTabStatusFor(update, isUpdating, false, false, "2:00 AM-5:00 AM")
      .line;

  it("offers no Open chat once the update's chat is gone", () => {
    const failed = STATES.failed.update;
    const status = updateTabStatusFor(failed, false, false, false, "");
    expect(updateTabActionsFor(failed, status, false, "")).toEqual([]);
  });

  it("says a run that found nothing to do changed nothing, with nothing to do about it", () => {
    const already = {
      ...CURRENT,
      verdict: "ALREADY_CURRENT" as const,
      chat_agent_id: CHAT,
    };
    const status = updateTabStatusFor(already, false, false, false, "");
    expect(status.title).toBe("Already up to date");
    expect(status.line).toBe(
      "This workspace is already up to date on v0.4.1. Nothing was changed.",
    );
    expect(updateTabActionsFor(already, status, false, CHAT)).toEqual([]);
  });

  it("names the version a refused update can still apply in place", () => {
    expect(
      lineOf({
        ...BEHIND,
        verdict: "REFUSED",
        in_place_compatible_ref: "minds-v0.4.0",
        chat_agent_id: CHAT,
      }),
    ).toBe(
      "The update wasn't applied. v0.4.0 can still be applied to this workspace. Check in with the update agent in the update chat.",
    );
    expect(lineOf({ ...BEHIND, verdict: "REFUSED", chat_agent_id: CHAT })).toBe(
      "The update wasn't applied. Check in with the update agent in the update chat.",
    );
  });

  it("says an agent that found the update can't apply in place, with the check-in line", () => {
    expect(lineOf({ ...BEHIND, verdict: "NEEDS_RECREATION", chat_agent_id: CHAT })).toBe(
      "The update agent found that this update can't be applied to this workspace in place. Check in with the update agent in the update chat.",
    );
  });

  it("points at the run's chat only while the app knows it", () => {
    const chatless = (fields: Partial<UiWorkspaceUpdate>, isUpdating = false): string =>
      lineOf({ ...BEHIND, ...fields }, isUpdating);
    expect(chatless({ verdict: "STUCK" })).toBe("The update ran into a problem it couldn't recover from.");
    expect(chatless({ verdict: "REFUSED" })).toBe("The update wasn't applied.");
    expect(chatless({ verdict: "NEEDS_DECISION", is_decision_pending: true })).toBe(
      "The update stopped short of applying: it can't keep something you built in this workspace.",
    );
    expect(chatless({ availability: "UP_TO_DATE", verdict: "UPDATED_WITH_REBUILD_ITEMS" })).toBe(
      "The update agent left a note for you, but its chat can't be found.",
    );
    expect(chatless({ activity: "STALLED", chat_agent_name: "update-self-1330f8" })).toBe(
      "The update agent is no longer running and never reported a result.",
    );
    expect(chatless({ activity: "RUNNING" }, true)).toBe("Preparing the update.");
    expect(chatless({ activity: "WAITING" }, true)).toBe("The update agent is waiting for you.");
    // A chat it knows is still offered.
    expect(chatless({ verdict: "STUCK", chat_agent_id: CHAT })).toBe(
      "The update ran into a problem it couldn't recover from. Check in with the update agent in the update chat.",
    );
    // The pill's hover text likewise.
    expect(updatePillFor({ ...BEHIND, is_decision_pending: true }, false, false, false)?.tooltip).toBe(
      "This workspace's update needs your answer.",
    );
    // And the home badges'.
    const waiting = { ...BEHIND, activity: "WAITING" as const };
    expect(updateBadgeFor(waiting, true)?.tooltip).toBe(
      "The update agent has stopped and is waiting for you — open this workspace's updates to continue",
    );
    expect(updateBadgeFor({ ...waiting, chat_agent_id: CHAT }, true)?.tooltip).toBe(
      "The update agent has stopped and is waiting in its chat — open this workspace to continue",
    );
    const undecided = { ...BEHIND, verdict: "NEEDS_DECISION" as const, is_decision_pending: true };
    expect(updateBadgeFor(undecided, false)?.tooltip).toBe(
      "The update can't keep something you built in this workspace",
    );
    expect(updateBadgeFor({ ...undecided, chat_agent_id: CHAT }, false)?.tooltip).toBe(
      "The update can't keep something you built in this workspace — decide in its chat",
    );
  });

  it("says why a window was missed, under the schedule's line", () => {
    const missed = {
      ...BEHIND,
      is_scheduled: true,
      last_skip_reason: "An update was already running in this workspace.",
    };
    expect(
      updateTabStatusFor(missed, false, false, false, "2:00 AM-5:00 AM")
        .secondaryLine,
    ).toBe(
      "An update was already running in this workspace. Studio will try again in the next window.",
    );
  });

  it("names the version a schedule or a run is aimed at", () => {
    expect(
      lineOf({
        ...BEHIND,
        is_scheduled: true,
        override_ref: "upstream/some-branch",
      }),
    ).toBe(
      "Scheduled to update to upstream/some-branch in the next update window (2:00 AM-5:00 AM).",
    );
    expect(
      lineOf(
        { ...BEHIND, activity: "RUNNING", target_override: "minds-v0.4.2", chat_agent_id: CHAT },
        true,
      ),
    ).toBe(
      "Preparing the update to minds-v0.4.2. The update chat has the live progress.",
    );
    // A schedule for another version can still be moved to the app's own.
    const aimed = {
      ...BEHIND,
      is_scheduled: true,
      override_ref: "upstream/some-branch",
    };
    const status = updateTabStatusFor(aimed, false, false, false, "");
    expect(updateTabActionsFor(aimed, status, false, "")).toContainEqual(
      expect.objectContaining({
        label: "Schedule update",
        targetRef: "minds-v0.4.1",
      }),
    );
  });

  it("says a run's chat was deleted before it reported, rather than to check in with it", () => {
    expect(lineOf({ ...BEHIND, activity: "STALLED", is_chat_gone: true })).toBe(
      "The update's chat was deleted before the update recorded a result.",
    );
  });

  it("drops Try again for a workspace with nothing to run", () => {
    const refused = {
      ...CURRENT,
      dispatch_failure: "Couldn't start this workspace to run the update.",
    };
    const status = updateTabStatusFor(refused, false, false, false, "");
    expect(updateTabActionsFor(refused, status, false, "")).toEqual([]);
  });
});
