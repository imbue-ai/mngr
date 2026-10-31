import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  getAppContext,
  registerAppContext,
} from "../../../app-context";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import { AutoUpdatePromptModel } from "../../../models/autoUpdatePrompt";
import { createEmptyStores } from "../../../models/boot";
import { ShellState } from "../../shell/shell-state";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  byId,
  classTokensOf,
  classesOf,
  collectVnodes,
  jsonResponse,
  publishWorkspaceLiveness,
  renderDeep,
  scheduleWindowAtLocalHour,
  settingsOverview,
  settle,
} from "../../../testing";
import { UpdatesGroup } from "./UpdatesGroup";

// Two machines, one without backups: the tab's state must be keyed on which is being drawn.
const UNBACKED = "agent-" + "a".repeat(8);
const BACKED = "agent-" + "b".repeat(8);
const NO_BACKUP_QUESTION = "This workspace has no backups.";

const OUT_OF_DATE: UiWorkspaceUpdate = {
  availability: "OUT_OF_DATE",
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  activity: "IDLE",
};

interface Harness {
  /** Draw the tab for one machine against the same component instance, as a route change would. */
  draw: (agentId: string) => AnyVnode;
  /** Take the tab down, as closing the panel or opening another section does. */
  leave: () => void;
  requests: string[];
  bodies: Record<string, unknown>;
  routeSet: ReturnType<typeof vi.fn>;
  shell: ShellState;
}

interface HarnessOptions {
  respond?: (url: string) => Promise<Response>;
  isAutoUpdateEnabled?: boolean;
  /** The one-time offer's recorded answer, read before the first draw; undefined leaves it unread. */
  promptDecision?: "accepted" | "declined" | null;
  /** Draw before the first updates frame has arrived. */
  isBeforeFirstFrame?: boolean;
}

async function harness(options: HarnessOptions = {}): Promise<Harness> {
  const respond = options.respond ?? (() => Promise.resolve(jsonResponse({})));
  const requests: string[] = [];
  const bodies: Record<string, unknown> = {};
  const fetchImpl = (url: string, init?: RequestInit): Promise<Response> => {
    requests.push(url);
    if (init?.body !== undefined) bodies[url] = JSON.parse(String(init.body));
    if (url === "/ui/api/settings")
      return Promise.resolve(
        jsonResponse(
          settingsOverview({
            auto_update_prompt_decision: options.promptDecision ?? null,
          }),
        ),
      );
    return respond(url);
  };
  vi.stubGlobal("fetch", fetchImpl);
  const prompt = new AutoUpdatePromptModel(
    fetchImpl as typeof fetch,
    () => Promise.resolve(false),
    () => undefined,
  );
  const shell = new ShellState(createEmptyStores(), undefined, prompt);
  registerAppContext({ stores: shell.stores, shell });
  if (!(options.isBeforeFirstFrame ?? false))
    applyUpdates(
      shell,
      {
        [UNBACKED]: { ...OUT_OF_DATE, is_backup_configured: false },
        [BACKED]: { ...OUT_OF_DATE, is_backup_configured: true },
      },
      options.isAutoUpdateEnabled ?? false,
    );
  vi.spyOn(m.route, "get").mockReturnValue(
    "/workspace/x/options?tab=settings&group=updates",
  );
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  if (options.promptDecision !== undefined) {
    await prompt.load();
    requests.length = 0;
  }
  const instance = UpdatesGroup() as unknown as m.Component;
  let drawn: m.Vnode | null = null;
  const draw = (agentId: string): AnyVnode => {
    const vnode = m(instance, {
      agentId,
    } as unknown as m.Attributes) as m.Vnode;
    if (drawn !== null)
      (instance.onbeforeupdate as (v: m.Vnode, old: m.Vnode) => boolean).call(
        instance,
        vnode,
        drawn,
      );
    drawn = vnode;
    return renderDeep(
      (instance.view as unknown as (v: m.Vnode) => m.Children).call(
        instance,
        vnode,
      ),
    ) as AnyVnode;
  };
  const leave = (): void =>
    (instance.onremove as (v: m.Vnode) => void).call(instance, drawn as m.Vnode);
  return {
    draw,
    leave,
    requests,
    bodies,
    routeSet: routeSet as unknown as ReturnType<typeof vi.fn>,
    shell,
  };
}

function applyUpdates(
  shell: ShellState,
  updates: Record<string, UiWorkspaceUpdate>,
  isAutoUpdateEnabled = false,
): void {
  shell.stores.updates.applyUpdatesMessage({
    type: "workspace_updates",
    updates: Object.fromEntries(
      Object.entries(updates).map(([agentId, update]) => [
        agentId,
        {
          is_auto_update_enabled: isAutoUpdateEnabled,
          update_window: "2:00 AM-5:00 AM",
          ...update,
        },
      ]),
    ),
    update_window: "2:00 AM-5:00 AM",
    is_auto_update_enabled: isAutoUpdateEnabled,
  });
}

function republish(
  updates: Record<string, UiWorkspaceUpdate>,
  isAutoUpdateEnabled = false,
): void {
  applyUpdates(getAppContext().shell, updates, isAutoUpdateEnabled);
}

