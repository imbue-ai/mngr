import m from "mithril";
import { INPUT_BASE } from "./constants";
import { Icon16 } from "./Icon";
import { splitAttrs } from "./attrs";

interface TextInputAttrs extends m.Attributes {
  name: string;
  radius?: "md" | "lg";
  extra?: string;
}

/**
 * Styled <input> (TextInput.jinja). The visual shell comes from the shared
 * INPUT_BASE constant so TextInput, Select and Textarea share one source of
 * truth for the focus ring. As a single-line control it adds leading-tight.
 */
export function TextInput(): m.Component<TextInputAttrs> {
  return {
    view(vnode) {
      const { name, radius = "md", extra = "" } = vnode.attrs;
      return m("input", {
        type: "text",
        name,
        class:
          "w-full leading-tight " +
          INPUT_BASE +
          " rounded-" +
          radius +
          " " +
          extra,
        ...splitAttrs(vnode.attrs, ["name", "radius", "extra"]),
      });
    },
  };
}

interface SelectAttrs extends m.Attributes {
  name: string;
  width?: string;
  extra?: string;
}

/**
 * Styled <select> with a custom dropdown chevron (Select.jinja). The native
 * arrow cannot be themed, so it is hidden (appearance-none) and a
 * chevron-down Icon16 overlays the right edge; children are <option> vnodes.
 */
export function Select(): m.Component<SelectAttrs> {
  return {
    view(vnode) {
      const { name, width = "w-full", extra = "" } = vnode.attrs;
      return m("div", { class: "relative " + width }, [
        m(
          "select",
          {
            name,
            class:
              "appearance-none w-full pr-8 leading-tight " +
              INPUT_BASE +
              " rounded-md " +
              extra,
            ...splitAttrs(vnode.attrs, ["name", "width", "extra"]),
          },
          vnode.children,
        ),
        m(
          "span",
          {
            class:
              "pointer-events-none absolute inset-y-0 right-2 flex items-center text-secondary",
          },
          m(Icon16, { name: "chevron-down" }),
        ),
      ]);
    },
  };
}

interface TextareaAttrs extends m.Attributes {
  name: string;
  value?: string;
  rows?: number;
  width?: string;
  extra?: string;
}

/** Styled <textarea> (Textarea.jinja); keeps type-body's roomier 1.5 leading. */
export function Textarea(): m.Component<TextareaAttrs> {
  return {
    view(vnode) {
      const {
        name,
        value = "",
        rows = 4,
        width = "w-full",
        extra = "",
      } = vnode.attrs;
      return m(
        "textarea",
        {
          name,
          rows,
          class: width + " " + INPUT_BASE + " rounded-md " + extra,
          ...splitAttrs(vnode.attrs, [
            "name",
            "value",
            "rows",
            "width",
            "extra",
          ]),
        },
        value,
      );
    },
  };
}

interface FormLabelAttrs extends m.Attributes {
  target: string;
  inline?: boolean;
  extra?: string;
}

/** Field label (FormLabel.jinja): block with mb-1.5 by default, or inline. */
export function FormLabel(): m.Component<FormLabelAttrs> {
  return {
    view(vnode) {
      const { target, inline = false, extra = "" } = vnode.attrs;
      const layout = inline ? "" : "block mb-1.5";
      return m(
        "label",
        {
          for: target,
          class: "type-label text-primary " + layout + " " + extra,
          ...splitAttrs(vnode.attrs, ["target", "inline", "extra"]),
        },
        vnode.children,
      );
    },
  };
}
