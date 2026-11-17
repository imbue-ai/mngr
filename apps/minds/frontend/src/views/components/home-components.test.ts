import { afterEach, describe, expect, it, vi } from "vitest";
import { monogramColor } from "../../models/monogram";
import { allText, attrsOf, classesOf, collectVnodes, renderRoot, required, withAttr } from "../../testing";
import { Avatar } from "./Avatar";
import { CardMenu } from "./CardMenu";
import { HomeAppIcon } from "./HomeAppIcon";
import { HomeBadge, homeBadgeClass } from "./HomeBadge";
import { MonogramTile } from "./MonogramTile";
import { TabStrip } from "./TabStrip";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TabStrip", () => {
  it("marks the selected pill, counts a waiting tab in orange, and selects on a click", () => {
    const onSelect = vi.fn();
    const rendered = renderRoot(
      TabStrip,
      {
        tabs: [
          { id: "workspaces", label: "Workspaces" },
          { id: "invitations", label: "Invitations", count: 2 },
        ],
        selected: "workspaces",
        onSelect,
        label: "Home",
      },
    );
    const tabs = withAttr(rendered, "data-home-tab");
    expect(tabs.map((tab) => [attrsOf(tab)["data-home-tab"], attrsOf(tab)["aria-selected"]])).toEqual([
      ["workspaces", "true"],
      ["invitations", "false"],
    ]);
    // The chosen pill is the solid inverse one: a transparent background on
    // it too would win in the stylesheet and leave its white text on white.
    expect(classesOf(tabs[0])).toContain("bg-surface-inverse");
    expect(classesOf(tabs[0])).not.toContain("bg-transparent");
    expect(classesOf(tabs[1])).toContain("bg-transparent");
    expect(attrsOf(required(withAttr(rendered, "data-tab-count")[0]))["data-tab-count"]).toBe("2");
    (attrsOf(tabs[1]).onclick as () => void)();
    expect(onSelect).toHaveBeenCalledWith("invitations");
  });
});

describe("HomeBadge", () => {
  it("wears one of the three tones, the neutral one as the white chip", () => {
    expect(homeBadgeClass("alarm", "")).toContain("bg-important");
    expect(homeBadgeClass("warn", "")).toContain("--c-warning");
    expect(homeBadgeClass("neutral", "")).toContain("status-chip");
    const rendered = renderRoot(HomeBadge, { tone: "neutral", isSpinning: true }, "Starting…");
    expect(attrsOf(rendered)["data-badge-tone"]).toBe("neutral");
    expect(allText(rendered)).toContain("Starting…");
    expect(collectVnodes(rendered).some((v) => typeof v.tag === "function")).toBe(true);
  });
});

describe("Avatar", () => {
  it("draws the picture when there is one, else a monogram of the label", () => {
    const picture = renderRoot(Avatar, { src: "https://example.com/p.png", label: "Owen", size: 16 });
    expect(picture.tag).toBe("img");
    expect(attrsOf(picture)["data-avatar-for"]).toBe("Owen");
    const monogram = renderRoot(Avatar, { src: null, label: "Owen" });
    expect(monogram.tag).toBe("span");
    expect(allText(monogram)).toBe("O");
  });
});

describe("MonogramTile", () => {
  it("wears the name's letters on the name's colour, in the classes it is given", () => {
    const tile = renderRoot(MonogramTile, { name: "Trip Planner", extra: "size-10" });
    expect(attrsOf(tile)["data-monogram"]).toBe("TP");
    expect(allText(tile)).toBe("TP");
    expect(classesOf(tile)).toContain("size-10");
    expect(attrsOf(tile).style).toBe(`background-color: ${monogramColor("Trip Planner")};`);
  });
});

