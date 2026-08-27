import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  UiNotificationEntry,
  UiNotificationsMessage,
} from "../channel/messages";
import { jsonResponse, notificationEntry } from "../testing";
import {
  DEFAULT_NOTIFICATION_PREFS,
  NotificationsUiController,
  applyNotificationPrefs,
  currentNotificationPrefs,
  maybeRequestOsPermissionForStyle,
  openReviewRoute,
  resetNotificationPrefsForTests,
  resetReviewGestureContextForTests,
  setReviewGestureContext,
  type NotificationsUiHooks,
  type ReviewGestureContext,
} from "./notificationsUi";

afterEach(() => {
  resetNotificationPrefsForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** This suite's entries name a catalog service, as real permission asks do. */
function entry(
  id: string,
  overrides: Partial<UiNotificationEntry> = {},
): UiNotificationEntry {
  return notificationEntry(id, { service_name: "slack", ...overrides });
}

function message(
  entries: UiNotificationEntry[],
  overrides: Partial<UiNotificationsMessage> = {},
): UiNotificationsMessage {
  return {
    type: "notifications",
    entries,
    unresolved_count: entries.filter((e) => !e.is_resolved).length,
    is_snapshot: false,
    ...overrides,
  };
}

interface Made {
  controller: NotificationsUiController;
  relayed: { type: string; count?: unknown }[];
  hooks: {
    onScreen: string | null;
    isOverlayOpen: boolean;
    hasFocus: boolean;
    isDesktop: boolean;
  };
}

function makeController(overrides: Partial<NotificationsUiHooks> = {}): Made {
  const relayed: { type: string; count?: unknown }[] = [];
  const hooks = {
    onScreen: null as string | null,
    isOverlayOpen: false,
    hasFocus: true,
    isDesktop: false,
  };
  const controller = new NotificationsUiController({
    onScreenWorkspaceAgentId: () => hooks.onScreen,
    isFeedOverlayOpen: () => hooks.isOverlayOpen,
    hasWindowFocus: () => hooks.hasFocus,
    isDesktop: () => hooks.isDesktop,
    relayShellEvent: (event) =>
      relayed.push(event as { type: string; count?: unknown }),
    redraw: () => undefined,
    ...overrides,
  });
  return { controller, relayed, hooks };
}

/** A controller past its snapshot seeding, so the next frame is a live edge. */
function seeded(overrides: Partial<NotificationsUiHooks> = {}): Made {
  const made = makeController(overrides);
  made.controller.handleNotificationsMessage(
    message([], { is_snapshot: true }),
  );
  return made;
}

/** A Notification stub recording constructions; returns the created list. */
function stubNotification(
  permission: string,
): { title: string; options: unknown }[] {
  const created: { title: string; options: unknown }[] = [];
  class FakeNotification {
    static permission = permission;
    static requestPermission = vi.fn(async () => "granted");
    onclick: (() => void) | null = null;
    constructor(title: string, options: unknown) {
      created.push({ title, options });
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  return created;
}

describe("NotificationsUiController flash decisions", () => {
  it("seeds silently from a snapshot frame and flashes only genuinely new entries", () => {
    const { controller } = makeController();
    controller.handleNotificationsMessage(
      message([entry("n1")], { is_snapshot: true }),
    );
    expect(controller.liveToastIds).toEqual([]);

    // The same entry replayed live is not news; a new id is.
    controller.handleNotificationsMessage(message([entry("n1")]));
    expect(controller.liveToastIds).toEqual([]);
    controller.handleNotificationsMessage(message([entry("n2"), entry("n1")]));
    expect(controller.liveToastIds).toEqual(["n2"]);
  });

  it("never flashes an entry already present at the real cold-boot snapshot", () => {
    // Distinct from the "seeds silently from a snapshot frame" test above:
    // that one seeds via a reconnect-shaped live frame (is_snapshot: true),
    // the path every other flash-suppression test exercises. This is the
    // actual production cold-boot entry point (index.ts calls
    // seedFromSnapshot once with the bootstrap snapshot), which was
    // otherwise only ever exercised with an empty entry list.
    const { controller } = makeController();
    controller.seedFromSnapshot({ entries: [entry("n1")], unresolvedCount: 1 });

    controller.handleNotificationsMessage(message([entry("n1")]));
    expect(controller.liveToastIds).toEqual([]);

    controller.handleNotificationsMessage(message([entry("n1"), entry("n2")]));
    expect(controller.liveToastIds).toEqual(["n2"]);
  });

  it("never flashes off a reconnect snapshot, even for unseen entries", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(
      message([entry("n1")], { is_snapshot: true }),
    );
    expect(controller.liveToastIds).toEqual([]);
  });

  it("records resolved arrivals without flashing them", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(
      message([entry("n1", { is_resolved: true, outcome: "approved" })]),
    );
    expect(controller.liveToastIds).toEqual([]);
  });

  it("flashes the toast even for the workspace already on screen", () => {
    // The in-chat card shows the same ask inline, but the toast is still a
    // worthwhile nudge in its own right -- unlike the OS banner (see the
    // browser-mode test below), it is not suppressed just because you
    // happen to already be looking at the right machine.
    const made = seeded();
    made.hooks.onScreen = "agent-aa11";
    made.controller.handleNotificationsMessage(
      message([entry("n1"), entry("n2", { workspace_agent_id: "agent-bb22" })]),
    );
    expect(made.controller.liveToastIds).toEqual(["n1", "n2"]);
  });

  it("still scopes the browser-mode OS banner to workspaces not already on screen", () => {
    const made = seeded();
    made.hooks.onScreen = "agent-aa11";
    const osCreated = stubNotification("granted");
    made.controller.handleNotificationsMessage(
      message([entry("n1"), entry("n2", { workspace_agent_id: "agent-bb22" })]),
    );
    expect(made.controller.liveToastIds).toEqual(["n1", "n2"]);
    expect(osCreated.map((n) => n.title)).toEqual(["alpha asks — Slack access"]);
  });

  it("still fires the browser-mode OS banner for the on-screen workspace when this tab is unfocused", () => {
    // "On screen" is a route check, not a focus check: an alt-tabbed-away or
    // background tab can still be showing the asking workspace's route
    // without the reader actually looking at its in-chat card, so the banner
    // must not be suppressed just because the route happens to match.
    const made = seeded();
    made.hooks.onScreen = "agent-aa11";
    made.hooks.hasFocus = false;
    const osCreated = stubNotification("granted");
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(osCreated).toHaveLength(1);
  });

  it("suppresses everything while the open feed overlay is actually visible (window focused)", () => {
    const made = seeded();
    made.hooks.isOverlayOpen = true;
    const osCreated = stubNotification("granted");
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(made.controller.liveToastIds).toEqual([]);
    expect(osCreated).toEqual([]);
  });

  it("still fires the OS banner when the open feed overlay sits in an unfocused window", () => {
    const made = seeded();
    made.hooks.isOverlayOpen = true;
    made.hooks.hasFocus = false;
    const osCreated = stubNotification("granted");
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(made.controller.liveToastIds).toEqual([]);
    expect(osCreated).toHaveLength(1);
  });

  it("queues the card for later rather than flashing in an unfocused window; the OS channel is unaffected", () => {
    const made = seeded();
    made.hooks.hasFocus = false;
    const osCreated = stubNotification("granted");
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(made.controller.liveToastIds).toEqual([]);
    expect(osCreated).toHaveLength(1);

    // Regaining focus flushes it: with one window, there is no OTHER window
    // this could have shown in, so the arrival is not gone -- just delayed
    // until the reader is back.
    made.hooks.hasFocus = true;
    made.controller.handleWindowFocusGained();
    expect(made.controller.liveToastIds).toEqual(["n1"]);
  });

  it("drops a queued catch-up flash if the request resolved before focus returned", () => {
    const made = seeded();
    made.hooks.hasFocus = false;
    made.controller.handleNotificationsMessage(message([entry("n1")]));

    // Resolved while still unfocused: a fresh, unseen resolution is not
    // "surfacing" material (announceResolved-style flows aside), and the
    // queued catch-up must not later flash a receipt for something the
    // reader never got asked about.
    made.controller.handleNotificationsMessage(
      message([entry("n1", { is_resolved: true, outcome: "approved" })]),
    );

    made.hooks.hasFocus = true;
    made.controller.handleWindowFocusGained();
    expect(made.controller.liveToastIds).toEqual([]);
  });

  it("drops a queued catch-up flash if the feed overlay is open by the time focus returns", () => {
    const made = seeded();
    made.hooks.hasFocus = false;
    made.controller.handleNotificationsMessage(message([entry("n1")]));

    made.hooks.hasFocus = true;
    made.hooks.isOverlayOpen = true;
    made.controller.handleWindowFocusGained();
    expect(made.controller.liveToastIds).toEqual([]);

    // And it does not linger for a LATER focus gain either.
    made.hooks.isOverlayOpen = false;
    made.controller.handleWindowFocusGained();
    expect(made.controller.liveToastIds).toEqual([]);
  });

  it("is a cheap no-op when nothing is queued", () => {
    const made = seeded();
    made.controller.handleWindowFocusGained();
    expect(made.controller.liveToastIds).toEqual([]);
  });

  it("honors the prefs: disabled silences everything, style splits the channels", () => {
    const disabled = seeded();
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      is_enabled: false,
    });
    const disabledOs = stubNotification("granted");
    disabled.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(disabled.controller.liveToastIds).toEqual([]);
    expect(disabledOs).toEqual([]);

    resetNotificationPrefsForTests();
    const osOnly = seeded();
    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, style: "os" });
    const osOnlyCreated = stubNotification("granted");
    osOnly.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(osOnly.controller.liveToastIds).toEqual([]);
    expect(osOnlyCreated).toHaveLength(1);

    resetNotificationPrefsForTests();
    const cardsOnly = seeded();
    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, style: "cards" });
    const cardsOnlyCreated = stubNotification("granted");
    cardsOnly.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(cardsOnly.controller.liveToastIds).toEqual(["n1"]);
    expect(cardsOnlyCreated).toEqual([]);
  });

  it("does not re-flash entries repeated across frames, and drops vanished toasts", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(message([entry("n1")]));
    controller.handleNotificationsMessage(message([entry("n1")]));
    expect(controller.liveToastIds).toEqual(["n1"]);
    controller.handleNotificationsMessage(message([]));
    expect(controller.liveToastIds).toEqual([]);
  });

  it("keeps a live toast whose entry merely resolved (its click then just navigates)", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(message([entry("n1")]));
    controller.handleNotificationsMessage(
      message([entry("n1", { is_resolved: true, outcome: "denied" })]),
    );
    expect(controller.liveToastIds).toEqual(["n1"]);
  });
});