function pressableWithLabel(
  node: unknown,
  label: string,
): AnyVnode | undefined {
  return collectVnodes(node).find(
    (vnode) =>
      typeof attrsOf(vnode).onclick === "function" &&
      allText(vnode.children).trim() === label,
  );
}

function press(node: unknown, label: string): void {
  const pressable = pressableWithLabel(node, label);
  if (pressable === undefined) throw new Error(`no "${label}" to press`);
  if (attrsOf(pressable).disabled === true)
    throw new Error(`"${label}" is disabled`);
  (attrsOf(pressable).onclick as () => void)();
}

/** Press a control by id: the collapsibles' triggers carry a chevron beside their words. */
function pressId(node: unknown, id: string): void {
  (attrsOf(byId(node, id) as AnyVnode).onclick as () => void)();
}

function typeSpecificVersion(node: unknown, value: string): void {
  const field = byId(node, "update-override-input");
  if (field === undefined)
    throw new Error("the specific-version field was not drawn");
  (attrsOf(field).oninput as (event: InputEvent) => void)({
    target: { value },
  } as unknown as InputEvent);
}

function choose(node: unknown, choice: string): void {
  const option = collectVnodes(node).find(
    (vnode) => attrsOf(vnode)["data-auto-update-choice"] === choice,
  );
  const radio = collectVnodes(option).find(
    (vnode) => attrsOf(vnode).type === "radio",
  );
  if (radio === undefined) throw new Error(`no "${choice}" choice`);
  (attrsOf(radio).onchange as () => void)();
}

function statusOf(node: unknown): Record<string, unknown> {
  return attrsOf(byId(node, "update-status") as AnyVnode);
}

/** A press answers over several promise hops: the request, its body parse, the store, the tab's own callback. */
async function settleAll(): Promise<void> {
  for (let index = 0; index < 5; index += 1) await settle();
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

describe("the Updates tab's no-backup question", () => {
  it("holds a press on a machine without backups and asks in place of the actions", async () => {
    const { draw, requests } = await harness();

    press(draw(UNBACKED), "Update now");

    const asked = draw(UNBACKED);
    expect(allText(asked)).toContain(NO_BACKUP_QUESTION);
    // A live "Update now" under "Go ahead?" would be a second way to answer.
    expect(pressableWithLabel(asked, "Update now")).toBeUndefined();
    expect(requests).toEqual([]);
  });

  it("is dropped when the workspace stops, so starting it again does not bring it back", async () => {
    const { draw } = await harness();
    publishWorkspaceLiveness(UNBACKED, "RUNNING");
    press(draw(UNBACKED), "Update now");

    publishWorkspaceLiveness(UNBACKED, "STOPPED");
    draw(UNBACKED);
    publishWorkspaceLiveness(UNBACKED, "RUNNING");

    expect(
      pressableWithLabel(draw(UNBACKED), "Go ahead without backups"),
    ).toBeUndefined();
  });

  it("dispatches only once answered, for the machine it asked about, then takes the reader into it", async () => {
    const { draw, requests, routeSet } = await harness();

    press(draw(UNBACKED), "Update now");
    press(draw(UNBACKED), "Go ahead without backups");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${UNBACKED}/now`]);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${UNBACKED}`, {});
    expect(allText(draw(UNBACKED))).not.toContain(NO_BACKUP_QUESTION);
  });

  it("takes back the press when the question is cancelled", async () => {
    const { draw, requests } = await harness();

    press(draw(UNBACKED), "Update now");
    press(draw(UNBACKED), "Cancel");

    const settled = draw(UNBACKED);
    expect(allText(settled)).not.toContain(NO_BACKUP_QUESTION);
    expect(pressableWithLabel(settled, "Update now")).toBeDefined();
    expect(requests).toEqual([]);
  });

  it("never puts one machine's question in front of another, nor answers it for one", async () => {
    // A route change between machines keeps this instance, so A's question
    // must neither stand over B nor dispatch to it.
    const { draw, requests } = await harness();
    press(draw(UNBACKED), "Update now");

    const other = draw(BACKED);
    expect(allText(other)).not.toContain(NO_BACKUP_QUESTION);
    press(other, "Update now");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/now`]);
    // Unanswered, not discarded: the machine it was raised for still has it.
    expect(allText(draw(UNBACKED))).toContain(NO_BACKUP_QUESTION);
  });
});

describe("the Updates tab's presses", () => {
  it("keeps a dispatch's spinner and its refusal on the machine the dispatch was made for", async () => {
    // A dispatch can take minutes, so the reader may switch machines while it is out.
    let answer: ((response: Response) => void) | null = null;
    const { draw, requests, routeSet } = await harness({
      respond: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    press(draw(BACKED), "Update now");
    expect(requests).toEqual([`/ui/api/updates/${BACKED}/now`]);

    const otherButton = pressableWithLabel(draw(UNBACKED), "Update now");
    expect(attrsOf(otherButton as AnyVnode).disabled).toBe(false);

    (answer as unknown as (response: Response) => void)(
      jsonResponse(
        { error: "An update is already running in this workspace." },
        409,
      ),
    );
    await settleAll();

    expect(allText(draw(UNBACKED))).not.toContain(
      "An update is already running",
    );
    expect(allText(draw(BACKED))).toContain("An update is already running");
    // A refusal keeps the reader on the tab that explains it.
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("says once that a refused Update now needs a sign-in", async () => {
    const refusal =
      "Sign in to an AI account in this workspace, then try again.";
    const { draw } = await harness({
      respond: () => Promise.resolve(jsonResponse({ error: refusal }, 409)),
    });
    press(draw(BACKED), "Update now");
    await settleAll();
    // The refusal also lands on the row, as the next updates frame carries it.
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        needs_sign_in: true,
      },
    });

    const text = allText(draw(BACKED));
    expect(text).toContain(refusal);
    expect(text).not.toContain("so its update can't start");
  });

  it("quotes the machine's own verdict under the refusal", async () => {
    // The reason a wedged machine gives is the only thing that tells the reader
    // retrying will not help, so it has to survive all the way onto the tab.
    const { draw } = await harness({
      respond: () =>
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
    });

    press(draw(BACKED), "Update now");
    await settleAll();

    const refused = allText(draw(BACKED));
    expect(refused).toContain(
      "Couldn't start the update agent in this workspace.",
    );
    expect(refused).toContain(
      "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']",
    );
  });

  it("schedules a press past the no-backup question", async () => {
    const { draw, requests } = await harness();

    press(draw(UNBACKED), "Schedule update");
    expect(allText(draw(UNBACKED))).toContain(NO_BACKUP_QUESTION);
    expect(requests).toEqual([]);
    press(draw(UNBACKED), "Go ahead without backups");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${UNBACKED}/schedule`]);
  });

  it("offers to cancel a scheduled machine's run in place of scheduling it again", async () => {
    const { draw, requests } = await harness();
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        is_scheduled: true,
      },
    });

    const tab = draw(BACKED);
    expect(allText(tab)).toContain(
      "Scheduled to update in the next update window (2:00 AM-5:00 AM)",
    );
    expect(pressableWithLabel(tab, "Schedule update")).toBeUndefined();
    press(tab, "Cancel schedule");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule/cancel`]);
  });

  it("sends a sign-in into the workspace, and comes back here to say why one could not open", async () => {
    const { draw, requests, routeSet, shell } = await harness({
      respond: () =>
        Promise.resolve(
          jsonResponse(
            { error: "Couldn't open a chat in this workspace to sign in." },
            502,
          ),
        ),
    });
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        needs_sign_in: true,
      },
    });

    press(draw(BACKED), "Sign in");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/sign-in`]);
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${BACKED}`, {});
    expect(shell.takeUpdatesTabArrival(BACKED)).not.toBeNull();
    expect(allText(draw(BACKED))).toContain(
      "Couldn't open a chat in this workspace to sign in.",
    );
  });
});

describe("the Updates tab's Update now on a stopped workspace", () => {
  it("enters the workspace before its update goes out, and comes back here to explain a refusal", async () => {
    let answer: ((response: Response) => void) | null = null;
    const { draw, routeSet } = await harness({
      respond: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        dispatch_failure: "Couldn't start this workspace to run the update.",
      },
    });
    publishWorkspaceLiveness(BACKED, "STOPPED");

    press(draw(BACKED), "Try again");

    // Entered while the dispatch is still out: the run's chat tab opens only for a connected client.
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${BACKED}`, {});
    (answer as unknown as (response: Response) => void)(
      jsonResponse(
        {
          error: "Couldn't start this workspace to run the update.",
          detail: "box gone",
        },
        502,
      ),
    );
    await settleAll();

    expect(allText(byId(draw(BACKED), "ws-update-error"))).toContain(
      "box gone",
    );
  });
});

