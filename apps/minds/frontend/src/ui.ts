// The button / input class recipes, mirrored from the Catalog globals in
// templates.py (BTN_BASE / BTN_SIZES / BTN_VARIANTS / INPUT_BASE -- Figma
// "Button" node 342-4059 and text field node 345-4059). Components render the
// same class strings the JinjaX primitives emit so both render paths stay
// pixel-identical; keep the two files in sync until the remaining Jinja
// button call sites are migrated.

export const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 leading-tight " +
  "transition-transform duration-100 ease-in-out disabled:opacity-40 disabled:cursor-not-allowed " +
  "cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export const BTN_SIZES: Record<"md" | "lg" | "icon", string> = {
  md: "px-4 py-2 rounded-md type-label",
  lg: "px-4 py-3 rounded-md type-label",
  icon: "p-1.5 rounded-md type-label",
};

export const BTN_VARIANTS: Record<"primary" | "secondary" | "danger" | "success" | "ghost", string> = {
  primary: "bg-surface-inverse text-inverse-primary border border-transparent hover:opacity-80",
  secondary: "bg-transparent text-primary border border-default hover:bg-fill-hover",
  danger: "bg-important text-white border border-transparent hover:opacity-90",
  success: "bg-success text-white border border-transparent hover:opacity-90",
  ghost: "bg-transparent text-primary border border-transparent hover:bg-fill-hover",
};

export function buttonClasses(variant: keyof typeof BTN_VARIANTS, size: keyof typeof BTN_SIZES = "md"): string {
  return `${BTN_BASE} ${BTN_SIZES[size]} ${BTN_VARIANTS[variant]}`;
}

// Select.jinja's recipe: the native arrow is hidden (appearance-none) and the
// caller overlays a chevron icon; pr-8 leaves room for it. Wrap the <select>
// in a relative div and place the chevron per the Select component.
export const SELECT_CLASSES =
  "appearance-none w-full pr-8 leading-tight p-2 type-body border border-strong bg-surface-primary " +
  "text-primary placeholder:text-tertiary hover:border-stronger focus:border-stronger " +
  "focus:outline-2 focus:outline-offset-2 focus:outline-accent rounded-md";

// TextInput.jinja's recipe: INPUT_BASE plus the single-line control's width,
// radius and tight leading.
export const TEXT_INPUT_CLASSES =
  "w-full leading-tight p-2 type-body border border-strong bg-surface-primary text-primary " +
  "placeholder:text-tertiary hover:border-stronger focus:border-stronger " +
  "focus:outline-2 focus:outline-offset-2 focus:outline-accent rounded-md";
