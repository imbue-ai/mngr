import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import { Titlebar } from "./Titlebar";
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes } from "../../testing";

const WORKSPACE_ID = "agent-ab12";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Render the titlebar without a DOM. `window` is stubbed because the Electron
 * bridge feature-detects `window.mindsNative` while deciding whether to draw
 * the window controls. */
function renderTitlebar(routeSearch: string, panelRouteBehindOverlay: string | null = null): AnyVnode {
  vi.stubGlobal("window", {});
  vi.spyOn(m.route, "get").mockReturnValue(
    `/workspace/${WORKSPACE_ID}${routeSearch ? `?${routeSearch}` : ""}`,
  );
  const shell = {
    isMac: true,
    panelRouteBehindOverlay,
    stores: {
      workspaces: {
        accentEntry: () => ({ name: "alpha" }),
        toAgentScopedId: (anyId: string) => anyId,
      },
      requests: { count: 0 },
      health: { isContentAssumedReady: () => true },
    },
    openSidebar: () => undefined,
    displayedWorkspaceAnyId: WORKSPACE_ID,
  } as unknown as ShellState;
  const instance = Titlebar() as unknown as m.Component;
  const routePath = routeSearch === "" ? `/workspace/${WORKSPACE_ID}` : `/workspace/${WORKSPACE_ID}/options`;
  const vnode = m(instance, { shell, routePath } as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode) as unknown as AnyVnode;
}

function tabStripIds(root: AnyVnode): unknown[] {
  // m() normalizes the `div#ws-tab-strip` selector into tag + attrs.id.
  const strip = collectVnodes(root).find((vnode) => attrsOf(vnode).id === "ws-tab-strip");
  expect(strip).toBeDefined();
  return collectVnodes(strip?.children)
    .map((vnode) => attrsOf(vnode).id)
    .filter((id) => typeof id === "string" && id.startsWith("ws-tab-"));
}

function tabButton(root: AnyVnode, id: string): AnyVnode {
  const button = collectVnodes(root).find((vnode) => attrsOf(vnode).id === id);
  expect(button, `no titlebar button ${id}`).toBeDefined();
  return button as AnyVnode;
}

describe("Titlebar right cluster", () => {
  it("offers no requests entry: the popup is the only review surface", () => {
    const root = renderTitlebar("");
    const ids = collectVnodes(root).map((vnode) => attrsOf(vnode).id);
    expect(ids).not.toContain("requests-toggle");
    expect(ids).not.toContain("requests-badge");
    // The bug-report button is still there, so this is not passing because the
    // cluster failed to render at all.
    expect(ids).toContain("help-toggle");
  });
});

describe("Titlebar workspace tab strip", () => {
  it("leads the strip with the Permissions tab", () => {
    const root = renderTitlebar("");
    expect(tabStripIds(root)).toEqual(["ws-tab-permissions", "ws-tab-share", "ws-tab-settings"]);
  });

  it("labels the Permissions tab with the key glyph, tooltipped like its neighbours", () => {
    const button = tabButton(renderTitlebar(""), "ws-tab-permissions");
    expect(attrsOf(button)["aria-label"]).toBe("Permissions");
    // The delegated [data-tooltip] chrome, not a native title: the two would
    // otherwise show two different tooltips on the same strip.
    expect(attrsOf(button)["data-tooltip"]).toBe("Permissions");
    expect(attrsOf(button).title).toBeUndefined();
    const icon = collectVnodes(button.children).find((vnode) => attrsOf(vnode).name !== undefined);
    expect(attrsOf(icon as AnyVnode).name).toBe("key");
  });

  it("opens the options overlay on the permissions tab", () => {
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const button = tabButton(renderTitlebar(""), "ws-tab-permissions");
    (attrsOf(button).onclick as () => void)();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}/options?tab=permissions`);
  });

  it("hides its own tabs while the docked panel draws them, popup or no popup", () => {
    // Two strips at the same measured rect would ghost through each other. The
    // panel is still up, frozen, while a request popup floats over it -- and
    // the route is the popup's by then, so the route alone cannot say so.
    const open = renderTitlebar("tab=permissions");
    expect(attrsOf(tabButton(open, "ws-tab-permissions")).extra).toBe("invisible");

    const frozen = renderTitlebar("", `/workspace/${WORKSPACE_ID}/options?tab=permissions`);
    expect(attrsOf(tabButton(frozen, "ws-tab-permissions")).extra).toBe("invisible");

    const closed = renderTitlebar("");
    expect(attrsOf(tabButton(closed, "ws-tab-permissions")).extra).not.toBe("invisible");
  });

  it("highlights only the tab the URL selected, and closes it on a second click", () => {
    const root = renderTitlebar("tab=permissions");
    expect(attrsOf(tabButton(root, "ws-tab-permissions")).tone).toBe("default");
    expect(attrsOf(tabButton(root, "ws-tab-share")).tone).toBe("muted");
    expect(attrsOf(tabButton(root, "ws-tab-settings")).tone).toBe("muted");

    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    (attrsOf(tabButton(root, "ws-tab-permissions")).onclick as () => void)();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`);
  });
});