describe("the Updates tab's specific-version field", () => {
  it("starts empty, with no prefill, even on a build pinned to a branch", async () => {
    const { draw } = await harness();
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        supported_version: "gabriel/some-branch",
        availability: "UNKNOWN",
      },
    });

    pressId(draw(BACKED), "update-override-trigger");

    expect(
      attrsOf(byId(draw(BACKED), "update-override-input") as AnyVnode).value,
    ).toBe("");
    expect(
      attrsOf(byId(draw(BACKED), "update-override-btn") as AnyVnode).disabled,
    ).toBe(true);
  });

  it("keeps a typed ref on the machine it was typed for", async () => {
    const { draw } = await harness();

    pressId(draw(UNBACKED), "update-override-trigger");
    typeSpecificVersion(draw(UNBACKED), "upstream/some-branch");

    expect(byId(draw(BACKED), "update-override-input")).toBeUndefined();
    expect(
      attrsOf(byId(draw(UNBACKED), "update-override-input") as AnyVnode).value,
    ).toBe("upstream/some-branch");
  });

  it("starts no run over a decision the last one is waiting on", async () => {
    const { draw } = await harness();
    republish({
      [UNBACKED]: {
        ...OUT_OF_DATE,
        verdict: "NEEDS_DECISION",
        is_decision_pending: true,
      },
    });

    pressId(draw(UNBACKED), "update-override-trigger");
    typeSpecificVersion(draw(UNBACKED), "minds-v0.9.1");

    const tab = draw(UNBACKED);
    for (const id of ["update-override-btn", "update-override-schedule-btn"]) {
      expect(attrsOf(byId(tab, id) as AnyVnode).disabled).toBe(true);
    }
  });

  it("schedules the named version, its own Schedule spinning rather than the plain one", async () => {
    const { draw, requests, bodies } = await harness({
      respond: () => new Promise<Response>(() => undefined),
    });

    pressId(draw(BACKED), "update-override-trigger");
    typeSpecificVersion(draw(BACKED), "upstream/some-branch");
    (
      attrsOf(byId(draw(BACKED), "update-override-schedule-btn") as AnyVnode)
        .onclick as () => void
    )();

    const tab = draw(BACKED);
    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule`]);
    expect(bodies[`/ui/api/updates/${BACKED}/schedule`]).toEqual({
      target_ref: "upstream/some-branch",
    });
    expect(allText(byId(tab, "update-override-schedule-btn")?.children)).toBe(
      "",
    );
    expect(allText(byId(tab, "ws-update-schedule")?.children)).toBe(
      "Schedule update",
    );
  });
});

describe("the Updates tab after an update that never went out", () => {
  function recordFailureOnBacked(): void {
    republish({
      [UNBACKED]: { ...OUT_OF_DATE, is_backup_configured: false },
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        dispatch_failure: "Couldn't start this workspace to run the update.",
        dispatch_failure_detail: "ERROR: The box behind host-5821 is gone",
      },
    });
  }

  it("shows the machine's recorded reason under its line, on that machine only, with Try again", async () => {
    const { draw } = await harness();
    recordFailureOnBacked();

    const backed = draw(BACKED);
    expect(allText(byId(backed, "update-status-title"))).toBe(
      "Update didn't start",
    );
    expect(allText(backed)).toContain(
      "Couldn't start this workspace to run the update.",
    );
    expect(allText(backed)).toContain(
      "ERROR: The box behind host-5821 is gone",
    );
    expect(pressableWithLabel(backed, "Try again")).toBeDefined();
    expect(allText(draw(UNBACKED))).not.toContain(
      "Couldn't start this workspace",
    );
  });

  it("holds the recorded reason back while a new press is out", async () => {
    // The press is the newer question; the old answer beside its spinner would read as its reply.
    const { draw } = await harness({
      respond: () => new Promise<Response>(() => undefined),
    });
    recordFailureOnBacked();

    press(draw(BACKED), "Try again");

    expect(allText(draw(BACKED))).not.toContain(
      "Couldn't start this workspace",
    );
  });
});

describe("leaving the Updates tab", () => {
  const LANDED = {
    ...OUT_OF_DATE,
    availability: "UP_TO_DATE",
    current_version: "minds-v0.4.1",
    success_note_version: "minds-v0.4.1",
  } as const;
  const dismissals = (requests: string[]): string[] =>
    requests.filter((url) => url.endsWith("/dismiss"));

  it.each<[string, UiWorkspaceUpdate, string[]]>([
    [
      "didn't start",
      { ...OUT_OF_DATE, dispatch_failure: "Couldn't start this workspace to run the update." },
      ["dismiss"],
    ],
    ["didn't finish", { ...OUT_OF_DATE, verdict: "STUCK" }, ["dismiss"]],
    ["updated, with a note", { ...LANDED, verdict: "UPDATED_WITH_REBUILD_ITEMS" }, ["dismiss", "note/dismiss"]],
    ["updated", { ...LANDED, verdict: "UPDATED" }, ["dismiss", "note/dismiss"]],
    ["already up to date", { ...LANDED, verdict: "ALREADY_CURRENT" }, ["dismiss"]],
  ])("settles a run that %s, read there", async (_, update, dismissed) => {
    const { draw, leave, requests } = await harness();
    republish({ [BACKED]: update });

    draw(BACKED);
    expect(dismissals(requests)).toEqual([]);
    leave();
    await settleAll();

    expect(dismissals(requests)).toEqual(dismissed.map((path) => `/ui/api/updates/${BACKED}/${path}`));
  });

  it.each<[string, UiWorkspaceUpdate]>([
    ["waits on a decision, whose dismissal is a skip", { ...OUT_OF_DATE, is_decision_pending: true }],
    ["is under way", { ...OUT_OF_DATE, activity: "RUNNING", chat_agent_id: "agent-chat" }],
    ["is on offer", OUT_OF_DATE],
  ])("leaves an update that %s", async (_, update) => {
    const { draw, leave, requests } = await harness();
    republish({ [BACKED]: update });

    draw(BACKED);
    leave();
    await settleAll();

    expect(dismissals(requests)).toEqual([]);
  });

  it("leaves a stopped workspace's outcome, which the tab does not show", async () => {
    const { draw, leave, requests } = await harness();
    republish({ [BACKED]: { ...OUT_OF_DATE, verdict: "STUCK" } });
    publishWorkspaceLiveness(BACKED, "STOPPED");

    expect(allText(byId(draw(BACKED), "update-status-title"))).toBe("Workspace stopped");
    leave();
    await settleAll();

    expect(dismissals(requests)).toEqual([]);
  });

  it("settles the machine it moves away from, and only that one", async () => {
    const { draw, requests } = await harness();
    republish({
      [BACKED]: { ...OUT_OF_DATE, verdict: "STUCK" },
      [UNBACKED]: { ...OUT_OF_DATE, verdict: "STUCK" },
    });

    draw(BACKED);
    draw(BACKED);
    expect(dismissals(requests)).toEqual([]);
    draw(UNBACKED);
    await settleAll();

    expect(dismissals(requests)).toEqual([`/ui/api/updates/${BACKED}/dismiss`]);
  });

  it("leaves the outcome a Try again on the way out is replacing", async () => {
    // Update now on a stopped workspace enters it at once, so the tab goes before it redraws.
    const { draw, leave, requests } = await harness({
      respond: () => new Promise<Response>(() => undefined),
    });
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_backup_configured: true,
        dispatch_failure: "Couldn't start this workspace to run the update.",
      },
    });
    publishWorkspaceLiveness(BACKED, "STOPPED");

    press(draw(BACKED), "Try again");
    leave();
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/now`]);
  });
});

