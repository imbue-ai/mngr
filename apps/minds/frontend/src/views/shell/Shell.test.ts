import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import { NoticeBand } from "./NoticeBand";
import { Shell } from "./Shell";
import { ToastLayer } from "./ToastLayer";
import { WorkspaceFrame } from "./WorkspaceFrame";
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes, notificationEntry } from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const OPTIONS_PATH = `/workspace/${WORKSPACE_ID}/options`;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Every OverlayShell vnode the Shell floated. The Shell renders at most one
 * (its single overlay slot; the options panel's own layer renders its
 * OverlayShell only once the routed page inside it is itself rendered). */
function overlayShells(root: AnyVnode): AnyVnode[] {
  return collectVnodes(root).filter(
    (vnode) =>
      typeof vnode.tag === "function" &&
      (vnode.tag as { name?: string }).name === "OverlayShell",
  );
}

/** The overlay layer, if the Shell floated one. */
function appOverlay(root: AnyVnode): AnyVnode | undefined {
  return overlayShells(root)[0];
}

/** Render an overlay down to its own DOM tree (the Shell hands back the
 * OverlayShell component vnode unrendered). */
function renderOverlay(vnode: AnyVnode): AnyVnode {
  const name =
    typeof vnode.tag === "function"
      ? ((vnode.tag as { name?: string }).name ?? "")
      : "";
  return name === "OverlayShell" ? renderComponent(vnode) : vnode;
}

/** Render a component vnode's own tree (the Shell's view returns it unrendered). */
function renderComponent(vnode: AnyVnode): AnyVnode {
  const component = (vnode.tag as unknown as () => m.Component)();
  return (component.view as unknown as (v: unknown) => AnyVnode).call(
    component,
    {
      attrs: vnode.attrs,
      children: vnode.children,
    },
  );
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
    notificationsUi: null,
    currentRouteSearch: () => `workspace=${WORKSPACE_ID}`,
    closeAppOverlay: () => true,
    displayedWorkspaceAgentId: () => WORKSPACE_ID,
    stores: {
      workspaces: {
        toAgentScopedId: (anyId: string) => anyId,
        entryByAnyId: () => null,
      },
      health: {
        statusFor: () => "healthy",
        discoveryHealth: "healthy",
        appEnvironmentCondition: () => "NONE",
        recoveryKindFor: () => null,
      },
      notifications: {
        entries: [],
        unresolvedCount: 0,
        hasUnresolvedForWorkspace: () => false,
      },
    },
    isRecoveryModalOpenFor: () => false,
    ...overrides,
  } as unknown as ShellState;
  return { state };
}

/** The titlebar's measurable boxes in a 1000px window, laid out as the real
 * one lays them out: the machine tab strip after the breadcrumb, the bell and
 * the bug-report button at the right edge. */
const TITLEBAR_RECTS: Record<
  string,
  { left: number; top: number; width: number; height: number }
> = {
  "ws-tab-strip": { left: 331, top: 5, width: 92, height: 28 },
  "ws-tab-permissions": { left: 331, top: 5, width: 28, height: 28 },
  "ws-tab-settings": { left: 363, top: 5, width: 28, height: 28 },
  "ws-tab-share": { left: 395, top: 5, width: 28, height: 28 },
  "notifications-toggle": { left: 900, top: 5, width: 28, height: 28 },
  "help-toggle": { left: 940, top: 5, width: 28, height: 28 },
};

/** Put a measurable titlebar on screen. `ids` names which boxes are there; the
 * rest read as absent, the way a hub page's machine tabs do. Call it AFTER
 * `renderShell` (whose own window stub carries no innerWidth) and before the
 * overlay component is instantiated, which is when it measures. */
function stubTitlebar(
  ids: readonly string[] = Object.keys(TITLEBAR_RECTS),
): void {
  vi.stubGlobal("document", {
    getElementById: (id: string) => {
      const rect = TITLEBAR_RECTS[id];
      if (rect === undefined || !ids.includes(id)) return null;
      return { getBoundingClientRect: () => rect };
    },
  });
  vi.stubGlobal("window", { location: { search: "" }, innerWidth: 1000 });
}

/** The raised icon strip an open surface mounted, rendered: it is a component
 * vnode, so a single-level view() call leaves its buttons unexpanded. */
