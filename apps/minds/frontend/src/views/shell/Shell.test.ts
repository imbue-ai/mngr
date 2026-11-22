import m from "mithril";
import { RequestsStore } from "../../models/requests";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import { LocalPageNotice } from "./LocalPageNotice";
import { NoticeBand } from "./NoticeBand";
import { Shell } from "./Shell";
import { ToastLayer } from "./ToastLayer";
import { RecoveryModal } from "../recovery/RecoveryModal";
import { DialogCloseButton } from "../components/Modal";
import { UpdateApplyModal } from "../components/UpdateApplyModal";
import { WebLoginModal } from "../components/WebLoginModal";
import { WorkspaceFrame } from "./WorkspaceFrame";
import { AutoUpdatePromptModal } from "../components/AutoUpdatePromptModal";
import { AutoUpdatePromptModel } from "../../models/autoUpdatePrompt";
import type { UiNotificationEntry } from "../../channel/messages";
import type { ToastLink } from "../../electron-bridge";
import type { ToastItem } from "../../models/notificationsUi";
import type { AnyVnode, StubRect } from "../../testing";
import {
  allText,
  attrsOf,
  collectVnodes,
  jsonResponse,
  notificationEntry,
  renderRoot,
  settingsOverview,
  settle,
  stubTitlebarRects,
} from "../../testing";

const WORKSPACE_ID = "agent-ab12";
const SETTINGS_PATH = "/settings";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Every OverlayShell vnode the Shell floated: at most one in its single
 * overlay slot, and Settings' own beside it. */
function overlayShells(root: AnyVnode): AnyVnode[] {
  return collectVnodes(root).filter(
    (vnode) =>
      typeof vnode.tag === "function" &&
      (vnode.tag as { name?: string }).name === "OverlayShell",
  );
}

/** The layer Settings has to itself, which the Shell emits on every route. */
function settingsLayer(root: AnyVnode): AnyVnode {
  const layer = collectVnodes(root).find((vnode) => vnode.attrs?.id === "settings-layer");
  expect(layer, "no settings layer").toBeDefined();
  return layer as AnyVnode;
}

/** Settings' own overlay, if the Shell floated it. */
function settingsOverlay(root: AnyVnode): AnyVnode | undefined {
  return overlayShells(settingsLayer(root))[0];
}

/** The overlay in the Shell's one shared slot (every floating surface but Settings), if it floated one. */
function appOverlay(root: AnyVnode): AnyVnode | undefined {
  const settings = settingsOverlay(root);
  return overlayShells(root).find((vnode) => vnode !== settings);
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

/** The updates store as the Shell reads it; a stub missing a method only works
 * until the short-circuit hiding it moves. */
function updatesStoreStub(
  availability: "UP_TO_DATE" | "OUT_OF_DATE",
  isUpdating = false,
  isApplying = false,
  isAutoUpdateEnabled = false,
  fields: Record<string, unknown> = {},
) {
  const update = { availability, activity: isApplying ? "APPLYING" : "IDLE", ...fields };
  return {
    forAgent: () => update,
    publishedFor: () => update,
    isApplying: () => isApplying,
    isUpdating: () => isUpdating,
    isAutoUpdateEnabled,
    autoUpdateFor: () => ({ isEnabled: isAutoUpdateEnabled, window: "2:00 AM-5:00 AM", override: null }),
    hasReceivedUpdates: true,
  };
}

/** A shell whose machine is healthy and whose discovery is up, so neither the
 * notice band nor the recovery card floats: these suites are about the overlay
 * layer, and both of those surfaces are covered by their own tests. */
function makeShell(overrides: Partial<ShellState> = {}): FakeShell {
  const state = {
    channel: null,
    isWorkspaceSwitcherOpen: false,
    isAccountSwitcherOpen: false,
    notificationsUi: null,
    currentRouteSearch: () => `workspace=${WORKSPACE_ID}`,
    closeAppOverlay: () => true,
    displayedWorkspaceAnyId: WORKSPACE_ID,
    displayedWorkspaceAgentId: () => WORKSPACE_ID,
    accountId: "local",
    isAccountSignedOut: () => false,
    stores: {
      requests: { hasPendingForWorkspace: () => false },
      accounts: { labelFor: () => "This device", emailLabelFor: () => "This device" },
      workspaces: {
        workspaces: [],
        toAgentScopedId: (anyId: string) => anyId,
        entryByAnyId: () => null,
        accentEntry: () => null,
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
      },
      updates: updatesStoreStub("UP_TO_DATE"),
    },
    isRecoveryModalOpenFor: () => false,
    // Never loaded, so it never offers: suites not about it see no prompt.
    autoUpdatePrompt: new AutoUpdatePromptModel(),
    ...overrides,
  } as unknown as ShellState;
  return { state };
}

/** The bar's measurable boxes in a 1000px window, laid out as the real one
 * lays them out: the trail's account and workspace buttons at the left; the
 * bell, the bug-report button, the gear and Share at the right. */
const TITLEBAR_RECTS: Record<string, StubRect> = {
  "account-switcher-btn": { left: 96, top: 8, width: 160, height: 28 },
  "workspace-switcher-btn": { left: 272, top: 8, width: 120, height: 28 },
  "notifications-toggle": { left: 812, top: 8, width: 28, height: 28 },
  "help-toggle": { left: 844, top: 8, width: 28, height: 28 },
  "settings-btn": { left: 876, top: 8, width: 28, height: 28 },
  "share-btn": { left: 912, top: 8, width: 76, height: 28 },
};

/** The bar of a hub page other than Home: no workspace button, and no Share. */
const HUB_PAGE_BAR_IDS = ["account-switcher-btn", "notifications-toggle", "help-toggle", "settings-btn"];

/** Put a measurable titlebar on screen. `ids` names which boxes are there; the
 * rest read as absent, the way a hub page's workspace button does. Call it AFTER
 * `renderShell` (whose own window stub carries no innerWidth) and before the
 * overlay component is instantiated, which is when it measures. */
function stubTitlebar(
  ids: readonly string[] = Object.keys(TITLEBAR_RECTS),
): void {
  stubTitlebarRects(Object.fromEntries(ids.filter((id) => id in TITLEBAR_RECTS).map((id) => [id, TITLEBAR_RECTS[id]])));
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

/** Whether an overlay, rendered, raised a strip at all. */
function hasRaisedStrip(rendered: AnyVnode): boolean {
  return collectVnodes(rendered).some(
    (vnode) => typeof vnode.tag === "function" && (vnode.tag as { name?: string }).name === "RaisedTitlebarIcons",
  );
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
  settingsContent?: m.Children;
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
  // The shell reports the route it is rendered at, as the live one does: the raised strip reads it.
  Object.assign(shell, { currentRoutePath: () => routePath });
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
    settingsContent: options.settingsContent ?? null,
  } as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  ) as unknown as AnyVnode;
}

