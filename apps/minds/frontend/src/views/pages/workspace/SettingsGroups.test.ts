import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  getAppContext,
  registerAppContext,
} from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import type {
  SettingsGroup,
  WorkspaceMachineSizeData,
  WorkspaceOptionsData,
} from "../../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { ShellState } from "../../shell/shell-state";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  byId,
  classesOf,
  collectVnodes,
  jsonResponse,
  renderDeep,
  settle,
} from "../../../testing";
import { CopyField } from "../../components/Layout";
import { SettingsGroups } from "./SettingsGroups";

// Two machines, one without backups: the pane's state must be keyed on which
// is being drawn.
const UNBACKED = "agent-" + "a".repeat(8);
const BACKED = "agent-" + "b".repeat(8);

const OUT_OF_DATE: UiWorkspaceUpdate = {
  availability: "OUT_OF_DATE",
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  activity: "IDLE",
};

interface Harness {
  /** Draw the pane for one machine against the same component instance, as a
   * route change would. */
  draw: (
    agentId: string,
    group?: SettingsGroup,
    data?: Partial<WorkspaceOptionsData>,
  ) => m.Children;
  requests: string[];
  /** The per-machine model the pane draws against, keyed by agent id. */
  models: Map<string, WorkspaceOptionsModel>;
}

function pressableWithLabel(
  node: AnyVnode,
  label: string,
): AnyVnode | undefined {
  return collectVnodes(node).find(
    (vnode) =>
      typeof attrsOf(vnode).onclick === "function" &&
      allText(vnode.children).includes(label),
  );
}

function press(node: AnyVnode, label: string): void {
  const pressable = pressableWithLabel(node, label);
  if (pressable === undefined) throw new Error(`no "${label}" to press`);
  if (attrsOf(pressable).disabled === true)
    throw new Error(`"${label}" is disabled`);
  (attrsOf(pressable).onclick as () => void)();
}

function harness(
  respond: (url: string, init?: RequestInit) => Promise<Response> = () =>
    Promise.resolve(jsonResponse({})),
  sshCommandByAgent: Record<string, string> = {},
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
  // A successful press routes and redraws; there is no mount for either.
  vi.spyOn(m.route, "get").mockReturnValue(
    "/workspace/x/options?tab=settings&group=updates",
  );
  vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);

  const models = new Map<string, WorkspaceOptionsModel>();
  const instance = SettingsGroups() as unknown as m.Component;
  function draw(
    agentId: string,
    group: SettingsGroup = "updates",
    data: Partial<WorkspaceOptionsData> = {},
  ): m.Children {
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
        is_account_owned_provider: false,
        leased_owner_email: "",
        has_account: false,
        account_email: "",
        account_display_name: null,
        account_profile_picture_url: null,
        current_account: null,
        accounts: [],
        app_services: [],
        service_labels: {},
        whole_service: "",
        public_email_domains: [],
        ssh_command: sshCommandByAgent[agentId] ?? "",
        ...data,
      };
      model.lastSavedColor = model.data.color;
      models.set(agentId, model);
    }
    const attrs = {
      model,
      selectedGroup: group,
      onSelectGroup: () => undefined,
    };
    const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
    return (instance.view as unknown as (v: m.Vnode) => m.Children).call(
      instance,
      vnode,
    );
  }
  return { draw, requests, models };
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

describe("the Updates entry in the settings nav", () => {
  function navDot(root: m.Children): AnyVnode | undefined {
    return byId(root, "updates-nav-dot");
  }

  it("wears the titlebar pill's colour while the pill shows, and nothing otherwise", () => {
    const { draw } = harness();
    // Out of date, nothing handling it: the pill's orange dot.
    expect(classesOf(navDot(draw(BACKED)) as AnyVnode)).toContain("bg-warning");

    getAppContext().stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: { [BACKED]: { ...OUT_OF_DATE, availability: "UP_TO_DATE", verdict: "UPDATED_WITH_REBUILD_ITEMS" } },
      update_window: "2:00 AM-5:00 AM",
    });
    // A landed update with a note: good news, green.
    expect(classesOf(navDot(draw(BACKED)) as AnyVnode)).toContain("bg-success");

    getAppContext().stores.updates.applyUpdatesMessage({
      type: "workspace_updates",
      updates: { [BACKED]: { ...OUT_OF_DATE, is_scheduled: true } },
      update_window: "2:00 AM-5:00 AM",
    });
    // A schedule asks nothing, so there is no pill and no dot.
    expect(navDot(draw(BACKED))).toBeUndefined();
  });
});

