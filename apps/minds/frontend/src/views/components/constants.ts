// Shared class-string constants for the component catalog, ported VERBATIM
// from the legacy templates.py catalog globals (BTN_BASE / BTN_SIZES /
// BTN_VARIANTS / INPUT_BASE) so the SPA components render pixel-identical
// markup to the JinjaX primitives they replace. One source of truth: to
// restyle every button, edit here.

// The subtle press animation nudges the whole button to 98% scale -- animated
// over 100ms on the standard ease-in-out curve -- for a tactile click across
// every variant. Scoped to transition-transform so only the press scale eases;
// hover/press color + opacity changes flip instantly.
export const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 leading-tight " +
  "transition-transform duration-100 ease-in-out disabled:opacity-40 disabled:cursor-not-allowed " +
  "cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export type ButtonSize = "md" | "lg" | "icon";

// All sizes share the md control radius (rounded-md = 6px); they differ only
// in padding and (for icon) shape.
export const BTN_SIZES: Record<ButtonSize, string> = {
  md: "px-4 py-2 rounded-md type-label",
  lg: "px-4 py-3 rounded-md type-label",
  icon: "p-1.5 rounded-md type-label",
};

export type ButtonVariant =
  "primary" | "secondary" | "danger" | "success" | "ghost";

// Variant recipes (Figma "Button" component, node 342-4059). Every variant
// carries a 1px border -- visible on secondary, transparent elsewhere -- so
// all variants share the exact same box height regardless of border. Solid
// variants dim via opacity on hover; the no-fill variants tint with the fill
// tokens on hover.
export const BTN_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-surface-inverse text-inverse-primary border border-transparent hover:opacity-80",
  secondary:
    "bg-transparent text-primary border border-default hover:bg-fill-hover",
  danger: "bg-important text-white border border-transparent hover:opacity-90",
  success: "bg-success text-white border border-transparent hover:opacity-90",
  ghost:
    "bg-transparent text-primary border border-transparent hover:bg-fill-hover",
};

// Shared class string for the three form-control components (TextInput,
// Select, Textarea): the focus ring, border, padding and text size live in
// exactly one place. Width, border-radius and line-height vary per-component
// so they are NOT included here.
export const INPUT_BASE =
  "p-2 type-body border border-strong bg-surface-primary text-primary " +
  "placeholder:text-tertiary hover:border-stronger focus:border-stronger " +
  "focus:outline-2 focus:outline-offset-2 focus:outline-accent";

// Assemble a button's full class string. Full-width (block) buttons press
// with a gentler scale than the base active:scale-[0.98]: the same
// percentage moves the wide edges much farther, so it reads as too much.
export function buttonClass(
  variant: ButtonVariant,
  size: ButtonSize,
  block: boolean,
  extra: string,
): string {
  return (
    BTN_BASE +
    " " +
    BTN_SIZES[size] +
    " " +
    BTN_VARIANTS[variant] +
    (block ? " w-full active:!scale-[0.99]" : "") +
    " " +
    extra
  );
}
