import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import { Badge, formatBadgeCount } from "./Badge";
import { routeLinkAttrs } from "./route-link";
import { Button } from "./Button";
import { Card, cardClass } from "./Card";
import { Disclosure } from "./Disclosure";
import { ICONS_12, ICONS_16 } from "./icons";
import type { IconName } from "./icons";
import { Modal } from "./Modal";
import { Notice, noticeClass } from "./Notice";
import { spinnerClass } from "./Spinner";
import { navEntryClass, splitPane } from "./SplitPane";
import { statusBadgeClass } from "./StatusBadge";
import { titlebarButtonClass } from "./TitlebarButton";
import { renderRoot } from "../../testing";

interface ElementVnode {
  tag: string;
  attrs: Record<string, unknown>;
}

function tokensOf(node: unknown): string[] {
  const attrs = (node as ElementVnode).attrs ?? {};
  return String(attrs.className ?? attrs.class ?? "")
    .split(/\s+/)
    .filter((token) => token !== "");
}

describe("Button", () => {
  it("renders a type=button element with the secondary md classes by default", () => {
    const root = renderRoot(Button, {}, "Save") as unknown as ElementVnode;
    expect(root.tag).toBe("button");
    expect(root.attrs.type).toBe("button");
    // mithril's hyperscript normalizes `class` into `className` on the vnode.
    expect(String(root.attrs.className)).toContain("border-default");
  });

  it("passes through undeclared HTML attributes like aria-label and disabled", () => {
    const root = renderRoot(
      Button,
      { "aria-label": "Restart", disabled: true },
      "R",
    ) as unknown as ElementVnode;
    expect(root.attrs["aria-label"]).toBe("Restart");
    expect(root.attrs.disabled).toBe(true);
    expect(root.attrs.variant).toBeUndefined();
  });
});

describe("Card", () => {
  it("builds the class list the way Card.jinja joins fragments", () => {
    expect(cardClass("row", "tight", true, "a", "mt-4")).toBe(
      "minds-card flex items-center gap-1.5 px-4 py-2 cursor-pointer hover:border-strong hover:shadow-raised no-underline text-inherit mt-4",
    );
    expect(cardClass("block", "default", false, "div", "")).toBe(
      "minds-card p-4",
    );
  });

  it("renders an anchor with href for as=a", () => {
    const root = renderRoot(Card, {
      as: "a" as const,
      href: "/x",
      interactive: true,
    }) as unknown as ElementVnode;
    expect(root.tag).toBe("a");
    expect(root.attrs.href).toBe("/x");
  });
});

describe("Modal", () => {
  it("bounds the card to the window and scrolls its content, not the card", () => {
    // A card sized to its content centers its overflow, putting its own title
    // and close X off the top of the screen.
    const body = m("p", "a very long explanation");
    const overlay = renderRoot(Modal, { isOpen: true }, body);
    expect(tokensOf(overlay)).toContain("modal-viewport");
    const card = (overlay.children as m.Vnode[])[0];
    expect(tokensOf(card)).toEqual(
      expect.arrayContaining(["max-h-full", "flex", "flex-col"]),
    );
    expect(tokensOf(card)).not.toContain("overflow-y-auto");
    const scroller = (card.children as m.Vnode[])[0];
    expect(tokensOf(scroller)).toEqual(
      expect.arrayContaining(["overflow-y-auto", "min-h-0"]),
    );
    expect(scroller.children).toContain(body);
  });

  it("pads the scroller rather than the card, so overflow clips at the card edge", () => {
    // Content that overflows must reach the card's own bottom edge, not stop a
    // padding's width above it.
    const overlay = renderRoot(Modal, { isOpen: true }, m("p", "body"));
    const card = (overlay.children as m.Vnode[])[0];
    expect(tokensOf(card)).toContain("overflow-hidden");
    expect(tokensOf(card)).not.toContain("p-6");
    const scroller = (card.children as m.Vnode[])[0];
    expect(tokensOf(scroller)).toContain("p-6");
  });

  it("dims over the titlebar rather than stopping at it", () => {
    // The titlebar is z-100, so a backdrop below that leaves the top strip
    // undimmed. style.css pairs this z with no-drag on .modal-viewport.
    const overlay = renderRoot(Modal, { isOpen: true }, m("p", "body"));
    expect(tokensOf(overlay)).toContain("z-[110]");
    expect(tokensOf(overlay)).toContain("inset-0");
  });

  it("gives the card exactly the caller's width, and only one", () => {
    // Two max-w-* utilities on one card are decided by their order in the
    // generated stylesheet, not by the caller: .max-w-sm follows .max-w-md
    // there, so a wide modal asking for md silently rendered at sm.
    const card = (
      renderRoot(Modal, { isOpen: true, size: "xl" as const })
        .children as m.Vnode[]
    )[0];
    const widths = tokensOf(card).filter((token) => token.startsWith("max-w-"));
    expect(widths).toEqual(["max-w-xl"]);
  });
});

