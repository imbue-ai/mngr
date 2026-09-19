import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "../shell/shell-state";
import { RecoveryPage } from "./RecoveryPage";
import {
  RecoveryModel,
  type LifecycleDeps,
  type RecoveryInfo,
} from "../../models/backups";
import {
  attrsOf,
  collectText,
  collectVnodes,
  workspacesMessage,
  type AnyVnode,
} from "../../testing";
import { MAINTENANCE_MESSAGE, RETIRED_MESSAGE } from "./landing-controls";

const RECOVERY_ROUTE =
  "/agents/agent-aa11/recovery?return_to=%2Fgoto%2Fhost-bb22%2F&intent=start";
const RETURN_TO = "/goto/host-bb22/";

/** Deps that answer nothing and schedule nothing: these tests drive a state the
 * page has already reached, they do not fetch one. */
const IDLE_DEPS: LifecycleDeps = {
  getJson: async () => null,
  postJson: async () => ({ status: 500, json: null }),
  deleteResource: async () => 500,
  openEventSource: () => ({ close: () => {}, onmessage: null, onerror: null }),
  schedule: () => {},
  redraw: () => {},
};

const ANSWERING: RecoveryInfo = {
  agent_id: "agent-aa11",
  workspace_name: "alpha",
  health: "healthy",
  health_error: "",
  recovery_kind: null,
  ssh_command: "",
  is_host_offline: false,
  device_environment: "NONE",
  is_backend_unreachable: false,
  provider_label: "",
  unreachable_reason: "",
  is_device_cannot_connect: false,
  device_error_detail: "",
};

type UpdateVnode = Parameters<NonNullable<typeof RecoveryPage.onupdate>>[0];
type InitVnode = Parameters<NonNullable<typeof RecoveryPage.oninit>>[0];
type RecoveryState = UpdateVnode["state"];

interface PageOverrides {
  model?: Partial<RecoveryModel>;
  state?: Partial<RecoveryState>;
}

/** The page as a click-through left it: a model holding one reading of the
 * machine, and the destination the click-through carried. */
function pageShowing(
  info: RecoveryInfo | null,
  overrides: PageOverrides = {},
): UpdateVnode {
  const model = Object.assign(
    new RecoveryModel("agent-aa11", IDLE_DEPS),
    { info },
    overrides.model ?? {},
  );
  const state: RecoveryState = {
    model,
    returnTo: RETURN_TO,
    hasReturned: false,
    isDispatchSettled: true,
    heldMessage: null,
    ...overrides.state,
  };
  return { state } as UpdateVnode;
}

/** Register a shell on the recovery route, capturing where the page navigates. */
function withShell(): { shell: ShellState; routeSets: string[] } {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  vi.spyOn(m.route, "get").mockReturnValue(RECOVERY_ROUTE);
  const routeSets: string[] = [];
  vi.spyOn(m.route, "set").mockImplementation(((path: string) => {
    routeSets.push(path);
  }) as typeof m.route.set);
  return { shell, routeSets };
}

function runUpdate(vnode: UpdateVnode): void {
  (RecoveryPage.onupdate as (v: UpdateVnode) => void)(vnode);
}

/** What the page puts on screen for the state it holds. */
function render(vnode: UpdateVnode): m.Vnode {
  return (RecoveryPage.view as (v: UpdateVnode) => m.Vnode)(vnode);
}

/** The recovery panel in a rendered page, found by the panel's own id so the
 * search does not depend on how deep the page wraps it. */
function findPanel(rendered: m.Vnode): AnyVnode | undefined {
  return collectVnodes(rendered).find(
    (node) => typeof attrsOf(node).panelId === "string",
  );
}

/** The attrs the page hands the recovery panel. */
function panelAttrs(vnode: UpdateVnode): {
  onEnterMachine?: (() => void) | null;
} {
  const panel = findPanel(render(vnode));
  if (panel === undefined)
    throw new Error("the page rendered no recovery panel");
  return attrsOf(panel) as { onEnterMachine?: (() => void) | null };
}

afterEach(() => {
  vi.restoreAllMocks();
  clearAppContextForTests();
});

