import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellState } from "./shell-state";
import { Titlebar } from "./Titlebar";
import type { AnyVnode } from "../../testing";
import {
  attrsOf,
  classTokensOf,
  collectVnodes,
  notificationEntry,
} from "../../testing";
import type { UiNotificationEntry } from "../../channel/messages";

const WORKSPACE_ID = "agent-ab12";

/** Expands one level of an unrendered component vnode by calling its own
 * view() -- TitlebarButton computes its final class string there, so an
 * unrendered `m(TitlebarButton, ...)` vnode's own attrs carry no class yet. */
function renderComponentVnode(vnode: AnyVnode): AnyVnode {
  const component = (vnode.tag as unknown as () => m.Component)();
  return (component.view as unknown as (v: unknown) => AnyVnode).call(
    component,
    { attrs: vnode.attrs, children: vnode.children },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

interface RenderTitlebarOptions {
  panelRouteBehindOverlay?: string | null;
  /** Route the titlebar renders under; derived from routeSearch when absent. */
  routePath?: string;
  unresolvedNotificationCount?: number;
  isNotificationsOpen?: boolean;
  /** Records the shell's toggleNotifications calls. */
  toggleNotifications?: () => void;
  notificationEntries?: UiNotificationEntry[];
}

/** Render the titlebar without a DOM. `window` is stubbed because the Electron
 * bridge feature-detects `window.mindsNative` while deciding whether to draw
 * the window controls. */
function renderTitlebar(
  routeSearch: string,
  options: RenderTitlebarOptions = {},
): AnyVnode {
  vi.stubGlobal("window", {});
  vi.spyOn(m.route, "get").mockReturnValue(
    `/workspace/${WORKSPACE_ID}${routeSearch ? `?${routeSearch}` : ""}`,
  );
  const shell = {
    isMac: true,
    panelRouteBehindOverlay: options.panelRouteBehindOverlay ?? null,
    stores: {
      workspaces: {
        accentEntry: () => ({ name: "alpha" }),
        toAgentScopedId: (anyId: string) => anyId,
      },
      requests: { count: 0 },
      notifications: {
        unresolvedCount: options.unresolvedNotificationCount ?? 0,
        entries: options.notificationEntries ?? [],
      },
      health: { isContentAssumedReady: () => true },
    },
    openSidebar: () => undefined,
    displayedWorkspaceAnyId: WORKSPACE_ID,
    isNotificationsOpen: options.isNotificationsOpen ?? false,
    toggleNotifications: options.toggleNotifications ?? (() => undefined),
  } as unknown as ShellState;
  const instance = Titlebar() as unknown as m.Component;
  const routePath =
    options.routePath ??
    (routeSearch === ""
      ? `/workspace/${WORKSPACE_ID}`
      : `/workspace/${WORKSPACE_ID}/options`);
  const vnode = m(instance, {
    shell,
    routePath,
  } as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  ) as unknown as AnyVnode;
}

function tabStripIds(root: AnyVnode): unknown[] {
  // m() normalizes the `div#ws-tab-strip` selector into tag + attrs.id.
  const strip = collectVnodes(root).find(
    (vnode) => attrsOf(vnode).id === "ws-tab-strip",
  );
  expect(strip).toBeDefined();
  return collectVnodes(strip?.children)
    .map((vnode) => attrsOf(vnode).id)
    .filter((id) => typeof id === "string" && id.startsWith("ws-tab-"));
}

function tabButton(root: AnyVnode, id: string): AnyVnode {
  const button = collectVnodes(root).find((vnode) => attrsOf(vnode).id === id);
  expect(button, `no titlebar button ${id}`).toBeDefined();
  return button as AnyVnode;
}

describe("Titlebar right cluster", () => {
  it("offers the notification bell as the one 'something needs you' entry", () => {
    // This deliberately replaces the old "no requests entry" pin: the bell's
    // resolution-based badge is now the chrome's single such signal. The
    // popup is still the only review surface -- requests themselves never got
    // a titlebar entry, so the old id stays forbidden below.
    const root = renderTitlebar("", { unresolvedNotificationCount: 3 });
    const ids = collectVnodes(root).map((vnode) => attrsOf(vnode).id);
    expect(ids).not.toContain("requests-toggle");
    expect(ids).toContain("help-toggle");

    const bell = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    expect(bell).toBeDefined();
    expect(attrsOf(bell as AnyVnode)["aria-label"]).toBe("Notifications");
    expect(attrsOf(bell as AnyVnode)["data-tooltip"]).toBe("Notifications");
    expect(attrsOf(bell as AnyVnode)["aria-expanded"]).toBe("false");
    const icon = collectVnodes((bell as AnyVnode).children).find(
      (vnode) => attrsOf(vnode).name !== undefined,
    );
    expect(attrsOf(icon as AnyVnode).name).toBe("bell");

    // The badge renders the unresolved count (its component caps at 99+).
    const badge = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "notifications-badge",
    );
    expect(badge).toBeDefined();
    expect(attrsOf(badge as AnyVnode).count).toBe(3);
  });

  it("hides the badge entirely while nothing is unresolved", () => {
    const root = renderTitlebar("", { unresolvedNotificationCount: 0 });
    const ids = collectVnodes(root).map((vnode) => attrsOf(vnode).id);
    expect(ids).toContain("notifications-toggle");
    expect(ids).not.toContain("notifications-badge");
  });

  it("toggles the feed popover in place (no navigation), and reads expanded while open", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    let toggles = 0;
    const closed = renderTitlebar("", {
      toggleNotifications: () => (toggles += 1),
    });
    const bell = collectVnodes(closed).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    (attrsOf(bell as AnyVnode).onclick as () => void)();
    expect(toggles).toBe(1);
    // The feed is a popover over the current surface, never a route.
    expect(routeSet).not.toHaveBeenCalled();
    expect(attrsOf(bell as AnyVnode)["aria-expanded"]).toBe("false");

    const open = renderTitlebar("", { isNotificationsOpen: true });
    const openBell = collectVnodes(open).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    expect(attrsOf(openBell as AnyVnode)["aria-expanded"]).toBe("true");
  });

  it("hides the bell by visibility while its feed is open", () => {
    // NotificationsOverlay draws its own raised copy of the bell over the
    // dimmed titlebar (see Shell.test.ts), so the real one just gets out of
    // the way -- keeping its box and rect true for NotificationsOverlay to
    // measure -- rather than the two ghosting through each other.
    const closed = renderTitlebar("");
    const closedBell = collectVnodes(closed).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    const closedTokens = classTokensOf(
      renderComponentVnode(closedBell as AnyVnode),
    );
    expect(closedTokens).not.toContain("invisible");

    const open = renderTitlebar("", { isNotificationsOpen: true });
    const openBell = collectVnodes(open).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    const openTokens = classTokensOf(
      renderComponentVnode(openBell as AnyVnode),
    );
    expect(openTokens).toContain("invisible");
  });

  it("hides the bug-report button the same way while its modal is open", () => {
    const closed = renderTitlebar("");
    const closedBug = collectVnodes(closed).find(
      (vnode) => attrsOf(vnode).id === "help-toggle",
    );
    expect(
      classTokensOf(renderComponentVnode(closedBug as AnyVnode)),
    ).not.toContain("invisible");

    const open = renderTitlebar("", { routePath: "/help" });
    const openBug = collectVnodes(open).find(
      (vnode) => attrsOf(vnode).id === "help-toggle",
    );
    const openTokens = classTokensOf(
      renderComponentVnode(openBug as AnyVnode),
    );
    expect(openTokens).toContain("invisible");
  });
});

