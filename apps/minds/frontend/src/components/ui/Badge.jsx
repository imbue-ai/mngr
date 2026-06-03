import { splitProps } from 'solid-js';

// Status badge / pill. Used across the landing, destroying, recovery,
// and accounts pages to render small inline status labels (e.g.
// "Destroying...", "Failed", "Default", "Signed out").
//
// The base + per-variant class strings mirror the inline Tailwind utility
// runs the Jinja templates were using directly. Surfacing them through
// this component means a future tone change touches one file rather than
// twenty.

const BASE =
  'inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-sm font-medium';

const VARIANTS = {
  success: 'bg-emerald-100 text-emerald-800',
  warn: 'bg-amber-100 text-amber-800',
  error: 'bg-red-100 text-red-800',
  info: 'bg-blue-100 text-blue-800',
  neutral: 'bg-zinc-100 text-zinc-600',
};

export function Badge(props) {
  const [local, rest] = splitProps(props, ['variant', 'extra', 'class', 'children']);
  const variant = local.variant || 'neutral';
  const variantClass = VARIANTS[variant];
  if (!variantClass) {
    throw new Error(`Unknown badge variant: ${variant}`);
  }
  const cls = [BASE, variantClass, local.extra, local.class].filter(Boolean).join(' ');
  return (
    <span class={cls} {...rest}>
      {local.children}
    </span>
  );
}