describe("NotificationsUiController toast set", () => {
  it("dismisses one toast and clears them all when the overlay opens", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(message([entry("n1")]));
    controller.handleNotificationsMessage(message([entry("n2"), entry("n1")]));
    expect(controller.liveToastIds).toEqual(["n2", "n1"]);

    controller.dismissToast("n2");
    expect(controller.liveToastIds).toEqual(["n1"]);

    controller.clearLiveToasts();
    expect(controller.liveToastIds).toEqual([]);
  });

  it("maps live ids to entries in newest-first order", () => {
    const { controller } = seeded();
    const first = entry("n1");
    const second = entry("n2");
    controller.handleNotificationsMessage(message([first]));
    controller.handleNotificationsMessage(message([second, first]));
    expect(
      controller.liveToastEntries([first, second]).map((e) => e.id),
    ).toEqual(["n2", "n1"]);
  });
});

describe("NotificationsUiController badge relay", () => {
  it("relays the dock count only when it changes", () => {
    const { controller, relayed } = makeController();
    controller.seedFromSnapshot({ entries: [], unresolvedCount: 0 });
    expect(relayed).toEqual([{ type: "notifications_count", count: 0 }]);

    controller.handleNotificationsMessage(
      message([entry("n1")], { is_snapshot: true }),
    );
    controller.handleNotificationsMessage(message([entry("n1")]));
    expect(relayed).toEqual([
      { type: "notifications_count", count: 0 },
      { type: "notifications_count", count: 1 },
    ]);

    controller.handleNotificationsMessage(
      message([entry("n1", { is_resolved: true, outcome: "approved" })]),
    );
    expect(relayed.at(-1)).toEqual({ type: "notifications_count", count: 0 });
  });
});

