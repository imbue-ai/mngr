import type m from "mithril";
import { describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, collectVnodes } from "../../../testing";
import { PresetCard } from "../../components/PresetCard";
import { PRESET_TITLES, PresetCards } from "./PresetCards";
import type { PresetName } from "./form-model";

function renderCards(
  selectedPreset: PresetName | null,
  onSelect: (name: PresetName) => void,
  isRemoteOffered = true,
): AnyVnode[] {
  const component = PresetCards();
  const vnode = { attrs: { selectedPreset, isRemoteOffered, onSelect } } as m.Vnode<{
    selectedPreset: PresetName | null;
    isRemoteOffered: boolean;
    onSelect(name: PresetName): void;
  }>;
  return collectVnodes(component.view.call(component, vnode)).filter((node) => node.tag === PresetCard);
}

describe("PresetCards", () => {
  it("offers Imbue Cloud, this computer, and your own cloud in that order, each reporting its own preset", () => {
    const onSelect = vi.fn();
    const cards = renderCards("own-cloud", onSelect);

    expect(cards.map((card) => attrsOf(card).preset)).toEqual(["remote", "local", "own-cloud"]);
    expect(cards.map((card) => attrsOf(card).selected)).toEqual([false, false, true]);
    expect(cards.map((card) => allText(card.children))).toEqual([
      expect.stringContaining(PRESET_TITLES.remote),
      expect.stringContaining(PRESET_TITLES.local),
      expect.stringContaining(PRESET_TITLES["own-cloud"]),
    ]);

    for (const card of cards) (attrsOf(card).onclick as () => void)();
    expect(onSelect.mock.calls).toEqual([["remote"], ["local"], ["own-cloud"]]);
  });

  it("offers no Imbue Cloud card when it is not offered, keeping the other two", () => {
    const cards = renderCards("local", vi.fn(), false);

    expect(cards.map((card) => attrsOf(card).preset)).toEqual(["local", "own-cloud"]);
    expect(cards.map((card) => attrsOf(card).selected)).toEqual([true, false]);
  });
});
