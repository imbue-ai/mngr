import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../../app-context";
import type { UiWorkspaceUpdate } from "../../../channel/messages";
import { SettingsModel } from "../../../models/settings";
import type { AnyVnode } from "../../../testing";
import {
  OUT_OF_DATE_UPDATE,
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  renderDeep,
  settingsOverview,
  settle,
  updatesShell,
  workspacesMessage,
} from "../../../testing";
import { workspaceUpdateSettings } from "./WorkspaceUpdateSettings";

const CUSTOM = "agent-" + "c".repeat(8);
const MANUAL = "agent-" + "d".repeat(8);
const FOLLOWING = "agent-" + "e".repeat(8);
const REMOVED = "agent-" + "f".repeat(8);

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

/** Studio settings' workspace updates over three listed workspaces, two with
 * their own setting, and a removed one whose setting the frame still carries. */
function setup(respond: () => Promise<Response> = () => Promise.resolve(jsonResponse({}))) {
  const bodies: Record<string, unknown> = {};
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    if (init?.body !== undefined) bodies[url] = JSON.parse(String(init.body));
    return respond();
  });
  const manual: UiWorkspaceUpdate = {
    ...OUT_OF_DATE_UPDATE,
    is_auto_update_enabled: false,
    auto_update_override: { mode: "manual" },
  };
  const shell = updatesShell(
    {
      [CUSTOM]: {
        ...OUT_OF_DATE_UPDATE,
        is_auto_update_enabled: true,
        update_window: "1:00 AM-4:00 AM",
        auto_update_override: { mode: "custom_window", window_start_hour: 1, window_end_hour: 4 },
      },
      [MANUAL]: manual,
      [FOLLOWING]: OUT_OF_DATE_UPDATE,
      [REMOVED]: manual,
    },
    false,
  );
  const listed = workspacesMessage().workspaces[0];
  shell.stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({
      workspaces: [
        { ...listed, id: CUSTOM, name: "borealis" },
        { ...listed, id: MANUAL, name: "cirrus" },
        { ...listed, id: FOLLOWING, name: "nimbus" },
      ],
    }),
  );
  const model = new SettingsModel(undefined, () => undefined);
  model.overview = settingsOverview();
  const draw = (): AnyVnode => renderDeep(workspaceUpdateSettings(model)) as AnyVnode;
  return { shell, model, draw, bodies };
}

function rowFor(tree: AnyVnode, agentId: string): AnyVnode {
  const row = collectVnodes(byId(tree, "update-settings-custom")).find(
    (vnode) => attrsOf(vnode)["data-agent-id"] === agentId,
  );
  if (row === undefined) throw new Error(`no row for ${agentId}`);
  return row;
}

/** A row's text with whitespace collapsed: allText puts a space between the name button and the text after it. */
function rowText(tree: AnyVnode, agentId: string): string {
  return allText(rowFor(tree, agentId)).replace(/\s+/g, " ").trim();
}

function pressIn(node: AnyVnode, label: string): void {
  const pressable = collectVnodes(node).find(
    (vnode) => typeof attrsOf(vnode).onclick === "function" && allText(vnode) === label,
  );
  if (pressable === undefined) throw new Error(`nothing labelled "${label}"`);
  (attrsOf(pressable).onclick as () => void)();
}

describe("Studio settings' custom settings row", () => {
  it("lists each existing workspace with its own setting, and no other", () => {
    const { draw } = setup();

    const tree = draw();
    const listedIds = collectVnodes(byId(tree, "update-settings-custom"))
      .map((vnode) => attrsOf(vnode)["data-agent-id"])
      .filter((id) => id !== undefined);

    expect(listedIds).toEqual([CUSTOM, MANUAL]);
    expect(rowText(tree, CUSTOM)).toBe("borealis · auto-update on · 1:00 AM-4:00 AM Use Studio settings");
    expect(rowText(tree, MANUAL)).toBe("cirrus · auto-update off Use Studio settings");
  });

  it("opens a listed workspace's Updates tab from its name", () => {
    const { shell, draw } = setup();
    const openUpdatesTab = vi.spyOn(shell, "openUpdatesTab").mockImplementation(() => undefined);

    pressIn(rowFor(draw(), MANUAL), "cirrus");

    expect(openUpdatesTab).toHaveBeenCalledWith(MANUAL);
  });

  it("puts a workspace back on Studio's setting at once, and says why when it could not", async () => {
    const { draw, bodies, model } = setup(() =>
      Promise.resolve(jsonResponse({ error: "Settings storage is not configured" }, 503)),
    );

    pressIn(rowFor(draw(), CUSTOM), "Use Studio settings");
    await settle();
    await settle();

    expect(bodies[`/ui/api/updates/${CUSTOM}/auto-update`]).toEqual({
      mode: null,
      window_start_hour: null,
      window_end_hour: null,
    });
    expect(model.workspaceResettingId).toBeNull();
    const row = allText(byId(draw(), "update-settings-custom"));
    expect(row).toContain(
      "Couldn't put this workspace back on Studio settings. Try again. If this persists, report a bug.",
    );
    expect(row).toContain("See details");
    expect(row).not.toContain("Settings storage is not configured");
  });
});

describe("Studio settings' automatic updates switch", () => {
  it("says the change wasn't saved, with the route's words behind See details, and leaves the switch as it was", async () => {
    let writes = 0;
    const { draw, model } = setup(() => {
      writes += 1;
      return Promise.resolve(jsonResponse({ error: "Could not save your settings: read-only file" }, 500));
    });
    const isEnabledBefore = model.overview?.is_auto_update_enabled;

    await model.setAutoUpdateEnabled("local", true);

    const failure = allText(byId(draw(), "auto-update-error-app"));
    expect(failure).toContain("Couldn't save this change. Try again. If this persists, report a bug.");
    expect(failure).toContain("Report a bug");
    expect(failure).not.toContain("read-only file");
    expect(model.overview?.is_auto_update_enabled).toBe(isEnabledBefore);

    // Its Try again sends the same change again.
    pressIn(byId(draw(), "auto-update-error-app") as AnyVnode, "Try again");
    await settle();
    expect(writes).toBe(2);
  });

  it("says Imbue Studio didn't respond when the change never got an answer", async () => {
    const { draw, model } = setup(() => Promise.reject(new TypeError("Failed to fetch")));

    await model.setAutoUpdateEnabled("local", true);

    const failure = allText(byId(draw(), "auto-update-error-app"));
    expect(failure).toContain("Couldn't save this change — Imbue Studio didn't respond. Try again in a moment.");
    expect(failure).not.toContain("Report a bug");
  });
});