describe("recovery page return", () => {
  it("enters the machine once it answers, even though the restart failed", () => {
    // The reported dead end: a start that errored (a stale host-key pin, say)
    // while the machine came back anyway. The card then says "nothing further
    // is needed here" on a surface with no way into the machine it is talking
    // about, and the reader has to go Home and back in by hand.
    const { routeSets } = withShell();
    const vnode = pageShowing(ANSWERING, {
      model: { recoveryError: "Start step of host restart failed" },
    });

    runUpdate(vnode);

    expect(routeSets).toEqual(["/workspace/host-bb22"]);
    expect(vnode.state.hasReturned).toBe(true);
  });

  it("stays put while the restart is still running", () => {
    const { routeSets } = withShell();
    // The tracker reports recovering; the last good reading is still healthy.
    runUpdate(pageShowing(ANSWERING, { model: { isRecoveryRunning: true } }));
    expect(routeSets).toEqual([]);
  });

  it("stays put on a machine that is not answering yet", () => {
    const { routeSets } = withShell();
    runUpdate(pageShowing({ ...ANSWERING, health: "stuck" }));
    expect(routeSets).toEqual([]);
  });

  it("stays put on a stopped machine, which reads healthy but is nowhere to send anyone", () => {
    const { routeSets } = withShell();
    runUpdate(pageShowing({ ...ANSWERING, is_host_offline: true }));
    expect(routeSets).toEqual([]);
  });

  it("stays put until the click-through's own restart has been asked for", () => {
    // ?intent=start/restart means the reader asked for something; the health
    // read before that dispatch describes the state it was meant to change,
    // and leaving on it would leave without doing it.
    const { routeSets } = withShell();
    runUpdate(pageShowing(ANSWERING, { state: { isDispatchSettled: false } }));
    expect(routeSets).toEqual([]);
  });

  it("does not navigate out from under a bug report opened over the page", () => {
    // Report a problem floats the help form on this page. The machine coming
    // back mid-sentence must not take the form with it.
    const { shell, routeSets } = withShell();
    shell.pageRouteBehindOverlay = RECOVERY_ROUTE;

    const vnode = pageShowing(ANSWERING);
    runUpdate(vnode);

    expect(routeSets).toEqual([]);
    expect(vnode.state.hasReturned).toBe(false);
  });

  it("returns only once", () => {
    const { routeSets } = withShell();
    const vnode = pageShowing(ANSWERING);

    runUpdate(vnode);
    runUpdate(vnode);

    expect(routeSets).toEqual(["/workspace/host-bb22"]);
  });

  it("goes nowhere when the page was opened without a destination", () => {
    // A deep link into recovery carries no ?return_to; there is nowhere the
    // reader was headed, so the card simply reports the machine's state.
    const { routeSets } = withShell();
    runUpdate(pageShowing(ANSWERING, { state: { returnTo: null } }));
    expect(routeSets).toEqual([]);
  });
});

describe("recovery page exit button", () => {
  it("offers the way into a machine that is answering, with no destination to return to", () => {
    // The dead end this exists for: a page reached without ?return_to, on a
    // machine that has since come back. The automatic return has nowhere to go
    // -- correctly, the reader was never headed anywhere -- so without a button
    // the card says "nothing further is needed here" over a machine the reader
    // cannot reach from it.
    const { routeSets } = withShell();
    const vnode = pageShowing(ANSWERING, { state: { returnTo: null } });

    const enter = panelAttrs(vnode).onEnterMachine;
    expect(enter).not.toBeNull();
    enter?.();

    // The machine itself, since the click-through named nowhere else.
    expect(routeSets).toEqual(["/workspace/agent-aa11"]);
  });

  it("lands the click where waiting would have landed the reader", () => {
    // Two ways out of one page. A reader who clicks rather than waiting for the
    // return must not end up somewhere else.
    const { routeSets: clicked } = withShell();
    panelAttrs(pageShowing(ANSWERING)).onEnterMachine?.();
    vi.restoreAllMocks();
    clearAppContextForTests();
    const { routeSets: waited } = withShell();
    runUpdate(pageShowing(ANSWERING));

    // Pinned, not just compared: two paths that both went nowhere would agree.
    expect(clicked).toEqual(["/workspace/host-bb22"]);
    expect(clicked).toEqual(waited);
  });

  it("withholds the button over a machine that is not answering", () => {
    // The reason the card has no exit anywhere else: before the machine
    // answers, this button would name a destination known not to work.
    withShell();
    expect(
      panelAttrs(pageShowing({ ...ANSWERING, health: "stuck" })).onEnterMachine,
    ).toBeNull();
    expect(
      panelAttrs(pageShowing({ ...ANSWERING, is_host_offline: true }))
        .onEnterMachine,
    ).toBeNull();
    expect(
      panelAttrs(pageShowing(ANSWERING, { model: { isRecoveryRunning: true } }))
        .onEnterMachine,
    ).toBeNull();
  });
});

