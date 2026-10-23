import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, getAppContext } from "../../app-context";
import type { UiWorkspaceUpdate } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  OUT_OF_DATE_UPDATE,
  publishWorkspaceLiveness,
  recordingFetch,
  scheduleWindowAtLocalHour,
  settle,
  updatesShell,
} from "../../testing";
import { UpdateModal } from "./UpdateModal";
import type { UpdateModalAttrs } from "./UpdateModal";

const AGENT = "agent-" + "c".repeat(8);

interface Harness {
  draw: () => m.Children;
  requests: string[];
  routeSet: ReturnType<typeof vi.fn>;
  onClose: ReturnType<typeof vi.fn>;
}

function pressableWithLabel(
  root: m.Children,
  label: string,
): AnyVnode | undefined {
  return collectVnodes(root).find(
    (vnode) =>
      typeof attrsOf(vnode).onclick === "function" &&
      allText(vnode.children).includes(label),
  );
}

/** The labels of every button the modal draws, in order. */
function buttonLabels(root: m.Children): string[] {
  return collectVnodes(root)
    .filter((vnode) => typeof attrsOf(vnode).onclick === "function" && vnode.tag !== "div")
    .map((vnode) => allText(vnode.children).trim())
    .filter(Boolean);
}

function press(root: m.Children, label: string): void {
  const pressable = pressableWithLabel(root, label);
  if (pressable === undefined) throw new Error(`no "${label}" to press`);
  (attrsOf(pressable).onclick as () => void)();
}

function harness(
  respond: (url: string) => Promise<Response>,
  update: UiWorkspaceUpdate = OUT_OF_DATE_UPDATE,
  isAutoUpdateEnabled = false,
  openedWith: Pick<UpdateModalAttrs, "isSkipConfirmOpen" | "openingFailure"> = {},
): Harness {
  updatesShell({ [AGENT]: update }, isAutoUpdateEnabled);
  const { fetchImpl, requests } = recordingFetch(respond);
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const onClose = vi.fn();
  const instance = UpdateModal() as unknown as m.Component;
  const attrs = { agentId: AGENT, workspaceName: "box", ...openedWith, onClose };
  (instance.oninit as unknown as (v: m.Vnode) => void).call(instance, m(instance, attrs as unknown as m.Attributes) as m.Vnode);
  function draw(): m.Children {
    const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
    return (instance.view as unknown as (v: m.Vnode) => m.Children).call(
      instance,
      vnode,
    );
  }
  return { draw, requests, routeSet, onClose };
}

/** A dispatch answers over several promise hops: the request, its body parse,
 * the store's lock release, then the modal's own callback. */
