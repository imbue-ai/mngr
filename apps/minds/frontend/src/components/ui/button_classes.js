// Shared button class strings. Kept in one place so Button.jsx,
// ButtonLink.jsx, and the form-submit variants don't drift apart.
// Mirrors _BTN_BASE / _BTN_VARIANTS in templates/_macros.html.

export const BTN_BASE =
  'inline-flex items-center justify-center gap-1.5 px-3.5 py-2 ' +
  'rounded-md font-medium text-sm leading-tight transition-colors ' +
  'disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer ' +
  'no-underline whitespace-nowrap';

export const BTN_VARIANTS = {
  primary:
    'bg-zinc-900 text-zinc-50 border border-transparent hover:bg-zinc-800',
  secondary:
    'bg-zinc-100 text-zinc-900 border border-zinc-200 hover:bg-zinc-200',
  danger:
    'bg-red-50 text-red-600 border border-red-200 hover:bg-red-100',
  success:
    'bg-emerald-800 text-emerald-50 border border-transparent hover:bg-emerald-900',
  ghost:
    'bg-transparent text-zinc-600 border border-transparent hover:bg-zinc-100 hover:text-zinc-900',
};

export function buttonClass({ variant = 'secondary', block = false, extra = '' } = {}) {
  const variantClass = BTN_VARIANTS[variant];
  if (!variantClass) {
    throw new Error(`Unknown button variant: ${variant}`);
  }
  return [BTN_BASE, variantClass, block ? 'w-full' : '', extra].filter(Boolean).join(' ');
}
