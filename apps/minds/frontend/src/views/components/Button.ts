import m from "mithril";
import { buttonClass, type ButtonSize, type ButtonVariant } from "./constants";
import { splitAttrs } from "./attrs";

interface ButtonAttrs extends m.Attributes {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
  extra?: string;
}

const OWN_KEYS = ["variant", "size", "block", "extra"] as const;

/**
 * Plain <button type="button">, the port of Button.jinja. Use for in-page
 * actions (modal triggers, toggles, fetch-backed buttons). Any HTML
 * attribute not named in ButtonAttrs (id, onclick, title, aria-*, data-*,
 * disabled) passes through to the element.
 */
export function Button(): m.Component<ButtonAttrs> {
  return {
    view(vnode) {
      const {
        variant = "secondary",
        size = "md",
        block = false,
        extra = "",
      } = vnode.attrs;
      return m(
        "button",
        {
          type: "button",
          class: buttonClass(variant, size, block, extra),
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        vnode.children,
      );
    },
  };
}

interface ButtonLinkAttrs extends ButtonAttrs {
  href: string;
}

/** Anchor styled as a button (ButtonLink.jinja): navigation that reads as a button. */
export function ButtonLink(): m.Component<ButtonLinkAttrs> {
  return {
    view(vnode) {
      const {
        variant = "secondary",
        size = "md",
        block = false,
        extra = "",
        href,
      } = vnode.attrs;
      return m(
        "a",
        {
          href,
          class: buttonClass(variant, size, block, extra),
          ...splitAttrs(vnode.attrs, [...OWN_KEYS, "href"]),
        },
        vnode.children,
      );
    },
  };
}

interface ButtonSubmitAttrs extends ButtonAttrs {
  form?: string;
  disabled?: boolean;
}

/** <button type="submit"> (ButtonSubmit.jinja). Pass form= to target a form by id. */
export function ButtonSubmit(): m.Component<ButtonSubmitAttrs> {
  return {
    view(vnode) {
      const {
        variant = "primary",
        size = "md",
        block = false,
        extra = "",
        form,
        disabled = false,
      } = vnode.attrs;
      return m(
        "button",
        {
          type: "submit",
          class: buttonClass(variant, size, block, extra),
          disabled,
          ...(form ? { form } : {}),
          ...splitAttrs(vnode.attrs, [...OWN_KEYS, "form", "disabled"]),
        },
        vnode.children,
      );
    },
  };
}