async function settleDispatch(): Promise<void> {
  for (let index = 0; index < 5; index += 1) await settle();
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

describe("the update modal's Update now", () => {
  it("enters the machine before the dispatch answers, and closes only once it has", async () => {
    // The machine's interface opens the run's chat tab only for clients
    // connected when the agent appears, so the frame must be up before dispatch.
    let answer: ((response: Response) => void) | null = null;
    const { draw, requests, routeSet, onClose } = harness(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    press(draw(), "Update now");

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, {});
    expect(requests).toEqual([`/ui/api/updates/${AGENT}/now`]);
    expect(onClose).not.toHaveBeenCalled();

    (answer as unknown as (response: Response) => void)(jsonResponse({}));
    await settleDispatch();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("stays up over the machine to show a refusal", async () => {
    const { draw, onClose } = harness(() =>
      Promise.resolve(
        jsonResponse(
          { error: "An update is already running in this workspace." },
          409,
        ),
      ),
    );
    press(draw(), "Update now");
    await settleDispatch();

    expect(onClose).not.toHaveBeenCalled();
    expect(allText(draw())).toContain(
      "An update is already running in this workspace.",
    );
  });

  it("quotes the machine's own verdict under the refusal", async () => {
    // The reason a wedged machine gives is the only thing that tells the
    // reader retrying will not help, so it has to survive all the way here.
    const { draw } = harness(() =>
      Promise.resolve(
        jsonResponse(
          {
            error: "Couldn't start the update agent in this workspace.",
            detail:
              "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']",
          },
          502,
        ),
      ),
    );
    press(draw(), "Update now");
    await settleDispatch();

    expect(allText(draw())).toContain(
      "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']",
    );
  });

  it("shows an update that never went out to a modal opened after the press", async () => {
    // The press's own answer died with the modal that was waiting on it, so
    // the machine's row is the only record of it the reader can reach.
    const { draw, requests, onClose } = harness(
      () => Promise.resolve(jsonResponse({ ok: true })),
      {
        ...OUT_OF_DATE_UPDATE,
        dispatch_failure: "Couldn't reach this workspace to start the update.",
        dispatch_failure_detail: "Error: Could not reach host host-1234: connection refused",
      },
    );

    const text = allText(draw());
    expect(text).toContain("Couldn't reach this workspace to start the update.");
    expect(text).toContain("Error: Could not reach host host-1234: connection refused");

    press(draw(), "Dismiss");
    await settleDispatch();
    expect(requests).toEqual([`/ui/api/updates/${AGENT}/dismiss`]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("holds an update that never went out back while a new press is out", () => {
    // The press is the newer question; the old answer beside its spinner would read as its reply.
    const { draw } = harness(() => new Promise<Response>(() => undefined), {
      ...OUT_OF_DATE_UPDATE,
      dispatch_failure: "Couldn't reach this workspace to start the update.",
    });
    expect(allText(draw())).toContain("Couldn't reach this workspace to start the update.");

    press(draw(), "Try again");

    expect(allText(draw())).not.toContain("Couldn't reach this workspace to start the update.");
  });

  it("offers scheduling as the primary action", () => {
    const schedule = pressableWithLabel(
      harness(() => Promise.resolve(jsonResponse({}))).draw(),
      "Schedule update",
    );
    expect(schedule).toBeDefined();
    expect(attrsOf(schedule as AnyVnode).variant).toBe("primary");
  });
});

describe("the update modal's verdict line", () => {
  it("names the version the run landed, not the one detection has caught up to", () => {
    // The seconds after an apply: the verdict is published at once, while the
    // detection sweep is still execing into a machine whose services are
    // coming back, so `current_version` still reads the version it moved off.
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      verdict: "UPDATED",
      current_version: "minds-v0.3.9",
      success_note_version: "minds-v0.4.1",
    });

    const root = draw();
    expect(allText(root)).toContain(
      "This workspace was updated to v0.4.1.",
    );
    // Not offered again on the strength of a read that has not caught up.
    expect(buttonLabels(root)).toEqual(["Dismiss"]);
  });

  it("offers a newer release over an earlier run's success", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      verdict: "UPDATED",
      success_note_version: "minds-v0.4.0",
    });
    const root = draw();

    expect(allText(root)).not.toContain("This workspace was updated");
    expect(buttonLabels(root)).toEqual(["Update to a different version…", "Schedule update", "Update now"]);
  });

  it("reports a run to a branch with its chat, which the row's badge leaves no other way to", () => {
    const { draw, routeSet, onClose } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      availability: "UNKNOWN",
      unknown_reason: "NO_MACHINE_VERSION",
      current_version: "upstream/fake-catchup-848b",
      verdict: "UPDATED",
      success_note_version: "upstream/fake-catchup-848b",
      chat_agent_id: "agent-lead-4",
    });
    const root = draw();

    expect(allText(root)).toContain("This workspace was updated to upstream/fake-catchup-848b.");
    expect(buttonLabels(root)).toEqual(["Open chat", "Dismiss"]);
    press(root, "Open chat");
    expect(onClose).toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, { chat: "agent-lead-4" });
  });

  it("offers an update over a machine of unknown version once its last run's note is dismissed", () => {
    // A run to a branch leaves the machine's version unreadable; Dismiss clears the note, not the verdict.
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      availability: "UNKNOWN",
      unknown_reason: "NO_MACHINE_VERSION",
      current_version: "upstream/fake-catchup-848b",
      verdict: "UPDATED",
      success_note_version: "",
    });
    const root = draw();

    expect(allText(root)).not.toContain("This workspace was updated");
    expect(buttonLabels(root)).toEqual(["Update to a different version…", "Schedule update", "Update now"]);
  });

  it("reports a run that found nothing to do, which earns no note of its own", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      availability: "UNKNOWN",
      unknown_reason: "NO_MACHINE_VERSION",
      current_version: "upstream/fake-catchup-848b",
      verdict: "ALREADY_CURRENT",
      success_note_version: "",
    });
    const root = draw();

    expect(allText(root)).toContain("Nothing was changed.");
    expect(buttonLabels(root)).toEqual(["Dismiss"]);
  });

  it.each([
    ["a run that landed", { verdict: "UPDATED", availability: "UNKNOWN", unknown_reason: "NO_MACHINE_VERSION" }, true],
    ["a run that landed with a note for the user", { verdict: "UPDATED_WITH_REBUILD_ITEMS" }, true],
    ["a later run that found nothing to do", { verdict: "ALREADY_CURRENT", availability: "UP_TO_DATE" }, false],
  ] as const)("dismisses the row's hidden Updated to X note along with %s only if that run earned it", async (_case, fields, isNoteDismissed) => {
    const { draw, requests, onClose } = harness(() => Promise.resolve(jsonResponse({ ok: true })), {
      ...OUT_OF_DATE_UPDATE,
      success_note_version: "minds-v0.4.0",
      ...fields,
    } as UiWorkspaceUpdate);

    press(draw(), "Dismiss");
    await settleDispatch();

    const dismissed = [`/ui/api/updates/${AGENT}/dismiss`];
    expect(requests).toEqual(isNoteDismissed ? [...dismissed, `/ui/api/updates/${AGENT}/note/dismiss`] : dismissed);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("settles a failed run before offering another: its chat and Dismiss, no update buttons", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      verdict: "REFUSED",
      chat_agent_id: "agent-lead-4",
    });
    const root = draw();

    expect(buttonLabels(root)).toEqual(["Open chat", "Dismiss"]);
    expect(allText(root)).not.toContain("uses credits");
  });

  it("says why the band's Dismiss it was opened for did not land", () => {
    const { draw } = harness(
      () => Promise.resolve(jsonResponse({})),
      { ...OUT_OF_DATE_UPDATE, verdict: "REFUSED" },
      false,
      { openingFailure: { isOk: false, error: "Couldn't dismiss this update's outcome.", detail: "" } },
    );

    expect(allText(draw())).toContain("Couldn't dismiss this update's outcome.");
  });

  it("neither names a run that ended short by its agent's internal name nor offers machinery of its own", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      verdict: "NEEDS_RECREATION",
      chat_agent_name: "update-fox",
    });
    const root = draw();

    expect(allText(root)).not.toContain("update-fox");
    expect(pressableWithLabel(root, "Migrate")).toBeUndefined();
    expect(pressableWithLabel(root, "Dismiss")).toBeDefined();
  });
});

