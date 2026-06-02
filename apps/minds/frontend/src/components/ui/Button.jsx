import { splitProps } from 'solid-js';
import { buttonClass } from './button_classes.js';

// Generic <button> styled the same as the Jinja `ui.btn_button` / `ui.btn_submit`
// macros. Pass `type="submit"` to act as a form submit button.
export function Button(props) {
  const [local, rest] = splitProps(props, ['variant', 'block', 'extra', 'class', 'children']);
  return (
    <button
      class={buttonClass({ variant: local.variant, block: local.block, extra: [local.extra, local.class].filter(Boolean).join(' ') })}
      {...rest}
    >
      {local.children}
    </button>
  );
}