describe("the Updates tab under auto-update", () => {
  const readable = {
    ...OUT_OF_DATE,
    is_backup_configured: true,
    is_workspace_readable: true,
  };

  it("offers a schedule auto-update armed a skip, not a cancel, and asks before skipping", async () => {
    const { draw, requests } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...readable,
          is_scheduled: true,
          is_schedule_auto_armed: true,
        },
      },
      true,
    );

    const armed = draw(BACKED);
    expect(allText(armed)).toContain(
      "Studio will update this workspace in the next update window",
    );
    expect(allText(armed)).toContain("No action is needed.");
    expect(byId(armed, "ws-update-schedule")).toBeUndefined();
    expect(pressableWithLabel(armed, "Cancel schedule")).toBeUndefined();
    press(armed, "Skip v0.4.1");
    const asking = draw(BACKED);
    expect(pressableWithLabel(asking, "Cancel")).toBeDefined();
    expect(requests).toEqual([]);
    press(asking, "Skip v0.4.1");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/skip`]);
  });

  it("offers an ordinary Cancel schedule for that same schedule once the workspace is set to manual", async () => {
    // Switching to manual leaves the schedule auto-update armed; it is the user's to cancel now.
    const { draw } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...readable,
          is_scheduled: true,
          is_schedule_auto_armed: true,
          is_auto_update_enabled: false,
          auto_update_override: { mode: "manual" },
        },
      },
      true,
    );

    const tab = draw(BACKED);
    expect(allText(tab)).toContain(
      "Scheduled to update in the next update window",
    );
    expect(pressableWithLabel(tab, "Cancel schedule")).toBeDefined();
    expect(pressableWithLabel(tab, "Skip v0.4.1")).toBeUndefined();
  });

  it.each([
    ["auto-update has not armed yet", OUT_OF_DATE],
    [
      "only a patch behind",
      {
        ...OUT_OF_DATE,
        availability: "PATCH_AVAILABLE",
        current_version: "minds-v0.4.0",
      },
    ],
    [
      "of unknown version",
      { ...OUT_OF_DATE, availability: "UNKNOWN", current_version: "" },
    ],
    ["whose release was skipped", { ...OUT_OF_DATE, is_update_skipped: true }],
  ] as const)("offers to schedule a machine %s", async (_case, update) => {
    const { draw, requests } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...(update as UiWorkspaceUpdate),
          is_backup_configured: true,
          is_workspace_readable: true,
        },
      },
      true,
    );

    press(draw(BACKED), "Schedule update");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule`]);
  });

  it("says Studio will schedule a behind machine it will arm, and still offers the presses", async () => {
    const { draw } = await harness({ isAutoUpdateEnabled: true });
    republish({ [BACKED]: readable }, true);

    const tab = draw(BACKED);
    expect(allText(byId(tab, "update-status-line"))).toBe(
      "v0.4.1 is ready for this workspace. Studio will schedule it for the next window, or schedule or update it yourself.",
    );
    expect(pressableWithLabel(tab, "Schedule update")).toBeDefined();
  });

  it("offers no schedule for a machine the user scheduled to the supported version by name", async () => {
    const { draw } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...readable,
          is_scheduled: true,
          override_ref: "minds-v0.4.1",
        },
      },
      true,
    );

    const tab = draw(BACKED);
    expect(byId(tab, "ws-update-schedule")).toBeUndefined();
    expect(pressableWithLabel(tab, "Cancel schedule")).toBeDefined();
  });

  it("replaces a schedule for another version with one naming the supported version", async () => {
    const { draw, requests, bodies } = await harness({
      isAutoUpdateEnabled: true,
    });
    republish(
      {
        [BACKED]: {
          ...readable,
          is_scheduled: true,
          override_ref: "upstream/some-branch",
        },
      },
      true,
    );

    const tab = draw(BACKED);
    expect(pressableWithLabel(tab, "Cancel schedule")).toBeDefined();
    press(tab, "Schedule update");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule`]);
    // A bare press keeps the armed version, so this one names the app's.
    expect(bodies[`/ui/api/updates/${BACKED}/schedule`]).toEqual({
      target_ref: "minds-v0.4.1",
    });
  });

  it("lets the user cancel a schedule they armed, which says it is scheduled rather than that nothing is asked", async () => {
    const { draw, requests } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...readable,
          is_scheduled: true,
          is_schedule_auto_armed: false,
          override_ref: "upstream/some-branch",
          ...scheduleWindowAtLocalHour(1, 3),
        },
      },
      true,
    );

    const tab = draw(BACKED);
    expect(allText(tab)).toContain(
      "Scheduled to update to upstream/some-branch in the next update window",
    );
    expect(allText(tab)).not.toContain("No action is needed.");
    expect(byId(tab, "ws-update-skip")).toBeUndefined();
    press(tab, "Cancel schedule");
    await settleAll();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule/cancel`]);
  });
});