describe("Shell Settings layer", () => {
  it("floats Settings in a layer of its own, hung from the gear, and leaves the shared slot empty", () => {
    const { state } = makeShell();
    const content = m("div#settings-page");
    const root = renderShell(state, SETTINGS_PATH, content, { workspaceParam: null });

    expect(appOverlay(root)).toBeUndefined();
    const settings = settingsOverlay(root);
    expect(settings).toBeDefined();
    expect(collectVnodes(settings).filter((vnode) => vnode === content)).toHaveLength(1);
    expect(attrsOf(settings as AnyVnode)).toMatchObject({
      placement: "anchored",
      selected: "settings",
      panelId: "settings-panel",
      backdropId: "settings-backdrop",
    });
    // Right-aligned to its own button: only the feed and Get help share a box.
    expect(attrsOf(settings as AnyVnode).alignTo).toBeUndefined();
  });

  it("keeps the workspace Settings was opened over mounted behind it", () => {
    const { state } = makeShell();
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });

    const frame = collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame);
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
    expect(collectVnodes(root).some((vnode) => attrsOf(vnode).id === "home-content")).toBe(false);
  });

  it("floats Settings over Home when it names no workspace, whatever workspace it has picked", () => {
    const { state } = makeShell({
      currentRouteSearch: () => `tab=workspace&settings_workspace=${WORKSPACE_ID}`,
    } as Partial<ShellState>);
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });

    expect(settingsOverlay(root)).toBeDefined();
    expect(collectVnodes(root).some((vnode) => attrsOf(vnode).id === "home-content")).toBe(true);
    expect(collectVnodes(root).some((vnode) => vnode.tag === WorkspaceFrame)).toBe(false);
  });

  it("keeps the remembered recovery page painted behind Settings, instead of the machine it names", () => {
    const { state } = makeShell();
    const recoveryPage = m("div#recovery-page");
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), {
      workspaceParam: null,
      behindContent: recoveryPage,
    });

    expect(settingsOverlay(root)).toBeDefined();
    expect(collectVnodes(root)).toContain(recoveryPage);
    expect(collectVnodes(root).some((vnode) => vnode.tag === WorkspaceFrame)).toBe(false);
  });

  it("hangs the card from the gear's bottom edge, right-aligned and joined to it", () => {
    const { state } = makeShell();
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });
    // The rects are measured when the overlay component instantiates.
    stubTitlebar();
    const rendered = renderOverlay(settingsOverlay(root) as AnyVnode);

    const panel = collectVnodes(rendered).find((vnode) => attrsOf(vnode).id === "settings-panel");
    expect(panel).toBeDefined();
    const style = String(attrsOf(panel as AnyVnode).style);
    // top = the gear's bottom (8 + 28), flush; right = 1000 - (876 + 28).
    expect(style).toContain("top: 36px");
    expect(style).toContain("right: 96px");
    const panelClass = String(attrsOf(panel as AnyVnode).className);
    expect(panelClass).toContain("w-[880px]");
    // The gear stands at the card's right corner, so that corner squares off.
    expect(panelClass).toContain("rounded-tr-none");
  });

  it("draws no close X on Settings: a flyout is put away by its own button", () => {
    const { state } = makeShell();
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });
    stubTitlebar();
    const rendered = renderOverlay(settingsOverlay(root) as AnyVnode);

    expect(collectVnodes(rendered).some((vnode) => vnode.tag === DialogCloseButton)).toBe(false);
  });

  it("raises every bar button over Settings, the gear lit, and puts Settings away from the gear", () => {
    const { state } = makeShell();
    let dismissals = 0;
    (state as unknown as { dismissAppOverlay: () => boolean }).dismissAppOverlay = () => {
      dismissals += 1;
      return true;
    };
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });
    stubTitlebar();
    const rendered = renderOverlay(settingsOverlay(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual(["account", "workspace", "notifications", "help", "settings"]);
    const gear = raisedIcon(rendered, "settings") as AnyVnode;
    expect(attrsOf(gear)["aria-selected"]).toBe("true");
    expect(attrsOf(raisedIcon(rendered, "help") as AnyVnode)["aria-selected"]).toBe("false");

    (attrsOf(gear).onclick as () => void)();
    expect(dismissals).toBe(1);
  });

  it("dismisses Settings through the shell from a click on the dimmed window", () => {
    const { state } = makeShell();
    let dismissals = 0;
    (state as unknown as { dismissAppOverlay: () => boolean }).dismissAppOverlay = () => {
      dismissals += 1;
      return true;
    };
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });

    (attrsOf(settingsOverlay(root) as AnyVnode).onDismiss as () => void)();

    expect(dismissals).toBe(1);
  });

  it("carries the waiting-on-you dot on the raised gear while a dismissed reminder still has a pending request", () => {
    const { state } = makeShell();
    const requests = new RequestsStore();
    requests.applyRequestsMessage({
      count: 1,
      request_ids: ["evt-a"],
      workspace_agent_ids: [WORKSPACE_ID],
    });
    state.stores.requests = requests;
    const root = renderShell(state, "/help", m("div#help-form"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(appOverlay(root) as AnyVnode);

    const gear = raisedIcon(rendered, "settings") as AnyVnode;
    expect(collectVnodes(gear).some((vnode) => attrsOf(vnode).id === "permissions-badge-raised")).toBe(true);
    // The feed's own count is the bell's, and nothing is unread there.
    expect(
      collectVnodes(raisedStrip(rendered)).some(
        (vnode) => attrsOf(vnode).id === "notifications-badge-raised",
      ),
    ).toBe(false);
  });

  it("emits the Settings layer on every route, empty where Settings is not up, so Settings keeps one slot", () => {
    const { state } = makeShell();
    for (const [routePath, workspaceParam] of [
      [`/workspace/${WORKSPACE_ID}`, WORKSPACE_ID],
      ["/help", null],
      ["/inbox", null],
      ["/account/local/", null],
    ] as const) {
      const root = renderShell(state, routePath, m("div#routed-page"), { workspaceParam });
      expect(settingsOverlay(root), routePath).toBeUndefined();
      expect(String(attrsOf(settingsLayer(root)).style), routePath).toBe("display: contents");
    }
  });
});

