// @vitest-environment jsdom
//
// The bar tells a press on itself from a press on one of its controls by
// walking the real DOM, so that test needs real elements.
import { afterEach, describe, expect, it } from "vitest";
import type { ShellState } from "./shell-state";
import { PopoutChrome, isBarDragPress } from "./PopoutChrome";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, classTokensOf, collectVnodes, renderRoot } from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const WINDOW_ID = "win-0123";

function renderBar(accent: string | null, title: string, returns: { count: number } = { count: 0 }): AnyVnode {
  const shell = {
    stores: {
      workspaces: {
        accentEntry: () => ({ accent, name: "Research" }),
      },
    },
    popoutWindowTitle: () => title,
    returnPopoutToDesktop: () => {
      returns.count += 1;
      return Promise.resolve();
    },
  } as unknown as ShellState;
  return renderRoot(PopoutChrome, { shell, workspaceAnyId: WORKSPACE_ID, windowId: WINDOW_ID }) as AnyVnode;
}

function clickButton(bar: AnyVnode, id: string): void {
  const button = collectVnodes(bar).find((vnode) => attrsOf(vnode).id === id);
  if (button === undefined) throw new Error(`no ${id}`);
  (attrsOf(button).onclick as () => void)();
}

afterEach(() => {
  delete window.mindsNative;
});

describe("PopoutChrome", () => {
  it("names the workspace and the window, and offers the way back", () => {
    const bar = renderBar("#aabbcc", "Notes");
    expect(attrsOf(bar).id).toBe("minds-popout-bar");
    expect(attrsOf(bar)["data-popout-window-id"]).toBe(WINDOW_ID);
    const text = allText(bar);
    expect(text).toContain("Research");
    expect(text).toContain("Notes");
    const ids = collectVnodes(bar).map((vnode) => attrsOf(vnode).id);
    expect(ids).toContain("popout-return-btn");
    expect(ids).toContain("popout-close-btn");
  });

  it("returns the window by closing the desktop window, which takes it back on its way out", () => {
    // In the desktop app the close is the way back: main returns the window through a desktop window
    // showing the workspace before this one goes.
    const closes = { count: 0 };
    window.mindsNative = {
      platform: "linux",
      close: () => {
        closes.count += 1;
      },
    } as unknown as NonNullable<Window["mindsNative"]>;
    const returns = { count: 0 };
    clickButton(renderBar("#aabbcc", "Notes", returns), "popout-return-btn");
    expect(closes.count).toBe(1);
    expect(returns.count).toBe(0);
    // Off the desktop app there is no window to close: the popout's own shell takes the window back.
    delete window.mindsNative;
    clickButton(renderBar("#aabbcc", "Notes", returns), "popout-return-btn");
    expect(returns.count).toBe(1);
  });

  it("wears the titlebar's self-theming recipe only once an accent is painted", () => {
    expect(classTokensOf(renderBar("#aabbcc", "Notes"))).toContain("titlebar-surface");
    expect(classTokensOf(renderBar(null, "Notes"))).not.toContain("titlebar-surface");
  });

  it("starts a drag from the bar itself, never from its controls, and only in the desktop app", () => {
    // The bar's controls wrap a label or an icon, so a press lands on that
    // inner element as often as on the button itself.
    const bar = document.createElement("div");
    const button = document.createElement("button");
    const label = document.createElement("span");
    button.appendChild(label);
    bar.appendChild(button);
    expect(isBarDragPress(bar, true)).toBe(true);
    expect(isBarDragPress(button, true)).toBe(false);
    expect(isBarDragPress(label, true)).toBe(false);
    expect(isBarDragPress(bar, false)).toBe(false);
  });
});