describe("NotificationsUiController web notifications", () => {
  it("fires only with granted permission, tagging by entry id", () => {
    const denied = seeded();
    const deniedCreated = stubNotification("denied");
    denied.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(deniedCreated).toEqual([]);

    const granted = seeded();
    const created = stubNotification("granted");
    granted.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(created).toEqual([
      {
        title: "alpha asks — Slack access",
        options: { body: "wants to read messages", tag: "n1" },
      },
    ]);
  });

  it("leaves OS delivery to the backend in desktop mode", () => {
    const made = seeded();
    made.hooks.isDesktop = true;
    const created = stubNotification("granted");
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(created).toEqual([]);
    // Cards still flash in-app on desktop.
    expect(made.controller.liveToastIds).toEqual(["n1"]);
  });

  it("stays silent when the Notification API is missing entirely", () => {
    const { controller } = seeded();
    controller.handleNotificationsMessage(message([entry("n1")]));
    expect(controller.liveToastIds).toEqual(["n1"]);
  });
});

describe("notification prefs plumbing", () => {
  it("loads prefs from the settings overview and tolerates an absent field", async () => {
    const { controller } = makeController({
      fetchImpl: async () =>
        jsonResponse({
          notification_prefs: {
            is_enabled: false,
            style: "os",
            is_os_hint_dismissed: true,
            version: "v1",
          },
        }),
    });
    await controller.loadPrefs();
    expect(currentNotificationPrefs()).toEqual({
      is_enabled: false,
      style: "os",
      is_os_hint_dismissed: true,
      version: "v1",
    });

    resetNotificationPrefsForTests();
    const bare = makeController({ fetchImpl: async () => jsonResponse({}) });
    await bare.controller.loadPrefs();
    expect(currentNotificationPrefs()).toEqual(DEFAULT_NOTIFICATION_PREFS);
  });

  it("discards a load response that a newer write outran", async () => {
    let resolveFetch: (response: Response) => void = () => undefined;
    const { controller } = makeController({
      fetchImpl: () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    });
    const load = controller.loadPrefs();
    // The user disables notifications while the boot load is on the wire; the
    // stale response must not silently re-enable them.
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      is_enabled: false,
      version: "v2",
    });
    resolveFetch(
      jsonResponse({
        notification_prefs: { ...DEFAULT_NOTIFICATION_PREFS, version: "v1" },
      }),
    );
    await load;
    expect(currentNotificationPrefs().is_enabled).toBe(false);
    expect(currentNotificationPrefs().version).toBe("v2");
  });

  it("shares one fetch across concurrent loads (the focus-gain refresh)", async () => {
    let fetchCount = 0;
    let resolveFetch: (response: Response) => void = () => undefined;
    const { controller } = makeController({
      fetchImpl: () => {
        fetchCount += 1;
        return new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        });
      },
    });
    const first = controller.loadPrefs();
    const second = controller.loadPrefs();
    expect(fetchCount).toBe(1);
    resolveFetch(
      jsonResponse({
        notification_prefs: {
          ...DEFAULT_NOTIFICATION_PREFS,
          style: "os",
          version: "v1",
        },
      }),
    );
    await Promise.all([first, second]);
    expect(currentNotificationPrefs().style).toBe("os");
    // Once settled, the next call fetches anew.
    const third = controller.loadPrefs();
    expect(fetchCount).toBe(2);
    resolveFetch(jsonResponse({}));
    await third;
  });

  it("persists the hint dismissal with the prefs' If-Match version", async () => {
    applyNotificationPrefs({
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
      version: "v7",
    });
    const writes: { url: string; ifMatch: string | null; body: unknown }[] = [];
    const { controller } = makeController({
      fetchImpl: async (url, init) => {
        writes.push({
          url: String(url),
          ifMatch: new Headers(init?.headers).get("If-Match"),
          body: JSON.parse(String(init?.body)),
        });
        return jsonResponse({ version: "v8" });
      },
    });
    await controller.dismissOsHint();
    expect(writes).toEqual([
      {
        url: "/ui/api/settings/notifications",
        ifMatch: "v7",
        body: { is_enabled: true, style: "both", is_os_hint_dismissed: true },
      },
    ]);
    expect(currentNotificationPrefs().version).toBe("v8");
    expect(currentNotificationPrefs().is_os_hint_dismissed).toBe(true);
  });

  it("merges onto prefs a concurrent writer already updated, not its own pre-await snapshot", async () => {
    applyNotificationPrefs({
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
      version: "v7",
    });
    let resolveFetch: (response: Response) => void = () => undefined;
    const { controller } = makeController({
      fetchImpl: () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    });
    const dismiss = controller.dismissOsHint();
    // A different writer to the shared prefs cell (e.g. the desktop
    // permission probe's own downgrade, or a settings-panel save) lands its
    // own change while this write is still on the wire.
    applyNotificationPrefs({
      ...currentNotificationPrefs(),
    });
    resolveFetch(jsonResponse({ version: "v8" }));
    await dismiss;

    expect(currentNotificationPrefs()).toEqual({
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: true,
      version: "v8",
    });
  });

  it("shows the OS hint only in browser mode with an undecided permission", () => {
    const { controller, hooks } = makeController();
    stubNotification("default");
    expect(controller.shouldShowOsHint()).toBe(true);

    hooks.isDesktop = true;
    expect(controller.shouldShowOsHint()).toBe(false);
    hooks.isDesktop = false;

    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, style: "cards" });
    expect(controller.shouldShowOsHint()).toBe(false);
    resetNotificationPrefsForTests();

    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      is_os_hint_dismissed: true,
    });
    expect(controller.shouldShowOsHint()).toBe(false);
    resetNotificationPrefsForTests();

    stubNotification("granted");
    expect(controller.shouldShowOsHint()).toBe(false);
  });

  it("hides the hint for the session once dismissed, even if the write fails", async () => {
    stubNotification("default");
    const { controller } = makeController({
      fetchImpl: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    expect(controller.shouldShowOsHint()).toBe(true);
    await controller.dismissOsHint();
    expect(controller.shouldShowOsHint()).toBe(false);
  });

  it("asks the browser for permission on the hint's affirmative click, then redraws", async () => {
    stubNotification("default");
    const redraw = vi.fn();
    const { controller } = makeController({ redraw });

    await controller.requestOsPermissionFromHint();

    expect(
      (Notification as unknown as { requestPermission: () => void })
        .requestPermission,
    ).toHaveBeenCalledTimes(1);
    expect(redraw).toHaveBeenCalledTimes(1);
  });

  it("no-ops when the Notification API is missing entirely", async () => {
    const redraw = vi.fn();
    const { controller } = makeController({ redraw });

    await controller.requestOsPermissionFromHint();

    expect(redraw).not.toHaveBeenCalled();
  });
});

