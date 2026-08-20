import m from "mithril";
import { describe, expect, it } from "vitest";
import { SETTINGS_SECTIONS, SettingsModel } from "../../../models/settings";
import { SettingsSections } from "./SettingsSections";
import type { AnyVnode } from "../../../testing";
import { classTokensOf, collectText, collectVnodes } from "../../../testing";

/** The sections view, rendered from a bare model: every panel guards on a
 * null overview and renders nothing, so the layout is exercised without a
 * payload fixture or any network. */
function renderSections(): AnyVnode[] {
  const instance = SettingsSections() as unknown as m.Component;
  const vnode = m(instance, { model: new SettingsModel() } as unknown as m.Attributes) as m.Vnode;
  const rendered = (instance.view as unknown as (v: m.Vnode) => m.Children).call(instance, vnode);
  return rendered as unknown as AnyVnode[];
}

/** The nav column and the panel column of the settings pane, in that order. */
function columns(): AnyVnode[] {
  const row = renderSections()[0];
  return row.children as AnyVnode[];
}

describe("SettingsSections layout", () => {
  it("scrolls the section list and the panel independently of each other", () => {
    // Regression guard for the bug this pane replaced: the app-overlay card was
    // the only scroller, so reading down a long panel carried the section list
    // off the top of the card with it.
    const [nav, panel] = columns();
    expect(nav.tag).toBe("nav");
    expect(nav.attrs?.["aria-label"]).toBe("Settings sections");
    for (const column of [nav, panel]) {
      expect(classTokensOf(column)).toEqual(
        expect.arrayContaining(["overflow-y-auto", "min-h-0"]),
      );
    }
  });

  it("takes a bounded height from the card rather than growing to its content", () => {
    // items-start (the old row) leaves both columns content-height, so their
    // overflow never bites however the card above them is shaped.
    const row = renderSections()[0];
    expect(classTokensOf(row)).toEqual(expect.arrayContaining(["flex-1", "min-h-0"]));
    expect(classTokensOf(row)).not.toContain("items-start");
  });

  it("keeps the group headings and every section in the nav", () => {
    const [nav] = columns();
    const navText = collectText(nav).join(" ");
    for (const heading of ["Permissions", "Other"]) {
      expect(navText, heading).toContain(heading);
    }
    for (const section of SETTINGS_SECTIONS) {
      expect(navText, section.label).toContain(section.label);
    }
  });

  it("keeps the revoke dialog beside the pane, not inside it as a third column", () => {
    // The pane's contract is two columns; a fixed-position dialog parked in the
    // row would be a real bug the moment it stopped being fixed-position.
    const [, ...siblings] = renderSections();
    expect(siblings).toHaveLength(1);
    expect(columns()).toHaveLength(2);
  });

  it("styles its nav entries like every other pane's", () => {
    // "The main page's settings have a different left menu" -- they no longer
    // do: the entries come from the shared recipe, not a fifth one.
    const [nav] = columns();
    const entries = collectVnodes(nav).filter((vnode) => vnode.tag === "button");
    expect(entries).toHaveLength(SETTINGS_SECTIONS.length);
    for (const entry of entries) {
      expect(classTokensOf(entry)).toEqual(
        expect.arrayContaining(["type-body", "rounded-md", "text-primary"]),
      );
    }
  });
});
