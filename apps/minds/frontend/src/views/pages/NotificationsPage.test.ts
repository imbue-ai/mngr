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

function renderFeed(entries: UiNotificationEntry[]): {
  root: AnyVnode;
  store: { entries: UiNotificationEntry[]; unresolvedCount: number };
} {
  const store = {
    entries,
    unresolvedCount: entries.filter((e) => !e.is_resolved).length,
  };
  registerAppContext({
    stores: { notifications: store },
    shell: {},
  } as unknown as AppContext);
  const instance = NotificationsPage as () => m.Component;
  const component = instance();
  const vnode = m(component as m.ComponentTypes) as m.Vnode;
  component.oninit?.call(component, vnode as m.VnodeDOM);
  const root = (component.view as (v: m.Vnode) => AnyVnode).call(
    component,
    vnode,
  );
  return { root, store };
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
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { root } = renderFeed([entry("n1")]);
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
    expect(routeSet).toHaveBeenCalledWith("/workspace/agent-aa11", {
      review: "req-n1",
    });
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

  it("offers no clear-all and no per-row dismiss", () => {
    const { root } = renderFeed([
      entry("n1"),
      entry("n2", { is_resolved: true, outcome: "closed" }),
    ]);
    const buttons = collectVnodes(root).filter(
      (vnode) => vnode.tag === "button",
    );
    // The only button is the pending row itself.
    expect(buttons).toHaveLength(1);
    expect(attrsOf(buttons[0])["data-notification-id"]).toBe("n1");
  });
});