function raisedStrip(rendered: AnyVnode): AnyVnode {
  const strip = collectVnodes(rendered).find(
    (vnode) =>
      typeof vnode.tag === "function" &&
      (vnode.tag as { name?: string }).name === "RaisedTitlebarIcons",
  );
  expect(strip, "no raised titlebar icons").toBeDefined();
  return renderComponent(strip as AnyVnode);
}

/** The raised copy of one titlebar popup icon, drawn by whichever surface is
 * open over the dimmed real one. */
function raisedIcon(rendered: AnyVnode, popupId: string): AnyVnode | undefined {
  return collectVnodes(raisedStrip(rendered)).find(
    (vnode) => attrsOf(vnode)["data-titlebar-popup"] === popupId,
  );
}

/** Every popup icon the open surface raised, in strip order. */
function raisedIconIds(rendered: AnyVnode): unknown[] {
  return collectVnodes(raisedStrip(rendered))
    .map((vnode) => attrsOf(vnode)["data-titlebar-popup"])
    .filter((popupId) => popupId !== undefined);
}

/** The anchored OverlayShell the Shell mounted for `id` (the bell's feed or
 * Get help): both take the same placement, so they are told apart by which
 * icon each has lit. */
function popoverLayer(root: AnyVnode, id: string): AnyVnode | undefined {
  return collectVnodes(root).find(
    (vnode) =>
      typeof vnode.tag === "function" &&
      (vnode.tag as { name?: string }).name === "OverlayShell" &&
      attrsOf(vnode).placement === "anchored" &&
      attrsOf(vnode).selected === id,
  );
}

interface RenderOptions {
  workspaceParam?: string | null;
  optionsContent?: m.Children;
  behindContent?: m.Children;
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
    workspaceParam:
      options.workspaceParam === undefined
        ? WORKSPACE_ID
        : options.workspaceParam,
    content,
    homeContent: m("div#home-content"),
    behindContent: options.behindContent ?? null,
    optionsContent: options.optionsContent ?? null,
  } as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  ) as unknown as AnyVnode;
}

