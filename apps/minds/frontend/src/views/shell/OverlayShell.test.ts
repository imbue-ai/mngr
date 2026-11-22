import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import type { AnyVnode, StubRect } from "../../testing";
import {
  attrsOf,
  byId,
  classTokensOf,
  collectVnodes,
  renderRoot,
  required,
  stubTitlebarRects,
} from "../../testing";
import { DialogCloseButton } from "../components/Modal";
import { OverlayBackdrop } from "./OverlayBackdrop";
import type { OverlayShellAttrs } from "./OverlayShell";
import { ANCHORED_CARD_CLASS, OverlayShell } from "./OverlayShell";
import type { RaisedTitlebarIconsAttrs } from "./RaisedTitlebarIcons";
import { RaisedTitlebarIcons } from "./RaisedTitlebarIcons";
import { ShellState } from "./shell-state";

const WINDOW_WIDTH = 1000;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const BELL_RECT: StubRect = { left: 860, top: 8, width: 28, height: 28 };
const BUG_RECT: StubRect = { left: 892, top: 8, width: 28, height: 28 };
const GEAR_RECT: StubRect = { left: 924, top: 8, width: 28, height: 28 };
const RIGHT_HAND_BUTTONS: Record<string, StubRect> = {
  "notifications-toggle": BELL_RECT,
  "help-toggle": BUG_RECT,
  "settings-btn": GEAR_RECT,
};

/** A window `WINDOW_WIDTH` wide whose titlebar shows `rects`' buttons, and nothing else. */
function stubTitlebar(rects: Record<string, StubRect>): void {
  vi.stubGlobal("window", { innerWidth: WINDOW_WIDTH });
  stubTitlebarRects(rects);
}

/** The overlay as it first draws over `titlebar`, with every vnode it returned. */
function renderOverlay(
  titlebar: Record<string, StubRect>,
  overrides: Partial<OverlayShellAttrs>,
): { nodes: AnyVnode[]; shell: ShellState; dismissals: () => number } {
  stubTitlebar(titlebar);
  const shell = new ShellState(createEmptyStores());
  let dismissals = 0;
  const attrs: OverlayShellAttrs = {
    shell,
    placement: "anchored",
    selected: "settings",
    panelId: "the-panel",
    backdropId: "the-backdrop",
    cardClass: ANCHORED_CARD_CLASS,
    bodyClass: "the-body",
    onDismiss: () => (dismissals += 1),
    ...overrides,
  };
  const root = renderRoot(OverlayShell, attrs, m("p#the-content", "content"));
  return { nodes: collectVnodes(root), shell, dismissals: () => dismissals };
}

function card(nodes: AnyVnode[]): AnyVnode {
  return required(byId(nodes, "the-panel"));
}

function closeButton(nodes: AnyVnode[]): AnyVnode | undefined {
  return nodes.find((vnode) => vnode.tag === DialogCloseButton);
}

function strip(nodes: AnyVnode[]): AnyVnode | undefined {
  return nodes.find((vnode) => vnode.tag === RaisedTitlebarIcons);
}

/** The full-window layer the card sits in: it centers a centered card, and only holds an anchored one. */
function region(nodes: AnyVnode[]): AnyVnode {
  return required(nodes.find((vnode) => Array.isArray(vnode.children) && vnode.children.includes(card(nodes))));
}

describe("OverlayShell as a centered card", () => {
  it("has a close X that dismisses it, raises no strip, and sits in the middle of the window", () => {
    const { nodes, dismissals } = renderOverlay(RIGHT_HAND_BUTTONS, {
      placement: "centered",
      selected: null,
      closeButtonId: "the-close",
      closeLabel: "Close the request",
    });

    const close = required(closeButton(nodes));
    expect(attrsOf(close).id).toBe("the-close");
    expect(attrsOf(close)["aria-label"]).toBe("Close the request");
    expect(attrsOf(close)["data-tooltip"]).toBe("Close the request");
    (attrsOf(close).onClose as () => void)();
    expect(dismissals()).toBe(1);

    expect(strip(nodes)).toBeUndefined();
    expect(attrsOf(card(nodes)).style).toBeUndefined();
    expect(classTokensOf(region(nodes))).toEqual(expect.arrayContaining(["items-center", "justify-center"]));
    expect(byId(nodes, "the-content")).toBeDefined();
  });

  it("leaves the close X its plain label when the surface names none", () => {
    const { nodes } = renderOverlay(RIGHT_HAND_BUTTONS, { placement: "centered", selected: null });

    const close = required(closeButton(nodes));
    expect(attrsOf(close)["aria-label"]).toBeUndefined();
    expect(attrsOf(close)["data-tooltip"]).toBeUndefined();
  });
});

