import { describe, expect, it } from "vitest";
import { Icon16 } from "../../components/Icon";
import { ICONS_16 } from "../../components/icons";
import type { IconName } from "../../components/icons";
import { attrsOf, collectVnodes } from "../../../testing";
import { paneTitle } from "./OptionsPanel";
import type { OptionsTab } from "../../../models/workspaceOptions";

/** The glyph the pane heading resolves to, '' when it names artwork the
 * registry does not carry. */
function headingGlyph(tab: OptionsTab): string {
  const icon = collectVnodes(paneTitle(tab, "Imbue HUD")).find(
    (vnode) => vnode.tag === Icon16,
  );
  if (icon === undefined) throw new Error("the heading drew no icon");
  return ICONS_16[attrsOf(icon).name as IconName] ?? "";
}

describe("the options pane heading", () => {
  it("draws the person-with-plus glyph beside Share machine", () => {
    expect(headingGlyph("share")).toBe(ICONS_16["user-plus"]);
  });

  it("draws the gear glyph beside Machine settings", () => {
    expect(headingGlyph("settings")).toBe(ICONS_16["settings"]);
  });
});
