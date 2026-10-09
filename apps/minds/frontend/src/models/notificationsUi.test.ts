import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  UiNotificationEntry,
  UiNotificationsMessage,
} from "../channel/messages";
import { jsonResponse, notificationEntry } from "../testing";
import { electronBridge } from "../electron-bridge";
import {
  DEFAULT_NOTIFICATION_PREFS,
  NotificationsUiController,
  applyNotificationPrefs,
  currentNotificationPrefs,
  NotificationGestures,
  resetNotificationPrefsForTests,
  type NotificationGestureContext,
  type NotificationsUiHooks,
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

function agentMessage(
  id: string,
  overrides: Partial<UiNotificationEntry> = {},
): UiNotificationEntry {
  return notificationEntry(id, {
    kind: "agent_message",
    title: "Build chat",
    body: "The build is green.",
    request_id: "",
    chat_agent_id: "agent-cc33",
    ...overrides,
  });
}

function systemEvent(
  id: string,
  overrides: Partial<UiNotificationEntry> = {},
): UiNotificationEntry {
  return notificationEntry(id, {
    kind: "system_event",
    title: "Backup setup failed",
    body: "Could not reach the backup host.",
    request_id: "",
    ...overrides,
  });
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
  /** Every feed-action POST the controller made, in order. */
  posted: string[];
  hooks: { isOverlayOpen: boolean };
}

function makeController(overrides: Partial<NotificationsUiHooks> = {}): Made {
  const relayed: { type: string; count?: unknown }[] = [];
  const posted: string[] = [];
  const hooks = { isOverlayOpen: false };
  const controller = new NotificationsUiController({
    isFeedOverlayOpen: () => hooks.isOverlayOpen,
    relayShellEvent: (event) =>
      relayed.push(event as { type: string; count?: unknown }),
    fetchImpl: async (url, init) => {
      if (init?.method === "POST") posted.push(String(url));
      return jsonResponse({ ok: true });
    },
    redraw: () => undefined,
    ...overrides,
  });
  return { controller, relayed, posted, hooks };
}

/** A controller past its snapshot seeding, so the next frame is a live edge. */
function seeded(overrides: Partial<NotificationsUiHooks> = {}): Made {
  const made = makeController(overrides);
  made.controller.handleNotificationsMessage(
    message([], { is_snapshot: true }),
  );
  return made;
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
    // The production cold-boot entry point (index.ts calls seedFromSnapshot
    // once with the bootstrap snapshot), distinct from the reconnect-shaped
    // live frame the other suppression tests seed through.
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

  it("flashes every kind, in every window, whatever is on screen or focused", () => {
    // No focus gate and no on-screen gate: the controller takes neither
    // signal, so a background window and the workspace already on screen
    // flash exactly like a focused window showing something else.
    const { controller } = seeded();
    controller.handleNotificationsMessage(
      message([
        systemEvent("s1", { workspace_agent_id: "" }),
        agentMessage("m1"),
        entry("n1"),
      ]),
    );
    expect(controller.liveToastIds).toEqual(["s1", "m1", "n1"]);
  });

  it("suppresses the flash while the feed overlay is open", () => {
    const made = seeded();
    made.hooks.isOverlayOpen = true;
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(made.controller.liveToastIds).toEqual([]);
    // And does not queue it for later either: the arrival sat in the feed
    // in plain sight.
    made.hooks.isOverlayOpen = false;
    made.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(made.controller.liveToastIds).toEqual([]);
  });

  it("honors the prefs: disabled or OS-only silences the cards", () => {
    const disabled = seeded();
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      is_enabled: false,
    });
    disabled.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(disabled.controller.liveToastIds).toEqual([]);

    resetNotificationPrefsForTests();
    const osOnly = seeded();
    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, style: "os" });
    osOnly.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(osOnly.controller.liveToastIds).toEqual([]);

    resetNotificationPrefsForTests();
    const cardsOnly = seeded();
    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, style: "cards" });
    cardsOnly.controller.handleNotificationsMessage(message([entry("n1")]));
    expect(cardsOnly.controller.liveToastIds).toEqual(["n1"]);
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

