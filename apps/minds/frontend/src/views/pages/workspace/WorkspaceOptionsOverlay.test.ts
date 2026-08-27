import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { PermissionsModel } from "../../../models/workspacePermissions";
import { WorkspaceOptionsOverlay } from "./WorkspaceOptionsOverlay";
import type { AnyVnode } from "../../../testing";
import { attrsOf, collectVnodes } from "../../../testing";

const AGENT_ID = "agent-" + "c".repeat(8);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** m() normalizes an element vnode's `class` selector into `className`. */
function classOf(vnode: AnyVnode): string {
  return String(attrsOf(vnode).className ?? attrsOf(vnode).class ?? "");
}

/** Render the docked overlay with the titlebar tab strip measuring at `stripX`,
 * or with no titlebar at all (a cold-start deep link's first paint). The three
 * machine tabs sit inside the strip and the right-hand pair at the window's
 * edge, as the real titlebar lays them out -- the panel raises all five. */
function render(stripX: number | null): AnyVnode {
  const rects: Record<string, { left: number; top: number; width: number; height: number }> =
    stripX === null
      ? {}
      : {
          "ws-tab-strip": { left: stripX, top: 6, width: 90, height: 26 },
          "ws-tab-permissions": { left: stripX, top: 6, width: 26, height: 26 },
          "ws-tab-settings": { left: stripX + 32, top: 6, width: 26, height: 26 },
          "ws-tab-share": { left: stripX + 64, top: 6, width: 26, height: 26 },
          "notifications-toggle": { left: 900, top: 6, width: 26, height: 26 },
          "help-toggle": { left: 940, top: 6, width: 26, height: 26 },
        };
  vi.stubGlobal("document", {
    getElementById: (id: string) => {
      const rect = rects[id];
      return rect === undefined ? null : { getBoundingClientRect: () => rect };
    },
  });
  const fetchJson = () => Promise.resolve({ ok: true, status: 200, body: {} });
  const instance = WorkspaceOptionsOverlay() as unknown as m.Component;
  const vnode = m(instance, {
    // The panel raises the whole titlebar icon strip, so it reads the bell's
    // count off the shell; nothing else in these geometry cases needs one.
    shell: {
      stores: {
        notifications: {
          unresolvedCount: 0,
          hasUnresolvedForWorkspace: () => false,
        },
      },
      displayedWorkspaceAgentId: () => AGENT_ID,
    },
    agentId: AGENT_ID,
    model: new WorkspaceOptionsModel(AGENT_ID, { fetchJson, redraw: () => undefined }),
    permissions: new PermissionsModel(AGENT_ID, { fetchJson, redraw: () => undefined }),
    tab: "permissions",
    group: "general",
    section: null,
    onSelectTab: () => undefined,
    onSelectGroup: () => undefined,
    onSelectSection: () => undefined,
    onReviewRequest: () => undefined,
  } as unknown as m.Attributes) as m.Vnode;
  // The panel's own view returns the OverlayShell it delegates its chrome to,
  // so the geometry under test lives one render deeper.
  const shellVnode = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  ) as unknown as AnyVnode;
  const overlay = (shellVnode.tag as unknown as () => m.Component)();
  return (overlay.view as unknown as (v: unknown) => AnyVnode).call(overlay, {
    attrs: shellVnode.attrs,
    children: shellVnode.children,
  });
}

function card(root: AnyVnode): AnyVnode {
  const found = collectVnodes(root).find((vnode) => attrsOf(vnode).id === "ws-options-panel");
  expect(found, "no #ws-options-panel").toBeDefined();
  return found as AnyVnode;
}

function region(root: AnyVnode): AnyVnode {
  const found = collectVnodes(root).find((vnode) => classOf(vnode).includes("pointer-events-none"));
  expect(found, "no positioning region").toBeDefined();
  return found as AnyVnode;
}

describe("the docked options panel's geometry", () => {
  it("hangs from the tab strip, capped at 880px", () => {
    const root = render(300);
    // Its left edge overhangs the strip's, and the card stops widening at 880.
    expect(String(attrsOf(region(root)).style)).toContain("padding-left: 280px");
    expect(classOf(card(root))).toContain("max-w-[880px]");
    expect(classOf(card(root))).toContain("w-full");
    // The raised strip sits exactly on the titlebar icons it stands in for.
    // It is a component vnode, so render it to reach its buttons.
    const stripVnode = collectVnodes(root).find(
      (vnode) => typeof vnode.tag === "function" && (vnode.tag as { name?: string }).name === "RaisedTitlebarIcons",
    );
    expect(stripVnode, "no raised titlebar icons").toBeDefined();
    const instance = ((stripVnode as AnyVnode).tag as unknown as () => m.Component)();
    const strip = (instance.view as unknown as (v: unknown) => AnyVnode).call(instance, {
      attrs: (stripVnode as AnyVnode).attrs,
      children: (stripVnode as AnyVnode).children,
    });
    const raised = collectVnodes(strip).filter((vnode) => attrsOf(vnode)["data-titlebar-popup"] !== undefined);

    // All five, not just this panel's three: the panel covers the whole
    // titlebar, so leaving the bell and the bug button off would take them
    // away for as long as it is open.
    expect(raised.map((vnode) => attrsOf(vnode)["data-titlebar-popup"])).toEqual([
      "permissions",
      "settings",
      "share",
      "notifications",
      "help",
    ]);
    expect(String(attrsOf(raised[0]).style)).toContain("left: 300px");
    // The open tab is filled with the card's surface and square-bottomed, so
    // it reads as joined to the panel below it.
    expect(attrsOf(raised[0])["aria-selected"]).toBe("true");
    expect(classOf(raised[0])).toContain("rounded-b-none");
  });

  it("keeps a 24px gutter on both sides when the strip sits near the edge", () => {
    const style = String(attrsOf(region(render(30))).style);
    expect(style).toContain("padding-left: 24px");
    expect(style).toContain("padding-right: 24px");
  });

  it("falls back to a centered card before the titlebar has been measured", () => {
    const root = render(null);
    expect(classOf(region(root))).toContain("items-center");
    expect(classOf(card(root))).toContain("max-w-[880px]");
  });
});
