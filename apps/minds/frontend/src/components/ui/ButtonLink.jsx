import { splitProps } from 'solid-js';
import { buttonClass } from './button_classes.js';

// Anchor styled as a button. Mirrors the Jinja `ui.btn_link` macro --
// used heavily in chrome / sidebar / landing.
export function ButtonLink(props) {
  const [local, rest] = splitProps(props, ['variant', 'block', 'extra', 'class', 'children']);
  return (
    <a
      class={buttonClass({ variant: local.variant, block: local.block, extra: [local.extra, local.class].filter(Boolean).join(' ') })}
      {...rest}
    >
      {local.children}
    </a>
  );
}