describe("the Updates tab's skip question", () => {
  function armBacked(isScheduled: boolean): void {
    republish(
      {
        [BACKED]: {
          ...OUT_OF_DATE,
          is_backup_configured: true,
          is_scheduled: isScheduled,
          is_schedule_auto_armed: isScheduled,
        },
      },
      true,
    );
  }

  it("stays up, held, while the skip is out, and gives way to its answer", async () => {
    let answer: ((response: Response) => void) | null = null;
    const { draw } = await harness({
      isAutoUpdateEnabled: true,
      respond: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    armBacked(true);
    press(draw(BACKED), "Skip v0.4.1");
    press(draw(BACKED), "Skip v0.4.1");
    // The skip cancels the schedule first, so the unscheduled row arrives before its answer.
    armBacked(false);

    const cancel = pressableWithLabel(draw(BACKED), "Cancel");
    expect(attrsOf(cancel as AnyVnode).disabled).toBe(true);

    const refusal = "This workspace couldn't record the skip.";
    (answer as unknown as (response: Response) => void)(
      jsonResponse({ error: refusal }, 409),
    );
    await settleAll();

    const answered = draw(BACKED);
    expect(pressableWithLabel(answered, "Cancel")).toBeUndefined();
    expect(allText(answered)).toContain(refusal);
  });

  it("is dropped once the schedule it asks about is gone, so it does not come back unasked", async () => {
    const { draw } = await harness({ isAutoUpdateEnabled: true });
    armBacked(true);
    press(draw(BACKED), "Skip v0.4.1");
    expect(pressableWithLabel(draw(BACKED), "Cancel")).toBeDefined();

    armBacked(false);
    draw(BACKED);
    armBacked(true);

    expect(pressableWithLabel(draw(BACKED), "Cancel")).toBeUndefined();
  });

  it("is dropped when the workspace stops, so starting it again does not bring it back", async () => {
    const { draw } = await harness({ isAutoUpdateEnabled: true });
    armBacked(true);
    publishWorkspaceLiveness(BACKED, "RUNNING");
    press(draw(BACKED), "Skip v0.4.1");
    expect(pressableWithLabel(draw(BACKED), "Cancel")).toBeDefined();

    publishWorkspaceLiveness(BACKED, "STOPPED");
    draw(BACKED);
    publishWorkspaceLiveness(BACKED, "RUNNING");

    expect(pressableWithLabel(draw(BACKED), "Cancel")).toBeUndefined();
  });

  it("asks to skip the release when a pending decision is dismissed", async () => {
    // Dismissing a decision lets a fresh run set the kept work aside, so it is a skip.
    const { draw, requests } = await harness();
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        verdict: "NEEDS_DECISION",
        verdict_detail:
          "Your Recipes app edits the chat page this update replaces.",
        is_decision_pending: true,
        chat_agent_id: "agent-update-chat",
      },
    });

    const tab = draw(BACKED);
    expect(allText(byId(tab, "update-status-title"))).toBe(
      "Needs your decision",
    );
    expect(allText(byId(tab, "ws-update-quote"))).toBe(
      "Your Recipes app edits the chat page this update replaces.",
    );
    press(tab, "Dismiss");
    expect(allText(draw(BACKED))).toContain(
      "Imbue Studio won't update this workspace to v0.4.1, and won't remind you",
    );
    expect(requests).toEqual([]);
  });
});

