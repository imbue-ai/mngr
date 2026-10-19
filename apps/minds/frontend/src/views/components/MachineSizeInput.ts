import m from "mithril";
import type { MachineSizeDimension } from "../../models/machineSize";
import { FormLabel, TextInput } from "./FormControls";

export interface MachineSizeInputAttrs {
  dimension: MachineSizeDimension;
  label: string;
  value: string;
  isInvalid?: boolean;
  disabled?: boolean;
  /** The smallest and largest value the field accepts; "1" and unbounded by default. */
  min?: string;
  max?: string;
  oninput: (text: string) => void;
}

/**
 * One labelled whole-number field of a machine size (CPUs, GB RAM, GB disk),
 * as both the create form and the settings page's resize draw it; the input's
 * id is `machine-size-<dimension>`.
 */
export function MachineSizeInput(): m.Component<MachineSizeInputAttrs> {
  return {
    view(vnode) {
      const {
        dimension,
        label,
        value,
        isInvalid = false,
        disabled = false,
        min = "1",
        max,
        oninput,
      } = vnode.attrs;
      const id = `machine-size-${dimension}`;
      return m("div", { class: "flex items-center gap-2" }, [
        m(
          FormLabel,
          { target: id, inline: true, extra: "type-helper text-secondary" },
          label,
        ),
        m(TextInput, {
          id,
          name: dimension,
          type: "number",
          min,
          max,
          step: "1",
          inputmode: "numeric",
          value,
          disabled,
          width: "w-20",
          extra: isInvalid ? "!border-important focus:!outline-important" : "",
          oninput: (event: InputEvent) => {
            oninput((event.target as HTMLInputElement).value);
          },
        }),
      ]);
    },
  };
}
