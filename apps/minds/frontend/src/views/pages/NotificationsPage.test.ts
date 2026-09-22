import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import type { UiNotificationEntry } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  notificationEntry as entry,
} from "../../testing";
import { NotificationsPage } from "./NotificationsPage";

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The controller calls the page makes, recorded instead of acted on. */
interface FakeController {
  opened: UiNotificationEntry[];
  cleared: string[];
  clearAllCount: number;
}

function renderFeed(entries: UiNotificationEntry[]): {
  root: AnyVnode;
  store: { entries: UiNotificationEntry[]; unresolvedCount: number };
  controller: FakeController;
  rerender: () => AnyVnode;
} {
  const store = {
    entries,
    unresolvedCount: entries.filter((e) => !e.is_resolved).length,
  };
  const controller: FakeController = {
    opened: [],
    cleared: [],
    clearAllCount: 0,
  };
  registerAppContext({
    stores: { notifications: store },
    shell: {
      notificationsUi: {
        openEntry: (entry: UiNotificationEntry) =>
          controller.opened.push(entry),
        clearEntry: (id: string) => controller.cleared.push(id),
        clearAll: () => (controller.clearAllCount += 1),
      },
    },
  } as unknown as AppContext);
  const instance = NotificationsPage as () => m.Component;
  const component = instance();
  const vnode = m(component as m.ComponentTypes) as m.Vnode;
  component.oninit?.call(component, vnode as m.VnodeDOM);
  const rerender = (): AnyVnode =>
    (component.view as (v: m.Vnode) => AnyVnode).call(component, vnode);
  return { root: rerender(), store, controller, rerender };
}

function filterTab(root: AnyVnode, id: string): AnyVnode {
  const tab = collectVnodes(root).find(
    (vnode) => attrsOf(vnode)["data-filter"] === id,
  );
  expect(tab, `no filter tab ${id}`).toBeDefined();
  return tab as AnyVnode;
}

function rowFor(root: AnyVnode, id: string): AnyVnode {
  const row = collectVnodes(root).find(
    (vnode) => attrsOf(vnode)["data-notification-id"] === id,
  );
  expect(row, `no feed row for ${id}`).toBeDefined();
  return row as AnyVnode;
}