describe("the Account settings group on a workspace on an account-owned provider", () => {
  /** The drawn Account section's text, whitespace collapsed, and whether it offers an Unlink. */
  function accountSection(root: m.Children): {
    text: string;
    hasUnlink: boolean;
  } {
    const section = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "account-section",
    );
    if (section === undefined)
      throw new Error("the Account section was not drawn");
    return {
      text: allText(section.children)
        .replace(/\s+/g, " ")
        .replace(/ ([.,])/g, "$1")
        .trim(),
      hasUnlink: collectVnodes(section).some(
        (vnode) => attrsOf(vnode).id === "disassociate-btn",
      ),
    };
  }

  it("says the account cannot change and names the owner, with no Unlink to press", () => {
    const { draw } = harness();
    const section = accountSection(
      draw(UNBACKED, "account", {
        is_account_owned_provider: true,
        leased_owner_email: "owner@example.com",
      }),
    );

    expect(section.text).toBe(
      "This workspace is owned by owner@example.com. This workspace runs on a provider owned by its account (Imbue Cloud, or one of the account's cloud connections), so its account link is fixed and cannot be changed.",
    );
    expect(section.hasUnlink).toBe(false);
  });

  it("names the linked account for a workspace on its own account's provider, with no Unlink", () => {
    const { draw } = harness();
    const section = accountSection(
      draw(UNBACKED, "account", {
        is_account_owned_provider: true,
        has_account: true,
        account_email: "me@example.com",
        current_account: {
          user_id: "user-1",
          email: "me@example.com",
          display_name: null,
        },
        leased_owner_email: "",
      }),
    );

    expect(section.text).toBe(
      "Linked to me@example.com. This workspace runs on a provider owned by its account (Imbue Cloud, or one of the account's cloud connections), so its account link is fixed and cannot be changed.",
    );
    expect(section.hasUnlink).toBe(false);
  });

  it("leaves the owner out when it is not known", () => {
    const { draw } = harness();
    const section = accountSection(
      draw(UNBACKED, "account", {
        is_account_owned_provider: true,
        leased_owner_email: "",
      }),
    );

    expect(section.text).toBe(
      "This workspace runs on a provider owned by its account (Imbue Cloud, or one of the account's cloud connections), so its account link is fixed and cannot be changed.",
    );
    expect(section.hasUnlink).toBe(false);
  });
});

describe("the Account settings group's link to an account", () => {
  const OFFERED = {
    user_id: "user-1",
    email: "me@example.com",
    display_name: null,
  };

  /** A pane whose PATCH of the association answers `patchStatus`, and whose options reload answers the drawn data. */
  function linkingHarness(patchStatus: number): Harness {
    let drawn: Harness | null = null;
    const pane = harness((url, init) => {
      if (init?.method === "PATCH")
        return Promise.resolve(
          jsonResponse(
            patchStatus === 200 ? {} : { error: "refused" },
            patchStatus,
          ),
        );
      const model = drawn?.models.get(UNBACKED);
      return Promise.resolve(jsonResponse(model?.data ?? {}));
    });
    drawn = pane;
    return pane;
  }

  it("leaves for the window's account's hub once the workspace belongs to the account", async () => {
    const { draw, requests } = linkingHarness(200);
    const routeSet = vi.spyOn(m.route, "set");
    const root = draw(UNBACKED, "account", { accounts: [OFFERED] });

    press(root as AnyVnode, `Link to ${OFFERED.email}`);
    await settleDispatch();

    // The association is patched, then the options reloaded (which fetches the panes' own data too).
    expect(requests.slice(0, 2)).toEqual([
      `/api/v1/workspaces/${UNBACKED}`,
      `/ui/api/workspaces/${UNBACKED}/options`,
    ]);
    expect(routeSet.mock.calls.map((call) => call[0])).toEqual([
      "/account/local/",
    ]);
  });

  it("stays put and shows the refusal when the association is refused", async () => {
    const { draw, models } = linkingHarness(400);
    const routeSet = vi.spyOn(m.route, "set");
    const root = draw(UNBACKED, "account", { accounts: [OFFERED] });

    press(root as AnyVnode, `Link to ${OFFERED.email}`);
    await settleDispatch();

    expect(routeSet).not.toHaveBeenCalled();
    expect(models.get(UNBACKED)?.accountErrorMessage).toBe("refused");
  });
});