describe("Shell request popup layer", () => {
  it("keeps Settings mounted under the popup opened from it, and hides it", () => {
    // The popup is a route (/inbox), so Settings is no longer the routed page:
    // it is the copy the router keeps, and it holds the SAME vtree slot as the
    // routed one, so its models are not torn down and rebuilt.
    //
    // Hidden rather than unmounted: Settings keeps its scroll, its section and
    // its in-flight edits for the return trip.
    const { state } = makeShell();
    const popupBody = m("div#request-popup");
    const settings = m("div#settings-page");
    const root = renderShell(state, "/inbox", popupBody, {
      workspaceParam: null,
      settingsContent: settings,
    });

    expect(collectVnodes(appOverlay(root))).toContain(popupBody);
    expect(collectVnodes(settingsOverlay(root))).toContain(settings);
    expect(String(attrsOf(settingsLayer(root)).style)).toContain(
      "visibility: hidden",
    );
    // ...over the live workspace, which stays mounted behind both.
    const frame = collectVnodes(root).find(
      (vnode) => vnode.tag === WorkspaceFrame,
    );
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
  });

  it("emits the popup after the Settings it covers, so it paints on top at the same z", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
      settingsContent: m("div#settings-page"),
    });

    const order = collectVnodes(root);
    expect(order.indexOf(appOverlay(root) as AnyVnode)).toBeGreaterThan(
      order.indexOf(settingsOverlay(root) as AnyVnode),
    );
  });

  it("paints Settings when it is the route itself", () => {
    // Nothing covers it on its own route -- the hide is scoped to the popup
    // that floats over it.
    const { state } = makeShell();
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), { workspaceParam: null });

    expect(String(attrsOf(settingsLayer(root)).style)).not.toContain(
      "visibility: hidden",
    );
  });

  it("hides nothing under a popup that was opened from the chat, which has no Settings beneath it", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/inbox", m("div#request-popup"), { workspaceParam: null });

    expect(settingsOverlay(root)).toBeUndefined();
    expect(String(attrsOf(settingsLayer(root)).style)).not.toContain("visibility: hidden");
  });

  it("draws the popup as a centered card that resizes with its content", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    expect(attrsOf(appOverlay(root) as AnyVnode)).toMatchObject({
      placement: "centered",
      selected: null,
      animatesBox: true,
      panelId: "app-overlay-panel",
      backdropId: "app-overlay-backdrop",
    });
  });

  it("raises no strip over the popup, even with a bar on screen to copy, and gives it a close X", () => {
    // A centered card hangs from no bar button, so there is no button of its
    // own to raise, and it is put away by its X or the dimmed window.
    const { state } = makeShell();
    let dismissals = 0;
    (
      state as unknown as { dismissAppOverlay: () => boolean }
    ).dismissAppOverlay = () => {
      dismissals += 1;
      return true;
    };
    stubTitlebar();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });

    const rendered = renderOverlay(appOverlay(root) as AnyVnode);

    expect(hasRaisedStrip(rendered)).toBe(false);
    const region = collectVnodes(rendered).find((vnode) =>
      String(attrsOf(vnode).className ?? "").includes("items-center"),
    );
    expect(region, "no centered region").toBeDefined();
    const closeX = collectVnodes(rendered).find((vnode) => vnode.tag === DialogCloseButton);
    expect(closeX).toBeDefined();
    (attrsOf(closeX as AnyVnode).onClose as () => void)();
    expect(dismissals).toBe(1);
  });

  it("gives the popup its own card width, not the settings modal's", () => {
    const { state } = makeShell();
    stubTitlebar();
    const root = renderShell(state, "/inbox", m("div#request-popup"), {
      workspaceParam: null,
    });
    const panel = collectVnodes(
      renderOverlay(appOverlay(root) as AnyVnode),
    ).find((vnode) => attrsOf(vnode).id === "app-overlay-panel");
    expect(String(attrsOf(panel as AnyVnode).className)).toContain("w-[600px]");
  });

  it("keeps the routed page as the surface itself on a plain workspace route", () => {
    const { state } = makeShell();
    const content = m("div#panel-content");
    const root = renderShell(state, `/workspace/${WORKSPACE_ID}`, content);
    expect(appOverlay(root)).toBeUndefined();
    expect(settingsOverlay(root)).toBeUndefined();
    // The frame is the surface: the routed page vnode is not painted at all.
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

  it("leaves the network notice to a kept recovery page, which places its own", () => {
    const localNotices = (root: AnyVnode): AnyVnode[] =>
      collectVnodes(root).filter((vnode) => vnode.tag === LocalPageNotice);
    const { state: overRecovery } = makeShell({
      pageRouteBehindOverlay: `/agents/${WORKSPACE_ID}/recovery?workspace=${WORKSPACE_ID}`,
    } as Partial<ShellState>);
    const keptRecovery = renderShell(overRecovery, HELP_PATH, m("div#help-form"), {
      workspaceParam: null,
      behindContent: m("div#recovery-page"),
    });
    expect(localNotices(keptRecovery)).toHaveLength(0);

    // Home under the same modal has no notice of its own, so the Shell keeps it.
    const { state: overHome } = makeShell({
      currentRouteSearch: () => "",
    } as Partial<ShellState>);
    const home = renderShell(overHome, HELP_PATH, m("div#help-form"), {
      workspaceParam: null,
    });
    expect(localNotices(home)).toHaveLength(1);
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
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device", emailLabelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
          accentEntry: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("UP_TO_DATE"),
        notifications: {
          entries: [],
          unresolvedCount: 0,
        },
      },
    } as unknown as Partial<ShellState>);
  }

  it("floats over whatever surface is on screen, a hub page included, without touching it", () => {
    // The New workspace form is the routed content; opening the feed must leave
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

  it("suppresses the live Settings underneath for the same switch beat", () => {
    // Same beat, other routed flyout: feed opened from Settings, dismissal
    // navigation not yet landed. Settings (which draws its own OverlayShell
    // when rendered) must sit this beat out, or its backdrop doubles the
    // feed's.
    const { state } = shellWithFeedOpen();
    const content = m("div#settings-page");
    const root = renderShell(state, SETTINGS_PATH, content, { workspaceParam: null });

    expect(notificationsLayer(root)).toBeDefined();
    expect(overlayShells(root)).toHaveLength(1);
    expect(settingsOverlay(root)).toBeUndefined();
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
    // top = icon bottom (8 + 28), flush; right = 1000 - (844 + 28), the BUG
    // button's edge rather than the bell's -- so switching between the two
    // does not slide the box out from under the cursor.
    expect(style).toContain("top: 36px");
    expect(style).toContain("right: 128px");
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
    expect(style).toContain("left: 812px");
    expect(style).toContain("top: 8px");
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

  it("raises the bar's other buttons alongside the bell, so each is a click from the feed", () => {
    // On a hub page the workspace button is not on the bar, so it has no copy
    // -- and Get help and Settings are still reachable without clicking out
    // of the feed first.
    const { state } = shellWithFeedOpen();
    const switches: string[] = [];
    Object.assign(state, {
      openHelp: () => switches.push("help"),
      openSettingsFromBar: () => switches.push("settings"),
      switchToAccountSwitcher: () => switches.push("account"),
    });
    const root = renderShell(state, "/create", m("div#create-form"), {
      workspaceParam: null,
    });
    stubTitlebar(HUB_PAGE_BAR_IDS);
    const rendered = renderOverlay(notificationsLayer(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual(["account", "notifications", "help", "settings"]);
    expect(attrsOf(raisedIcon(rendered, "notifications") as AnyVnode)["aria-selected"]).toBe("true");
    for (const id of ["account", "help", "settings"]) {
      const copy = raisedIcon(rendered, id) as AnyVnode;
      expect(attrsOf(copy)["aria-selected"], id).toBe("false");
      (attrsOf(copy).onclick as () => void)();
    }
    // Each leaves the feed for its own flyout, through the shell's one switch per flyout.
    expect(switches).toEqual(["account", "help", "settings"]);
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
    // Identical top and right to the feed's: top = icon bottom (8 + 28),
    // flush; right = 1000 - (844 + 28).
    expect(style).toContain("top: 36px");
    expect(style).toContain("right: 128px");
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
    expect(style).toContain("left: 844px");
    expect(style).toContain("top: 8px");
    expect(style).toContain("width: 28px");
    expect(style).toContain("height: 28px");
    const raisedClass = String(attrsOf(raisedButton as AnyVnode).className);
    expect(raisedClass).toContain("bg-surface-primary");
    expect(raisedClass).toContain("text-primary");
    expect(raisedClass).toContain("rounded-b-none");
  });

  it("raises every bar button, so Settings and both menus are a click away from Get help", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);

    expect(raisedIconIds(rendered)).toEqual([
      "account",
      "workspace",
      "notifications",
      "help",
      "settings",
    ]);
    expect(
      attrsOf(raisedIcon(rendered, "help") as AnyVnode)["aria-selected"],
    ).toBe("true");
    // Share is not a flyout of its own, but its live copy stands in the strip beside them.
    expect(collectVnodes(raisedStrip(rendered)).some((vnode) => attrsOf(vnode).id === "share-btn-raised")).toBe(true);
  });

  it("draws no close X on Get help: a flyout is put away by its own button", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/help", m("div#help-content"), {
      workspaceParam: null,
    });
    stubTitlebar();
    const rendered = renderOverlay(helpLayer(root) as AnyVnode);

    expect(collectVnodes(rendered).some((vnode) => vnode.tag === DialogCloseButton)).toBe(false);
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

describe("Shell sign-in modal layering", () => {
  it("paints the sign-in modal over the Settings flyout whose Accounts tab opened it", () => {
    // Both sit at z-[110], so DOM order decides which is on top.
    const { state } = makeShell({ currentRouteSearch: () => "tab=accounts" } as Partial<ShellState>);
    const root = renderShell(state, SETTINGS_PATH, m("div#settings-page"), {
      workspaceParam: null,
    });

    const order = collectVnodes(root);
    const overlayIndex = order.indexOf(settingsOverlay(root) as AnyVnode);
    const signInIndex = order.findIndex((vnode) => vnode.tag === WebLoginModal);
    expect(overlayIndex).toBeGreaterThanOrEqual(0);
    expect(signInIndex).toBeGreaterThan(overlayIndex);
  });

  it("paints the sign-in modal over a centered app modal too", () => {
    const { state } = makeShell();
    const root = renderShell(state, "/settings/ai-keys", m("div#ai-keys"), {
      workspaceParam: null,
    });

    const order = collectVnodes(root);
    const overlayIndex = order.indexOf(appOverlay(root) as AnyVnode);
    const signInIndex = order.findIndex((vnode) => vnode.tag === WebLoginModal);
    expect(overlayIndex).toBeGreaterThanOrEqual(0);
    expect(signInIndex).toBeGreaterThan(overlayIndex);
  });
});

describe("Shell app-overlay card chrome", () => {
  function overlayAttrsAt(routePath: string): Record<string, unknown> {
    const { state } = makeShell();
    const root = renderShell(state, routePath, m("div#overlay-content"), {
      workspaceParam: null,
    });
    const overlay = routePath === SETTINGS_PATH ? settingsOverlay(root) : appOverlay(root);
    expect(overlay, `${routePath} floats an overlay card`).toBeDefined();
    return attrsOf(overlay as AnyVnode);
  }

  it("hands Settings a bounded column instead of a scrolling card body", () => {
    // Its pane scrolls its own two columns; a scroller here would take the
    // section list down with the panel, which is the bug it exists to prevent.
    const bodyClass = String(overlayAttrsAt(SETTINGS_PATH).bodyClass);
    expect(bodyClass).toContain("flex-1");
    expect(bodyClass).toContain("min-h-0");
    expect(bodyClass).toContain("flex-col");
    expect(bodyClass).not.toContain("overflow-y-auto");
  });

  it("gives the settings card a definite height for that column to fill", () => {
    // A flex-1 body inside an auto-height card has no height to hand its
    // columns, so they would never scroll and the card would resize per
    // section, moving the list out from under the cursor.
    const cardClass = String(overlayAttrsAt(SETTINGS_PATH).cardClass);
    expect(cardClass).toContain("h-[min(660px,");
    expect(cardClass).toContain("w-[880px]");
  });

  it("leaves every centered card scrolling as a whole, with no bar button of its own to raise", () => {
    // The request popup, the AI-keys dialog and the template stepper are
    // single columns that depend on the card scrolling. (Get help does not
    // come through here -- it takes the right-hand popover's own box, and its
    // own suite pins the scrolling body it gets there.)
    for (const routePath of [
      "/inbox",
      "/settings/ai-keys",
      "/create/template",
      // The route the stepper is actually opened on: its account's hub.
      "/account/local/create/template",
    ]) {
      const attrs = overlayAttrsAt(routePath);
      expect(String(attrs.bodyClass), routePath).toContain("overflow-y-auto");
      expect(attrs.placement, routePath).toBe("centered");
      // No bar button of its own, so no strip is raised over it.
      expect(attrs.selected, routePath).toBeNull();
    }
  });

  it("floats nothing for the retired Manage Accounts URL, which only redirects into Settings", () => {
    const { state } = makeShell({ currentRouteSearch: () => "" } as Partial<ShellState>);
    const content = m("div#redirecting");
    const root = renderShell(state, "/accounts", content, { workspaceParam: null });

    expect(overlayShells(root)).toHaveLength(0);
    expect(collectVnodes(root)).toContain(content);
  });
});

describe("Shell notice band wiring", () => {
  it("hands the device's condition to the band over a stuck machine", () => {
    // The band's decisions are its own suite's business; what is pinned here is
    // the call: the Shell reading the store's app-level condition into the
    // band's field, so a stuck machine on a blocked network is narrated as the
    // network rather than as its own failure.
    const { state } = makeShell({
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "stuck",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "SSH_BLOCKED",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("UP_TO_DATE"),
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    const band = collectVnodes(root).find((vnode) => vnode.tag === NoticeBand);
    expect(attrsOf(band as AnyVnode).payload).toMatchObject({
      message: "This network won't let Imbue Studio reach your workspaces. Try a different network.",
    });
  });

  it("leaves a band that would only open the recovery card to the open card, and keeps one offering anything else", () => {
    const bandUnderOpenCard = (discoveryHealth: string): AnyVnode | undefined => {
      const { state } = makeShell({
        stores: {
          requests: { hasPendingForWorkspace: () => false },
          accounts: { labelFor: () => "This device" },
          workspaces: {
            workspaces: [],
            toAgentScopedId: (anyId: string) => anyId,
            entryByAnyId: () => null,
          },
          health: {
            statusFor: () => "stuck",
            discoveryHealth,
            appEnvironmentCondition: () => "NONE",
            recoveryKindFor: () => null,
          },
          updates: updatesStoreStub("UP_TO_DATE"),
        },
        isRecoveryModalOpenFor: () => true,
        isRecoveryModalAutoRaised: () => false,
      } as unknown as Partial<ShellState>);
      const root = renderShell(state, `/workspace/${WORKSPACE_ID}`, m("div#content"));
      expect(collectVnodes(root).some((vnode) => vnode.tag === RecoveryModal)).toBe(true);
      return collectVnodes(root).find((vnode) => vnode.tag === NoticeBand);
    };

    // A stuck machine's band offers only "Open recovery", which is already open.
    expect(bandUnderOpenCard("healthy")).toBeUndefined();
    // Discovery dying offers an app restart the card does not.
    expect(attrsOf(bandUnderOpenCard("blocked") as AnyVnode).payload).toMatchObject({ key: "discovery-blocked" });
  });

  it.each([
    ["out of date", false, false],
    ["preparing its update", true, false],
    ["landing its update", true, true],
  ])("says nothing on the band about a machine %s: its update is the titlebar pill's", (_name, isUpdating, isApplying) => {
    const { state } = makeShell({
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          // Mid-apply the machine reads unhealthy: the apply took its services down on purpose.
          statusFor: () => (isApplying ? "stuck" : "healthy"),
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("OUT_OF_DATE", isUpdating, isApplying),
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(state, `/workspace/${WORKSPACE_ID}`, m("div#content"));

    expect(collectVnodes(root).some((vnode) => vnode.tag === NoticeBand)).toBe(false);
  });

  it("covers a machine whose update is landing with a card nothing dismisses", () => {
    // A band over a still-usable surface invites the attempt that looks like
    // the machine breaking; the backdrop starts below the titlebar so the
    // switcher stays reachable.
    const { state } = makeShell({
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => ({ name: "oldie" }),
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("OUT_OF_DATE", true, true),
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    const modal = collectVnodes(root).find(
      (vnode) => vnode.tag === UpdateApplyModal,
    );
    expect(attrsOf(modal as AnyVnode)).toMatchObject({
      workspaceName: "oldie",
    });
    const card = renderRoot(UpdateApplyModal, { workspaceName: "oldie" });
    expect(allText(card)).toContain("Updating oldie");
    expect(collectVnodes(card).some((vnode) => vnode.tag === "button")).toBe(
      false,
    );
  });

  it("does not cover a machine while its update is only preparing", () => {
    const { state } = makeShell({
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("OUT_OF_DATE", true),
      },
    } as unknown as Partial<ShellState>);

    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#content"),
    );

    expect(
      collectVnodes(root).some((vnode) => vnode.tag === UpdateApplyModal),
    ).toBe(false);
  });

  it("speaks the device's own condition over a machine nothing has convicted yet", () => {
    // The case the whole app-level reading exists for: the user lands straight
    // on a machine page with the wifi off, and no machine has failed a probe
    // long enough to be convicted -- yet the band must already say what is
    // wrong rather than nothing at all.
    const { state } = makeShell({
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
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
        updates: updatesStoreStub("UP_TO_DATE"),
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
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
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
        updates: updatesStoreStub("UP_TO_DATE"),
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
      message: "Lost connection to this workspace. Reconnecting…",
    });
  });
});

describe("Shell toast layer", () => {
  function findToastLayer(root: AnyVnode): AnyVnode | undefined {
    return collectVnodes(root).find((vnode) => vnode.tag === ToastLayer);
  }

  it("wires the live toasts and the dismiss and open callbacks from the notifications controller", () => {
    const dismissed: string[] = [];
    const openedEntries: string[] = [];
    const openedLinks: ToastLink[] = [];
    const liveEntry = notificationEntry("n1");
    const { state } = makeShell({
      notificationsUi: {
        liveToastItems: (entries: UiNotificationEntry[]) =>
          entries.map((entry) => ({ id: entry.id, entry })),
        dismissToast: (id: string) => dismissed.push(id),
        openEntry: (entry: UiNotificationEntry) =>
          openedEntries.push(entry.id),
        openToastLink: (link: ToastLink) => openedLinks.push(link),
      } as unknown as ShellState["notificationsUi"],
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("UP_TO_DATE"),
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
    expect(attrsOf(toastLayer as AnyVnode).toasts).toEqual([
      { id: "n1", entry: liveEntry },
    ]);

    (attrsOf(toastLayer as AnyVnode).onDismiss as (id: string) => void)("n1");
    expect(dismissed).toEqual(["n1"]);

    const onOpen = attrsOf(toastLayer as AnyVnode).onOpen as (
      item: ToastItem,
    ) => void;
    const link: ToastLink = {
      kind: "client-permissions",
      workspaceId: WORKSPACE_ID,
      text: "Permissions > Client permissions",
    };
    onOpen({ id: "n1", entry: liveEntry });
    onOpen({
      id: "transient-1",
      entry: null,
      title: "Microphone blocked",
      body: "Change it under Permissions > Client permissions.",
      link,
    });
    onOpen({
      id: "transient-2",
      entry: null,
      title: "Couldn't open link",
      body: "Copied instead.",
      link: null,
    });
    expect(openedEntries).toEqual(["n1"]);
    expect(openedLinks).toEqual([link]);
  });

  it("suppresses the floating stack while the feed overlay is open, without even asking for the live toasts", () => {
    // The bell's feed IS the toasts' durable home while it is open; the
    // floating cards would be redundant with it (see Shell.ts's own comment).
    const { state } = makeShell({
      notificationsUi: {
        liveToastItems: () => {
          throw new Error(
            "must not read live toasts while the feed overlay is open",
          );
        },
        dismissToast: () => undefined,
      } as unknown as ShellState["notificationsUi"],
      isNotificationsOpen: true,
      accountId: "local",
      stores: {
        requests: { hasPendingForWorkspace: () => false },
        accounts: { labelFor: () => "This device" },
        workspaces: {
          workspaces: [],
          toAgentScopedId: (anyId: string) => anyId,
          entryByAnyId: () => null,
        },
        health: {
          statusFor: () => "healthy",
          discoveryHealth: "healthy",
          appEnvironmentCondition: () => "NONE",
          recoveryKindFor: () => null,
        },
        updates: updatesStoreStub("UP_TO_DATE"),
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

describe("Shell on the popout route", () => {
  // A pulled-out workspace window's own desktop window: the same surface,
  // one window edge to edge, under the popout's own bar.
  const POPOUT_PATH = `/popout/${WORKSPACE_ID}/win-0123`;

  function componentNames(root: AnyVnode): string[] {
    return collectVnodes(root)
      .map((vnode) =>
        typeof vnode.tag === "function"
          ? ((vnode.tag as { name?: string }).name ?? "")
          : "",
      )
      .filter((name) => name !== "");
  }

  it("mounts the workspace surface in solo mode under the popout bar, with no titlebar or menus", () => {
    const { state } = makeShell({ currentRouteSearch: () => "" } as unknown as Partial<ShellState>);
    const root = renderShell(state, POPOUT_PATH, m("div#popout-page"));
    const names = componentNames(root);
    expect(names).toContain("PopoutChrome");
    expect(names).not.toContain("Titlebar");
    expect(names).not.toContain("WorkspaceSwitcherMenu");
    expect(names).not.toContain("AccountSwitcherMenu");
    const frame = collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame);
    expect(attrsOf(frame as AnyVnode).workspaceAnyId).toBe(WORKSPACE_ID);
    expect(attrsOf(frame as AnyVnode).soloWindowId).toBe("win-0123");
    // Opened for a tear-out (no mark on the route): the shell waits for the desktop's detach save.
    expect(attrsOf(frame as AnyVnode).isSoloReopened).toBe(false);
    // The routed page itself renders nothing of its own here.
    expect(collectVnodes(root).some((vnode) => vnode.attrs?.id === "popout-page")).toBe(false);
  });

  it("tells the workspace shell a popout the app reopened was reopened", () => {
    const { state } = makeShell({ currentRouteSearch: () => "reopened=1" } as unknown as Partial<ShellState>);
    const root = renderShell(state, POPOUT_PATH, m("div#popout-page"));
    const frame = collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame);
    expect(attrsOf(frame as AnyVnode).soloWindowId).toBe("win-0123");
    expect(attrsOf(frame as AnyVnode).isSoloReopened).toBe(true);
  });

  it("mounts the ordinary surface with no solo window", () => {
    const { state } = makeShell();
    const root = renderShell(state, `/workspace/${WORKSPACE_ID}`, m("div#surface"));
    const names = componentNames(root);
    expect(names).toContain("Titlebar");
    // The two menus the bar's trail opens are mounted with it, and only with it.
    expect(names).toContain("WorkspaceSwitcherMenu");
    expect(names).toContain("AccountSwitcherMenu");
    const frame = collectVnodes(root).find((vnode) => vnode.tag === WorkspaceFrame);
    expect(attrsOf(frame as AnyVnode).soloWindowId).toBeNull();
  });
});

describe("Shell auto-update prompt", () => {
  /** A prompt on the first launch into a new app version, with no answer
   * recorded; `isBackendUp` says whether the settings read succeeds. */
  function firstLaunchPrompt(isBackendUp: () => boolean = () => true): {
    model: AutoUpdatePromptModel;
    settingsReads: () => number;
  } {
    let reads = 0;
    const fetchImpl = (() => {
      reads += 1;
      return Promise.resolve(
        isBackendUp()
          ? jsonResponse(
              settingsOverview({ auto_update_prompt_decision: null }),
            )
          : jsonResponse({}, 503),
      );
    }) as typeof fetch;
    const model = new AutoUpdatePromptModel(
      fetchImpl,
      () => Promise.resolve(true),
      () => undefined,
    );
    return { model, settingsReads: () => reads };
  }

  function hasPrompt(root: AnyVnode): boolean {
    return collectVnodes(root).some(
      (vnode) => vnode.tag === AutoUpdatePromptModal,
    );
  }

  it("offers it over the machine a launch restored straight into", async () => {
    const { model } = firstLaunchPrompt();
    await model.load();
    const { state } = makeShell({
      autoUpdatePrompt: model,
    } as unknown as Partial<ShellState>);
    const root = renderShell(
      state,
      `/workspace/${WORKSPACE_ID}`,
      m("div#surface"),
    );
    expect(
      collectVnodes(root).some((vnode) => vnode.tag === WorkspaceFrame),
    ).toBe(true);
    expect(hasPrompt(root)).toBe(true);
  });

  it("offers it over Home, but not over the other pages or a popout window", async () => {
    const { model } = firstLaunchPrompt();
    await model.load();
    const { state } = makeShell({
      autoUpdatePrompt: model,
      currentRouteSearch: () => "",
    } as unknown as Partial<ShellState>);
    expect(
      hasPrompt(
        renderShell(state, "/", m("div#home"), { workspaceParam: null }),
      ),
    ).toBe(true);
    expect(
      hasPrompt(
        renderShell(state, "/create", m("div#create"), {
          workspaceParam: null,
        }),
      ),
    ).toBe(false);
    expect(
      hasPrompt(
        renderShell(state, `/popout/${WORKSPACE_ID}/win-0123`, m("div#popout")),
      ),
    ).toBe(false);
  });

  it("waits out one of the Shell's own modals over the machine, then comes up", async () => {
    const { model } = firstLaunchPrompt();
    await model.load();
    let isRecoveryOpen = true;
    const { state } = makeShell({
      autoUpdatePrompt: model,
      isRecoveryModalOpenFor: () => isRecoveryOpen,
      isRecoveryModalAutoRaised: () => false,
    } as unknown as Partial<ShellState>);
    const path = `/workspace/${WORKSPACE_ID}`;
    expect(hasPrompt(renderShell(state, path, m("div#surface")))).toBe(false);
    expect(hasPrompt(renderShell(state, "/settings", m("div#settings")))).toBe(
      false,
    );
    isRecoveryOpen = false;
    expect(hasPrompt(renderShell(state, path, m("div#surface")))).toBe(true);
  });

  it("reads its answer when the window opens, and again on the next navigation after a failed read", async () => {
    let isBackendUp = false;
    const { model, settingsReads } = firstLaunchPrompt(() => isBackendUp);
    const { state } = makeShell({
      autoUpdatePrompt: model,
    } as unknown as Partial<ShellState>);
    vi.stubGlobal("window", { location: { search: "" } });
    const instance = Shell() as unknown as m.Component;
    const lifecycle = (routePath: string): m.Vnode =>
      ({
        attrs: {
          shell: state,
          routePath,
          workspaceParam: WORKSPACE_ID,
          content: null,
          homeContent: null,
          behindContent: null,
          settingsContent: null,
        },
      }) as unknown as m.Vnode;
    const path = `/workspace/${WORKSPACE_ID}`;
    const view = (routePath: string) =>
      (instance.view as unknown as (v: m.Vnode) => AnyVnode).call(
        instance,
        lifecycle(routePath),
      );

    (instance.oncreate as unknown as (v: m.Vnode) => void).call(
      instance,
      lifecycle(path),
    );
    await settle();
    expect(settingsReads()).toBe(1);
    expect(hasPrompt(view(path))).toBe(false);

    isBackendUp = true;
    // A redraw on the same route does not read again.
    (instance.onupdate as unknown as (v: m.Vnode) => void).call(
      instance,
      lifecycle(path),
    );
    await settle();
    expect(settingsReads()).toBe(1);

    (instance.onupdate as unknown as (v: m.Vnode) => void).call(
      instance,
      lifecycle("/"),
    );
    await settle();
    expect(settingsReads()).toBe(2);
    expect(hasPrompt(view("/"))).toBe(true);
  });

  it("offers it for an out-of-date workspace only while that workspace follows Studio settings", async () => {
    const model = new AutoUpdatePromptModel(
      (() =>
        Promise.resolve(
          jsonResponse(settingsOverview({ auto_update_prompt_decision: null })),
        )) as typeof fetch,
      () => Promise.resolve(false),
      () => undefined,
    );
    await model.load();
    let override: { mode: "manual" } | null = { mode: "manual" };
    const { state } = makeShell({
      autoUpdatePrompt: model,
      currentRouteSearch: () => "",
    } as unknown as Partial<ShellState>);
    const stores = state.stores as unknown as Record<string, unknown>;
    stores.workspaces = {
      workspaces: [{ id: WORKSPACE_ID }],
      toAgentScopedId: (anyId: string) => anyId,
      entryByAnyId: () => null,
    };
    stores.updates = {
      ...updatesStoreStub("OUT_OF_DATE"),
      autoUpdateFor: () => ({ isEnabled: false, window: "2:00 AM-5:00 AM", override }),
    };
    const home = () =>
      hasPrompt(renderShell(state, "/", m("div#home"), { workspaceParam: null }));

    expect(home()).toBe(false);
    override = null;
    expect(home()).toBe(true);
  });
});