describe("NotificationsPage", () => {
  it("never clears the badge or the entries just because the feed was opened", () => {
    // The badge tracks unresolved requests, so it clears only when they
    // resolve, never because you looked: mounting the feed must leave the
    // store's entries and unresolved count exactly as the wire sent them.
    const wire = [
      entry("n1"),
      entry("n2", { is_resolved: true, outcome: "approved" }),
    ];
    const snapshot = structuredClone(wire);
    const { store } = renderFeed(wire);
    expect(store.entries).toBe(wire);
    expect(store.entries).toEqual(snapshot);
    expect(store.unresolvedCount).toBe(1);
  });

  it("shows the caught-up empty state when the feed is empty", () => {
    const { root } = renderFeed([]);
    expect(allText(root)).toContain("You're all caught up.");
  });

  it("renders entries in wire order without re-sorting", () => {
    const { root } = renderFeed([
      entry("n2"),
      entry("n1", { is_resolved: true, outcome: "approved" }),
      entry("n3"),
    ]);
    const ids = collectVnodes(root)
      .map((vnode) => attrsOf(vnode)["data-notification-id"])
      .filter((id) => id !== undefined);
    expect(ids).toEqual(["n2", "n1", "n3"]);
  });

  it("makes an unresolved row a button carrying the sentence line and the red-dotted time", () => {
    const { root, controller } = renderFeed([entry("n1")]);
    const row = rowFor(root, "n1");
    expect(row.tag).toBe("button");
    expect(allText(row)).toContain("alpha");
    expect(allText(row)).toContain("Slack access");
    // The unresolved meta leads with the red dot.
    expect(
      collectVnodes(row).some((vnode) =>
        classTokensOf(vnode).includes("bg-important"),
      ),
    ).toBe(true);
    (attrsOf(row).onclick as () => void)();
    // The click is the entry's own open gesture, whatever its kind.
    expect(controller.opened.map((opened) => opened.id)).toEqual(["n1"]);
  });

  it("marks every row with its kind", () => {
    const { root } = renderFeed([
      entry("n1"),
      entry("m1", {
        kind: "agent_message",
        request_id: "",
        chat_agent_id: "agent-cc33",
      }),
      entry("s1", { kind: "system_event", request_id: "" }),
    ]);
    const marks = collectVnodes(root)
      .map((vnode) => attrsOf(vnode)["data-kind-mark"])
      .filter((mark) => mark !== undefined);
    expect(marks).toEqual([
      "permission_request",
      "agent_message",
      "system_event",
    ]);
    // A message reads as "<workspace> — <chat>", not as an ask.
    expect(allText(rowFor(root, "m1"))).not.toContain("asks");
  });

  it("filters by kind through the tabs, all selected by default", () => {
    const { root, rerender } = renderFeed([
      entry("n1"),
      entry("m1", { kind: "agent_message", request_id: "" }),
      entry("s1", { kind: "system_event", request_id: "" }),
    ]);
    expect(attrsOf(filterTab(root, "all"))["aria-selected"]).toBe("true");
    const shownIds = (rendered: AnyVnode): unknown[] =>
      collectVnodes(rendered)
        .map((vnode) => attrsOf(vnode)["data-notification-id"])
        .filter((id) => id !== undefined);
    expect(shownIds(root)).toEqual(["n1", "m1", "s1"]);

    (attrsOf(filterTab(root, "messages")).onclick as () => void)();
    const messages = rerender();
    expect(attrsOf(filterTab(messages, "messages"))["aria-selected"]).toBe(
      "true",
    );
    expect(shownIds(messages)).toEqual(["m1"]);

    (attrsOf(filterTab(messages, "system")).onclick as () => void)();
    expect(shownIds(rerender())).toEqual(["s1"]);

    (attrsOf(filterTab(messages, "requests")).onclick as () => void)();
    expect(shownIds(rerender())).toEqual(["n1"]);
  });

  it("shows the caught-up state when the selected tab has nothing, even with entries elsewhere", () => {
    const { root, rerender } = renderFeed([entry("n1")]);
    (attrsOf(filterTab(root, "system")).onclick as () => void)();
    expect(allText(rerender())).toContain("You're all caught up.");
  });

  it("fades resolved rows to inert receipts with their outcome chips", () => {
    const { root } = renderFeed([
      entry("n1", { is_resolved: true, outcome: "approved" }),
      entry("n2", { is_resolved: true, outcome: "denied" }),
      entry("n3", { is_resolved: true, outcome: "closed" }),
    ]);
    for (const id of ["n1", "n2", "n3"]) {
      const row = rowFor(root, id);
      expect(row.tag).toBe("div");
      expect(attrsOf(row).onclick).toBeUndefined();
      expect(classTokensOf(row)).toEqual(
        expect.arrayContaining(["opacity-60", "grayscale"]),
      );
      // Resolved receipts drop the red unread dot.
      expect(
        collectVnodes(row).some((vnode) =>
          classTokensOf(vnode).includes("bg-important"),
        ),
      ).toBe(false);
    }
    const chipOutcomes = collectVnodes(root)
      .map((vnode) => attrsOf(vnode)["data-outcome"])
      .filter((outcome) => outcome !== undefined);
    expect(chipOutcomes).toEqual(["approved", "denied", "closed"]);
    expect(allText(rowFor(root, "n1"))).toContain("Approved");
    expect(allText(rowFor(root, "n2"))).toContain("Denied");
    expect(allText(rowFor(root, "n3"))).toContain("Closed");
  });

  it("clears one row from its X without opening it, and clears everything from the header", () => {
    const { root, controller } = renderFeed([
      entry("n1"),
      entry("n2", { is_resolved: true, outcome: "closed" }),
    ]);
    const clearButtons = collectVnodes(root).filter(
      (vnode) => attrsOf(vnode)["data-clear-notification"] !== undefined,
    );
    // Receipts have a clear button too.
    expect(
      clearButtons.map((button) => attrsOf(button)["data-clear-notification"]),
    ).toEqual(["n1", "n2"]);
    let propagationStopped = 0;
    (attrsOf(clearButtons[0]).onclick as (event: unknown) => void)({
      stopPropagation: () => (propagationStopped += 1),
    });
    expect(propagationStopped).toBe(1);
    expect(controller.cleared).toEqual(["n1"]);
    expect(controller.opened).toEqual([]);

    const clearAll = collectVnodes(root).find(
      (vnode) => attrsOf(vnode).id === "notifications-clear-all",
    );
    expect(clearAll).toBeDefined();
    (attrsOf(clearAll as AnyVnode).onclick as () => void)();
    expect(controller.clearAllCount).toBe(1);
  });

  it("offers no clear-all on an empty feed", () => {
    const { root } = renderFeed([]);
    expect(
      collectVnodes(root).some(
        (vnode) => attrsOf(vnode).id === "notifications-clear-all",
      ),
    ).toBe(false);
  });
});
