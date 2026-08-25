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

/** The app-modal layer, if the Shell floated one: the only vnode carrying a
 * `cardClass` (its own view, and the backdrop inside it, are not expanded by a
 * direct view() call). */
function appOverlay(root: AnyVnode): AnyVnode | undefined {
  return collectVnodes(root).find(
    (vnode) => attrsOf(vnode).cardClass !== undefined,
  );
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
    stores: {
      workspaces: {
        toAgentScopedId: (anyId: string) => anyId,
        entryByAnyId: () => null,
      },
      health: {
        statusFor: () => "healthy",
        discoveryHealth: "healthy",
        appEnvironmentBlock: () => "NONE",
      },
    },
    isRecoveryModalOpenFor: () => false,
    ...overrides,
  } as unknown as ShellState;
  return { state };
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

  it("draws all three option tabs, not just the one the popup is", () => {
    // The popup covers the titlebar's icon-tabs, so drawing only its own key
    // took Share and Machine settings off screen for as long as it was open.
    const { state } = makeShell();
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "ws-tab-strip"
          ? {
              getBoundingClientRect: () => ({
                left: 331,
                top: 5,
                width: 92,
                height: 28,
              }),
            }
          : null,
    });
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    const rendered = renderComponent(appOverlay(root) as AnyVnode);
    const strip = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "app-overlay-key-tab",
    );
    const tabs = collectVnodes(strip as AnyVnode)
      .map((vnode) => attrsOf(vnode)["data-wsopt-tab"])
      .filter((tab) => tab !== undefined);

    expect(tabs).toEqual(["permissions", "settings", "share"]);
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
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "ws-tab-strip"
          ? {
              getBoundingClientRect: () => ({
                left: 331,
                top: 5,
                width: 92,
                height: 28,
              }),
            }
          : null,
    });
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    // The Shell hands the popup off to its own component, so render that to
    // reach the chrome it draws around the card.
    const overlay = appOverlay(root) as AnyVnode;
    const rendered = renderComponent(overlay);
    const strip = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "app-overlay-key-tab",
    );
    const keyTab = collectVnodes(strip as AnyVnode).find(
      (vnode) => attrsOf(vnode)["data-wsopt-tab"] === "permissions",
    );

    expect(strip).toBeDefined();
    expect(keyTab).toBeDefined();
    (attrsOf(keyTab as AnyVnode).onclick as () => void)();

    expect(dismissals).toBe(1);
  });

  it("gives the popup its own card width, not the settings modal's", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });
    expect(String(attrsOf(appOverlay(root) as AnyVnode).cardClass)).toContain(
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

    expect(appOverlay(root)).toBeDefined();
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
  /** The popover's component vnode: keyed on shell state, not the route, so
   * it is found by the panel component it mounts rather than by cardClass
   * (the unanchored fallback reuses AppOverlay, the anchored form does not). */
  function notificationsLayer(root: AnyVnode): AnyVnode | undefined {
    return collectVnodes(root).find(
      (vnode) =>
        typeof vnode.tag === "function" &&
        (vnode.tag as { name?: string }).name === "NotificationsOverlay",
    );
  }

  function shellWithFeedOpen(): FakeShell {
    return makeShell({
      isNotificationsOpen: true,
      closeNotifications: () => undefined,
      stores: {
        workspaces: { toAgentScopedId: (anyId: string) => anyId },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentBlock: () => "NONE",
        },
        notifications: { entries: [], unresolvedCount: 0 },
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

  it("anchors the panel under the measured bell, right edge to right edge", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    // The bell rect is measured when the overlay component instantiates.
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "notifications-toggle"
          ? {
              getBoundingClientRect: () => ({
                left: 900,
                top: 5,
                width: 28,
                height: 28,
              }),
            }
          : null,
    });
    vi.stubGlobal("window", { location: { search: "" }, innerWidth: 1000 });
    const rendered = renderComponent(notificationsLayer(root) as AnyVnode);
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "notifications-panel",
    );
    expect(panel).toBeDefined();
    const style = String(attrsOf(panel as AnyVnode).style);
    // top = bell bottom (5 + 28), flush; right = 1000 - (900 + 28).
    expect(style).toContain("top: 33px");
    expect(style).toContain("right: 72px");
    // A narrow dropdown, not a centered modal's width.
    const panelClass = String(attrsOf(panel as AnyVnode).className);
    expect(panelClass).toContain("w-[360px]");
    // Square only the top-right corner (it is right-aligned under the raised
    // bell, and touches its own squared bottom edge); top-left has nothing
    // above it to join and stays rounded.
    expect(panelClass).toContain("rounded-xl");
    expect(panelClass).toContain("rounded-tr-none");
  });

  it("draws its own raised bell over the dimmed real one, at its measured rect", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "notifications-toggle"
          ? {
              getBoundingClientRect: () => ({
                left: 900,
                top: 5,
                width: 28,
                height: 28,
              }),
            }
          : null,
    });
    vi.stubGlobal("window", { location: { search: "" }, innerWidth: 1000 });
    const rendered = renderComponent(notificationsLayer(root) as AnyVnode);
    const raisedBell = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle-raised",
    );
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

  it("falls back to the centered card when no bell is mounted to hang from", () => {
    const { state } = shellWithFeedOpen();
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", { getElementById: () => null });
    const rendered = renderComponent(notificationsLayer(root) as AnyVnode);
    // The centered AppOverlay is itself a component vnode carrying cardClass,
    // dismissing to the popover's own closer rather than a navigation.
    expect(attrsOf(rendered).cardClass).toContain("w-[360px]");
    expect(typeof attrsOf(rendered).onDismiss).toBe("function");
  });
});