describe("first notification choice", () => {
  it("waits for a notification and loaded preferences, then keeps the choice available", () => {
    const { controller } = seeded();
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      has_chosen: false,
    });
    expect(controller.shouldChooseNotificationStyle).toBe(false);
    controller.handleNotificationsMessage(message([agentMessage("first")]));
    expect(controller.shouldChooseNotificationStyle).toBe(true);
    controller.handleNotificationsMessage(message([]));
    expect(controller.shouldChooseNotificationStyle).toBe(true);
    applyNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, has_chosen: true });
    expect(controller.shouldChooseNotificationStyle).toBe(false);
  });

  it("offers a choice for a notification received before this window opened", () => {
    const { controller } = makeController();
    controller.seedFromSnapshot({
      entries: [agentMessage("first")],
      unresolvedCount: 1,
    });
    expect(controller.shouldChooseNotificationStyle).toBe(false);
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      has_chosen: false,
    });
    expect(controller.shouldChooseNotificationStyle).toBe(true);
    expect(controller.liveToastIds).toEqual([]);
  });

  it.each(["both", "cards", "os", null] as const)(
    "persists %s and stops asking",
    async (style) => {
      const writes: unknown[] = [];
      const { controller } = seeded({
        fetchImpl: async (_url, init) => {
          if (init?.method === "POST") {
            writes.push(JSON.parse(String(init.body)));
            expect((init.headers as Record<string, string>)["If-Match"]).toBe(
              "before",
            );
            return jsonResponse({ version: "after" });
          }
          return jsonResponse({});
        },
      });
      controller.seedFromSnapshot({
        entries: [agentMessage("first")],
        unresolvedCount: 1,
      });
      applyNotificationPrefs({
        ...DEFAULT_NOTIFICATION_PREFS,
        has_chosen: false,
        version: "before",
      });
      await controller.chooseNotificationStyle(style);
      expect(writes).toEqual([
        { is_enabled: style !== null, style: style ?? "both" },
      ]);
      expect(controller.shouldChooseNotificationStyle).toBe(false);
      expect(currentNotificationPrefs().has_chosen).toBe(true);
      expect(controller.isSavingChoice).toBe(false);
    },
  );

  it("keeps the choice available after a failed save", async () => {
    const { controller } = makeController({
      fetchImpl: async () => jsonResponse({}, 500),
    });
    controller.seedFromSnapshot({
      entries: [agentMessage("first")],
      unresolvedCount: 1,
    });
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      has_chosen: false,
    });
    await controller.chooseNotificationStyle("cards");
    expect(controller.shouldChooseNotificationStyle).toBe(true);
    expect(controller.choiceError).toContain("Could not save");
  });

  it("accepts a choice already saved in another window on a version conflict", async () => {
    const { controller } = makeController({
      fetchImpl: async (_url, init) =>
        init?.method === "POST"
          ? jsonResponse({}, 412)
          : jsonResponse({
              notification_prefs: {
                ...DEFAULT_NOTIFICATION_PREFS,
                style: "os",
                has_chosen: true,
                version: "other",
              },
            }),
    });
    controller.seedFromSnapshot({
      entries: [agentMessage("first")],
      unresolvedCount: 1,
    });
    applyNotificationPrefs({
      ...DEFAULT_NOTIFICATION_PREFS,
      has_chosen: false,
    });
    await controller.chooseNotificationStyle("cards");
    expect(controller.shouldChooseNotificationStyle).toBe(false);
    expect(currentNotificationPrefs().style).toBe("os");
  });
});

