import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import type { UiWorkspaceUpdate } from "../../channel/messages";
import { ShellState } from "../shell/shell-state";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  collectVnodes,
  jsonResponse,
  settle,
} from "../../testing";
import { UpdateModal } from "./UpdateModal";

const AGENT = "agent-" + "c".repeat(8);

const OUT_OF_DATE: UiWorkspaceUpdate = {
  availability: "OUT_OF_DATE",
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  is_version_from_label: false,
  activity: "IDLE",
  is_backup_configured: true,
};

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

function press(root: m.Children, label: string): void {
  const pressable = pressableWithLabel(root, label);
  if (pressable === undefined) throw new Error(`no "${label}" to press`);
  (attrsOf(pressable).onclick as () => void)();
}

function harness(
  respond: (url: string) => Promise<Response>,
  update: UiWorkspaceUpdate = OUT_OF_DATE,
): Harness {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  shell.stores.updates.applyUpdatesMessage({
    type: "workspace_updates",
    updates: { [AGENT]: update },
    update_window: "2:00 AM-5:00 AM",
  });
  const requests: string[] = [];
  vi.stubGlobal("fetch", (url: string) => {
    requests.push(url);
    return respond(url);
  });
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const onClose = vi.fn();
  const instance = UpdateModal() as unknown as m.Component;
  function draw(): m.Children {
    const attrs = { agentId: AGENT, workspaceName: "box", onClose };
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
          { error: "An update is already running in this machine." },
          409,
        ),
      ),
    );
    press(draw(), "Update now");
    await settleDispatch();

    expect(onClose).not.toHaveBeenCalled();
    expect(allText(draw())).toContain(
      "An update is already running in this machine.",
    );
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
      ...OUT_OF_DATE,
      verdict: "UPDATED",
      current_version: "minds-v0.3.9",
      success_note_version: "minds-v0.4.1",
    });

    expect(allText(draw())).toContain(
      "This machine was updated to minds-v0.4.1.",
    );
  });

  it("points a run that ended short at its own chat rather than offering machinery of its own", () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse({})), {
      ...OUT_OF_DATE,
      verdict: "NEEDS_RECREATION",
      chat_agent_name: "update-fox",
    });
    const root = draw();

    expect(allText(root)).toContain(
      "Check in with the update agent in the update-fox tab",
    );
    expect(pressableWithLabel(root, "Migrate")).toBeUndefined();
    expect(pressableWithLabel(root, "Dismiss")).toBeDefined();
  });
});

describe("the update modal over a machine too old to update in place", () => {
  const TOO_OLD: UiWorkspaceUpdate = {
    ...OUT_OF_DATE,
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

    press(draw(), "Create a new machine");

    expect(onClose).toHaveBeenCalled();
    expect(routeSet).toHaveBeenCalledWith("/create");
  });
});
