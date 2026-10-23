// The "Between <hour> and <hour> local time" row that picks the window
// scheduled workspace updates start in. A plain renderer, like the switch: the
// callers' tests read the selects straight off the vnode.

import m from "mithril";
import { formatClockMinute } from "../../models/updates";

/** Whole hours only: the setting means "while I am asleep". */
const HOUR_OPTIONS: number[] = Array.from(
  { length: 24 },
  (_unused, hour) => hour,
);

function hourSelect(
  id: string,
  value: number,
  onchange: (hour: number) => void,
): m.Children {
  return m(
    "select",
    {
      id,
      class: "h-[34px] px-2 rounded-md type-body bg-fill-subtle text-primary",
      value: String(value),
      onchange: (event: Event) =>
        onchange(Number((event.target as HTMLSelectElement).value)),
    },
    HOUR_OPTIONS.map((hour) =>
      m("option", { value: String(hour) }, formatClockMinute(hour * 60)),
    ),
  );
}

export interface UpdateWindowPickerAttrs {
  /** Suffixes the selects' ids: `update-window-start-<idSuffix>` and `update-window-end-<idSuffix>`. */
  idSuffix: string;
  startHour: number;
  endHour: number;
  onChange: (startHour: number, endHour: number) => void;
  /** Classes for the row, beyond its flex layout. */
  class?: string;
}

export function renderUpdateWindowPicker(
  attrs: UpdateWindowPickerAttrs,
): m.Children {
  const { idSuffix, startHour, endHour, onChange } = attrs;
  const startId = `update-window-start-${idSuffix}`;
  const endId = `update-window-end-${idSuffix}`;
  return m(
    "div",
    { class: `flex items-center gap-2 ${attrs.class ?? ""}`.trim() },
    [
      m("label", { class: "type-body text-primary", for: startId }, "Between"),
      hourSelect(startId, startHour, (hour) => onChange(hour, endHour)),
      m("label", { class: "type-body text-primary", for: endId }, "and"),
      hourSelect(endId, endHour, (hour) => onChange(startHour, hour)),
      m("span", { class: "type-helper text-tertiary" }, "local time"),
    ],
  );
}
