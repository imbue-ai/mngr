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
 * or with no strip at all (a cold-start deep link's first paint). */
function render(stripX: number | null): AnyVnode {
  vi.stubGlobal("document", {
    getElementById: (id: string) =>
      id === "ws-tab-strip" && stripX !== null
        ? { getBoundingClientRect: () => ({ left: stripX, top: 6, width: 90, height: 26 }) }
        : null,
  });
  const fetchJson = () => Promise.resolve({ ok: true, status: 200, body: {} });
  const instance = WorkspaceOptionsOverlay() as unknown as m.Component;
  const vnode = m(instance, {
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
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode) as unknown as AnyVnode;
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
    // The docked strip sits exactly on the titlebar strip it stands in for.
    const strip = collectVnodes(root).find((vnode) => attrsOf(vnode).role === "tablist");
    expect(String(attrsOf(strip as AnyVnode).style)).toContain("left: 300px");
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