describe("the update modal over a machine too old to update in place", () => {
  const TOO_OLD: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    availability: "NEEDS_RECREATION",
    current_version: "minds-v0.3.4",
  };

  it("gives the two steps instead of an update to run", () => {
    const { draw, requests } = harness(
      () => Promise.resolve(jsonResponse({})),
      TOO_OLD,
    );
    const root = draw();

    expect(allText(root)).toContain("/migrate-workspace from box");
    expect(pressableWithLabel(root, "Update now")).toBeUndefined();
    expect(pressableWithLabel(root, "Schedule update")).toBeUndefined();
    expect(
      pressableWithLabel(root, "Update to a different version"),
    ).toBeUndefined();
    expect(requests).toEqual([]);
  });

  it("takes the first step, creating a machine, to the create page", () => {
    const { draw, routeSet, onClose } = harness(
      () => Promise.resolve(jsonResponse({})),
      TOO_OLD,
    );

    press(draw(), "Create a new workspace");

    expect(onClose).toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith("/create");
  });
});

// The row badges the outcome over either condition, so the modal it opens must settle it.
describe("the update modal over a run's outcome on a machine it cannot update in place", () => {
  it.each([
    [
      "ahead of the app, after a run that landed with a note",
      {
        ...OUT_OF_DATE_UPDATE,
        availability: "APP_BEHIND",
        verdict: "UPDATED_WITH_REBUILD_ITEMS",
        verdict_detail: "Rebuild the dashboard widget.",
        chat_agent_id: "agent-lead-6",
      },
      "Rebuild the dashboard widget.",
    ],
    [
      "too old, after a run that failed",
      { ...OUT_OF_DATE_UPDATE, availability: "NEEDS_RECREATION", verdict: "STUCK", chat_agent_id: "agent-lead-6" },
      "The update ran into a problem",
    ],
  ] as const)("shows the outcome, its chat, and Dismiss on a machine %s", (_case, update, outcomeText) => {
    const root = harness(() => Promise.resolve(jsonResponse({})), update as UiWorkspaceUpdate).draw();

    expect(allText(root)).toContain(outcomeText);
    expect(buttonLabels(root)).toEqual(["Open chat", "Dismiss"]);
  });
});

