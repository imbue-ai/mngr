import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, getAppContext, registerAppContext } from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import type { SettingsGroup } from "../../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { ShellState } from "../../shell/shell-state";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, collectVnodes, jsonResponse, settle } from "../../../testing";
import { SettingsGroups } from "./SettingsGroups";

// Two machines, one without backups: the pane's state must be keyed on which
// is being drawn.
const UNBACKED = "agent-" + "a".repeat(8);
const BACKED = "agent-" + "b".repeat(8);
const NO_BACKUP_QUESTION = "This machine has no backups.";

const OUT_OF_DATE: UiWorkspaceUpdate = {
  availability: "OUT_OF_DATE",
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  is_version_from_label: false,
  activity: "IDLE",
};

interface Harness {
  /** Draw the pane for one machine against the same component instance, as a
   * route change would. */
  draw: (agentId: string, group?: SettingsGroup) => m.Children;
  requests: string[];
}

/** The Updates group of one drawn pane; the destroy and unlink dialogs carry a
 * "Cancel" of their own. */
function updatesGroup(root: m.Children): AnyVnode {
  const group = collectVnodes(root).find((vnode) => attrsOf(vnode).id === "ws-updates-group");
  if (group === undefined) throw new Error("the Updates group was not drawn");
  return group;
}

function pressableWithLabel(node: AnyVnode, label: string): AnyVnode | undefined {
  return collectVnodes(node).find(
    (vnode) => typeof attrsOf(vnode).onclick === "function" && allText(vnode.children).includes(label),
  );
}

/** The specific-version field of one drawn Updates group. */
function overrideField(node: AnyVnode): AnyVnode {
  const field = collectVnodes(node).find((vnode) => attrsOf(vnode).id === "update-override-input");
  if (field === undefined) throw new Error("the specific-version field was not drawn");
  return field;
}

/** Type into that field: its oninput is the only writer of the typed ref. */
function typeOverride(node: AnyVnode, value: string): void {
  const oninput = attrsOf(overrideField(node)).oninput as (event: InputEvent) => void;
  oninput({ target: { value } } as unknown as InputEvent);
}

function press(node: AnyVnode, label: string): void {
  const pressable = pressableWithLabel(node, label);
  if (pressable === undefined) throw new Error(`no "${label}" to press`);
  if (attrsOf(pressable).disabled === true) throw new Error(`"${label}" is disabled`);
  (attrsOf(pressable).onclick as () => void)();
}

function harness(
  respond: (url: string, init?: RequestInit) => Promise<Response> = () => Promise.resolve(jsonResponse({})),
): Harness {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  shell.stores.updates.applyUpdatesMessage({
    type: "workspace_updates",
    updates: {
      [UNBACKED]: { ...OUT_OF_DATE, is_backup_configured: false },
      [BACKED]: { ...OUT_OF_DATE, is_backup_configured: true },
    },
    update_window: "2:00 AM-5:00 AM",
  });
  const requests: string[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    requests.push(url);
    return respond(url, init);
  });
  // No ?override=1, which would open the specific-version field. A successful
  // dispatch enters the machine and redraws; there is no mount for either.
  vi.spyOn(m.route, "get").mockReturnValue("/workspace/x/options?tab=settings&group=updates");
  vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);

  const models = new Map<string, WorkspaceOptionsModel>();
  const instance = SettingsGroups() as unknown as m.Component;
  function draw(agentId: string, group: SettingsGroup = "updates"): m.Children {
    let model = models.get(agentId);
    if (model === undefined) {
      model = new WorkspaceOptionsModel(agentId);
      model.data = {
        agent_id: agentId,
        host_id: `host-${agentId}`,
        name: agentId,
        color: "#aabbcc",
        palette: { blue: "#aabbcc", pink: "#e8a7a8" },
        is_stale: false,
        is_leased_imbue_cloud: false,
        has_account: false,
        account_email: "",
        account_display_name: null,
        account_profile_picture_url: null,
        current_account: null,
        accounts: [],
        app_services: [],
        service_labels: {},
        whole_service: "",
      };
      model.lastSavedColor = model.data.color;
      models.set(agentId, model);
    }
    const attrs = { model, selectedGroup: group, onSelectGroup: () => undefined };
    const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
    return (instance.view as unknown as (v: m.Vnode) => m.Children).call(instance, vnode);
  }
  return { draw, requests };
}

/** A dispatch answers over several promise hops: the request, its body parse,
 * the store's lock release, then the pane's own callback. */
