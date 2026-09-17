// A one-line toggle with a chevron that opens a detail underneath: the
// manifesto's points and the creation page's reading material use it, so a
// list of them reads as one idiom. Open state belongs to the caller (the page
// keeps a set of open ids), which keeps the component free of state and lets a
// redraw from anywhere preserve what the reader opened.

import m from "mithril";
import { Icon16 } from "./Icon";

interface DisclosureAttrs extends m.Attributes {
  isOpen: boolean;
  onToggle: () => void;
  /** The always-visible line the marker sits in front of. */
  summary: m.Children;
  /**
   * When the marker fades in, for a summary that streams in character by
   * character: the marker belongs to the line, so it arrives with the line's
   * first character rather than standing there ahead of an empty row.
   * Omitted when the line is simply already on the page.
   */
  markerStartAtMs?: number;
  /** How long the marker's fade lasts; paired with markerStartAtMs. */
  markerFadeMs?: number;
}

export function Disclosure(): m.Component<DisclosureAttrs> {
  return {
    view(vnode) {
      const { isOpen, onToggle, summary, markerStartAtMs, markerFadeMs } = vnode.attrs;
      const isMarkerStreamed = markerStartAtMs !== undefined;
      return m("div", { class: "flex flex-col", "data-disclosure": isOpen ? "open" : "closed" }, [
        m(
          "button",
          {
            type: "button",
            class:
              "flex items-start gap-2 text-left cursor-pointer bg-transparent border-0 p-0 " +
              "text-primary hover:text-accent",
            "aria-expanded": isOpen ? "true" : "false",
            onclick: onToggle,
          },
          [
            // No color of its own: Icon16 fills with currentColor, so the
            // chevron darkens on hover and turns with the rest of the row
            // rather than sitting a shade apart from the words it introduces.
            m(
              "span",
              {
                class:
                  // A fixed-width column, so the summary begins where the open
                  // detail's indent lands. mt-0.5 sets the glyph against the
                  // first line of a summary that wraps.
                  "mt-0.5 w-4 shrink-0 transition-transform duration-150 " +
                  (isOpen ? "rotate-90 " : "") +
                  (isMarkerStreamed ? "start-char" : ""),
                style: isMarkerStreamed
                  ? `--start-char-delay: ${markerStartAtMs}ms; --start-char-fade: ${markerFadeMs ?? 70}ms;`
                  : undefined,
                "aria-hidden": "true",
              },
              // One chevron rotated, not a second glyph swapped in, so opening
              // and closing is a turn rather than a cut.
              m(Icon16, { name: "chevron-right" }),
            ),
            m("span", { class: "leading-[1.5] " + (isOpen ? "font-bold" : "") }, summary),
          ],
        ),
        isOpen ? m("div", { class: "ml-6 mt-1 mb-2 text-primary leading-[1.5]" }, vnode.children) : null,
      ]);
    },
  };
}
