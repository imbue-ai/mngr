// The where-to-run preset cards shared by the create form and the Create from
// Template page (port of CreatePresetCards.jinja): one large Imbue Cloud card,
// the recommended answer, and two small cards under it for the others, the
// same three answers in the same order as the start flow's question. A window
// on the device offers no Imbue Cloud card: its workspaces are the device's
// own, and a cloud workspace is made from an account's window.

import m from "mithril";
import type { PresetName } from "./form-model";
import { Icon16 } from "../../components/Icon";
import { PresetCard } from "../../components/PresetCard";

interface PresetCardsAttrs {
  selectedPreset: PresetName | null;
  /** Whether the Imbue Cloud card is offered: false in a window on the device. */
  isRemoteOffered: boolean;
  onSelect(name: PresetName): void;
}

/** Each preset as the cards and a step summary name it. */
export const PRESET_TITLES: Record<PresetName, string> = {
  remote: "Imbue Cloud",
  local: "This computer",
  "own-cloud": "Your own cloud",
};

const MINOR_PRESETS: { name: PresetName; detail: string }[] = [
  { name: "local", detail: "5-10 minute setup. Runs only while this computer is on, and uses its memory." },
  { name: "own-cloud", detail: "Runs on a cloud provider account of your own. You manage uptime, backups and cost." },
];

function featureItem(text: string): m.Children {
  return m("li", { class: "flex items-start gap-2 type-body text-primary" }, [
    m("span", { class: "text-accent shrink-0 mt-0.5" }, m(Icon16, { name: "badge-check-filled" })),
    text,
  ]);
}

export function PresetCards(): m.Component<PresetCardsAttrs> {
  return {
    view(vnode) {
      const { selectedPreset, isRemoteOffered, onSelect } = vnode.attrs;
      return m("div", { class: "flex flex-col gap-4" }, [
        isRemoteOffered
          ? m(
              PresetCard,
              {
                preset: "remote",
                selected: selectedPreset === "remote",
                extra: "w-full !p-5",
                onclick: () => onSelect("remote"),
              },
              [
                m("div", { class: "flex items-center gap-2" }, [
                  m("span", { class: "type-heading text-primary" }, PRESET_TITLES.remote),
                  m(
                    "span",
                    {
                      class:
                        "inline-flex items-center px-2 py-0.5 rounded-md type-helper uppercase font-bold bg-accent/15 text-accent",
                    },
                    "Recommended",
                  ),
                ]),
                m("ul", { class: "grid grid-cols-2 gap-x-6 gap-y-1.5 mt-1" }, [
                  featureItem("30 second setup"),
                  featureItem("Runs even if your computer is off"),
                  featureItem("Use it from your phone"),
                  featureItem("Easy to share with others"),
                ]),
              ],
            )
          : null,
        m(
          "div",
          { class: "flex gap-4 items-stretch" },
          MINOR_PRESETS.map(({ name, detail }) =>
            m(
              PresetCard,
              {
                key: name,
                preset: name,
                selected: selectedPreset === name,
                extra: "flex-1 !gap-1 !p-3 !outline-solid not-aria-checked:outline-primary",
                onclick: () => onSelect(name),
              },
              [
                m("span", { class: "type-body font-semibold text-primary" }, PRESET_TITLES[name]),
                m("span", { class: "type-helper text-secondary" }, detail),
              ],
            ),
          ),
        ),
      ]);
    },
  };
}
