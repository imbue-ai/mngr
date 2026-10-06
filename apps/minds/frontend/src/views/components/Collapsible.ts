// A summary that opens a panel: the one place in the app that knows how a
// collapsible thing behaves.
//
// It owns everything a caller would otherwise have to assemble and could
// silently get wrong: the box, a trigger that is a real button and says
// whether it is open and which panel it opens, a panel that names its trigger
// back, a ring for keyboard focus, and the rule that a shut panel is not drawn
// at all rather than hidden -- so what it holds is out of reach of the
// keyboard and the screen reader instead of merely out of sight.
//
// A caller supplies what differs between one of these and the next, and
// nothing structural: the marker, the summary, and classes for the three
// elements. Presentation arrives through `extra` the way the rest of
// views/components takes it.
//
// Open state stays the caller's, so a redraw from anywhere preserves what the
// reader opened and nothing here has to remember it.

import m from "mithril";
import { splitAttrs } from "./attrs";
import { Icon16 } from "./Icon";

/** Drawn ahead of the summary, and told whether the panel is open. */
export type CollapsibleMarker = (isOpen: boolean) => m.Children;

/**
 * The marker every collapsible draws unless it asks for another: one chevron
 * turned, so opening reads as a turn rather than a cut between two glyphs.
 *
 * Exported because a caller that needs the same chevron with something added
 * to it -- the manifesto's, which fades in with the line it introduces --
 * should be adding to this one rather than drawing a second.
 */
export function chevronMarker(
  isOpen: boolean,
  options: { extra?: string; style?: string } = {},
): m.Children {
  return m(
    "span",
    {
      class:
        "w-4 shrink-0 transition-transform duration-150 " +
        (isOpen ? "rotate-90 " : "") +
        (options.extra ?? ""),
      style: options.style,
      "aria-hidden": "true",
    },
    m(Icon16, { name: "chevron-right" }),
  );
}

export interface CollapsibleAttrs extends m.Attributes {
  /**
   * The box's id. The trigger's and the panel's are built from it, which is
   * what lets each name the other.
   */
  id: string;
  isOpen: boolean;
  onToggle: () => void;
  /** The always-visible line the marker sits in front of. */
  summary: m.Children;
  /**
   * Defaults to the turning chevron. Pass `() => null` for a summary that
   * carries no marker.
   */
  marker?: CollapsibleMarker;
  /** Additive classes on the box. */
  extra?: string;
  /** Additive classes on the trigger. */
  triggerExtra?: string;
  /** Additive classes on the panel. */
  panelExtra?: string;
}

const OWN_KEYS = [
  "id",
  "key",
  "isOpen",
  "onToggle",
  "summary",
  "marker",
  "extra",
  "triggerExtra",
  "panelExtra",
] as const;

/** A ring for keyboard focus alone, so a pointer press does not draw one. */
const FOCUS_CLASS =
  "focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent";

export function triggerIdFor(id: string): string {
  return `${id}-trigger`;
}

export function panelIdFor(id: string): string {
  return `${id}-panel`;
}

export function Collapsible(): m.Component<CollapsibleAttrs> {
  return {
    view(vnode) {
      const {
        id,
        isOpen,
        onToggle,
        summary,
        marker,
        extra = "",
        triggerExtra = "",
        panelExtra = "",
      } = vnode.attrs;
      const triggerId = triggerIdFor(id);
      const panelId = panelIdFor(id);
      return m(
        "div",
        // A caller's own data- and aria- attributes ride along on the box;
        // `class` never does, which is what `extra` is for.
        { id, class: extra, ...splitAttrs(vnode.attrs, OWN_KEYS) },
        [
          m(
            "button",
            {
              id: triggerId,
              type: "button",
              "aria-expanded": isOpen ? "true" : "false",
              "aria-controls": panelId,
              onclick: onToggle,
              class: "cursor-pointer " + FOCUS_CLASS + " " + triggerExtra,
            },
            [(marker ?? chevronMarker)(isOpen), summary],
          ),
          isOpen
            ? m(
                "div",
                {
                  id: panelId,
                  role: "region",
                  "aria-labelledby": triggerId,
                  class: panelExtra,
                },
                vnode.children,
              )
            : null,
        ],
      );
    },
  };
}