describe("Badge", () => {
  it("caps counts above 99 at 99+", () => {
    expect(formatBadgeCount(99)).toBe("99");
    expect(formatBadgeCount(100)).toBe("99+");
  });

  it("renders the bare dot when no count is given", () => {
    const root = renderRoot(Badge, {}) as unknown as ElementVnode;
    expect(String(root.attrs.className)).toContain("w-2 h-2 rounded-full");
  });

  it("is a perfect circle for a single digit", () => {
    const root = renderRoot(Badge, { count: 7 }) as unknown as ElementVnode;
    const className = String(root.attrs.className);
    expect(className).toContain("w-[14px]");
    expect(className).toContain("h-[14px]");
    expect(className).not.toContain("min-w-[16px]");
  });

  it("widens into a pill once the count needs two or more characters", () => {
    for (const count of [10, 99, 100]) {
      const root = renderRoot(Badge, { count }) as unknown as ElementVnode;
      const className = String(root.attrs.className);
      expect(className).toContain("min-w-[16px]");
      expect(className).toContain("px-1");
      expect(className).not.toContain("w-[14px]");
    }
  });
});

describe("class builders keep the legacy recipes", () => {
  it("statusBadgeClass picks the type role from size", () => {
    expect(statusBadgeClass("neutral", "sm", "")).toContain("type-label");
    expect(statusBadgeClass("neutral", "xs", "")).toContain("type-helper");
  });

  it("noticeClass carries the semantic surface tint", () => {
    expect(noticeClass("warn")).toContain("--c-warning-surface");
  });

  it("Notice keeps the recipe classes when a caller adds spacing via extra", () => {
    const root = renderRoot(Notice, { extra: "mb-4" }, "hello") as unknown as ElementVnode;
    const className = String(root.attrs.className);
    // The full notice recipe survives (extra APPENDS; it must never replace
    // the recipe the way a passthrough `class:` attr would).
    for (const recipePart of noticeClass("info").split(" ")) {
      expect(className).toContain(recipePart);
    }
    expect(className).toContain("mb-4");
  });

  it("a caller-supplied class never clobbers the computed recipe", () => {
    // splitAttrs drops class/className from the passthrough: spreading the
    // passthrough after the computed class would otherwise let one stray
    // `class:` replace a component's entire recipe (the bug that shipped
    // twice as unstyled Notices).
    const root = renderRoot(Notice, { class: "mb-4" }, "hello") as unknown as ElementVnode;
    const className = String(root.attrs.className);
    for (const recipePart of noticeClass("info").split(" ")) {
      expect(className).toContain(recipePart);
    }
  });

  it("spinnerClass swaps tone classes", () => {
    expect(spinnerClass("sm", "accent", "")).toContain("spinner-accent");
    expect(spinnerClass("md", "inverse", "")).toContain("spinner-inverse");
    expect(spinnerClass("lg", "default", "")).not.toContain("spinner-accent");
  });

  it("titlebarButtonClass renders control geometry for window controls", () => {
    expect(titlebarButtonClass("control", "danger", "")).toContain(
      "w-9 h-[38px]",
    );
    expect(titlebarButtonClass("control", "danger", "")).toContain(
      "titlebar-btn-danger",
    );
  });
});

describe("splitPane", () => {
  /** The nav column and the panel column, in that order. */
  function columnsOf(row: m.Vnode): ElementVnode[] {
    return row.children as unknown as ElementVnode[];
  }

  it("scrolls each column on its own rather than the pane as a whole", () => {
    // The reason this component exists: one scroller around both columns takes
    // the section list off the top with the panel it is meant to stay beside.
    const row = splitPane({
      navLabel: "Sections",
      nav: "entries",
      content: "panel",
    }) as m.Vnode;
    expect(tokensOf(row)).toEqual(
      expect.arrayContaining(["flex", "flex-1", "min-h-0"]),
    );
    expect(tokensOf(row)).not.toContain("overflow-y-auto");
    for (const column of columnsOf(row)) {
      expect(tokensOf(column)).toEqual(
        expect.arrayContaining(["overflow-y-auto", "min-h-0"]),
      );
    }
  });

  it("labels the nav and keeps its entries in real child position", () => {
    // Children, not attrs: a nav handed to a component through an attr would
    // sit outside every vnode.children walk, the call-site tests included.
    const entry = m("button", { "data-entry": "one" });
    const row = splitPane({
      navLabel: "Settings sections",
      nav: entry,
      content: "panel",
    }) as m.Vnode;
    const [nav, content] = columnsOf(row);
    expect((nav as unknown as { tag: string }).tag).toBe("nav");
    expect(nav.attrs["aria-label"]).toBe("Settings sections");
    expect((nav as unknown as m.Vnode).children).toContain(entry);
    expect(tokensOf(content)).toContain("flex-1");
  });

  it("appends extra classes as whole literals the Tailwind scan can see", () => {
    const row = splitPane({
      navLabel: "Sections",
      nav: null,
      content: null,
      extra: "mt-8",
      contentExtra: "flex flex-col",
    }) as m.Vnode;
    expect(tokensOf(row)).toContain("mt-8");
    expect(tokensOf(columnsOf(row)[1])).toEqual(
      expect.arrayContaining(["flex", "flex-col", "overflow-y-auto"]),
    );
  });

  it("fills and bolds the selected nav entry only", () => {
    expect(navEntryClass(true)).toContain("bg-fill-hover font-semibold");
    expect(navEntryClass(false)).not.toContain("font-semibold");
    // The unselected recipe survives intact underneath the selected one.
    for (const part of navEntryClass(false).split(" ")) {
      expect(navEntryClass(true)).toContain(part);
    }
  });
});