describe("Shell request popup layer", () => {
  it("renders no app modal over the options panel while no request is open", () => {
    const { state } = makeShell();
    const content = m("div#panel-content");
    const root = renderShell(state, OPTIONS_PATH, content);
    expect(appOverlay(root)).toBeUndefined();
    expect(collectVnodes(root)).toContain(content);
  });

  it("keeps the options panel mounted under the popup, and hides it", () => {
    // The popup is a route (/inbox), so the panel is no longer the routed page:
    // it is the copy the router keeps painted, and it holds the SAME vtree slot
    // as the routed one, so its models are not torn down and rebuilt.
    //
    // It is hidden rather than painted: the popup resizes out of the panel's
    // own box, so for the length of that resize the two cards sit on top of
    // each other, each with its own close X -- which is what made one window
    // read as two. `visibility` keeps it laid out, so the popup can still
    // measure the box it is growing out of.
    const { state } = makeShell();
    const popupBody = m("div#request-popup");
    const panel = m("div#panel-content");
    const root = renderShell(state, "/inbox", popupBody, {
      workspaceParam: null,
      optionsContent: panel,
    });

    expect(appOverlay(root)).toBeDefined();
    expect(collectVnodes(root)).toContain(popupBody);
    expect(collectVnodes(root)).toContain(panel);
    const layer = collectVnodes(root).find(
      (vnode) => vnode.attrs?.id === "ws-options-layer",
    );
    expect(String(attrsOf(layer as AnyVnode).style)).toContain(
      "visibility: hidden",
    );
    // ...over the live workspace, which stays mounted behind both.
    const frame = collectVnodes(root).find(
      (vnode) => vnode.tag === WorkspaceFrame,
    );
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
  });

  it("paints the options panel when it is the surface itself", () => {
    // Nothing has taken the window over on the options route, so the panel is
    // shown -- the hide is scoped to the popup that replaces it.
    const { state } = makeShell();
    const root = renderShell(state, OPTIONS_PATH, m("div#panel-content"));

    const layer = collectVnodes(root).find(
      (vnode) => vnode.attrs?.id === "ws-options-layer",
    );
    expect(String(attrsOf(layer as AnyVnode).style)).not.toContain(
      "visibility: hidden",
    );
  });

  it("draws all five titlebar icons, not just the one the popup is", () => {
    // The popup covers the titlebar's icons, so drawing only its own key took
    // the other four off screen for as long as it was open.
    const { state } = makeShell();
    stubTitlebar();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    const rendered = renderOverlay(appOverlay(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual([
      "permissions",
      "settings",
      "share",
      "notifications",
      "help",
    ]);
  });

  it("closes the popup from the key it hangs off, which covers the titlebar's own", () => {
    // The raised key sits exactly over the titlebar tab it stands in for, and
    // that tab is how the Permissions panel is put away -- so the same gesture
    // has to put the popup away. Inert, the click did nothing and the next one
    // (landing on the backdrop) made the popup look like it needed two.
    const { state } = makeShell();
    let dismissals = 0;
    (
      state as unknown as { dismissAppOverlay: () => boolean }
    ).dismissAppOverlay = () => {
      dismissals += 1;
      return true;
    };
    // The popup hangs off the titlebar key, so it only draws its own key when
    // there is one on screen to measure and cover.
    stubTitlebar();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    // The Shell hands the popup off to its own component, so render that to
    // reach the chrome it draws around the card.
    const rendered = renderOverlay(appOverlay(root) as AnyVnode);
    const keyTab = raisedIcon(rendered, "permissions");

    expect(keyTab).toBeDefined();
    (attrsOf(keyTab as AnyVnode).onclick as () => void)();

    expect(dismissals).toBe(1);
  });

  it("gives the popup its own card width, not the settings modal's", () => {
    const { state } = makeShell();
    stubTitlebar();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });
    const panel = collectVnodes(renderOverlay(appOverlay(root) as AnyVnode)).find(
      (vnode) => attrsOf(vnode).id === "app-overlay-panel",
    );
    expect(String(attrsOf(panel as AnyVnode).className)).toContain(
      "w-[600px]",
    );
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

describe("Shell app-overlay backdrop", () => {
  const HELP_PATH = "/help";

  it("keeps the remembered page painted, instead of the machine the modal names", () => {
    // Report a problem on the recovery page forwards ?workspace= so the report
    // identifies the right machine -- and that same param used to make the
    // Shell mount that machine's surface behind the form. Over the recovery
    // page that is the machine the page exists because it would not load: the
    // card the reader was reading got replaced by a loading frame.
    const { state } = makeShell();
    const recoveryPage = m("div#recovery-page");
    const root = renderShell(state, HELP_PATH, m("div#help-form"), {
      workspaceParam: null,
      behindContent: recoveryPage,
    });

    expect(popoverLayer(root, "help")).toBeDefined();
    expect(collectVnodes(root)).toContain(recoveryPage);
    expect(
      collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame),
    ).toBeUndefined();
    expect(
      collectVnodes(root).find((vnode) => attrsOf(vnode).id === "home-content"),
    ).toBeUndefined();
  });

  it("still floats over the live machine when no page was remembered", () => {
    // The same modal opened from inside a machine (or from its recovery card
    // as a modal): that machine is the surface, and it stays mounted.
    const { state } = makeShell();
    const root = renderShell(state, HELP_PATH, m("div#help-form"), {
      workspaceParam: null,
    });

    const frame = collectVnodes(root).find(
      (vnode) => vnode.tag === WorkspaceFrame,
    );
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
  });
});

describe("Shell notifications overlay", () => {
  function notificationsLayer(root: AnyVnode): AnyVnode | undefined {
    return popoverLayer(root, "notifications");
  }

  function shellWithFeedOpen(): FakeShell {
    return makeShell({
      isNotificationsOpen: true,
      closeNotifications: () => undefined,
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        notifications: {
        entries: [],
        unresolvedCount: 0,
        hasUnresolvedForWorkspace: () => false,
      },
      },
    } as unknown as Partial<ShellState>);
  }

  it("floats over whatever surface is on screen, a hub page included, without touching it", () => {
    // The New machine form is the routed content; opening the feed must leave
    // it as the surface and float the popover on top -- not swap in Home.
    const { state } = shellWithFeedOpen();
    const content = m("div#create-form");
    const root = renderShell(state, "/create", content, {
      workspaceParam: null,
    });
    expect(collectVnodes(root)).toContain(content);
    expect(
      collectVnodes(root).some((vnode) => attrsOf(vnode).id === "home-content"),
    ).toBe(false);
    expect(notificationsLayer(root)).toBeDefined();
  });

  it("renders nothing of the feed while it is closed", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    expect(notificationsLayer(root)).toBeUndefined();
  });

  it("holds the one overlay slot alone while a popup route is still leaving", () => {
    // A strip switch opens the feed and THEN navigates the popup away, so for
    // a beat the feed is open while the route is still /help. The slot must
    // show exactly one surface -- the feed, the one being switched to. Two
    // OverlayShells here is the flash: a doubled backdrop and strip for the
    // frames until the navigation lands.
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/help", m("div#help-form"));

    const shells = overlayShells(root);
    expect(shells).toHaveLength(1);
    expect(attrsOf(shells[0]).selected).toBe("notifications");
    // The leaving popup's content is not rendered at all, not even hidden.
    expect(
      collectVnodes(root).some((vnode) => attrsOf(vnode).id === "help-form"),
    ).toBe(false);
  });

  it("suppresses the live options panel underneath for the same switch beat", () => {
    // Same beat, other left-hand surface: feed opened from the docked panel,
    // dismissal navigation not yet landed. The panel (which draws its own
    // OverlayShell when rendered) must sit this beat out, or its backdrop
    // doubles the feed's.
    const { state } = shellWithFeedOpen();
    const content = m("div#options-panel-content");
    const root = renderShell(state, OPTIONS_PATH, content);

    expect(notificationsLayer(root)).toBeDefined();
    expect(collectVnodes(root)).not.toContain(content);
  });

  it("hangs the panel from the right-hand pair, aligned to the bug button", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    // The rects are measured when the overlay component instantiates.
    stubTitlebar();
    const rendered = renderOverlay(notificationsLayer(root) as AnyVnode);
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "notifications-panel",
    );
    expect(panel).toBeDefined();
    const style = String(attrsOf(panel as AnyVnode).style);
    // top = icon bottom (5 + 28), flush; right = 1000 - (940 + 28), the BUG
    // button's edge rather than the bell's -- so switching between the two
    // does not slide the box out from under the cursor.
    expect(style).toContain("top: 33px");
    expect(style).toContain("right: 32px");
    const panelClass = String(attrsOf(panel as AnyVnode).className);
    // A narrow dropdown, not a centered modal's width, and the same width
    // Get help gets.
    expect(panelClass).toContain("w-[400px]");
    // The bell is not the icon standing at the panel's right corner, so that
    // corner has an unselected button above it and nothing to join: rounded,
    // like the rest of the card.
    expect(panelClass).toContain("rounded-xl");
    expect(panelClass).not.toContain("rounded-tr-none");
  });

  it("draws its own raised bell over the dimmed real one, at its measured rect", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(notificationsLayer(root) as AnyVnode);
    const raisedBell = raisedIcon(rendered, "notifications");
    expect(raisedBell).toBeDefined();
    const style = String(attrsOf(raisedBell as AnyVnode).style);
    expect(style).toContain("left: 900px");
    expect(style).toContain("top: 5px");
    expect(style).toContain("width: 28px");
    expect(style).toContain("height: 28px");
    const raisedClass = String(attrsOf(raisedBell as AnyVnode).className);
    // The card's own light surface (not a re-colored titlebar tone), and
    // both bottom corners square -- the panel's flat top edge runs the full
    // width beneath the bell, not just under its right corner.
    expect(raisedClass).toContain("bg-surface-primary");
    expect(raisedClass).toContain("text-primary");
    expect(raisedClass).toContain("rounded-b-none");
  });

  it("raises the bug button alongside the bell, so one is a click from the other", () => {
    // On a hub page the three machine tabs are not on screen, so the strip is
    // the right-hand pair alone -- and Get help is still reachable without
    // clicking out of the feed first.
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    stubTitlebar(["notifications-toggle", "help-toggle"]);
    const rendered = renderOverlay(notificationsLayer(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual(["notifications", "help"]);
    const bug = raisedIcon(rendered, "help") as AnyVnode;
    expect(attrsOf(bug)["aria-selected"]).toBe("false");
    expect(typeof attrsOf(bug).onclick).toBe("function");
  });

  it("falls back to the centered card when no titlebar is mounted to hang from", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", { getElementById: () => null });
    const rendered = renderOverlay(notificationsLayer(root) as AnyVnode);
    // Centered rather than hung at a guessed rect, and with no titlebar there
    // is no strip to raise.
    const region = collectVnodes(rendered).find((vnode) =>
      String(attrsOf(vnode).className ?? "").includes("items-center"),
    );
    expect(region, "no centered region").toBeDefined();
    expect(raisedIconIds(rendered)).toEqual([]);
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "notifications-panel",
    );
    expect(String(attrsOf(panel as AnyVnode).className)).toContain("w-[400px]");
  });
});