describe("maybeRequestOsPermissionForStyle", () => {
  it("asks only for OS-reaching styles with an undecided permission, in browser mode", () => {
    vi.stubGlobal("window", {});
    stubNotification("default");
    const requestPermission = (
      Notification as unknown as { requestPermission: () => void }
    ).requestPermission;

    maybeRequestOsPermissionForStyle("cards");
    expect(requestPermission).not.toHaveBeenCalled();

    maybeRequestOsPermissionForStyle("both");
    expect(requestPermission).toHaveBeenCalledTimes(1);

    stubNotification("granted");
    maybeRequestOsPermissionForStyle("os");
    expect(
      (Notification as unknown as { requestPermission: () => void })
        .requestPermission,
    ).not.toHaveBeenCalled();
  });

  it("never asks in desktop mode, where the backend owns OS delivery", () => {
    vi.stubGlobal("window", {});
    stubNotification("default");
    const requestPermission = (
      Notification as unknown as { requestPermission: () => void }
    ).requestPermission;

    maybeRequestOsPermissionForStyle("both", true);
    expect(requestPermission).not.toHaveBeenCalled();

    maybeRequestOsPermissionForStyle("both", false);
    expect(requestPermission).toHaveBeenCalledTimes(1);
  });
});

describe("openReviewRoute", () => {
  function gestureContext(
    overrides: Partial<ReviewGestureContext> = {},
  ): ReviewGestureContext & { openInPlace: ReturnType<typeof vi.fn> } {
    return {
      toAgentScopedId: (anyId: string) => anyId,
      createAttemptStateOf: () => "",
      displayedWorkspaceAgentId: () => null,
      openInPlace: vi.fn(),
      currentRoutePath: () => "/",
      ...overrides,
    } as ReviewGestureContext & { openInPlace: ReturnType<typeof vi.fn> };
  }

  afterEach(() => {
    resetReviewGestureContextForTests();
  });

  it("routes to an enterable workspace with the review param", () => {
    const context = gestureContext();
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
    expect(context.openInPlace).not.toHaveBeenCalled();
  });

  it("resolves a host-scoped id to the agent id before routing", () => {
    setReviewGestureContext(
      gestureContext({ toAgentScopedId: () => "agent-aa11" }),
    );
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("host-bb22", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
  });

  it("opens the popup in place over the displayed workspace instead of re-navigating", () => {
    const context = gestureContext({
      displayedWorkspaceAgentId: () => "agent-aa11",
    });
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-aa11", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("lands a machine still setting up on its own creating page, never the workspace fallback", () => {
    const context = gestureContext({ createAttemptStateOf: () => "creating" });
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/creating/agent-aa11");
    expect(context.openInPlace).not.toHaveBeenCalled();
  });

  it("opens the popup in place when already watching that machine's creating page", () => {
    const context = gestureContext({
      createAttemptStateOf: () => "creating",
      currentRoutePath: () => "/creating/agent-aa11",
    });
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-aa11", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("opens the popup in place for a machine the workspace list does not know", () => {
    const context = gestureContext({ createAttemptStateOf: () => null });
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-zz99", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("opens the popup in place when the entry has no workspace to hop to", () => {
    const context = gestureContext();
    setReviewGestureContext(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("falls back to the navigate-first gesture when unwired", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    openReviewRoute("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
    openReviewRoute("", "req-2");
    expect(routeSet).toHaveBeenCalledWith("/inbox", { selected: "req-2" });
  });
});
