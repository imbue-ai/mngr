import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "../shell/shell-state";
import { RecoveryPage } from "./RecoveryPage";
import { RecoveryModel, type LifecycleDeps, type RecoveryInfo } from "../../models/backups";

const RECOVERY_ROUTE = "/agents/agent-aa11/recovery?return_to=%2Fgoto%2Fhost-bb22%2F&intent=start";
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
  ssh_command: "",
  is_host_offline: false,
  is_backend_unreachable: false,
  provider_label: "",
  unreachable_reason: "",
};

type UpdateVnode = Parameters<NonNullable<typeof RecoveryPage.onupdate>>[0];
type RecoveryState = UpdateVnode["state"];

interface PageOverrides {
  model?: Partial<RecoveryModel>;
  state?: Partial<RecoveryState>;
}

/** The page as a click-through left it: a model holding one reading of the
 * machine, and the destination the click-through carried. */
function pageShowing(info: RecoveryInfo | null, overrides: PageOverrides = {}): UpdateVnode {
  const model = Object.assign(new RecoveryModel("agent-aa11", IDLE_DEPS), { info }, overrides.model ?? {});
  const state: RecoveryState = {
    model,
    returnTo: RETURN_TO,
    hasReturned: false,
    isDispatchSettled: true,
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
    const vnode = pageShowing(ANSWERING, { model: { restartError: "Start step of host restart failed" } });

    runUpdate(vnode);

    expect(routeSets).toEqual(["/workspace/host-bb22"]);
    expect(vnode.state.hasReturned).toBe(true);
  });

  it("stays put while the restart is still running", () => {
    const { routeSets } = withShell();
    // The tracker reports restarting; the last good reading is still healthy.
    runUpdate(pageShowing(ANSWERING, { model: { isRestartRunning: true } }));
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
