import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { firstRun, toggleStudioType } from "../../models/firstRun";
import { firstRunCover } from "../../models/firstRunCover";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, byId, jsonResponse, renderDeep } from "../../testing";
import { FirstRunCover, OPENING_WORKSPACE_LINE } from "./FirstRunCover";

function render(routePath: string): AnyVnode | null {
  const cover = FirstRunCover();
  const tree = (cover.view as (vnode: unknown) => m.Children)({ attrs: { routePath } });
  return tree === null ? null : (renderDeep(tree as m.Vnode) as AnyVnode);
}

describe("FirstRunCover", () => {
  afterEach(() => {
    firstRunCover.lower();
    firstRun.noteCreate("", null);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("draws nothing until it is raised", () => {
    expect(render("/workspace/agent-1")).toBeNull();
  });

  it("offers no Continue while the workspace is not ready, and one that lands at once when it is", () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const posted: { context?: string }[] = [];
    vi.stubGlobal("fetch", (_url: string, init?: RequestInit) => {
      posted.push(JSON.parse(init?.body as string) as { context?: string });
      return Promise.resolve(jsonResponse({ chat_id: "agent-chat" }));
    });
    firstRun.noteCreate("create-attempt-a", { userId: "u", email: "gleb@example.com", displayName: "Gleb" });
    firstRunCover.raise("create-attempt-a", false);

    const loading = render("/workspace/agent-1") as AnyVnode;
    expect(allText(byId(loading, "creating-welcome"))).toBe("Welcome, Gleb!");
    expect(allText(byId(loading, "creating-setting-up"))).toBe(OPENING_WORKSPACE_LINE);
    expect(byId(loading, "creating-continue")).toBeUndefined();

    // A report for another attempt changes nothing.
    firstRunCover.markReady("create-attempt-b");
    expect(byId(render("/workspace/agent-1") as AnyVnode, "creating-continue")).toBeUndefined();

    firstRun.studioTypes = toggleStudioType(firstRun.studioTypes, "film");
    firstRunCover.markReady("create-attempt-a");
    const ready = render("/workspace/agent-1") as AnyVnode;
    (attrsOf(byId(ready, "creating-continue") as AnyVnode).onclick as () => void)();

    // The cover is down at the press, and the answer as it stood goes to the workspace.
    expect(firstRunCover.state).toBeNull();
    expect(posted).toEqual([
      { title: "Welcome", turns: [], is_first_run: true, context: "I want to use Studio for: Film." },
    ]);
  });

  it("drops when the user goes somewhere that is neither the creation page nor a workspace", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    firstRunCover.raise("create-attempt-a", false);

    expect(render("/settings")).toBeNull();
    await Promise.resolve();

    expect(firstRunCover.state).toBeNull();
  });
});
