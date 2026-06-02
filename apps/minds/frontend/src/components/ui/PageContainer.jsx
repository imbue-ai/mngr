import { splitProps } from 'solid-js';

const BASE = 'max-w-[720px] mx-auto px-6 py-12';

export function PageContainer(props) {
  const [local, rest] = splitProps(props, ['extra', 'class', 'children']);
  const cls = [BASE, local.extra, local.class].filter(Boolean).join(' ');
  return (
    <div class={cls} {...rest}>
      {local.children}
    </div>
  );
}
