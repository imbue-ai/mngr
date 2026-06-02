import { splitProps } from 'solid-js';

const BASE = 'bg-white border border-zinc-200 rounded-xl shadow-sm p-4';

export function Card(props) {
  const [local, rest] = splitProps(props, ['extra', 'class', 'children']);
  const cls = [BASE, local.extra, local.class].filter(Boolean).join(' ');
  return (
    <div class={cls} {...rest}>
      {local.children}
    </div>
  );
}

export function CardRow(props) {
  const [local, rest] = splitProps(props, ['extra', 'class', 'children']);
  const cls = [BASE, 'flex items-center justify-between gap-3', local.extra, local.class].filter(Boolean).join(' ');
  return (
    <div class={cls} {...rest}>
      {local.children}
    </div>
  );
}