describe("the General settings group's color picker", () => {
  function swatch(root: m.Children, hex: string): AnyVnode {
    const found = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).hex === hex,
    );
    if (found === undefined) throw new Error(`no ${hex} swatch was drawn`);
    return found;
  }

  function hexInput(root: m.Children): AnyVnode {
    const found = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "color-hex-input",
    );
    if (found === undefined) throw new Error("the hex input was not drawn");
    return found;
  }

  /** A pane whose color saves record the sent color and stay unanswered until released, oldest first. */
  function heldSaveHarness(): {
    draw: Harness["draw"];
    sentColors: string[];
    releaseOldest: () => void;
  } {
    const sentColors: string[] = [];
    const heldReplies: (() => void)[] = [];
    const { draw } = harness((_url, init) => {
      sentColors.push(
        (JSON.parse(String(init?.body)) as { color: string }).color,
      );
      return new Promise<Response>((resolve) =>
        heldReplies.push(() => resolve(jsonResponse({}))),
      );
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

    (
      attrsOf(swatch(draw(UNBACKED, "general"), "#e8a7a8"))
        .onclick as () => void
    )();
    const whileSaving = draw(UNBACKED, "general");

    expect(attrsOf(swatch(whileSaving, "#e8a7a8")).selected).toBe(true);
    expect(attrsOf(swatch(whileSaving, "#aabbcc")).disabled).toBe(false);
    expect(allText(whileSaving)).not.toContain("Saving");

    (attrsOf(hexInput(whileSaving)).oninput as (event: unknown) => void)({
      target: { value: "#12" },
    });
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#e8a7a8")).selected).toBe(
      true,
    );

    (attrsOf(swatch(whileSaving, "#aabbcc")).onclick as () => void)();
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#aabbcc")).selected).toBe(
      true,
    );
    expect(sentColors).toEqual(["#e8a7a8", "#aabbcc"]);

    releaseOldest();
    await settleDispatch();
    expect(attrsOf(swatch(draw(UNBACKED, "general"), "#aabbcc")).selected).toBe(
      true,
    );
  });

  it("saves a valid hex draft on blur and then shows the normalized pick", () => {
    const { draw, sentColors } = heldSaveHarness();

    (
      attrsOf(hexInput(draw(UNBACKED, "general"))).oninput as (
        event: unknown,
      ) => void
    )({ target: { value: "#123" } });
    (attrsOf(hexInput(draw(UNBACKED, "general"))).onblur as () => void)();
    const afterBlur = draw(UNBACKED, "general");

    expect(sentColors).toEqual(["#112233"]);
    expect(attrsOf(hexInput(afterBlur)).value).toBe("#112233");
    expect(allText(afterBlur)).not.toContain("not valid");
  });
});

describe("the General settings group's SSH command", () => {
  it("offers a remote machine's SSH command outside recovery, and nothing for a machine without one", () => {
    const command = "ssh -i /k/id -p 2222 root@203.0.113.7";
    const { draw } = harness(undefined, { [BACKED]: command });

    const copyValues = (root: m.Children): unknown[] =>
      collectVnodes(root)
        .filter((vnode) => vnode.tag === CopyField)
        .map((vnode) => attrsOf(vnode).value);

    expect(copyValues(draw(BACKED, "general"))).toEqual([command]);
    expect(allText(draw(UNBACKED, "general"))).not.toContain(
      "Connect over SSH",
    );
    expect(copyValues(draw(UNBACKED, "general"))).toEqual([]);
  });
});

