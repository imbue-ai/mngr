import { splitProps } from 'solid-js';

const SIZE_DIMS = {
  sm: 'w-3.5 h-3.5 border',
  md: 'w-[18px] h-[18px] border-2',
  lg: 'w-8 h-8 border-[3px]',
};

export function Spinner(props) {
  const [local, rest] = splitProps(props, ['size', 'extra', 'class']);
  const size = local.size || 'md';
  const dim = SIZE_DIMS[size];
  if (!dim) {
    throw new Error(`Unknown spinner size: ${size}`);
  }
  const cls = ['spinner inline-block align-middle', dim, local.extra, local.class]
    .filter(Boolean)
    .join(' ');
  return <span class={cls} aria-hidden="true" {...rest} />;
}