describe("the update modal over a run whose agent stopped without a result", () => {
  it.each([
    // Nothing restarts it, but its chat is still there: opening it is how the run is resumed.
    ["stopped", { chat_agent_id: "agent-lead-7", is_chat_gone: false }, ["Open chat", "Dismiss"]],
    // Its chat was deleted: there is nothing to open.
    ["deleted", { chat_agent_id: "", is_chat_gone: true }, ["Dismiss"]],
  ] as const)("offers the run's chat only while it exists (%s)", (_case, chat, labels) => {
    const update = { ...OUT_OF_DATE_UPDATE, activity: "STALLED", chat_agent_name: "update-to-v0-9-0", ...chat };
    const root = harness(() => Promise.resolve(jsonResponse({})), update as UiWorkspaceUpdate).draw();

    expect(buttonLabels(root)).toEqual(labels);
    expect(allText(root).includes("deleted")).toBe(chat.is_chat_gone);
  });
});

describe("the update modal over a machine behind only by a patch", () => {
  const PATCH: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    availability: "PATCH_AVAILABLE",
    current_version: "minds-v0.4.0",
  };

  it("shows both versions and lets the user schedule the patch", async () => {
    const { draw, requests } = harness(
      () => Promise.resolve(jsonResponse({})),
      PATCH,
    );
    const root = draw();

    expect(allText(root)).toContain("v0.4.0");
    expect(allText(root)).toContain("v0.4.1");
    expect(pressableWithLabel(root, "Update now")).toBeDefined();

    press(root, "Schedule update");
    await settleDispatch();
    expect(requests).toEqual([`/ui/api/updates/${AGENT}/schedule`]);
  });
});