describe("OverlayShell as an anchored flyout", () => {
  it("hangs from its button's bottom edge, right-aligned to it, with no close X", () => {
    const { nodes } = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "settings" });

    // It is put away by its own button, by Escape, or by a click on the dimmed window.
    expect(closeButton(nodes)).toBeUndefined();
    const flyout = card(nodes);
    expect(attrsOf(flyout).style).toBe(
      `position: fixed; top: ${GEAR_RECT.top + GEAR_RECT.height}px; right: ${WINDOW_WIDTH - (GEAR_RECT.left + GEAR_RECT.width)}px`,
    );
    // Square where it joins its raised button.
    expect(classTokensOf(flyout)).toContain("rounded-tr-none");
    expect(classTokensOf(region(nodes))).not.toContain("items-center");
  });

  it("raises the strip with its own button selected, which dismisses it or switches flyouts through the shell", () => {
    const { nodes, shell, dismissals } = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "settings" });
    const openHelp = vi.spyOn(shell, "openHelp").mockImplementation(() => undefined);
    const switchToWorkspaceSwitcher = vi.spyOn(shell, "switchToWorkspaceSwitcher").mockImplementation(() => undefined);

    const attrs = attrsOf(required(strip(nodes))) as unknown as RaisedTitlebarIconsAttrs;
    expect(attrs.selected).toBe("settings");
    expect(attrs.shell).toBe(shell);
    attrs.onSelect("help");
    attrs.onSelect("workspace");
    expect(openHelp).toHaveBeenCalledTimes(1);
    expect(switchToWorkspaceSwitcher).toHaveBeenCalledTimes(1);

    attrs.onDismiss();
    expect(dismissals()).toBe(1);
  });

  it("paints the card before the strip, so the card's shadow never falls on the raised buttons", () => {
    const { nodes } = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "settings" });

    expect(nodes.indexOf(card(nodes))).toBeLessThan(nodes.indexOf(required(strip(nodes))));
  });

  it("right-aligns to the button `alignTo` names, so two flyouts that share a box do not move it", () => {
    // The feed and Report a bug both align to the bug button, the rightmost of the pair.
    const expectedStyle = `position: fixed; top: ${BUG_RECT.top + BUG_RECT.height}px; right: ${WINDOW_WIDTH - (BUG_RECT.left + BUG_RECT.width)}px`;

    const feed = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "notifications", alignTo: "help" }).nodes;
    expect(attrsOf(card(feed)).style).toBe(expectedStyle);
    // The button above that corner is not the selected one, so there is nothing to join: it stays round.
    expect(classTokensOf(card(feed))).not.toContain("rounded-tr-none");
    expect((attrsOf(required(strip(feed))) as unknown as RaisedTitlebarIconsAttrs).selected).toBe("notifications");

    const help = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "help", alignTo: "help" }).nodes;
    expect(attrsOf(card(help)).style).toBe(expectedStyle);
    expect(classTokensOf(card(help))).toContain("rounded-tr-none");
  });

  it("keeps a gutter from the window's right edge under a button flush against it", () => {
    const flush: StubRect = { left: WINDOW_WIDTH - 30, top: 8, width: 28, height: 28 };

    const { nodes } = renderOverlay({ "settings-btn": flush }, { selected: "settings" });

    expect(attrsOf(card(nodes)).style).toBe("position: fixed; top: 36px; right: 8px");
  });

  it("falls back to the middle of the window when its button has no rect, rather than drawing at a guessed one", () => {
    // A cold-start deep link draws before the titlebar has; a hidden button measures at nothing.
    const absent = renderOverlay({ "help-toggle": BUG_RECT }, { selected: "settings" }).nodes;
    const hidden = renderOverlay(
      { "settings-btn": { left: 0, top: 0, width: 0, height: 0 } },
      { selected: "settings" },
    ).nodes;

    for (const nodes of [absent, hidden]) {
      expect(attrsOf(card(nodes)).style).toBeUndefined();
      expect(classTokensOf(card(nodes))).not.toContain("rounded-tr-none");
      expect(classTokensOf(region(nodes))).toEqual(expect.arrayContaining(["items-center", "justify-center"]));
      // Still a flyout: the strip is asked for, and takes over once the bar can be measured.
      expect((attrsOf(required(strip(nodes))) as unknown as RaisedTitlebarIconsAttrs).selected).toBe("settings");
    }
  });

  it("falls back the same way when the button `alignTo` names is the one missing", () => {
    const { nodes } = renderOverlay({ "notifications-toggle": BELL_RECT }, { selected: "notifications", alignTo: "help" });

    expect(attrsOf(card(nodes)).style).toBeUndefined();
    expect(classTokensOf(region(nodes))).toContain("items-center");
  });
});

describe("OverlayShell's backdrop", () => {
  it("dims the whole window, the bar included, and dismisses on a click", () => {
    const { nodes, dismissals } = renderOverlay(RIGHT_HAND_BUTTONS, { selected: "settings" });

    const backdrop = required(nodes.find((vnode) => vnode.tag === OverlayBackdrop));
    expect(attrsOf(backdrop).backdropId).toBe("the-backdrop");
    expect(attrsOf(backdrop).fullWindow).toBe(true);
    (attrsOf(backdrop).onDismiss as () => void)();
    expect(dismissals()).toBe(1);
  });
});
