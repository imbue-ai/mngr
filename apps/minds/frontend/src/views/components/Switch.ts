// The one on/off switch the app draws, and what stands in for it while its
// value is unknown. Plain renderers rather than components: a switch is a
// single element, and the callers' tests read its attributes straight off the
// vnode they are handed.

import m from "mithril";

/** How a switch shows a write, or a re-read of its value, in flight. */
export type SwitchWait =
  /** The wait spins in the knob and the switch stays live. */
  | "settling"
  /** The switch dims and is inert; the caller draws the wait beside it. */
  | "busy";

export interface SwitchAttrs {
  id?: string;
  isOn: boolean;
  /** The accessible name. */
  label: string;
  /** Carries the state the flip asks for. */
  onFlip: (isOn: boolean) => void;
  wait?: SwitchWait | null;
  /** Inert for a reason other than its own write. */
  isDisabled?: boolean;
  /** Why it is inert, shown on hover. */
  title?: string | null;
  /** Id of an element that says more than the label does. */
  describedBy?: string;
  /** `data-*` attributes the caller keys the switch by. */
  data?: Record<string, string>;
}

export function renderSwitch(attrs: SwitchAttrs): m.Children {
  const wait = attrs.wait ?? null;
  return m("button", {
    ...(attrs.id === undefined ? {} : { id: attrs.id }),
    type: "button",
    role: "switch",
    "aria-checked": attrs.isOn ? "true" : "false",
    "aria-label": attrs.label,
    ...(attrs.describedBy === undefined
      ? {}
      : { "aria-describedby": attrs.describedBy }),
    ...(wait === "settling" ? { "aria-busy": "true" } : {}),
    ...(attrs.data ?? {}),
    class:
      wait === null
        ? "perm-switch shrink-0"
        : `perm-switch shrink-0 is-${wait}`,
    disabled: wait === "busy" || attrs.isDisabled === true,
    ...(attrs.title ? { title: attrs.title } : {}),
    onclick: () => attrs.onFlip(!attrs.isOn),
  });
}

export interface SwitchPlaceholderAttrs {
  id?: string;
  /** Whether the value is still being read, as opposed to a read that failed. */
  isChecking: boolean;
  /** What the wait, or the failure, is announced as. */
  label: string;
}

/** What stands where a switch will, while nothing says which way it should
 * point: a switch drawn off before the answer would be read as the answer. The
 * wait spins at the off side with no knob under it; a read that failed with
 * nothing to show leaves the bare track greyed out. */
export function renderSwitchPlaceholder(
  attrs: SwitchPlaceholderAttrs,
): m.Children {
  return m("span", {
    ...(attrs.id === undefined ? {} : { id: attrs.id }),
    role: "status",
    "aria-label": attrs.label,
    ...(attrs.isChecking ? { "aria-busy": "true" } : {}),
    class: attrs.isChecking
      ? "perm-switch shrink-0 is-checking"
      : "perm-switch shrink-0 is-unknown",
  });
}

export interface SwitchWordAttrs {
  id?: string;
  /** The switch's value, or null while it is unknown. */
  isOn: boolean | null;
}

// The word keeps the width of its wider value in every state, so the switch
// stands still when the word comes or goes.
const SWITCH_WORD_CLASS =
  "type-body text-secondary after:invisible after:block after:h-0 " +
  "after:content-['Yes']";

/** The switch's value spelled out beside it: Yes, No, or nothing yet. */
export function renderSwitchWord(attrs: SwitchWordAttrs): m.Children {
  return m(
    "span",
    {
      ...(attrs.id === undefined ? {} : { id: attrs.id }),
      class: SWITCH_WORD_CLASS,
    },
    attrs.isOn === null ? "" : attrs.isOn ? "Yes" : "No",
  );
}
