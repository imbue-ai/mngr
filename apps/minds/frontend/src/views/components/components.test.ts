import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import { Badge, formatBadgeCount } from "./Badge";
import { routeLinkAttrs } from "./route-link";
import { Button } from "./Button";
import { Card, cardClass } from "./Card";
import { ICONS_12, ICONS_16 } from "./icons";
import { Notice, noticeClass } from "./Notice";
import { spinnerClass } from "./Spinner";
import { statusBadgeClass } from "./StatusBadge";
import { titlebarButtonClass } from "./TitlebarButton";

// Render a component to its root vnode by instantiating the closure and
// calling view() directly -- the inner-app idiom of testing logic without a
// DOM. m() normalizes attrs/children exactly as mithril would at runtime.
function renderRoot<A>(
  component: () => m.Component<A>,
  attrs: A,
  ...children: m.Children[]
): m.Vnode {
  const instance = component() as unknown as m.Component;
  const vnode = m(instance, attrs as m.Attributes, ...children) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  );
}

interface ElementVnode {
  tag: string;
  attrs: Record<string, unknown>;
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

describe("Badge", () => {
  it("caps counts above 99 at 99+", () => {
    expect(formatBadgeCount(99)).toBe("99");
    expect(formatBadgeCount(100)).toBe("99+");
  });

  it("renders the bare dot when no count is given", () => {
    const root = renderRoot(Badge, {}) as unknown as ElementVnode;
    expect(String(root.attrs.className)).toContain("w-2 h-2 rounded-full");
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
    for (const name of [
      "menu",
      "home",
      "inbox",
      "bug",
      "share",
      "settings",
      "close",
      "check",
      "chevron-down",
    ]) {
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
