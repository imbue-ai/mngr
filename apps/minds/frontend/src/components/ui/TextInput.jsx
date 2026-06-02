import { splitProps } from 'solid-js';

const BASE =
  'w-full px-3 py-2.5 text-sm rounded-md border border-zinc-200 ' +
  'bg-white text-zinc-900 outline-none transition ' +
  'focus:border-blue-600 focus:ring-2 focus:ring-blue-600/15';

export function TextInput(props) {
  const [local, rest] = splitProps(props, ['extra', 'class', 'type']);
  const cls = [BASE, local.extra, local.class].filter(Boolean).join(' ');
  return <input type={local.type || 'text'} class={cls} {...rest} />;
}