describe("Titlebar workspace tab strip", () => {
  it("leads the strip with the Permissions tab", () => {
    const root = renderTitlebar("");
    expect(tabStripIds(root)).toEqual([
      "ws-tab-permissions",
      "ws-tab-settings",
      "ws-tab-share",
    ]);
  });

  it("labels the Permissions tab with the key glyph, tooltipped like its neighbours", () => {
    const button = tabButton(renderTitlebar(""), "ws-tab-permissions");
    expect(attrsOf(button)["aria-label"]).toBe("Permissions");
    // The delegated [data-tooltip] chrome, not a native title: the two would
    // otherwise show two different tooltips on the same strip.
    expect(attrsOf(button)["data-tooltip"]).toBe("Permissions");
    expect(attrsOf(button).title).toBeUndefined();
    const icon = collectVnodes(button.children).find(
      (vnode) => attrsOf(vnode).name !== undefined,
    );
    expect(attrsOf(icon as AnyVnode).name).toBe("key");
  });

  it("dots the Permissions tab when the current workspace has an unresolved request", () => {
    const button = tabButton(
      renderTitlebar("", {
        notificationEntries: [
          notificationEntry("n1", { workspace_agent_id: WORKSPACE_ID }),
        ],
      }),
      "ws-tab-permissions",
    );
    const dot = collectVnodes(button.children).find(
      (vnode) => attrsOf(vnode).id === "permissions-badge",
    );
    expect(dot).toBeDefined();
  });

  it("stays undotted for a resolved request, or one from a different workspace", () => {
    const resolved = tabButton(
      renderTitlebar("", {
        notificationEntries: [
          notificationEntry("n1", {
            workspace_agent_id: WORKSPACE_ID,
            is_resolved: true,
            outcome: "approved",
          }),
        ],
      }),
      "ws-tab-permissions",
    );
    expect(
      collectVnodes(resolved.children).find(
        (vnode) => attrsOf(vnode).id === "permissions-badge",
      ),
    ).toBeUndefined();

    const elsewhere = tabButton(
      renderTitlebar("", {
        notificationEntries: [
          notificationEntry("n2", { workspace_agent_id: "agent-zz99" }),
        ],
      }),
      "ws-tab-permissions",
    );
    expect(
      collectVnodes(elsewhere.children).find(
        (vnode) => attrsOf(vnode).id === "permissions-badge",
      ),
    ).toBeUndefined();
  });

  it("opens the options overlay on the permissions tab", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const button = tabButton(renderTitlebar(""), "ws-tab-permissions");
    (attrsOf(button).onclick as () => void)();
    expect(routeSet).toHaveBeenCalledWith(
      `/workspace/${WORKSPACE_ID}/options?tab=permissions`,
    );
  });

  it("hides its own tabs while the docked panel draws them, popup or no popup", () => {
    // Two strips at the same measured rect would ghost through each other. The
    // panel is still up, frozen, while a request popup floats over it -- and
    // the route is the popup's by then, so the route alone cannot say so.
    const open = renderTitlebar("tab=permissions");
    expect(
      String(attrsOf(tabButton(open, "ws-tab-permissions")).extra),
    ).toContain("invisible");

    const frozen = renderTitlebar("", {
      panelRouteBehindOverlay: `/workspace/${WORKSPACE_ID}/options?tab=permissions`,
    });
    expect(
      String(attrsOf(tabButton(frozen, "ws-tab-permissions")).extra),
    ).toContain("invisible");

    const closed = renderTitlebar("");
    expect(
      String(attrsOf(tabButton(closed, "ws-tab-permissions")).extra),
    ).not.toContain("invisible");
  });

  it("highlights only the tab the URL selected, and closes it on a second click", () => {
    const root = renderTitlebar("tab=permissions");
    expect(attrsOf(tabButton(root, "ws-tab-permissions")).tone).toBe("default");
    expect(attrsOf(tabButton(root, "ws-tab-share")).tone).toBe("muted");
    expect(attrsOf(tabButton(root, "ws-tab-settings")).tone).toBe("muted");

    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    (attrsOf(tabButton(root, "ws-tab-permissions")).onclick as () => void)();
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${WORKSPACE_ID}`);
  });
});