describe("the Updates tab over a run under way", () => {
  it.each([true, false])(
    "drops the schedule's controls, which would not stop the run (auto-update %s)",
    async (isAuto) => {
      const { draw } = await harness({ isAutoUpdateEnabled: isAuto });
      republish(
        {
          [BACKED]: {
            ...OUT_OF_DATE,
            is_backup_configured: true,
            is_scheduled: true,
            activity: "RUNNING",
            chat_agent_id: "agent-chat",
          },
        },
        isAuto,
      );

      const tab = draw(BACKED);
      expect(allText(byId(tab, "update-status-title"))).toBe(
        "Preparing the update",
      );
      expect(byId(tab, "ws-update-skip")).toBeUndefined();
      expect(byId(tab, "ws-update-cancel-schedule")).toBeUndefined();
      expect(pressableWithLabel(tab, "Open chat")).toBeDefined();
    },
  );
});

describe("the Updates tab of a stopped workspace", () => {
  it("says to start it, claims no version state, offers nothing to do, and keeps its automatic-updates setting", async () => {
    const { draw } = await harness();
    publishWorkspaceLiveness(BACKED, "STOPPED");

    const tab = draw(BACKED);
    expect(allText(byId(tab, "update-status-title"))).toBe("Workspace stopped");
    expect(allText(tab)).toContain("Start this workspace to see its updates.");
    expect(byId(tab, "ws-update-versions")).toBeUndefined();
    expect(allText(tab)).not.toContain("v0.3.9");
    expect(byId(tab, "update-override")).toBeUndefined();
    expect(byId(tab, "ws-auto-update-choices")).toBeDefined();
  });
});