describe("HomeAppIcon", () => {
  it("draws the served icon, or a monogram tile, with the owner's face and the From tooltip for a shared app", () => {
    const onOpen = vi.fn();
    const served = renderRoot(HomeAppIcon, {
      name: "Reading List",
      subtitle: "alpha",
      iconUrl: "/icon.svg?v=1",
      onOpen,
    });
    expect(collectVnodes(served).some((v) => v.tag === "img" && attrsOf(v).src === "/icon.svg?v=1")).toBe(true);
    expect(allText(served)).toContain("alpha");
    const button = required(collectVnodes(served).find((v) => v.tag === "button"));
    (attrsOf(button).onclick as () => void)();
    expect(onOpen).toHaveBeenCalledTimes(1);

    const shared = renderRoot(HomeAppIcon, {
      name: "Trip Planner",
      iconUrl: null,
      owner: { label: "Nick", pictureUrl: null },
      isDisabled: true,
      title: "no address",
      onOpen,
    });
    const tile = required(collectVnodes(shared).find((v) => v.tag === MonogramTile));
    expect(attrsOf(tile).name).toBe("Trip Planner");
    expect(allText(shared)).toContain("From");
    const disabled = required(collectVnodes(shared).find((v) => v.tag === "button"));
    expect(attrsOf(disabled)["aria-label"]).toBe("Trip Planner, from Nick");
    (attrsOf(disabled).onclick as () => void)();
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("HomeAppIcon", () => {
  it("falls back to the monogram for an icon that failed to load, and tries again under a new icon URL", () => {
    const icon = HomeAppIcon();
    const render = (iconUrl: string) =>
      (icon.view as unknown as (v: unknown) => unknown).call(icon, {
        attrs: { name: "Reading List", iconUrl, onOpen: vi.fn() },
      });
    const imageOf = (rendered: unknown) => collectVnodes(rendered).find((v) => v.tag === "img");

    const first = render("/icon.svg?v=1");
    (attrsOf(required(imageOf(first))).onerror as () => void)();
    expect(imageOf(render("/icon.svg?v=1"))).toBeUndefined();
    expect(attrsOf(required(imageOf(render("/icon.svg?v=2")))).src).toBe("/icon.svg?v=2");
  });
});

describe("CardMenu", () => {
  it("renders its facts and items only while open, and runs an item's action on a click that goes no further", () => {
    vi.stubGlobal("document", { addEventListener: vi.fn(), removeEventListener: vi.fn() });
    const onSelect = vi.fn();
    const menu = CardMenu();
    const attrs = {
      facts: [{ label: "Host:", value: "Local" }],
      items: [
        { icon: "settings" as const, label: "Workspace settings", onSelect },
        { icon: "box" as const, label: "Backups", onSelect: vi.fn(), isDisabled: true, title: "Unlock first" },
      ],
    };
    const render = () => (menu.view as unknown as (v: unknown) => unknown).call(menu, { attrs });

    const closed = render();
    expect(withAttr(closed, "role").filter((v) => attrsOf(v).role === "menu")).toHaveLength(0);
    const toggle = required(collectVnodes(closed).find((v) => attrsOf(v)["aria-expanded"] !== undefined));
    const stopPropagation = vi.fn();
    (attrsOf(toggle).onclick as (event: unknown) => void)({ stopPropagation, currentTarget: null });
    expect(stopPropagation).toHaveBeenCalled();

    const open = render();
    expect(allText(open)).toContain("Local");
    const items = withAttr(open, "data-menu-item");
    expect(
      items.map((item) => [attrsOf(item)["data-menu-item"], attrsOf(item)["aria-disabled"], attrsOf(item).title]),
    ).toEqual([
      ["Workspace settings", undefined, undefined],
      ["Backups", "true", "Unlock first"],
    ]);
    (attrsOf(items[1]).onclick as (event: unknown) => void)({ stopPropagation: vi.fn() });
    expect(attrsOf(items[1])["aria-disabled"]).toBe("true");
    (attrsOf(items[0]).onclick as (event: unknown) => void)({ stopPropagation: vi.fn() });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(withAttr(render(), "data-menu-item")).toHaveLength(0);
  });
});