describe("the update modal under auto-update", () => {
  const ARMED: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    is_scheduled: true,
    is_schedule_auto_armed: true,
    ...scheduleWindowAtLocalHour(1, 3),
  };
  function armed(isScheduled: boolean): void {
    getAppContext().stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: { [AGENT]: { ...ARMED, is_scheduled: isScheduled } },
      update_window: "2:00 AM-5:00 AM",
      is_auto_update_enabled: true,
    });
  }

  it("gives the armed window, asks nothing, and offers updating now or skipping the named release", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), ARMED, true);
    const root = draw();

    expect(allText(root)).toContain("in the next update window (1:00 AM-4:00 AM). No action is needed.");
    // The app arms the schedule itself, so the reader has none to make or cancel.
    expect(buttonLabels(root)).toEqual(["Update now instead", "Skip v0.4.1"]);
  });

  it("says what skipping means and skips only once confirmed", async () => {
    const { draw, requests, onClose } = harness(() => Promise.resolve(jsonResponse({})), ARMED, true);

    press(draw(), "Skip v0.4.1");
    const asking = draw();
    expect(allText(asking)).toContain(
      "Imbue Studio won't update this workspace to v0.4.1. When a newer version is available, it'll schedule that one instead.",
    );
    expect(buttonLabels(asking)).toEqual(["Skip v0.4.1", "Cancel"]);
    expect(requests).toEqual([]);

    press(asking, "Skip v0.4.1");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${AGENT}/skip`]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("drops the skip question once the schedule it asks about is gone, so it does not come back unasked", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), ARMED, true);
    press(draw(), "Skip v0.4.1");
    expect(pressableWithLabel(draw(), "Cancel")).toBeDefined();

    armed(false);
    draw();
    armed(true);

    expect(pressableWithLabel(draw(), "Cancel")).toBeUndefined();
  });

  it("keeps the skip question, held, while its skip is out, though the skip unschedules the row first", async () => {
    let answer: ((response: Response) => void) | null = null;
    const { draw, onClose } = harness(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
      ARMED,
      true,
    );
    press(draw(), "Skip v0.4.1");
    press(draw(), "Skip v0.4.1");
    armed(false);

    const cancel = pressableWithLabel(draw(), "Cancel");
    expect(cancel).toBeDefined();
    expect(attrsOf(cancel as AnyVnode).disabled).toBe(true);

    (answer as unknown as (response: Response) => void)(jsonResponse({}));
    await settleDispatch();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("drops the schedule's controls while its run, or one pressed instead, is under way", () => {
    // The run supersedes the schedule; a Skip beside it would not stop it.
    const root = harness(
      () => Promise.resolve(jsonResponse({})),
      { ...ARMED, activity: "RUNNING", chat_agent_id: "agent-lead-5", chat_agent_name: "Update-to-v041" },
      true,
    ).draw();

    expect(buttonLabels(root)).toEqual(["Open chat"]);
    expect(allText(root)).not.toContain("next update window");
  });

  // A skip outranks a standing sign-in, as on the row: the sweep will not arm a skipped release.
  it.each([false, true])(
    "tells a skipped machine what the skip means and still updates it by hand (needs sign-in: %s)",
    async (needsSignIn) => {
      const { draw, requests } = harness(
        () => Promise.resolve(jsonResponse({})),
        { ...OUT_OF_DATE_UPDATE, is_update_skipped: true, needs_sign_in: needsSignIn },
        true,
      );
      const root = draw();

      expect(allText(root)).toContain(
        "You skipped v0.4.1 for this workspace. Imbue Studio won't update it automatically until a newer version is available.",
      );
      expect(allText(root)).not.toContain("schedules its update again");
      expect(buttonLabels(root)).toEqual(["Schedule update", "Update now"]);
      press(root, "Update now");
      await settleDispatch();
      expect(requests).toEqual([`/ui/api/updates/${AGENT}/now`]);
    },
  );

  it("says why the last window passed without a start, and that the next one will try", () => {
    const { draw } = harness(
      () => Promise.resolve(jsonResponse({})),
      {
        ...ARMED,
        last_skip_reason: "Another update flow was working in this workspace during the last update window.",
      },
      true,
    );

    const text = allText(draw());
    expect(text).toContain(
      "Another update flow was working in this workspace during the last update window. Studio will try again in the next window.",
    );
  });

  it("offers to schedule a machine only a patch behind, which it will not arm itself", async () => {
    const { draw, requests } = harness(
      () => Promise.resolve(jsonResponse({})),
      { ...OUT_OF_DATE_UPDATE, availability: "PATCH_AVAILABLE", current_version: "minds-v0.4.0" },
      true,
    );

    press(draw(), "Schedule update");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${AGENT}/schedule`]);
  });

  it.each([
    ["armed by the user", { ...ARMED, is_schedule_auto_armed: false }],
    ["armed after the release was skipped", { ...ARMED, is_schedule_auto_armed: false, is_update_skipped: true }],
  ] as const)("shows a schedule %s as the user's own, to cancel rather than skip", async (_case, update) => {
    const { draw, requests } = harness(() => Promise.resolve(jsonResponse({})), update as UiWorkspaceUpdate, true);
    const root = draw();

    expect(allText(root)).toContain("Scheduled to update in the next update window (1:00 AM-4:00 AM).");
    expect(allText(root)).not.toContain("No action is needed.");
    expect(buttonLabels(root)).toEqual(["Update now instead", "Cancel schedule"]);
    press(root, "Cancel schedule");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${AGENT}/schedule/cancel`]);
  });

  it("offers to schedule a machine it has not armed yet, as without auto-update", () => {
    const root = harness(() => Promise.resolve(jsonResponse({})), OUT_OF_DATE_UPDATE, true).draw();
    expect(buttonLabels(root)).toEqual(["Update to a different version…", "Schedule update", "Update now"]);
  });

  it("offers to replace a schedule for another version with the supported one", async () => {
    const { draw, requests } = harness(
      () => Promise.resolve(jsonResponse({})),
      { ...ARMED, is_schedule_auto_armed: false, override_ref: "upstream/some-branch" },
      true,
    );
    const root = draw();

    expect(buttonLabels(root)).toEqual(["Update now instead", "Schedule update", "Cancel schedule"]);
    press(root, "Schedule update");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${AGENT}/schedule`]);
  });
});

