// A one-line toggle with a chevron that opens a detail underneath: the
// manifesto's points and the creation page's reading material use it, so a
// list of them reads as one idiom.
//
// One dress for `Collapsible`, which holds the behaviour. What is here is
// prose styling, and the marker's streaming entrance -- which belongs to the
// summary it introduces, not to collapsing in general.

import m from "mithril";
import { Collapsible, chevronMarker } from "./Collapsible";

interface DisclosureAttrs extends m.Attributes {
  isOpen: boolean;
  onToggle: () => void;
  /** Unique on the page; ties this summary to the detail it opens. */
  id: string;
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
      const { isOpen, onToggle, id, summary, markerStartAtMs, markerFadeMs } =
        vnode.attrs;
      const isMarkerStreamed = markerStartAtMs !== undefined;
      return m(
        Collapsible,
        {
          id,
          isOpen,
          onToggle,
          extra: "flex flex-col",
          triggerExtra:
            "flex items-start gap-2 text-left bg-transparent border-0 p-0 " +
            "text-primary hover:text-accent",
          panelExtra: "ml-6 mt-1 mb-2 text-primary leading-[1.5]",
          // The shared chevron, with mt-0.5 to set it against the first line
          // of a summary that wraps, and the fade for one that streams in.
          marker: (open: boolean) =>
            chevronMarker(open, {
              extra: "mt-0.5 " + (isMarkerStreamed ? "start-char" : ""),
              style: isMarkerStreamed
                ? `--start-char-delay: ${markerStartAtMs}ms; --start-char-fade: ${markerFadeMs ?? 70}ms;`
                : undefined,
            }),
          summary: m(
            "span",
            { class: "leading-[1.5] " + (isOpen ? "font-bold" : "") },
            summary,
          ),
        },
        vnode.children,
      );
    },
  };
}