describe("the Updates tab's status block", () => {
  it("shows the version jump, the line, and the costs for an update on offer", async () => {
    const { draw } = await harness();

    const tab = draw(BACKED);
    expect(allText(byId(tab, "update-status-title"))).toBe("Update available");
    expect(allText(byId(tab, "ws-update-versions"))).toBe("v0.3.9 → v0.4.1");
    expect(allText(byId(tab, "update-status-line"))).toBe(
      "v0.4.1 is ready for this workspace. Schedule it for the update window, or update now.",
    );
    expect(allText(byId(tab, "ws-update-costs"))).toContain(
      "which uses tokens",
    );
    expect(byId(tab, "ws-update-security")).toBeUndefined();
  });

  it("keeps a release tag whole in the version jump but lets a dev build's branch truncate", async () => {
    const { draw } = await harness();
    expect(
      classTokensOf(byId(draw(BACKED), "ws-update-versions-supported") as AnyVnode),
    ).toContain("shrink-0");

    republish({
      [BACKED]: { ...OUT_OF_DATE, supported_version: "gabriel/a-very-long-branch-name-for-a-dev-build" },
    });
    const supported = byId(draw(BACKED), "ws-update-versions-supported") as AnyVnode;
    expect(allText(supported)).toBe("gabriel/a-very-long-branch-name-for-a-dev-build");
    expect(classTokensOf(supported)).toContain("truncate");
    expect(classTokensOf(supported)).not.toContain("shrink-0");
  });

  it("cards a release's security fix, folds its details, and makes Update now the main action", async () => {
    const { draw } = await harness();
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        availability: "SECURITY_UPDATE",
        current_version: "minds-v0.4.0",
        is_backup_configured: true,
      },
    });

    const tab = draw(BACKED);
    expect(allText(byId(tab, "ws-update-security"))).toContain(
      "This update includes a critical security fix.",
    );
    expect(allText(tab)).not.toContain("eMudhra");
    const buttons = collectVnodes(tab).filter((vnode) =>
      String(attrsOf(vnode).id ?? "").startsWith("ws-update-"),
    );
    expect(
      buttons
        .map((vnode) => attrsOf(vnode).id)
        .filter((id) => id === "ws-update-now" || id === "ws-update-schedule"),
    ).toEqual(["ws-update-now", "ws-update-schedule"]);
    pressId(tab, "security-fix-details-trigger");
    expect(allText(draw(BACKED))).toContain("eMudhra");
  });

  it("flashes on arrival from another surface, takes the arrival once, and only for the machine it was asked for", async () => {
    const { draw, shell } = await harness({ isAutoUpdateEnabled: true });
    republish(
      {
        [BACKED]: {
          ...OUT_OF_DATE,
          is_scheduled: true,
          is_schedule_auto_armed: true,
        },
      },
      true,
    );
    expect(
      classesOf(byId(draw(BACKED), "update-status") as AnyVnode),
    ).not.toContain("update-status-arrive");

    shell.openUpdatesTab(UNBACKED);
    // Another machine's tab does not take it.
    expect(
      classesOf(byId(draw(BACKED), "update-status") as AnyVnode),
    ).not.toContain("update-status-arrive");
    expect(shell.takeUpdatesTabArrival(UNBACKED)).not.toBeNull();

    shell.openUpdatesTab(BACKED);
    const arrived = draw(BACKED);
    expect(classesOf(byId(arrived, "update-status") as AnyVnode)).toContain(
      "update-status-arrive",
    );
    expect(statusOf(arrived).style).toBe("--flash: var(--c-info)");
    // Taken, so a later opening of the tab does not replay it.
    expect(shell.takeUpdatesTabArrival(BACKED)).toBeNull();
  });

  it("explains a press made elsewhere that failed, which it was opened to explain", async () => {
    const { draw, shell } = await harness();

    shell.openUpdatesTab(BACKED, {
      failure: {
        isOk: false,
        error: "Couldn't reach this workspace.",
        detail: "ssh: timeout",
      },
    });

    expect(allText(byId(draw(BACKED), "ws-update-error"))).toBe(
      "Couldn't reach this workspace. ssh: timeout",
    );
  });
});