describe("routeLinkAttrs", () => {
  it("keeps the real href but routes the click through the router", () => {
    // With m.route.prefix = "" a bare internal href would trigger a full
    // document reload; the helper's onclick must intercept it instead.
    const attrs = routeLinkAttrs("/workspaces/destroyed");
    expect(attrs.href).toBe("/workspaces/destroyed");
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const preventDefault = vi.fn();
    (attrs.onclick as (event: { preventDefault(): void }) => void)({ preventDefault });
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(routeSet).toHaveBeenCalledWith("/workspaces/destroyed");
    routeSet.mockRestore();
  });
});

describe("icon catalogs", () => {
  it("ports the full 16px set including the titlebar glyphs", () => {
    const names: IconName[] = [
      "menu",
      "home",
      "inbox",
      "bell",
      "bug",
      "user-plus",
      "settings",
      "close",
      "check",
      "chevron-down",
    ];
    for (const name of names) {
      expect(ICONS_16[name], name).toBeTruthy();
    }
  });

  it("ports the three 12px window-control glyphs", () => {
    expect(Object.keys(ICONS_12).sort()).toEqual([
      "close",
      "maximize",
      "minimize",
    ]);
  });
});

describe("Disclosure", () => {
  interface Attrs {
    isOpen: boolean;
    onToggle: () => void;
    summary: m.Children;
    markerStartAtMs?: number;
    markerFadeMs?: number;
  }

  function partsOf(attrs: Attrs): { marker: m.Vnode; summary: m.Vnode; detail: m.Vnode | null } {
    const root = renderRoot(Disclosure, attrs, "the explanation") as unknown as m.Vnode;
    const [button, detail] = root.children as (m.Vnode | null)[];
    const [marker, summary] = (button as m.Vnode).children as m.Vnode[];
    return { marker, summary, detail: detail as m.Vnode | null };
  }

  it("leaves the marker uncolored so it follows the row it introduces", () => {
    // The row is text-primary and turns accent on hover; a marker with a color
    // of its own would sit a shade apart and stay put through the hover.
    const { marker } = partsOf({ isOpen: false, onToggle: () => undefined, summary: "a point" });
    const icon = (marker.children as m.Vnode[])[0];
    expect((icon.attrs as Record<string, unknown>).name).toBe("chevron-right");
    expect(tokensOf(marker)).not.toContain("text-tertiary");
    expect(tokensOf(marker).some((token) => token.startsWith("text-"))).toBe(false);
  });

  it("opens the detail flush with the summary, under a fixed-width marker", () => {
    // The marker's column is as wide as the detail's indent less the row's
    // gap, so a narrow glyph does not leave the detail indented past its label.
    const { marker, detail } = partsOf({ isOpen: true, onToggle: () => undefined, summary: "a point" });
    expect(tokensOf(marker)).toContain("w-4");
    expect(tokensOf(detail)).toContain("ml-6");
  });

  it("turns the marker and emphasizes the line when open", () => {
    const closed = partsOf({ isOpen: false, onToggle: () => undefined, summary: "a point" });
    expect(tokensOf(closed.marker)).not.toContain("rotate-90");
    expect(tokensOf(closed.summary)).not.toContain("font-bold");
    expect(closed.detail).toBeNull();

    const open = partsOf({ isOpen: true, onToggle: () => undefined, summary: "a point" });
    expect(tokensOf(open.marker)).toContain("rotate-90");
    expect(tokensOf(open.summary)).toContain("font-bold");
    expect(tokensOf(open.detail)).toContain("text-primary");
  });

  it("fades the marker in with its line when the summary streams", () => {
    // Otherwise every chevron stands there at once, in front of rows whose
    // labels have yet to type themselves in underneath.
    const { marker } = partsOf({
      isOpen: false,
      onToggle: () => undefined,
      summary: "a point",
      markerStartAtMs: 900,
      markerFadeMs: 70,
    });
    expect(tokensOf(marker)).toContain("start-char");
    expect(String((marker.attrs as Record<string, unknown>).style)).toContain("--start-char-delay: 900ms");
  });
});
