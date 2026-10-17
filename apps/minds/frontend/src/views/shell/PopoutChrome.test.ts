// @vitest-environment jsdom
//
// The bar tells a press on itself from a press on one of its controls by
// walking the real DOM, so that test needs real elements.
import { afterEach, describe, expect, it } from "vitest";
import type { ShellState } from "./shell-state";
import { PopoutChrome, isBarDragPress } from "./PopoutChrome";
import { TrafficLights } from "./TrafficLights";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, classTokensOf, collectVnodes, renderRoot } from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const WINDOW_ID = "win-0123";

function renderBar(accent: string | null, title: string, isMac = false, isTrafficLightsDrawn = false): AnyVnode {
  const shell = {
    isMac,
    isTrafficLightsDrawn,
    stores: {
      workspaces: {
        accentEntry: () => ({ accent, name: "Research" }),
      },
    },
    popoutWindowTitle: () => title,
  } as unknown as ShellState;
  return renderRoot(PopoutChrome, { shell, workspaceAnyId: WORKSPACE_ID, windowId: WINDOW_ID }) as AnyVnode;
}

function buttonOf(bar: AnyVnode, id: string): AnyVnode {
  const button = collectVnodes(bar).find((vnode) => attrsOf(vnode).id === id);
  if (button === undefined) throw new Error(`no ${id}`);
  return button;
}

afterEach(() => {
  delete window.mindsNative;
});

describe("PopoutChrome", () => {
  it("names the workspace and the window, and offers the close as the way back", () => {
    const bar = renderBar("#aabbcc", "Notes");
    expect(attrsOf(bar).id).toBe("minds-popout-bar");
    expect(attrsOf(bar)["data-popout-window-id"]).toBe(WINDOW_ID);
    const text = allText(bar);
    expect(text).toContain("Research");
    expect(text).toContain("Notes");
    expect(text).not.toContain("Return to desktop");
    expect(collectVnodes(bar).map((vnode) => attrsOf(vnode).id)).toContain("popout-close-btn");
  });

  it("closes the desktop window from its close control, which takes the window back on its way out", () => {
    const closes = { count: 0 };
    window.mindsNative = {
      platform: "linux",
      close: () => {
        closes.count += 1;
      },
    } as unknown as NonNullable<Window["mindsNative"]>;
    const close = buttonOf(renderBar("#aabbcc", "Notes"), "popout-close-btn");
    expect(attrsOf(close).hidden).toBe(false);
    (attrsOf(close).onclick as () => void)();
    expect(closes.count).toBe(1);
    // Off the desktop app there is no window to close.
    delete window.mindsNative;
    expect(attrsOf(buttonOf(renderBar("#aabbcc", "Notes"), "popout-close-btn")).hidden).toBe(true);
  });

  it("on macOS leaves room for the traffic lights and hides its own close control, which they carry", () => {
    window.mindsNative = { platform: "darwin", close: () => undefined } as unknown as NonNullable<
      Window["mindsNative"]
    >;
    const bar = renderBar("#aabbcc", "Notes", true);
    expect(attrsOf(buttonOf(bar, "popout-close-btn")).hidden).toBe(true);
    const spacer = collectVnodes(bar).find((vnode) => classTokensOf(vnode).includes("w-[72px]"));
    expect(spacer).toBeDefined();
    const elsewhere = collectVnodes(renderBar("#aabbcc", "Notes", false));
    expect(elsewhere.find((vnode) => classTokensOf(vnode).includes("w-[72px]"))).toBeUndefined();
  });

  it("draws its own traffic lights and hides its close control on a platform made to look like a Mac", () => {
    window.mindsNative = { platform: "linux", close: () => undefined } as unknown as NonNullable<
      Window["mindsNative"]
    >;
    const bar = renderBar("#aabbcc", "Notes", false, true);
    expect(attrsOf(buttonOf(bar, "popout-close-btn")).hidden).toBe(true);
    expect(collectVnodes(bar).find((vnode) => vnode.tag === TrafficLights)).toBeDefined();
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