describe("Shell help overlay", () => {
  function helpLayer(root: AnyVnode): AnyVnode | undefined {
    return popoverLayer(root, "help");
  }

  it("takes the same box the bell's feed takes, so a switch does not move it", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    // The rects are measured when the overlay component instantiates.
    stubTitlebar();
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "help-panel",
    );
    expect(panel).toBeDefined();
    const style = String(attrsOf(panel as AnyVnode).style);
    // Identical top and right to the feed's: top = icon bottom (5 + 28),
    // flush; right = 1000 - (940 + 28).
    expect(style).toContain("top: 33px");
    expect(style).toContain("right: 32px");
    const panelClass = String(attrsOf(panel as AnyVnode).className);
    expect(panelClass).toContain("w-[400px]");
    // The bug button IS the icon standing at the panel's right corner, so
    // that corner squares off and the two join into one shape with a tab;
    // top-left has nothing above it and stays rounded.
    expect(panelClass).toContain("rounded-xl");
    expect(panelClass).toContain("rounded-tr-none");
  });

  it("draws its own raised button over the dimmed real one, at its measured rect", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);
    const raisedButton = raisedIcon(rendered, "help");
    expect(raisedButton).toBeDefined();
    const style = String(attrsOf(raisedButton as AnyVnode).style);
    expect(style).toContain("left: 940px");
    expect(style).toContain("top: 5px");
    expect(style).toContain("width: 28px");
    expect(style).toContain("height: 28px");
    const raisedClass = String(attrsOf(raisedButton as AnyVnode).className);
    expect(raisedClass).toContain("bg-surface-primary");
    expect(raisedClass).toContain("text-primary");
    expect(raisedClass).toContain("rounded-b-none");
  });

  it("raises the machine tabs too, so the panel is a click away from a machine", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual([
      "permissions",
      "settings",
      "share",
      "notifications",
      "help",
    ]);
    expect(attrsOf(raisedIcon(rendered, "help") as AnyVnode)["aria-selected"]).toBe(
      "true",
    );
  });

  it("takes the same body shape the feed takes, so the two headers sit on one line", () => {
    // Both pages draw their own edge-to-edge title row and scroll below it
    // (HelpPage's suite pins its row); the card body is the same bare column
    // for both, so a switch swaps content under one unchanged header line.
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    const help = helpLayer(root);
    expect(String(attrsOf(help as AnyVnode).bodyClass)).toBe(
      "flex-1 min-h-0 flex flex-col",
    );
  });

  it("falls back to the centered card when no titlebar is mounted to hang from", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", { getElementById: () => null });
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);
    const region = collectVnodes(rendered).find((vnode) =>
      String(attrsOf(vnode).className ?? "").includes("items-center"),
    );
    expect(region, "no centered region").toBeDefined();
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "help-panel",
    );
    expect(String(attrsOf(panel as AnyVnode).className)).toContain("w-[400px]");
  });
});