describe("the General settings group's Machine size section", () => {
  const RECORDED: WorkspaceMachineSizeData = {
    is_available: true,
    cpu_count: 4,
    memory_gb: 8,
    disk_gb: null,
    is_resizable: false,
    is_disk_resizable: false,
    target_memory_gb: null,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
  };

  /** Draw the General group for one machine whose size has loaded as `size`. */
  function drawWithSize(
    h: Harness,
    agentId: string,
    size: WorkspaceMachineSizeData | null,
  ): m.Children {
    h.draw(agentId, "general");
    const model = h.models.get(agentId);
    if (model === undefined)
      throw new Error("the machine's model was not built");
    model.machineSize = size;
    return h.draw(agentId, "general");
  }

  function machineSizeSection(root: m.Children): AnyVnode | undefined {
    return collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "machine-size-section",
    );
  }

  /** The dimensions of the resize fields drawn in the section, in order. */
  function sizeFields(section: AnyVnode): AnyVnode[] {
    return collectVnodes(section).filter(
      (vnode) => attrsOf(vnode).dimension !== undefined,
    );
  }

  function typeSize(section: AnyVnode, dimension: string, value: string): void {
    const field = sizeFields(section).find(
      (vnode) => attrsOf(vnode).dimension === dimension,
    );
    if (field === undefined)
      throw new Error(`no ${dimension} field to type into`);
    (attrsOf(field).oninput as (text: string) => void)(value);
  }

  it("says the size is being read, not reported, or not readable before it is known, and shows it read-only for a machine that cannot be resized", () => {
    const h = harness();

    const reading = machineSizeSection(drawWithSize(h, UNBACKED, null));
    if (reading === undefined) throw new Error("the section was not drawn");
    expect(allText(reading)).toContain("Reading the machine's size");

    const model = h.models.get(UNBACKED);
    if (model === undefined)
      throw new Error("the machine's model was not built");
    model.machineSizeState = "unavailable";
    const unreported = machineSizeSection(h.draw(UNBACKED, "general"));
    if (unreported === undefined) throw new Error("the section was not drawn");
    expect(allText(unreported)).toContain("does not report its size");

    // A read that failed is not a machine without a size.
    model.machineSizeState = "load_failed";
    const unread = machineSizeSection(h.draw(UNBACKED, "general"));
    if (unread === undefined) throw new Error("the section was not drawn");
    expect(allText(unread)).toContain("could not be read");
    expect(allText(unread)).not.toContain("does not report its size");

    const section = machineSizeSection(drawWithSize(h, UNBACKED, RECORDED));
    if (section === undefined) throw new Error("the section was not drawn");
    expect(allText(section)).toContain("4 CPUs · 8 GB RAM");
    expect(allText(section)).not.toContain("Reading the machine's size");
    expect(sizeFields(section)).toEqual([]);
    expect(pressableWithLabel(section, "Apply and restart")).toBeUndefined();
  });

  it("shows the size with a loading state in place of the controls until the resize offer is read", () => {
    const section = machineSizeSection(
      drawWithSize(harness(), UNBACKED, {
        ...RECORDED,
        cpu_count: null,
        disk_gb: 28,
        is_resize_offer_pending: true,
      }),
    );
    if (section === undefined) throw new Error("the section was not drawn");

    expect(allText(section)).toContain("8 GB RAM · 28 GB disk");
    expect(allText(section)).toContain(
      "Checking whether this machine's size can be changed",
    );
    expect(pressableWithLabel(section, "Save size")).toBeUndefined();
    expect(pressableWithLabel(section, "Apply and restart")).toBeUndefined();
  });

  it("notes an Imbue Cloud machine's pending size and the restart that applies it", () => {
    const section = machineSizeSection(
      drawWithSize(harness(), UNBACKED, {
        ...RECORDED,
        cpu_count: null,
        disk_gb: 28,
        target_memory_gb: 16,
        is_restart_needed_to_apply: true,
      }),
    );
    if (section === undefined) throw new Error("the section was not drawn");

    expect(allText(section)).toContain("8 GB RAM · 28 GB disk");
    expect(allText(section)).toContain(
      "A new size is pending (16 GB RAM · 28 GB disk). Restart this workspace's machine to apply it.",
    );
  });

  it("offers CPUs and memory for a docker machine, and the disk too for a lima machine", () => {
    const h = harness();

    const docker = machineSizeSection(
      drawWithSize(h, UNBACKED, { ...RECORDED, is_resizable: true }),
    );
    if (docker === undefined)
      throw new Error("the docker section was not drawn");
    expect(sizeFields(docker).map((field) => attrsOf(field).dimension)).toEqual(
      ["cpu_count", "memory_gb"],
    );
    expect(pressableWithLabel(docker, "Apply and restart")).toBeDefined();

    const lima = machineSizeSection(
      drawWithSize(h, BACKED, {
        ...RECORDED,
        disk_gb: 100,
        is_resizable: true,
        is_disk_resizable: true,
      }),
    );
    if (lima === undefined) throw new Error("the lima section was not drawn");
    expect(sizeFields(lima).map((field) => attrsOf(field).dimension)).toEqual([
      "cpu_count",
      "memory_gb",
      "disk_gb",
    ]);
  });

  it("posts the typed size on Apply and keeps the draft while the machine restarts", async () => {
    const docker = { ...RECORDED, is_resizable: true };
    const posted: unknown[] = [];
    const h = harness((url, init) => {
      if (url.startsWith("/api/v1/")) {
        posted.push(JSON.parse(String(init?.body)));
        return Promise.resolve(
          jsonResponse({ is_restart_dispatched: true, operation_id: UNBACKED }),
        );
      }
      // Discovery has not caught up: the re-read still reports the old size.
      return Promise.resolve(jsonResponse(docker));
    });
    const section = machineSizeSection(drawWithSize(h, UNBACKED, docker));
    if (section === undefined) throw new Error("the section was not drawn");

    typeSize(section, "memory_gb", "16");
    const typed = machineSizeSection(h.draw(UNBACKED, "general"));
    if (typed === undefined) throw new Error("the section was not redrawn");
    press(typed, "Apply and restart");
    await settleDispatch();
    const after = machineSizeSection(h.draw(UNBACKED, "general"));
    if (after === undefined) throw new Error("the section was not redrawn");

    expect(posted).toEqual([{ memory_gb: 16 }]);
    expect(h.requests).toEqual([
      `/api/v1/workspaces/${UNBACKED}/machine-size`,
      `/ui/api/workspaces/${UNBACKED}/machine-size`,
    ]);
    expect(allText(after)).toContain(
      "The machine is restarting to apply the new size.",
    );
    // No second resize while the restart runs.
    const apply = pressableWithLabel(after, "Apply and restart");
    expect(apply === undefined ? undefined : attrsOf(apply).disabled).toBe(
      true,
    );
    const memoryField = sizeFields(after).find(
      (field) => attrsOf(field).dimension === "memory_gb",
    );
    expect(
      memoryField === undefined ? undefined : attrsOf(memoryField).value,
    ).toBe("16");
  });

  const CLOUD: WorkspaceMachineSizeData = {
    ...RECORDED,
    cpu_count: null,
    disk_gb: 28,
    is_resizable: true,
    is_disk_resizable: true,
    is_running: true,
    is_transitioning: false,
    memory_choices_gb: [8, 16, 24, 32],
    is_memory_capped_by_plan: true,
    plan_memory_limit_gb: 40,
    other_machines_memory_gb: 8,
    max_disk_gb: 200,
    plan_disk_limit_gb: 280,
    other_machines_disk_gb: 28,
  };

  /** The RAM select of a drawn section, rendered through its component. */
  function memorySelect(section: AnyVnode): AnyVnode {
    const select = collectVnodes(renderDeep(section) as m.Children).find(
      (vnode) => vnode.tag === "select" && attrsOf(vnode).name === "memory_gb",
    );
    if (select === undefined) throw new Error("the RAM select was not drawn");
    return select;
  }

  it("offers an Imbue Cloud machine the RAM sizes its plan allows, a bounded disk, and no CPUs", () => {
    const section = machineSizeSection(
      drawWithSize(harness(), UNBACKED, CLOUD),
    );
    if (section === undefined) throw new Error("the section was not drawn");

    expect(allText(section)).toContain("8 GB RAM · 28 GB disk");
    const options = collectVnodes(memorySelect(section)).filter(
      (vnode) => vnode.tag === "option",
    );
    expect(options.map((option) => attrsOf(option).value)).toEqual([
      "8",
      "16",
      "24",
      "32",
    ]);
    expect(allText(options[options.length - 1])).toBe("32 GB (plan limit)");
    expect(
      sizeFields(section).map((field) => attrsOf(field).dimension),
    ).toEqual(["disk_gb"]);
    const disk = sizeFields(section)[0];
    expect(attrsOf(disk).min).toBe("28");
    expect(attrsOf(disk).max).toBe("200");
    expect(pressableWithLabel(section, "Save size")).toBeDefined();
    expect(pressableWithLabel(section, "Apply and restart")).toBeUndefined();
    expect(allText(section)).toContain(
      "Your plan allows 40 GB of RAM across running machines",
    );
    expect(allText(section)).toContain("other running machines use 8 GB");
    expect(allText(section)).toContain("The disk can grow to 200 GB");

    // The quota leaves no disk headroom: the bound is floored at the current disk.
    const full = machineSizeSection(
      drawWithSize(harness(), UNBACKED, { ...CLOUD, max_disk_gb: 28 }),
    );
    if (full === undefined) throw new Error("the section was not drawn");
    expect(allText(full)).toContain(
      "The plan leaves no room to grow the disk.",
    );
    expect(allText(full)).not.toContain("can grow to");
  });

  it("offers a running Imbue Cloud machine with a pending size a restart, and a stopped one only a note", () => {
    const h = harness();
    const pending = {
      ...CLOUD,
      target_memory_gb: 16,
      is_restart_needed_to_apply: true,
    };

    const running = machineSizeSection(drawWithSize(h, UNBACKED, pending));
    if (running === undefined)
      throw new Error("the running section was not drawn");
    expect(allText(running)).toContain(
      "A new size is pending (16 GB RAM · 28 GB disk)",
    );
    expect(allText(running)).toContain("backs up the machine's disks first");
    expect(pressableWithLabel(running, "Restart now")).toBeDefined();

    const stopped = machineSizeSection(
      drawWithSize(h, BACKED, { ...pending, is_running: false }),
    );
    if (stopped === undefined)
      throw new Error("the stopped section was not drawn");
    expect(pressableWithLabel(stopped, "Restart now")).toBeUndefined();
    expect(allText(stopped)).toContain(
      "Restart this workspace's machine to apply it.",
    );
  });

  it("holds the Imbue Cloud controls while the machine is starting or stopping", () => {
    const section = machineSizeSection(
      drawWithSize(harness(), UNBACKED, {
        ...CLOUD,
        is_running: false,
        is_transitioning: true,
      }),
    );
    if (section === undefined) throw new Error("the section was not drawn");

    expect(allText(section)).toContain("starting or stopping");
    const save = pressableWithLabel(section, "Save size");
    expect(save === undefined ? undefined : attrsOf(save).disabled).toBe(true);
    expect(attrsOf(memorySelect(section)).disabled).toBe(true);
  });

  it("saves a picked Imbue Cloud size without a restart, then offers the restart", async () => {
    const posted: unknown[] = [];
    let recorded: WorkspaceMachineSizeData = CLOUD;
    const h = harness((url, init) => {
      if (url.startsWith("/api/v1/")) {
        posted.push(JSON.parse(String(init?.body)));
        recorded = {
          ...CLOUD,
          target_memory_gb: 16,
          is_restart_needed_to_apply: true,
        };
        return Promise.resolve(
          jsonResponse({ is_restart_dispatched: false, operation_id: null }),
        );
      }
      return Promise.resolve(jsonResponse(recorded));
    });
    const section = machineSizeSection(drawWithSize(h, UNBACKED, CLOUD));
    if (section === undefined) throw new Error("the section was not drawn");

    const onchange = attrsOf(memorySelect(section)).onchange as (
      event: Event,
    ) => void;
    onchange({ target: { value: "16" } } as unknown as Event);
    const picked = machineSizeSection(h.draw(UNBACKED, "general"));
    if (picked === undefined) throw new Error("the section was not redrawn");
    press(picked, "Save size");
    await settleDispatch();
    const after = machineSizeSection(h.draw(UNBACKED, "general"));
    if (after === undefined) throw new Error("the section was not redrawn");

    expect(posted).toEqual([{ memory_gb: 16 }]);
    expect(h.requests).toEqual([
      `/api/v1/workspaces/${UNBACKED}/machine-size`,
      `/ui/api/workspaces/${UNBACKED}/machine-size`,
    ]);
    // The pending size is the one notice about it; no second "saved" line.
    expect(allText(after)).toContain(
      "A new size is pending (16 GB RAM · 28 GB disk)",
    );
    expect(
      collectVnodes(after).find(
        (vnode) => attrsOf(vnode).id === "machine-resize-notice",
      ),
    ).toBeUndefined();
    expect(pressableWithLabel(after, "Restart now")).toBeDefined();
  });

  it("says the machine is restarting, offers no second restart, and holds the controls", () => {
    const h = harness();
    const pending = {
      ...CLOUD,
      target_memory_gb: 16,
      is_restart_needed_to_apply: true,
    };

    // Dispatched from this page: the connector still reports the machine
    // running with its size pending, which must not offer the restart again.
    drawWithSize(h, UNBACKED, pending);
    const model = h.models.get(UNBACKED);
    if (model === undefined)
      throw new Error("the machine's model was not built");
    model.isRestartDispatched = true;
    const dispatched = machineSizeSection(h.draw(UNBACKED, "general"));
    if (dispatched === undefined) throw new Error("the section was not drawn");
    expect(allText(dispatched)).toContain(
      "The machine is restarting to apply the new size (16 GB RAM · 28 GB disk)",
    );
    expect(allText(dispatched)).not.toContain("A new size is pending");
    expect(pressableWithLabel(dispatched, "Restart now")).toBeUndefined();
    const save = pressableWithLabel(dispatched, "Save size");
    expect(save === undefined ? undefined : attrsOf(save).disabled).toBe(true);

    // Reported by the app (a restart under way from anywhere, or the page reopened mid-restart).
    const reported = machineSizeSection(
      drawWithSize(h, BACKED, { ...pending, is_restart_in_progress: true }),
    );
    if (reported === undefined) throw new Error("the section was not drawn");
    expect(allText(reported)).toContain("The machine is restarting");
    expect(pressableWithLabel(reported, "Restart now")).toBeUndefined();

    // A recovery the app reports with no size pending and nothing dispatched
    // from this page (a stopped machine's start, the recovery card's restart)
    // is not applying a size: the section only waits it out.
    const plain = machineSizeSection(
      drawWithSize(h, BACKED, { ...CLOUD, is_restart_in_progress: true }),
    );
    if (plain === undefined) throw new Error("the section was not drawn");
    expect(allText(plain)).toContain("The machine is starting or restarting");
    expect(allText(plain)).not.toContain("apply the new size");
    expect(allText(plain)).not.toContain("backs up");
    const plainSave = pressableWithLabel(plain, "Save size");
    expect(
      plainSave === undefined ? undefined : attrsOf(plainSave).disabled,
    ).toBe(true);
  });
});
