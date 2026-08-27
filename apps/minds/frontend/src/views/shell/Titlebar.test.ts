import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotificationsStore } from "../../models/notifications";
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
const OPTIONS_PATH = `/workspace/${WORKSPACE_ID}/options`;

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
  /** Records the shell's switchToNotifications calls. */
  switchToNotifications?: () => void;
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
      // A real store, so the key-tab dot tests exercise the store's own
      // waiting-request derivation rather than a fake's copy of it.
      notifications: (() => {
        const store = new NotificationsStore();
        store.applyNotificationsMessage({
          entries: options.notificationEntries ?? [],
          unresolved_count: options.unresolvedNotificationCount ?? 0,
        });
        return store;
      })(),
      health: { isContentAssumedReady: () => true },
    },
    openSidebar: () => undefined,
    displayedWorkspaceAnyId: WORKSPACE_ID,
    isNotificationsOpen: options.isNotificationsOpen ?? false,
    switchToNotifications: options.switchToNotifications ?? (() => undefined),
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

  it("opens the feed through the shell's switch (no navigation of its own), and reads expanded while open", () => {
    // The switch, not a bare open: it puts a centered app modal away first,
    // so the feed never raises beneath that modal's backdrop.
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    let switches = 0;
    const closed = renderTitlebar("", {
      switchToNotifications: () => (switches += 1),
    });
    const bell = collectVnodes(closed).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    (attrsOf(bell as AnyVnode).onclick as () => void)();
    expect(switches).toBe(1);
    // The feed is a popover over the current surface, never a route.
    expect(routeSet).not.toHaveBeenCalled();
    expect(attrsOf(bell as AnyVnode)["aria-expanded"]).toBe("false");

    const open = renderTitlebar("", { isNotificationsOpen: true });
    const openBell = collectVnodes(open).find(
      (vnode) => attrsOf(vnode).id === "notifications-toggle",
    );
    expect(attrsOf(openBell as AnyVnode)["aria-expanded"]).toBe("true");
  });

  it("hides all five popup icons together, whichever of their surfaces is open", () => {
    // Every one of these surfaces raises a copy of ALL FIVE icons over the
    // dimmed titlebar, so the real five have to get out of the way together:
    // hiding only the one whose surface is up would leave four real buttons
    // ghosting through four raised ones.
    // The two route-backed popups forward ?workspace=, which is what keeps the
    // machine tabs on screen behind them.
    const OVER_MACHINE = `workspace=${WORKSPACE_ID}`;
    const POPUP_STATES: {
      label: string;
      search: string;
      options: RenderTitlebarOptions;
    }[] = [
      {
        label: "the docked options panel",
        search: "",
        options: { routePath: OPTIONS_PATH },
      },
      {
        label: "the request popup",
        search: OVER_MACHINE,
        options: { routePath: "/inbox" },
      },
      {
        label: "a modal over the frozen panel",
        search: "",
        options: { panelRouteBehindOverlay: `${OPTIONS_PATH}?tab=permissions` },
      },
      {
        label: "Get help",
        search: OVER_MACHINE,
        options: { routePath: "/help" },
      },
      {
        label: "the bell's feed",
        search: "",
        options: { isNotificationsOpen: true },
      },
    ];
    const POPUP_BUTTON_IDS = [
      "ws-tab-permissions",
      "ws-tab-settings",
      "ws-tab-share",
      "notifications-toggle",
      "help-toggle",
    ];

    for (const state of POPUP_STATES) {
      const root = renderTitlebar(state.search, state.options);
      for (const id of POPUP_BUTTON_IDS) {
        expect(
          classTokensOf(renderComponentVnode(tabButton(root, id))),
          `${id} stayed painted under ${state.label}`,
        ).toContain("invisible");
      }
    }

    // With nothing open they are all on screen, of course.
    const closed = renderTitlebar("");
    for (const id of POPUP_BUTTON_IDS) {
      expect(
        classTokensOf(renderComponentVnode(tabButton(closed, id))),
      ).not.toContain("invisible");
    }
  });

  it("keeps the right-hand pair painted under a request popup that raises nothing", () => {
    // Opened from an in-chat card with no machine on screen, /inbox forwards no
    // ?workspace=: the titlebar draws no #ws-tab-strip, so the popup has nothing
    // to hang from and falls back to a centered card with no raised icons.
    // Hiding the bell and the bug button then takes them away for nothing.
    const root = renderTitlebar("", { routePath: "/inbox" });

    for (const id of ["notifications-toggle", "help-toggle"]) {
      expect(
        classTokensOf(renderComponentVnode(tabButton(root, id))),
        `${id} went missing under the centered request popup`,
      ).not.toContain("invisible");
    }
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

describe("Titlebar breadcrumb", () => {
  it("renders no back button, on any route", () => {
    for (const routePath of [
      "/create",
      "/workspaces/destroyed",
      `/workspace/${WORKSPACE_ID}`,
      "/",
    ]) {
      const ids = collectVnodes(renderTitlebar("", { routePath })).map(
        (vnode) => attrsOf(vnode).id,
      );
      expect(ids, routePath).not.toContain("back-btn");
    }
  });
});
