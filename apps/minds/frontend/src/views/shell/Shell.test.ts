import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import { Shell } from "./Shell";
import { WorkspaceFrame } from "./WorkspaceFrame";
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes } from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const OPTIONS_PATH = `/workspace/${WORKSPACE_ID}/options`;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The app-modal layer, if the Shell floated one: the only vnode carrying a
 * `cardClass` (its own view, and the backdrop inside it, are not expanded by a
 * direct view() call). */
function appOverlay(root: AnyVnode): AnyVnode | undefined {
  return collectVnodes(root).find((vnode) => attrsOf(vnode).cardClass !== undefined);
}

interface FakeShell {
  state: ShellState;
}

/** A shell whose machine is healthy and whose discovery is up, so neither the
 * notice band nor the recovery card floats: these suites are about the overlay
 * layer, and both of those surfaces are covered by their own tests. */
function makeShell(overrides: Partial<ShellState> = {}): FakeShell {
  const state = {
    channel: null,
    isSidebarOpen: false,
    currentRouteSearch: () => `workspace=${WORKSPACE_ID}`,
    closeAppOverlay: () => true,
    stores: {
      workspaces: { toAgentScopedId: (anyId: string) => anyId },
      health: { statusFor: () => "healthy", discoveryHealth: "healthy" },
    },
    isRecoveryModalOpenFor: () => false,
    ...overrides,
  } as unknown as ShellState;
  return { state };
}

interface RenderOptions {
  workspaceParam?: string | null;
  optionsContent?: m.Children;
}

/** Instantiate the Shell and call view() directly (no DOM). `window` is
 * stubbed because the view reads the capture-mode query parameter. */
function renderShell(
  shell: ShellState,
  routePath: string,
  content: m.Children,
  options: RenderOptions = {},
): AnyVnode {
  vi.stubGlobal("window", { location: { search: "" } });
  const instance = Shell() as unknown as m.Component;
  const vnode = m(instance, {
    shell,
    routePath,
    workspaceParam: options.workspaceParam === undefined ? WORKSPACE_ID : options.workspaceParam,
    content,
    homeContent: m("div#home-content"),
    optionsContent: options.optionsContent ?? null,
  } as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode) as unknown as AnyVnode;
}

describe("Shell request popup layer", () => {
  it("renders no app modal over the options panel while no request is open", () => {
    const { state } = makeShell();
    const content = m("div#panel-content");
    const root = renderShell(state, OPTIONS_PATH, content);
    expect(appOverlay(root)).toBeUndefined();
    expect(collectVnodes(root)).toContain(content);
  });

  it("stacks the popup over the options panel without displacing it", () => {
    // The popup is a route (/inbox), so the panel is no longer the routed page:
    // it is the copy the router keeps painted, and it holds the SAME vtree slot
    // as the routed one, so its models are not torn down and rebuilt.
    const { state } = makeShell();
    const popupBody = m("div#request-popup");
    const panel = m("div#panel-content");
    const root = renderShell(state, "/inbox", popupBody, { workspaceParam: null, optionsContent: panel });

    expect(appOverlay(root)).toBeDefined();
    expect(collectVnodes(root)).toContain(popupBody);
    expect(collectVnodes(root)).toContain(panel);
    // ...over the live workspace, which stays mounted behind both.
    const frame = collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame);
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
  });

  it("gives the popup its own card width, not the settings modal's", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/inbox", m("div#request-popup"), { workspaceParam: null });
    expect(String(attrsOf(appOverlay(root) as AnyVnode).cardClass)).toContain("w-[600px]");
  });

  it("keeps the routed page as the surface itself on a plain workspace route", () => {
    const { state } = makeShell();
    const content = m("div#panel-content");
    const root = renderShell(state, `/workspace/${WORKSPACE_ID}`, content);
    expect(appOverlay(root)).toBeUndefined();
    // No options route and no remembered panel: nothing paints the panel.
    expect(collectVnodes(root)).not.toContain(content);
  });
});

describe("Shell app-overlay card chrome", () => {
  function overlayAttrsAt(routePath: string): Record<string, unknown> {
    const { state } = makeShell();
    const root = renderShell(state, routePath, m("div#overlay-content"), { workspaceParam: null });
    const overlay = appOverlay(root);
    expect(overlay, `${routePath} floats an app-overlay card`).toBeDefined();
    return attrsOf(overlay as AnyVnode);
  }

  it("hands Minds settings a bounded column instead of a scrolling card body", () => {
    // Its pane scrolls its own two columns; a scroller here would take the
    // section list down with the panel, which is the bug it exists to prevent.
    const bodyClass = String(overlayAttrsAt("/settings").bodyClass);
    expect(bodyClass).toContain("flex-1");
    expect(bodyClass).toContain("min-h-0");
    expect(bodyClass).toContain("flex-col");
    expect(bodyClass).not.toContain("overflow-y-auto");
  });

  it("gives the settings card a definite height for that column to fill", () => {
    // A flex-1 body inside an auto-height card has no height to hand its
    // columns, so they would never scroll and the card would resize per
    // section, moving the list out from under the cursor.
    expect(String(overlayAttrsAt("/settings").cardClass)).toContain("h-[min(660px,");
  });

  it("leaves every other overlay scrolling its card as a whole", () => {
    // Accounts, Get help, the request popup, the AI-keys dialog and the
    // template stepper are single columns that depend on the card scrolling.
    for (const routePath of [
      "/accounts",
      "/help",
      "/inbox",
      "/settings/ai-keys",
      "/create/template",
    ]) {
      expect(String(overlayAttrsAt(routePath).bodyClass), routePath).toContain("overflow-y-auto");
    }
  });
});

