// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { READY_HOLD_MS, firstRunCover } from "../../models/firstRunCover";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, byId, renderDeep } from "../../testing";
import { WORKSPACE_READY_LINE } from "../pages/create/FirstRunWelcome";
import { FirstRunCover, OPENING_WORKSPACE_LINE } from "./FirstRunCover";

function render(routePath: string): AnyVnode | null {
  const cover = FirstRunCover();
  const tree = (cover.view as (vnode: unknown) => m.Children)({ attrs: { routePath } });
  return tree === null ? null : (renderDeep(tree as m.Vnode) as AnyVnode);
}

describe("FirstRunCover", () => {
  afterEach(() => {
    firstRunCover.lower();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("draws nothing until it is raised", () => {
    expect(render("/workspace/agent-1")).toBeNull();
  });

  it("holds the welcome while the workspace is not ready, says it is ready for a beat, then lowers itself", () => {
    vi.useFakeTimers();
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const requestedUrls: string[] = [];
    vi.stubGlobal("fetch", (url: string) => {
      requestedUrls.push(url);
      return Promise.reject(new Error("the cover asks the desktop client for nothing"));
    });
    firstRunCover.raise("create-attempt-a", "Welcome, Gleb!");

    const loading = render("/workspace/agent-1") as AnyVnode;
    expect(allText(byId(loading, "creating-welcome"))).toBe("Welcome, Gleb!");
    expect(allText(byId(loading, "creating-setting-up"))).toBe(OPENING_WORKSPACE_LINE);

    // A report for another attempt changes nothing.
    firstRunCover.markReady("create-attempt-b");
    expect(byId(render("/workspace/agent-1") as AnyVnode, "creating-ready")).toBeUndefined();

    firstRunCover.markReady("create-attempt-a");
    const ready = render("/workspace/agent-1") as AnyVnode;
    expect(allText(byId(ready, "creating-ready"))).toBe(WORKSPACE_READY_LINE);
    expect(byId(ready, "creating-setting-up")).toBeUndefined();

    vi.advanceTimersByTime(READY_HOLD_MS - 1);
    expect(firstRunCover.state).not.toBeNull();
    vi.advanceTimersByTime(1);

    // The cover is down, and nothing more is handed to the workspace: its seed went at done.
    expect(firstRunCover.state).toBeNull();
    expect(render("/workspace/agent-1")).toBeNull();
    expect(requestedUrls).toEqual([]);
  });

  it("drops when the user goes somewhere that is neither the creation page nor a workspace", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    firstRunCover.raise("create-attempt-a", "Setting up workspace-2");

    expect(render("/settings")).toBeNull();
    await Promise.resolve();

    expect(firstRunCover.state).toBeNull();
  });

  /** The drawn cover's removal hook, run on ``element`` as mithril runs it when the cover lowers. */
  function lowerOnto(element: HTMLElement): Promise<void> | undefined {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    firstRunCover.raise("create-attempt-a", "Welcome, Gleb!");
    const cover = byId(render("/workspace/agent-1"), "first-run-cover") as AnyVnode;
    const onbeforeremove = attrsOf(cover).onbeforeremove as (vnode: { dom: Element }) => Promise<void> | undefined;
    return onbeforeremove({ dom: element });
  }

  it("lets clicks through to the workspace while it fades, and is removed once the fade is over", async () => {
    const element = document.createElement("div");
    let finishFade: () => void = () => undefined;
    const finished = new Promise<void>((resolve) => (finishFade = resolve));
    element.animate = vi.fn(() => ({ finished }) as unknown as Animation);

    let isRemoved = false;
    void lowerOnto(element)?.then(() => (isRemoved = true));

    expect(element.style.pointerEvents).toBe("none");
    await Promise.resolve();
    expect(isRemoved).toBe(false);

    finishFade();
    await vi.waitFor(() => expect(isRemoved).toBe(true));
  });

  it("is removed at once under reduced motion", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query === "(prefers-reduced-motion: reduce)" }));
    const element = document.createElement("div");
    element.animate = vi.fn();

    expect(lowerOnto(element)).toBeUndefined();
    expect(element.animate).not.toHaveBeenCalled();
  });
});