describe("recovery page held machine", () => {
  it("says the machine is under maintenance instead of offering the recovery panel", () => {
    // ?intent=start on a machine an operator holds: the start would only be
    // refused, so the page shows the connector's own sentence and dispatches
    // nothing (oninit settles the dispatch without one).
    withShell();
    const held = pageShowing(
      { ...ANSWERING, is_host_offline: true },
      { state: { heldMessage: MAINTENANCE_MESSAGE } },
    );
    const rendered = render(held);

    const text = collectText(rendered).join(" ");
    expect(text).toContain("Machine maintenance");
    expect(text).toContain(MAINTENANCE_MESSAGE);
    expect(findPanel(rendered)).toBeUndefined();
  });

  it("hands the page back to the recovery panel once the held machine answers", () => {
    // The operator's start landed while the reader was parked here: the
    // maintenance sentence is stale and the panel offers the machine.
    withShell();
    const back = pageShowing(ANSWERING, {
      state: { heldMessage: MAINTENANCE_MESSAGE },
    });
    const rendered = render(back);

    expect(collectText(rendered).join(" ")).not.toContain(
      "Machine maintenance",
    );
    expect(findPanel(rendered)).toBeDefined();
  });
});

/** Open the page the way a machines-list click-through does: the route names
 * the machine and the intent, the store holds the machine's entry with the
 * given stop kind, and the model's network calls are stubbed out. */
async function initPageOn(
  intent: string,
  stopKind: string,
): Promise<{
  state: InitVnode["state"];
  dispatch: ReturnType<typeof vi.spyOn>;
}> {
  const { shell } = withShell();
  const base = workspacesMessage().workspaces[0];
  shell.stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({
      workspaces: [{ ...base, liveness: "STOPPED", stop_kind: stopKind }],
    }),
  );
  const params: Record<string, string> = {
    agentId: "agent-aa11",
    intent,
    return_to: RETURN_TO,
  };
  vi.spyOn(m.route, "param").mockImplementation(
    ((key: string) => params[key]) as typeof m.route.param,
  );
  vi.spyOn(RecoveryModel.prototype, "load").mockResolvedValue(undefined);
  const dispatch = vi
    .spyOn(RecoveryModel.prototype, "dispatchRecovery")
    .mockResolvedValue(undefined);
  const vnode = { state: {} } as InitVnode;
  (RecoveryPage.oninit as (v: InitVnode) => void)(vnode);
  await vi.waitFor(() => expect(vnode.state.isDispatchSettled).toBe(true));
  return { state: vnode.state, dispatch };
}

describe("recovery page click-through on a held machine", () => {
  it("dispatches no start for a machine an operator holds, and says so", async () => {
    // ?intent=start on a maintenance-held machine: the connector would only
    // refuse it, so the page shows the connector's sentence instead of asking.
    const { state, dispatch } = await initPageOn("start", "maintenance");

    expect(state.heldMessage).toBe(MAINTENANCE_MESSAGE);
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("dispatches no start for a retired machine, and says it never starts again", async () => {
    // The retired verdict is final, so the page answers with the retired
    // sentence (the one the badge, the band and the connector use), not the
    // maintenance one that promises the machine back shortly.
    const { state, dispatch } = await initPageOn("start", "retired");

    expect(state.heldMessage).toBe(RETIRED_MESSAGE);
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("starts an idle-stopped machine, which is the owner's to start", async () => {
    const { state, dispatch } = await initPageOn("start", "idle");

    expect(state.heldMessage).toBeNull();
    expect(dispatch).toHaveBeenCalledWith("start");
  });

  it("holds only the start: a restart of the held machine is dispatched as asked", async () => {
    // The hold gates the idempotent start a click into a stopped machine asks
    // for; the full bounce is the user's explicit request, and the connector
    // answers it in its own words.
    const { state, dispatch } = await initPageOn("restart", "maintenance");

    expect(state.heldMessage).toBeNull();
    expect(dispatch).toHaveBeenCalledWith("restart");
  });
});