describe("the update modal over a run that needs the user's decision", () => {
  const PENDING: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    verdict: "NEEDS_DECISION",
    verdict_detail: "Your dashboard widget and the new layout both changed the same file.",
    is_decision_pending: true,
    chat_agent_id: "agent-lead-1",
    chat_agent_name: "update-to-minds-v0-4-1",
  };

  it("shows the run's own account, the way into its chat, and Dismiss, with no update offered", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), PENDING);
    const root = draw();

    expect(allText(root)).toContain("Your dashboard widget and the new layout both changed the same file.");
    expect(buttonLabels(root)).toEqual(["Open chat", "Dismiss"]);
  });

  it("offers Dismiss alone when the run's chat is unknown, so the badge is never a dead end", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), { ...PENDING, chat_agent_id: "" });
    expect(buttonLabels(draw())).toEqual(["Dismiss"]);
  });

  it("dismisses a decision as a skip of the release, once confirmed", async () => {
    const { draw, requests } = harness(() => Promise.resolve(jsonResponse({})), PENDING);

    press(draw(), "Dismiss");
    const asking = draw();
    expect(allText(asking)).toContain(
      "Imbue Studio won't update this workspace to v0.4.1, and won't remind you about it. When a newer version is available, it'll let you know.",
    );
    press(asking, "Skip v0.4.1");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${AGENT}/skip`]);
  });

  it("opens on the confirmation when the band's Dismiss asked for it", () => {
    const root = harness(() => Promise.resolve(jsonResponse({})), PENDING, false, {
      isSkipConfirmOpen: true,
    }).draw();
    expect(buttonLabels(root)).toEqual(["Skip v0.4.1", "Cancel"]);
  });

  it("lands in the workspace on the run's chat", () => {
    const { draw, routeSet, onClose, requests } = harness(() => Promise.resolve(jsonResponse({})), PENDING);

    press(draw(), "Open chat");

    expect(onClose).toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, { chat: "agent-lead-1" });
    expect(requests).toEqual([]);
  });
});

describe("the update modal over a machine that needs a sign-in", () => {
  const REFUSED: UiWorkspaceUpdate = { ...OUT_OF_DATE_UPDATE, needs_sign_in: true };

  it("enters the machine before asking it for the sign-in chat", async () => {
    // The chat opens only in a connected frame, so the entry comes first.
    let answer: ((response: Response) => void) | null = null;
    let routeSetsWhenAsked: number | null = null;
    const { draw, requests, routeSet, onClose } = harness(() => {
      routeSetsWhenAsked = routeSet.mock.calls.length;
      return new Promise<Response>((resolve) => {
        answer = resolve;
      });
    }, REFUSED);

    press(draw(), "Sign in");

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, {});
    expect(requests).toEqual([`/ui/api/updates/${AGENT}/sign-in`]);
    expect(routeSetsWhenAsked).toBe(1);
    (answer as unknown as (response: Response) => void)(jsonResponse({}));
    await settleDispatch();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("says once that a refused Update now needs a sign-in", async () => {
    const refusal = "Sign in to an AI account in this workspace, then try again.";
    const { draw } = harness(() => Promise.resolve(jsonResponse({ error: refusal }, 409)));
    press(draw(), "Update now");
    await settleDispatch();
    // The refusal also lands on the row, as the next updates frame carries it.
    updatesShell({ [AGENT]: REFUSED }, false);

    const text = allText(draw());
    expect(text).toContain(refusal);
    expect(text).not.toContain("so its update can't start");
  });

  it("stays up to show a sign-in the machine could not open", async () => {
    const { draw, onClose } = harness(
      () => Promise.resolve(jsonResponse({ error: "Couldn't open a chat in this workspace." }, 502)),
      REFUSED,
      true,
    );

    press(draw(), "Sign in");
    await settleDispatch();

    expect(onClose).not.toHaveBeenCalled();
    expect(allText(draw())).toContain("Couldn't open a chat in this workspace.");
  });
});

describe("the update modal over a failed run", () => {
  it("opens the run's chat alongside Dismiss", () => {
    const { draw, routeSet } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      verdict: "STUCK",
      chat_agent_id: "agent-lead-2",
      started_by: "SCHEDULED",
    });
    const root = draw();
    expect(pressableWithLabel(root, "Dismiss")).toBeDefined();

    press(root, "Open chat");

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, { chat: "agent-lead-2" });
  });
});

describe("the update modal over a run in flight", () => {
  const WAITING: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    activity: "WAITING",
    chat_agent_name: "update-to-minds-v0-4-1",
    chat_agent_id: "agent-lead-3",
    started_by: "SCHEDULED",
  };

  it("opens the run's chat, which a scheduled run never put on screen", () => {
    const { draw, routeSet } = harness(() => Promise.resolve(jsonResponse({})), WAITING, true);

    press(draw(), "Open chat");

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${AGENT}`, { chat: "agent-lead-3" });
  });

  it("offers no chat while the run is still starting, before its chat exists", () => {
    const root = harness(() => Promise.resolve(jsonResponse({})), { ...WAITING, activity: "STARTING" }).draw();
    expect(pressableWithLabel(root, "Open chat")).toBeUndefined();
  });

  it("offers no chat while this window's own press is on its way, though the row names the last run's", () => {
    const { draw } = harness(() => new Promise<Response>(() => undefined), {
      ...OUT_OF_DATE_UPDATE,
      chat_agent_id: "agent-earlier-lead",
    });
    void getAppContext().stores.updates.updateNow(AGENT);

    expect(pressableWithLabel(draw(), "Open chat")).toBeUndefined();
  });
});