describe("Shell app-overlay card chrome", () => {
  function overlayAttrsAt(routePath: string): Record<string, unknown> {
    const { state } = makeShell();
    const root = renderShell(state, routePath, m("div#overlay-content"), {
      workspaceParam: null,
    });
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
    expect(String(overlayAttrsAt("/settings").cardClass)).toContain(
      "h-[min(660px,",
    );
  });

  it("leaves every other overlay scrolling its card as a whole", () => {
    // Accounts, the request popup, the AI-keys dialog and the template stepper
    // are single columns that depend on the card scrolling. (Get help does not
    // come through here -- it takes the right-hand popover's own box, and its
    // own suite pins the scrolling body it gets there.)
    for (const routePath of [
      "/accounts",
      "/inbox",
      "/settings/ai-keys",
      "/create/template",
    ]) {
      expect(String(overlayAttrsAt(routePath).bodyClass), routePath).toContain(
        "overflow-y-auto",
      );
    }
  });
});

describe("Shell notice band wiring", () => {
  it("hands the device's condition to the band over a stuck machine", () => {
    // The band's decisions are its own suite's business; what is pinned here is
    // the call: the Shell reading the store's app-level condition into the
    // band's field, so a stuck machine on a blocked network is narrated as the
    // network rather than as its own failure.
    const { state } = makeShell({
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "stuck",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "SSH_BLOCKED",
          recoveryKindFor: () => null,
        },
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    const band = collectVnodes(root).find((vnode) => vnode.tag === NoticeBand);
    expect(attrsOf(band as AnyVnode).payload).toMatchObject({
      message: "This network blocks the connection to your machines.",
    });
  });

  it("speaks the device's own condition over a machine nothing has convicted yet", () => {
    // The case the whole app-level reading exists for: the user lands straight
    // on a machine page with the wifi off, and no machine has failed a probe
    // long enough to be convicted -- yet the band must already say what is
    // wrong rather than nothing at all.
    const { state } = makeShell({
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => ({
            id: WORKSPACE_ID,
            is_network_dependent: true,
          }),
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "OFFLINE",
          recoveryKindFor: () => null,
        },
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    const band = collectVnodes(root).find((vnode) => vnode.tag === NoticeBand);
    expect(attrsOf(band as AnyVnode).payload).toMatchObject({
      message: "No network connection.",
    });
  });

  it("reads locality off the machine's own row before speaking over it", () => {
    // A docker container answers over loopback, so a dead network explains
    // nothing about it and the band must not displace its recovery copy. The
    // band's own suite proves the parameter works; what is pinned here is which
    // field the Shell puts in it -- another boolean off the same row would
    // type-check, compile, and give every on-device machine a no-network band.
    const { state } = makeShell({
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => ({
            id: WORKSPACE_ID,
            is_network_dependent: false,
          }),
        },
        health: {
          statusFor: () => "stuck",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "OFFLINE",
          recoveryKindFor: () => null,
        },
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    const band = collectVnodes(root).find((vnode) => vnode.tag === NoticeBand);
    // Still a band -- the machine is stuck -- but the ordinary one, with the
    // device's condition kept out of it.
    expect(attrsOf(band as AnyVnode).payload).toMatchObject({
      message: "Lost connection to this machine. Reconnecting…",
    });
  });
});

describe("Shell toast layer", () => {
  function findToastLayer(root: AnyVnode): AnyVnode | undefined {
    return collectVnodes(root).find((vnode) => vnode.tag === ToastLayer);
  }

  it("wires the live toasts and dismiss callback from the notifications controller", () => {
    const dismissed: string[] = [];
    const liveEntry = notificationEntry("n1");
    const { state } = makeShell({
      notificationsUi: {
        liveToastEntries: (entries: unknown) => entries,
        dismissToast: (id: string) => dismissed.push(id),
      } as unknown as ShellState["notificationsUi"],
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        notifications: { entries: [liveEntry] },
      } as unknown as ShellState["stores"],
    });
    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#panel-content"),
    );

    const toastLayer = findToastLayer(root);
    expect(toastLayer).toBeDefined();
    expect(attrsOf(toastLayer as AnyVnode).toasts).toEqual([liveEntry]);

    (attrsOf(toastLayer as AnyVnode).onDismiss as (id: string) => void)("n1");
    expect(dismissed).toEqual(["n1"]);
  });

  it("suppresses the floating stack while the feed overlay is open, without even asking for the live toasts", () => {
    // The bell's feed IS the toasts' durable home while it is open; the
    // floating cards would be redundant with it (see Shell.ts's own comment).
    const { state } = makeShell({
      notificationsUi: {
        liveToastEntries: () => {
          throw new Error(
            "must not read live toasts while the feed overlay is open",
          );
        },
        dismissToast: () => undefined,
      } as unknown as ShellState["notificationsUi"],
      isNotificationsOpen: true,
      stores: {
        workspaces: {
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        notifications: { entries: [notificationEntry("n1")] },
      } as unknown as ShellState["stores"],
    });
    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#panel-content"),
    );

    const toastLayer = findToastLayer(root);
    expect(toastLayer).toBeDefined();
    expect(attrsOf(toastLayer as AnyVnode).toasts).toEqual([]);
  });

  it("renders no toast layer at all when the notifications controller isn't wired", () => {
    const { state } = makeShell(); // default notificationsUi: null
    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#panel-content"),
    );
    expect(findToastLayer(root)).toBeUndefined();
  });
});