describe("Shell help overlay", () => {
  /** HelpOverlay's own component vnode, found the same way notificationsLayer
   * finds NotificationsOverlay's -- by the component it mounts, since the
   * unanchored fallback reuses AppOverlay and would not carry cardClass at
   * this level. */
  function helpLayer(root: AnyVnode): AnyVnode | undefined {
    return collectVnodes(root).find(
      (vnode) =>
        typeof vnode.tag === "function" &&
        (vnode.tag as { name?: string }).name === "HelpOverlay",
    );
  }

  it("anchors the panel under the measured bug-report button, right edge to right edge", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    // The button rect is measured when the overlay component instantiates.
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "help-toggle"
          ? {
              getBoundingClientRect: () => ({
                left: 940,
                top: 5,
                width: 28,
                height: 28,
              }),
            }
          : null,
    });
    vi.stubGlobal("window", { location: { search: "" }, innerWidth: 1000 });
    const rendered = renderComponent(helpLayer(root) as AnyVnode);
    const panel = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "help-panel",
    );
    expect(panel).toBeDefined();
    const style = String(attrsOf(panel as AnyVnode).style);
    // top = button bottom (5 + 28), flush; right = 1000 - (940 + 28).
    expect(style).toContain("top: 33px");
    expect(style).toContain("right: 32px");
    const panelClass = String(attrsOf(panel as AnyVnode).className);
    // Square only the top-right corner (it is right-aligned under the raised
    // button, and touches its own squared bottom edge); top-left has
    // nothing above it to join and stays rounded.
    expect(panelClass).toContain("rounded-xl");
    expect(panelClass).toContain("rounded-tr-none");
  });

  it("draws its own raised button over the dimmed real one, at its measured rect", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", {
      getElementById: (id: string) =>
        id === "help-toggle"
          ? {
              getBoundingClientRect: () => ({
                left: 940,
                top: 5,
                width: 28,
                height: 28,
              }),
            }
          : null,
    });
    vi.stubGlobal("window", { location: { search: "" }, innerWidth: 1000 });
    const rendered = renderComponent(helpLayer(root) as AnyVnode);
    const raisedButton = collectVnodes(rendered).find(
      (vnode) => attrsOf(vnode).id === "help-toggle-raised",
    );
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

  it("falls back to the centered card when no bug-report button is mounted to hang from", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    vi.stubGlobal("document", { getElementById: () => null });
    const rendered = renderComponent(helpLayer(root) as AnyVnode);
    const inner = appOverlay(rendered);
    expect(inner).toBeDefined();
    expect(typeof attrsOf(inner as AnyVnode).onDismiss).toBe("function");
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
    // Accounts, Get help, the request popup, the AI-keys dialog and the
    // template stepper are single columns that depend on the card scrolling.
    for (const routePath of [
      "/accounts",
      "/help",
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
          appEnvironmentBlock: () => "SSH_BLOCKED",
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
          appEnvironmentBlock: () => "OFFLINE",
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
          appEnvironmentBlock: () => "OFFLINE",
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
          appEnvironmentBlock: () => "NONE",
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
          appEnvironmentBlock: () => "NONE",
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