async function settleDispatch(): Promise<void> {
  for (let index = 0; index < 5; index += 1) await settle();
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

describe("the Updates settings group's no-backup question", () => {
  it("holds a press on a machine without backups and asks in place of the controls", () => {
    const { draw, requests } = harness();

    press(updatesGroup(draw(UNBACKED)), "Update now");

    const asked = updatesGroup(draw(UNBACKED));
    expect(allText(asked)).toContain(NO_BACKUP_QUESTION);
    // A live "Update now" under "Go ahead?" would be a second way to answer.
    expect(pressableWithLabel(asked, "Update now")).toBeUndefined();
    expect(requests).toEqual([]);
  });

  it("dispatches only once the question is answered, and only for the machine it asked about", async () => {
    const { draw, requests } = harness();

    press(updatesGroup(draw(UNBACKED)), "Update now");
    press(updatesGroup(draw(UNBACKED)), "Go ahead without backups");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${UNBACKED}/now`]);
    expect(allText(updatesGroup(draw(UNBACKED)))).not.toContain(NO_BACKUP_QUESTION);
  });

  it("takes back the press when the question is cancelled", () => {
    const { draw, requests } = harness();

    press(updatesGroup(draw(UNBACKED)), "Update now");
    press(updatesGroup(draw(UNBACKED)), "Cancel");

    const settled = updatesGroup(draw(UNBACKED));
    expect(allText(settled)).not.toContain(NO_BACKUP_QUESTION);
    expect(pressableWithLabel(settled, "Update now")).toBeDefined();
    expect(requests).toEqual([]);
  });

  it("never puts one machine's question in front of another, nor answers it for one", async () => {
    // A route change between machines preserves this instance, so A's question
    // must neither stand over B nor dispatch to it.
    const { draw, requests } = harness();
    press(updatesGroup(draw(UNBACKED)), "Update now");

    const other = updatesGroup(draw(BACKED));
    expect(allText(other)).not.toContain(NO_BACKUP_QUESTION);
    press(other, "Update now");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/now`]);
    // Unanswered, not discarded: the machine it was raised for still has it.
    expect(allText(updatesGroup(draw(UNBACKED)))).toContain(NO_BACKUP_QUESTION);
  });

  it("keeps a dispatch's spinner and its refusal on the machine the dispatch was made for", async () => {
    // A dispatch can take minutes, so the reader may switch machines while it is out.
    let answer: ((response: Response) => void) | null = null;
    const { draw, requests } = harness(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    press(updatesGroup(draw(BACKED)), "Update now");
    expect(requests).toEqual([`/ui/api/updates/${BACKED}/now`]);

    const other = updatesGroup(draw(UNBACKED));
    const otherButton = pressableWithLabel(other, "Update now");
    expect(otherButton).toBeDefined();
    expect(attrsOf(otherButton as AnyVnode).disabled).toBe(false);

    (answer as unknown as (response: Response) => void)(
      jsonResponse({ error: "An update is already running in this machine." }, 409),
    );
    await settleDispatch();

    expect(allText(updatesGroup(draw(UNBACKED)))).not.toContain("An update is already running");
    expect(allText(updatesGroup(draw(BACKED)))).toContain("An update is already running");
  });

  it("quotes the machine's own verdict under the refusal", async () => {
    // The reason a wedged machine gives is the only thing that tells the reader
    // retrying will not help, so it has to survive all the way onto the pane.
    const { draw } = harness(() =>
      Promise.resolve(
        jsonResponse(
          {
            error: "Couldn't start the update agent in this machine.",
            detail: "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']",
          },
          502,
        ),
      ),
    );

    press(updatesGroup(draw(BACKED)), "Update now");
    await settleDispatch();

    const refused = allText(updatesGroup(draw(BACKED)));
    expect(refused).toContain("Couldn't start the update agent in this machine.");
    expect(refused).toContain("Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']");
  });
});

describe("the Updates settings group's scheduled update", () => {
  it("asks the no-backup question for a schedule press too, and arms the schedule once answered", async () => {
    const { draw, requests } = harness();

    press(updatesGroup(draw(UNBACKED)), "Schedule update");
    expect(allText(updatesGroup(draw(UNBACKED)))).toContain(NO_BACKUP_QUESTION);
    expect(requests).toEqual([]);

    press(updatesGroup(draw(UNBACKED)), "Go ahead without backups");
    await settleDispatch();

    // A held schedule press arms a schedule, not a run.
    expect(requests).toEqual([`/ui/api/updates/${UNBACKED}/schedule`]);
  });

  it("offers to cancel a scheduled machine's run in place of scheduling it again", async () => {
    const { draw, requests } = harness();
    getAppContext().stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: { [BACKED]: { ...OUT_OF_DATE, is_backup_configured: true, is_scheduled: true } },
      update_window: "2:00 AM-5:00 AM",
    });

    const group = updatesGroup(draw(BACKED));
    expect(allText(group)).toContain("Scheduled to update in the next update window (2:00 AM-5:00 AM)");
    expect(pressableWithLabel(group, "Schedule update")).toBeUndefined();
    press(group, "Cancel schedule");
    await settleDispatch();

    expect(requests).toEqual([`/ui/api/updates/${BACKED}/schedule/cancel`]);
  });
});