describe("notification clicks across desktop windows", () => {
  it.each([
    [agentMessage("msg"), "/workspace/agent-aa11?chat=agent-cc33"],
    [entry("req"), "/workspace/agent-aa11?review=req-req"],
    [systemEvent("sys"), "/workspace/agent-aa11/backups"],
  ])(
    "opens the existing window without navigating this one",
    async (notification, route) => {
      const routed = vi
        .spyOn(electronBridge, "openNotificationInExistingWindow")
        .mockResolvedValue(true);
      const navigate = vi
        .spyOn(m.route, "set")
        .mockImplementation(() => undefined);
      const { controller, posted } = makeController();
      controller.openEntry(notification);
      await Promise.resolve();
      expect(routed).toHaveBeenCalledWith(route, notification);
      expect(navigate).not.toHaveBeenCalled();
      // The selected window runs the shared acknowledgement action.
      expect(posted).toEqual([]);
    },
  );

  it("falls back to this window when no existing destination window is found", async () => {
    vi.spyOn(
      electronBridge,
      "openNotificationInExistingWindow",
    ).mockResolvedValue(false);
    const navigate = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { controller } = makeController();
    controller.openEntry(agentMessage("msg"));
    await Promise.resolve();
    expect(navigate).toHaveBeenCalledWith("/workspace/agent-aa11", {
      chat: "agent-cc33",
    });
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

  it("stacks transient messages in front of the flashing entries, newest first", () => {
    const { controller } = seeded();
    const first = entry("n1");
    const second = entry("n2");
    controller.handleNotificationsMessage(message([first]));
    controller.handleNotificationsMessage(message([second, first]));
    controller.showTransientToast("Couldn't open link", "Copied instead.");
    const items = controller.liveToastItems([first, second]);
    expect(items.map((item) => item.entry?.id ?? item.id)).toEqual([
      "transient-1",
      "n2",
      "n1",
    ]);
    expect(items[0]).toMatchObject({
      entry: null,
      title: "Couldn't open link",
      body: "Copied instead.",
    });

    // A transient toast retires through the same dismissal as an entry's
    // flash, and opening the feed overlay does not touch it (it is not in
    // the feed).
    controller.clearLiveToasts();
    expect(controller.liveToastItems([])).toHaveLength(1);
    controller.dismissToast("transient-1");
    expect(controller.liveToastItems([])).toEqual([]);
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

describe("NotificationsUiController feed actions", () => {
  it.each([entry("ask"), agentMessage("msg"), systemEvent("sys")])(
    "native opening clears the reminder and toast in every window and closes the menu",
    (notification) => {
      vi.spyOn(m.route, "set").mockImplementation(() => undefined);
      const reroute = vi.spyOn(
        electronBridge,
        "openNotificationInExistingWindow",
      );
      const closeMenu = vi.fn();
      const target = seeded({ onEntryOpened: closeMenu });
      const otherWindow = seeded();
      for (const window of [target, otherWindow]) {
        window.controller.handleNotificationsMessage(message([notification]));
        expect(window.controller.liveToastIds).toEqual([notification.id]);
      }

      // This is the action registered with the native onOpenNotification bridge.
      target.controller.openEntryHere(notification);
      expect(target.posted).toEqual([
        `/ui/api/notifications/${notification.id}/clear`,
      ]);
      expect(target.controller.liveToastIds).toEqual([]);
      expect(closeMenu).toHaveBeenCalledOnce();
      expect(reroute).not.toHaveBeenCalled();

      // The shared feed publishes the cleared entry to every connected window.
      for (const window of [target, otherWindow]) {
        window.controller.handleNotificationsMessage(message([]));
        expect(window.controller.liveToastIds).toEqual([]);
        expect(window.relayed.at(-1)).toEqual({
          type: "notifications_count",
          count: 0,
        });
      }
    },
  );

  it("dismisses a request reminder and opens its review without approving or denying it", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { controller, posted } = seeded();
    controller.openEntry(entry("n1"));
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-n1",
    });
    expect(posted).toEqual(["/ui/api/notifications/n1/clear"]);
  });

  it("reads an agent message and lands in its chat", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { controller, posted } = seeded();
    controller.openEntry(agentMessage("m1"));
    expect(posted).toEqual(["/ui/api/notifications/m1/clear"]);
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      chat: "agent-cc33",
    });
  });

  it("asks the displayed workspace's frame for the chat directly instead of re-navigating", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const focusChat = vi.fn();
    const { controller } = seeded({
      gestures: new NotificationGestures(
        gestureContext({
          displayedWorkspaceAgentId: () => "agent-aa11",
          focusChat,
        }),
      ),
    });
    controller.openEntry(agentMessage("m1"));
    expect(routeSet).not.toHaveBeenCalled();
    expect(focusChat).toHaveBeenCalledWith("agent-aa11", "agent-cc33");
  });

  it("reads a system event and lands on the backups page, or the accounts page for an account-level one", () => {
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { controller, posted } = seeded();
    controller.openEntry(systemEvent("s1"));
    controller.openEntry(systemEvent("s2", { workspace_agent_id: "" }));
    expect(posted).toEqual([
      "/ui/api/notifications/s1/clear",
      "/ui/api/notifications/s2/clear",
    ]);
    expect(routeSet).toHaveBeenNthCalledWith(
      1,
      "/workspace/agent-aa11/backups",
    );
    expect(routeSet).toHaveBeenNthCalledWith(2, "/accounts");
  });

  it("clears one entry (retiring its flash) and clears everything", () => {
    const { controller, posted } = seeded();
    controller.handleNotificationsMessage(message([entry("n1"), entry("n2")]));
    controller.clearEntry("n1");
    expect(controller.liveToastIds).toEqual(["n2"]);
    controller.clearAll();
    expect(controller.liveToastIds).toEqual([]);
    expect(posted).toEqual([
      "/ui/api/notifications/n1/clear",
      "/ui/api/notifications/clear-all",
    ]);
  });

  it("tells the app when a workspace is displayed, so its agent messages read", () => {
    const { controller, posted } = seeded();
    controller.handleWorkspaceDisplayed("agent-aa11");
    expect(posted).toEqual(["/ui/api/notifications/workspace/agent-aa11/read"]);
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
            version: "v1",
          },
        }),
    });
    await controller.loadPrefs();
    expect(currentNotificationPrefs()).toEqual({
      is_enabled: false,
      style: "os",
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
});