describe("the Updates tab's automatic updates", () => {
  it("pitches the one-time offer while it is unanswered and Studio's switch is off, and either answer answers it", async () => {
    const { draw, requests, bodies } = await harness({ promptDecision: null });

    const tab = draw(BACKED);
    expect(byId(tab, "ws-auto-update-pitch")).toBeDefined();
    expect(byId(tab, "ws-auto-update-choices")).toBeUndefined();
    press(tab, "Not now");
    await settleAll();

    expect(requests).toEqual(["/ui/api/settings/auto-update-prompt"]);
    expect(bodies["/ui/api/settings/auto-update-prompt"]).toEqual({
      decision: "declined",
    });
    expect(byId(draw(BACKED), "ws-auto-update-pitch")).toBeUndefined();
    expect(byId(draw(BACKED), "ws-auto-update-choices")).toBeDefined();
  });

  it("pitches nothing before the first frame says whether Studio's switch is on", async () => {
    // Turning the switch on in Studio settings records no answer to the offer.
    const { draw, shell } = await harness({
      promptDecision: null,
      isBeforeFirstFrame: true,
    });

    expect(byId(draw(BACKED), "ws-auto-update-pitch")).toBeUndefined();
    applyUpdates(shell, { [BACKED]: OUT_OF_DATE }, true);
    const tab = draw(BACKED);
    expect(byId(tab, "ws-auto-update-pitch")).toBeUndefined();
    expect(byId(tab, "ws-auto-update-choices")).toBeDefined();
  });

  it("shows a workspace's own setting rather than the pitch, though the offer is unanswered", async () => {
    // Studio's switch, turned on and off again in Studio settings, never answers the offer.
    const { draw } = await harness({ promptDecision: null });
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_auto_update_enabled: true,
        update_window: "1:00 AM-4:00 AM",
        auto_update_override: {
          mode: "custom_window",
          window_start_hour: 1,
          window_end_hour: 4,
        },
      },
      [UNBACKED]: OUT_OF_DATE,
    });

    const tab = draw(BACKED);
    expect(byId(tab, "ws-auto-update-pitch")).toBeUndefined();
    expect(
      attrsOf(byId(tab, "update-window-start-workspace") as AnyVnode).value,
    ).toBe("1");
    expect(byId(draw(UNBACKED), "ws-auto-update-pitch")).toBeDefined();
  });

  it("shows the three choices once the offer is answered, with Studio's own state", async () => {
    const { draw } = await harness({ promptDecision: "declined" });

    const tab = draw(BACKED);
    expect(allText(byId(tab, "ws-auto-update-choices"))).toContain(
      "Use Studio settings Off No automatic updates; you'll be asked when one is available.",
    );
    const checked = collectVnodes(byId(tab, "ws-auto-update-choices")).find(
      (vnode) =>
        attrsOf(vnode)["data-auto-update-choice"] !== undefined &&
        collectVnodes(vnode).some(
          (inner) =>
            attrsOf(inner).type === "radio" && attrsOf(inner).checked === true,
        ),
    );
    expect(attrsOf(checked as AnyVnode)["data-auto-update-choice"]).toBe(
      "studio",
    );
  });

  it.each([
    [
      "custom_window",
      { mode: "custom_window", window_start_hour: 2, window_end_hour: 5 },
    ],
    [
      "manual",
      { mode: "manual", window_start_hour: null, window_end_hour: null },
    ],
  ] as const)(
    "writes the workspace's own %s setting, starting a custom window from Studio's",
    async (choice, body) => {
      const { draw, bodies } = await harness({ promptDecision: "declined" });

      choose(draw(BACKED), choice);
      await settleAll();

      expect(bodies[`/ui/api/updates/${BACKED}/auto-update`]).toEqual(body);
    },
  );

  it("puts a workspace with its own setting back on Studio's", async () => {
    const { draw, bodies } = await harness({ promptDecision: "declined" });
    republish({
      [BACKED]: {
        ...OUT_OF_DATE,
        is_auto_update_enabled: true,
        update_window: "1:00 AM-4:00 AM",
        auto_update_override: {
          mode: "custom_window",
          window_start_hour: 1,
          window_end_hour: 4,
        },
      },
    });

    const tab = draw(BACKED);
    expect(
      attrsOf(byId(tab, "update-window-start-workspace") as AnyVnode).value,
    ).toBe("1");
    choose(tab, "studio");
    await settleAll();

    expect(bodies[`/ui/api/updates/${BACKED}/auto-update`]).toEqual({
      mode: null,
      window_start_hour: null,
      window_end_hour: null,
    });
  });

  it("leaves a press that is still out pending when the setting is changed meanwhile", async () => {
    const { draw, requests } = await harness({
      promptDecision: "declined",
      respond: (url) =>
        url.endsWith("/schedule")
          ? new Promise<Response>(() => undefined)
          : Promise.resolve(jsonResponse({})),
    });
    press(draw(BACKED), "Schedule update");

    choose(draw(BACKED), "manual");
    await settleAll();

    expect(requests).toEqual([
      `/ui/api/updates/${BACKED}/schedule`,
      `/ui/api/updates/${BACKED}/auto-update`,
    ]);
    const tab = draw(BACKED);
    expect(attrsOf(byId(tab, "ws-update-now") as AnyVnode).disabled).toBe(true);
    // Its button still spins: a spinner in place of its words.
    expect(allText(byId(tab, "ws-update-schedule")?.children)).toBe("");
  });

  it("says a refused write beside the choices, leaving the update's own status as it was", async () => {
    const { draw } = await harness({
      promptDecision: "declined",
      respond: (url) =>
        Promise.resolve(
          url.endsWith("/auto-update")
            ? jsonResponse({ error: "Invalid auto-update setting" }, 400)
            : jsonResponse({}),
        ),
    });
    republish({
      [BACKED]: { ...OUT_OF_DATE, is_backup_configured: true, needs_sign_in: true },
    });
    const signInLine = allText(byId(draw(BACKED), "update-status-line"));

    choose(draw(BACKED), "manual");
    await settleAll();

    const tab = draw(BACKED);
    expect(allText(byId(tab, "ws-auto-update-error"))).toContain(
      "Invalid auto-update setting",
    );
    expect(byId(tab, "ws-update-error")).toBeUndefined();
    expect(allText(byId(tab, "update-status-line"))).toBe(signInLine);
  });
});
