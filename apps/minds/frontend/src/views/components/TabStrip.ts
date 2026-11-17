import m from "mithril";
import { joinClasses } from "./attrs";

export interface TabStripTab {
  id: string;
  label: string;
  /** Things waiting on this tab: shown as an orange count, and the label turns orange with it. */
  count?: number;
}

interface TabStripAttrs extends m.Attributes {
  tabs: readonly TabStripTab[];
  selected: string;
  onSelect: (id: string) => void;
  /** The strip's accessible name. */
  label: string;
}

/**
 * Pill tabs centred on the page column (specs/home-screen-redesign, 4.3): the
 * chosen one a solid inverse pill, the rest plain text; a tab with things
 * waiting wears the needs-you orange until it is the chosen pill.
 */
export function TabStrip(): m.Component<TabStripAttrs> {
  return {
    view(vnode) {
      const { tabs, selected, onSelect, label } = vnode.attrs;
      return m(
        "div",
        { class: "flex justify-center" },
        m(
          "div",
          { role: "tablist", "aria-label": label, class: "flex items-center gap-1" },
          tabs.map((tab) => {
            const isSelected = tab.id === selected;
            const count = tab.count ?? 0;
            const isWaiting = count > 0;
            return m(
              "button",
              {
                type: "button",
                role: "tab",
                "aria-selected": isSelected ? "true" : "false",
                "data-home-tab": tab.id,
                onclick: () => onSelect(tab.id),
                class: joinClasses(
                  "inline-flex h-9 cursor-pointer items-center justify-center gap-1.5 whitespace-nowrap rounded-full type-label transition-colors border-0",
                  // With a badge, its right edge sits 8px in, so the two rounds share a centre.
                  isWaiting ? "pl-5 pr-2" : "px-5",
                  // The stylesheet orders bg-transparent after bg-surface-inverse, so the
                  // chosen pill must not carry both or it loses its fill.
                  isSelected
                    ? "bg-surface-inverse text-inverse-primary"
                    : "bg-transparent text-primary hover:bg-fill-hover",
                ),
              },
              [
                m("span", { class: isWaiting && !isSelected ? "text-[var(--c-warning)]" : "" }, tab.label),
                isWaiting
                  ? m(
                      "span",
                      {
                        "data-tab-count": String(count),
                        class:
                          "inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--c-warning)] px-1.5 text-xs font-semibold text-white",
                      },
                      String(count),
                    )
                  : null,
              ],
            );
          }),
        ),
      );
    },
  };
}