function gestureContext(
  overrides: Partial<NotificationGestureContext> = {},
): NotificationGestureContext & { openInPlace: ReturnType<typeof vi.fn> } {
  return {
    toAgentScopedId: (anyId: string) => anyId,
    createAttemptStateOf: () => "",
    displayedWorkspaceAgentId: () => null,
    openInPlace: vi.fn(),
    currentRoutePath: () => "/",
    focusChat: vi.fn(),
    ...overrides,
  } as NotificationGestureContext & { openInPlace: ReturnType<typeof vi.fn> };
}

describe("NotificationGestures.openReview", () => {
  it("routes to an enterable workspace with the review param", () => {
    const context = gestureContext();
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
    expect(context.openInPlace).not.toHaveBeenCalled();
  });

  it("resolves a host-scoped id to the agent id before routing", () => {
    const gestures = new NotificationGestures(
      gestureContext({ toAgentScopedId: () => "agent-aa11" }),
    );
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("host-bb22", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
  });

  it("opens the popup in place over the displayed workspace instead of re-navigating", () => {
    const context = gestureContext({
      displayedWorkspaceAgentId: () => "agent-aa11",
    });
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-aa11", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("lands a machine still setting up on its own creating page, never the workspace fallback", () => {
    const context = gestureContext({ createAttemptStateOf: () => "creating" });
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/creating/agent-aa11");
    expect(context.openInPlace).not.toHaveBeenCalled();
  });

  it("opens the popup in place when already watching that machine's creating page", () => {
    const context = gestureContext({
      createAttemptStateOf: () => "creating",
      currentRoutePath: () => "/creating/agent-aa11",
    });
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-aa11", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("opens the popup in place for a machine the workspace list does not know", () => {
    const context = gestureContext({ createAttemptStateOf: () => null });
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-zz99", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("opens the popup in place when the entry has no workspace to hop to", () => {
    const context = gestureContext();
    const gestures = new NotificationGestures(context);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("", "req-1");
    expect(routeSet).not.toHaveBeenCalled();
    expect(context.openInPlace).toHaveBeenCalledWith("req-1");
  });

  it("falls back to the navigate-first gesture when unwired", () => {
    const gestures = new NotificationGestures();
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openReview("agent-aa11", "req-1");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-1",
    });
    gestures.openReview("", "req-2");
    expect(routeSet).toHaveBeenCalledWith("/inbox", { selected: "req-2" });
  });
});

describe("NotificationGestures.openChat and .openSystemEvent", () => {
  it("resolves the workspace coordinate before routing with the chat param", () => {
    const gestures = new NotificationGestures(
      gestureContext({ toAgentScopedId: () => "agent-aa11" }),
    );
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openChat("host-bb22", "agent-cc33");
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      chat: "agent-cc33",
    });
  });

  it("goes nowhere for a message whose workspace never resolved", () => {
    const gestures = new NotificationGestures();
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openChat("", "agent-cc33");
    expect(routeSet).not.toHaveBeenCalled();
  });

  it("lands a workspace event on its backups page and an account event on the accounts page", () => {
    const gestures = new NotificationGestures(
      gestureContext({ toAgentScopedId: () => "agent-aa11" }),
    );
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    gestures.openSystemEvent("host-bb22");
    gestures.openSystemEvent("");
    expect(routeSet).toHaveBeenNthCalledWith(
      1,
      "/workspace/agent-aa11/backups",
    );
    expect(routeSet).toHaveBeenNthCalledWith(2, "/accounts");
  });
});
