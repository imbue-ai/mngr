// The remote/local compute-preset card pair shared by the create form and
// the Create from Inspiration page (port of CreatePresetCards.jinja).

import m from "mithril";
import type { PresetName } from "./form-model";
import { Icon16 } from "../../components/Icon";
import { PresetCard } from "../../components/PresetCard";

interface PresetCardsAttrs {
  selectedPreset: PresetName | null;
  onSelect(name: PresetName): void;
}

function featureItem(text: string, isAccent: boolean): m.Children {
  return m("li", { class: "flex items-start gap-2 type-body text-primary" }, [
    m(
      "span",
      { class: (isAccent ? "text-accent " : "") + "shrink-0 mt-0.5" },
      m(Icon16, { name: isAccent ? "badge-check-filled" : "badge-check" }),
    ),
    text,
  ]);
}

export function PresetCards(): m.Component<PresetCardsAttrs> {
  return {
    view(vnode) {
      const { selectedPreset, onSelect } = vnode.attrs;
      return m("div", { class: "flex gap-6 items-stretch" }, [
        m(
          PresetCard,
          {
            preset: "remote",
            selected: selectedPreset === "remote",
            extra: "flex-1",
            onclick: () => onSelect("remote"),
          },
          [
            m("div", { class: "flex items-center gap-2" }, [
              m("span", { class: "type-heading text-primary" }, "Imbue Cloud"),
              m(
                "span",
                {
                  class:
                    "inline-flex items-center px-2 py-0.5 rounded-md type-helper uppercase font-bold bg-accent/15 text-accent",
                },
                "Recommended",
              ),
            ]),
            m("ul", { class: "flex flex-col gap-1.5 mt-1" }, [
              featureItem("30 second setup", true),
              featureItem("Runs even if your computer is off", true),
              featureItem("Accessible from mobile", true),
              featureItem("Shareable with other people", true),
            ]),
          ],
        ),
        m(
          PresetCard,
          {
            preset: "local",
            selected: selectedPreset === "local",
            extra: "flex-1",
            onclick: () => onSelect("local"),
          },
          [
            m("span", { class: "type-heading text-primary" }, "Directly on your computer"),
            m("ul", { class: "flex flex-col gap-1.5 mt-1" }, [
              featureItem("5-10 minute setup", false),
              featureItem("Runs only when your computer is on", false),
              featureItem("Uses your computer's memory (may slow your device)", false),
            ]),
          ],
        ),
      ]);
    },
  };
}
