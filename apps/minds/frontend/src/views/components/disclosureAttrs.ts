// What every disclosure in the app shares, kept apart from how any one of
// them looks: a trigger that says whether it is open and what it controls,
// and a panel that names its trigger back. The caller writes its own markup
// and spreads these onto it, so two that share no class at all still answer
// to a screen reader alike.
//
// Open state stays the caller's. A redraw from anywhere then preserves what
// the reader opened, and nothing here has to remember it.

import type m from "mithril";

export interface DisclosureState {
  isOpen: boolean;
  onToggle: () => void;
  /**
   * Unique on the page. The trigger and panel ids are built from it, which is
   * what lets each name the other.
   */
  id: string;
}

export interface DisclosureParts {
  /** Spread onto the control that opens and closes the panel. */
  trigger: m.Attributes;
  /** Spread onto the panel, which the caller draws only while open. */
  panel: m.Attributes;
}

/** A ring for keyboard focus alone, so a pointer press does not draw one. */
export const DISCLOSURE_FOCUS_CLASS =
  "focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent";

/**
 * The attributes that make a control and a box into a disclosure.
 *
 * A closed disclosure's panel is not drawn at all rather than hidden, so what
 * it holds is out of reach of the keyboard and the screen reader instead of
 * merely out of sight. That is the caller's `isOpen` guard; everything that
 * can be stated as an attribute is here.
 */
export function disclosureAttrs(state: DisclosureState): DisclosureParts {
  const triggerId = `${state.id}-trigger`;
  const panelId = `${state.id}-panel`;
  return {
    trigger: {
      id: triggerId,
      type: "button",
      "aria-expanded": state.isOpen ? "true" : "false",
      "aria-controls": panelId,
      onclick: state.onToggle,
    },
    panel: {
      id: panelId,
      role: "region",
      "aria-labelledby": triggerId,
    },
  };
}