describe("the update modal over a workspace that stops", () => {
  it("offers nothing and claims no version until the workspace runs again", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})));
    publishWorkspaceLiveness(AGENT, "RUNNING");
    expect(buttonLabels(draw())).toEqual(["Update to a different version…", "Schedule update", "Update now"]);

    publishWorkspaceLiveness(AGENT, "STOPPED");
    const stopped = draw();
    expect(buttonLabels(stopped)).toEqual([]);
    expect(byId(stopped, "update-modal-stopped")).toBeDefined();
    expect(allText(stopped)).not.toContain("can't detect");

    publishWorkspaceLiveness(AGENT, "RUNNING");
    expect(buttonLabels(draw())).toEqual(["Update to a different version…", "Schedule update", "Update now"]);
  });

  it("drops a held no-backups question, so it neither shows while stopped nor comes back unasked", () => {
    const { draw, requests } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE_UPDATE,
      is_backup_configured: false,
    });
    publishWorkspaceLiveness(AGENT, "RUNNING");
    press(draw(), "Update now");
    expect(pressableWithLabel(draw(), "Go ahead without backups")).toBeDefined();

    publishWorkspaceLiveness(AGENT, "STOPPED");
    expect(pressableWithLabel(draw(), "Go ahead without backups")).toBeUndefined();
    publishWorkspaceLiveness(AGENT, "RUNNING");

    expect(pressableWithLabel(draw(), "Go ahead without backups")).toBeUndefined();
    expect(requests).toEqual([]);
  });
});
