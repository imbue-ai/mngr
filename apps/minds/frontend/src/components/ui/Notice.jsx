import { splitProps } from 'solid-js';

const BASE = 'px-3 py-2.5 rounded-md text-sm my-2';

const VARIANTS = {
  info: 'bg-blue-50 text-blue-900 border border-blue-100',
  warn: 'bg-amber-50 text-amber-900 border border-amber-200',
  success: 'bg-emerald-50 text-emerald-700 border border-emerald-200',
  error: 'bg-red-50 text-red-600 border border-red-200',
};

export function Notice(props) {
  const [local, rest] = splitProps(props, ['variant', 'extra', 'class', 'children']);
  const variant = local.variant || 'info';
  const variantClass = VARIANTS[variant];
  if (!variantClass) {
    throw new Error(`Unknown notice variant: ${variant}`);
  }
  const cls = [BASE, variantClass, local.extra, local.class].filter(Boolean).join(' ');
  return (
    <div class={cls} {...rest}>
      {local.children}
    </div>
  );
}