describe("the Updates settings group's specific-version field", () => {
  it("keeps a typed ref on the machine it was typed for", () => {
    const { draw } = harness();

    // The disclosure is one toggle for the pane, so it stays open across the switch.
    press(updatesGroup(draw(UNBACKED)), "Update to a specific version");
    typeOverride(updatesGroup(draw(UNBACKED)), "upstream/some-branch");

    const other = updatesGroup(draw(BACKED));
    expect(attrsOf(overrideField(other)).value).toBe("");
    // Nothing was typed for this machine, so there is nothing to send.
    const otherDispatch = pressableWithLabel(other, "Update to this version");
    expect(otherDispatch).toBeDefined();
    expect(attrsOf(otherDispatch as AnyVnode).disabled).toBe(true);

    // Kept for the machine it was typed for, not wiped on every switch.
    expect(attrsOf(overrideField(updatesGroup(draw(UNBACKED)))).value).toBe("upstream/some-branch");
  });
});

describe("the General settings group's color picker", () => {
  function swatch(root: m.Children, hex: string): AnyVnode {
    const found = collectVnodes(root).find((vnode) => attrsOf(vnode).hex === hex);
    if (found === undefined) throw new Error(`no ${hex} swatch was drawn`);
    return found;
  }

  function hexInput(root: m.Children): AnyVnode {
    const found = collectVnodes(root).find((vnode) => attrsOf(vnode).id === "color-hex-input");
    if (found === undefined) throw new Error("the hex input was not drawn");
    return found;
  }

  /** A pane whose color saves record the sent color and stay unanswered until released, oldest first. */
  function heldSaveHarness(): { draw: Harness["draw"]; sentColors: string[]; releaseOldest: () => void } {
    const sentColors: string[] = [];
    const heldReplies: (() => void)[] = [];
    const { draw } = harness((_url, init) => {
      sentColors.push((JSON.parse(String(init?.body)) as { color: string }).color);
      return new Promise<Response>((resolve) => heldReplies.push(() => resolve(jsonResponse({}))));
    });
    const releaseOldest = (): void => {
      const release = heldReplies.shift();
      if (release === undefined) throw new Error("no color save is in flight");
      release();
    };
    return { draw, sentColors, releaseOldest };
  }

  it("keeps the swatches live and sends each pick at once while a save is still running", async () => {
    const { draw, sentColors, releaseOldest } = heldSaveHarness();

    (attrsOf(swatch(draw(UNBACKED, "general"), "#e8a7a8")).onclick as () => void)();
    const whileSaving = draw(UNBACKED, "general");

    expect(attrsOf(swatch(whileSaving, "#e8a7a8")).selected).toBe(true);
    expect(attrsOf(swatch(whileSaving, "#aabbcc")).disabled).toBe(false);
    expect(allText(whileSaving)).not.toContain("Saving");

    (attrsOf(hexInput(whileSaving)).oninput as (event: unknown) => void)({ target: { value: "#12" } });
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#e8a7a8")).selected).toBe(true);

    (attrsOf(swatch(whileSaving, "#aabbcc")).onclick as () => void)();
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#aabbcc")).selected).toBe(true);
    expect(sentColors).toEqual(["#e8a7a8", "#aabbcc"]);

    releaseOldest();
    await settleDispatch();
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#aabbcc")).selected).toBe(true);
  });

  it("saves a valid hex draft on blur and then shows the normalized pick", () => {
    const { draw, sentColors } = heldSaveHarness();

    (attrsOf(hexInput(draw(UNBACKED, "general"))).oninput as (event: unknown) => void)({ target: { value: "#123" } });
    (attrsOf(hexInput(draw(UNBACKED, "general"))).onblur as () => void)();
    const afterBlur = draw(UNBACKED, "general");

    expect(sentColors).toEqual(["#112233"]);
    expect(attrsOf(hexInput(afterBlur)).value).toBe("#112233");
    expect(allText(afterBlur)).not.toContain("not valid");
  });
});
