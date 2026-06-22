Replaced the desktop client's runtime Tailwind (Play CDN JIT) with a compiled Tailwind v4 build step. The chrome's styles now come from a single minified, tree-shaken stylesheet (`app.min.css`) built ahead of time from `static/app.css` -- no runtime JIT, fully offline, and smaller. This is the foundation for an upcoming light/dark design-token system.

What changed for developers:

- `static/tokens.css` is gone; its hand-written tokens + component CSS now live in `static/app.css` (the Tailwind v4 source entry), which compiles to the gitignored `static/app.min.css`.

- Build the stylesheet with `just minds-css` (replaces `just minds-tailwind`). It also runs automatically on `pnpm install` (postinstall) and is rebuilt before packaging by `scripts/build.js`.

- `just minds-start` now runs the compiler in `--watch` mode alongside Electron, so class changes rebuild live. Because the sheet is compiled, a new/changed Tailwind class only takes effect after a rebuild.

- The compiled sheet is force-included into the wheel via `[tool.hatch.build] artifacts`; `@tailwindcss/cli` and `tailwindcss` are pinned to exact versions.

Began the light/dark design-token system, starting with text colors:

- New themeable text utilities: `text-primary` / `-secondary` / `-tertiary` (text on the current surface) and `text-inverse-*` (text on an inverted surface). Pure black/white at three alpha steps; regular and inverse mirror each other and swap between light and dark.

- Tokens are built in two layers in `app.css`: a per-mode value layer (`:root`/`.light` for light, `.dark` for dark) and an `@theme inline` token layer. Switching the whole app between modes is a single `.dark` class on `<html>` -- no component changes. A `.light` scope can force a light island under a dark ancestor (and vice versa).

- The dev styleguide (`/_dev/styleguide`) gains a light/dark toggle (persisted in `localStorage`, honored app-wide via a pre-paint script in `Base.jinja`) and a "Text color tokens" section showing both modes side by side.

- Migrated the on-light text call sites off the raw zinc ramp to these tokens (`text-zinc-900/800/700` → `text-primary`, `-600/-500` → `text-secondary`, `-400` → `text-tertiary`) across templates, vanilla JS, and the shared button/input class constants. On-dark / inverse text (e.g. log boxes, the primary button label) is intentionally left until the chrome and button stages.

Added themeable border tokens (next design-system category): `border-subtle` / `border-default` / `border-strong` (Figma's 10% / 16% / 25% alpha), pure black in light and pure white in dark.

- Migrated the border call sites: `border-zinc-200` → `border-default` (standard surfaces), form-control borders (`INPUT_BASE`) → `border-strong` to match Figma's form fields, `border-zinc-300` → `border-strong`, `border-zinc-100` → `border-subtle`. On-dark borders (the bg-black menus / log boxes) and status/accent borders are left for their stages.

- Retired the v4 border-compat shim: the global default border color now resolves to the `border-default` token, so every bare `border` is themeable without naming a color. Standard borders are now slightly more defined and inputs noticeably so, matching Figma.

Added themeable surface + fill tokens (next category): surfaces `bg-surface-primary` (solid base; white in light, pure black in dark), `bg-surface-inverse` (its mirror -- the neutral accent, pairs with `text-inverse-*` for primary buttons), and `bg-surface-overlay` (the inverse color at 20%, for backdrops); fills `bg-fill-subtle` / `-hover` / `-active` (translucent tints; Figma's `selected` dropped as redundant).

- Migrated the background call sites: `bg-white` → `bg-surface-primary` (page, cards, inputs), `bg-zinc-100` / `bg-zinc-50` → `bg-fill-subtle`, `hover:bg-zinc-*` → `hover:bg-fill-hover`, modal/drawer scrims (`bg-black/20-30`) → `bg-surface-overlay`. The primary button is now `bg-surface-inverse text-inverse-primary` with an `opacity-90` hover (it flips: black button/white text in light, white/black in dark). On-dark fixed islands (the bg-black floating menus, bg-zinc-900 log boxes, on-dark white tints) are left for the chrome stage.

- Added `color-scheme: light` / `dark` to the theme roots so native controls (form fields, scrollbars, autofill, caret) render in the right scheme.

With surfaces tokenized, dark mode now renders correctly across page/cards/text/borders/buttons. (Light mode is unchanged.)

Added status / feedback tokens (Figma): `important` / `success` / `warning` / `info` (one solid hue each, mode-independent) + `focus-ring`. Notice / badge backgrounds derive from a single hue via an opacity modifier (e.g. `bg-success/12 border-success/30 text-success`), which adapts to the surface in both modes.

- Migrated the status call sites: Notice and StatusBadge variants, inline status text/boxes, the danger button (`bg-important/10 text-important`), and status pills now use the tokens; the solid success button is `bg-success` with fixed white text. Focus rings (inputs, color swatches) now use `focus-ring`. Link / selection blue is intentionally left as-is (not part of the status set).

Reworked the titlebar to self-theme from the workspace color in pure CSS. The bar derives a black/white contrast from `--titlebar-bg` via relative color (`lch(from …)`) on a `.titlebar-surface` scope and re-bases the foreground tokens on it, so the title and buttons read correctly on any workspace color (dark or light) -- with no JavaScript luminance and no server-side foreground calculation. `chrome.js` now just toggles `.titlebar-surface` alongside `--titlebar-bg`; the `TitlebarButton` and page title are plain tokens (`text-secondary` / `text-primary` / `hover:bg-fill-hover`). Neutral chrome (no workspace) follows the app's own tokens.

- Removed the now-dead foreground machinery this replaced: the SSE workspaces payload no longer carries an `accent_fg` triple, the accent-preview IPC bridge (`content-relay-preload.js` / `main.js`) no longer takes an `accentFg` argument, and `pick_workspace_foreground` (plus its sRGB-luminance helpers) is gone from `workspace_color.py`. `workspace_accent.js` keeps only the `normalizeHex` helper.

Made the always-dark surfaces (the floating workspace menu and the log / terminal / credential boxes) `.dark`-scoped islands styled with tokens (`bg-surface-primary`, `border-subtle`, `text-primary` / `text-secondary`, `hover:bg-fill-hover`, `bg-fill-active` for the selected row) instead of raw `bg-black` / `text-white/NN`. They stay dark regardless of the app theme but now derive their colors from the design tokens.

Added the accent / interactive token (last color family): a single mode-independent `accent` (a blue, `#0069d9`, chosen to clear WCAG AA as link text on both the white and pure-black surfaces) behind links, selected states, focus rings, and progress. Solid for selection / progress (`bg-accent`, `border-accent`); lighter rings and tints derive via an opacity modifier (`ring-accent/40`, `bg-accent/15`).

- Unified the two blues that had coexisted: the form focus ring (previously a separate Apple-blue `focus-ring` token) and the raw Tailwind `blue-600` used for links / selection / progress now both resolve to the one `accent` token. The standalone `focus-ring` token is gone.

- Migrated the call sites: `Link` (and `sharing.js` links), the ghost-Button "link" recipe (`Create` / `Latchkey` "Configure" / "Adjust"), the `Opt` selected state + textarea focus, the color-picker selection / focus rings, the create-flow progress bar + pulse dot, the accent spinner, the form-control focus ring (`INPUT_BASE`), the auth "waiting" notice, and the Landing "Backing up" badge. The styleguide gains an "Accent / interactive token" section (light + dark) and drops `focus` from the status grid.

Tokenized the last hardcoded-neutral component recipes in `app.css` so they theme in dark mode: the `.code-pill` background (`fill-subtle`), the `.opt` onboarding cards (`fill-subtle` background, `border-subtle` / `border-strong` borders + radio), the `.spinner` ring + top (`border-subtle` / `text-primary`), and the color hex-input pill background (`surface-primary`). The spinner now stays visible on dark surfaces too (e.g. the Destroying island), where its near-black top was previously invisible. The intentionally-always-dark elements (color-swatch rims, the close-button-hover red) are unchanged.

Tokenized the dev styleguide page's own chrome (`/_dev/styleguide`) so its light/dark toggle now themes the whole page, not just the demo panels. Migrated the section headers, captions, demo-card frames, and footer file-refs from raw zinc/white onto the design tokens; converted the text/border demo "Dark" panels (and the Icon12 chrome-glyph demo) from hardcoded `#18181b` to `.dark` token islands; and refreshed the link / ghost-link demos to the shipped recipes. Removed the now-obsolete "legacy zinc" text-ramp section (the migration it was waiting on is complete).

Tightened the corner-radius scale to four steps -- `rounded-sm` 2px / `rounded-md` 4px / `rounded-lg` 8px / `rounded-xl` 16px (plus `rounded-full` / `rounded-none`), defined in `app.css` `@theme`. The old 6px and 12px values round down: buttons / badges / inputs land at 4-8px and cards / modals / log boxes at 8px (the previous 12px). `rounded-xl` (16px) is reserved for the largest surfaces. The chrome content frame keeps a structural `rounded-[12px]` -- that 12px still matches Electron's `CONTENT_CORNER_RADIUS` and the OS window's outer rounding, so it stays the one documented exception to the scale. Styleguide radius section updated to the four steps.

Constrained the spacing scale to a fixed subset of Tailwind's native steps. `--spacing` stays the stock `0.25rem` (so `p-1` = 4px, `p-4` = 16px -- standard Tailwind, with all its docs / tooling / IntelliSense intact). Padding / margin / gap are limited to ten steps: `0.5 / 1 / 1.5 / 2 / 3 / 4 / 6 / 8 / 12 / 16` (= 2 / 4 / 6 / 8 / 12 / 16 / 24 / 32 / 48 / 64 px).

- The handful of off-scale spacings were snapped to the nearest step, e.g. inputs/buttons tighten slightly (`py-2.5` -> `py-2`, `px-3.5` -> `px-3`). Width / height / inset stay free for layout (component sizes untouched), and large fixed dimensions keep their explicit `[NNpx]` values.

- The styleguide gains a "Spacing scale" section listing the allowed steps and their px values.

Added two guard tests that hold the scales: padding / margin / gap must use the constrained spacing steps, and corner radius must use `rounded-sm/-md/-lg/-xl` / `-full` / `-none` (no `rounded-2xl`/`-3xl`/`-xs`, no arbitrary `rounded-[..]` except the documented content-frame `rounded-[12px]`). Both scan the authored source while skipping SVG path data. The radius guard caught the floating workspace menu's `rounded-[10px]`, now snapped to `rounded-lg` (8px).

Added the type ramp (Figma): six semantic roles defined as `@utility` in `app.css`, each bundling font-size + weight + line-height (and uppercase + tracking for the section eyebrow), so a text element's role is a single class. Color stays orthogonal -- compose with `text-primary` / `-secondary` / `-tertiary`.

- `type-heading-lg` 24/bold, `type-heading` 18/semibold, `type-label` 14/semibold, `type-body` 14/regular, `type-helper` 12/regular, `type-section` 12/semibold/all-caps. Sizes reuse Tailwind's native steps (24/18/14/12 = text-2xl/lg/sm/xs).

- Migrated every content-text site to a role (strict four sizes): 20px headings collapse to `type-heading` (18), the Welcome 30px splash to `type-heading-lg` (24), and 10/11/13px captions to `type-helper` (12) / `type-body` (14). `font-medium` is dropped app-wide (the ramp is 400/600) -- block roles bundle their weight and inline emphasis is now `font-semibold`. Components (FormLabel, SectionHeader, StatusBadge, Button + inputs, ...), pages, and JS-built DOM all use the roles; the ghost-Button "link" recipe uses `!type-helper`.

- The styleguide gains a "Type ramp" section demoing the six roles. A guard test keeps content text on the roles: no raw font-size utilities or `font-medium` in the authored source (SVG path data skipped); inline `font-normal` / `font-semibold` / `font-bold` stay allowed.

- Fixed a `Notice` regression from the migration: the role swap dropped the separating space in the component's runtime class concat, fusing `my-2` with the variant background (e.g. `my-2bg-info/12`) so every notice banner lost its vertical margin and background tint. Restored the space.

- Fixed the same dropped-space regression in the Landing page's JS-built badges: the role swap turned `text-sm font-medium ` (trailing space) into `type-label` (no trailing space), so the four `'... type-label' + tone` concatenations for the mind container-state, provider-status, and backup-status badges fused into an invalid `type-labelbg-...` class -- silently dropping both the type role and the tone color. Restored the separating space.

Dropped the unused `--shadow-seam` token. It was only ever demoed in the styleguide (no real surface applied it -- the titlebar drop shadow it once named is gone), so the definition, both styleguide demos, and its drift-guard entry were removed.

Added an elevation scale: two box-shadow steps defined in `app.css` `@theme` (generating `shadow-raised` / `shadow-overlay`), with a styleguide "Elevation" section and a guard.

- `shadow-raised` is the subtle hover lift on interactive cards (the prior `shadow-sm` value, so cards are unchanged). `shadow-overlay` is the soft floating shadow for surfaces above the page -- menus, modals, tooltips -- taken from Figma's `minds-elevation-1` (two 8%-black drop shadows: `0 1px 1px` + `0 3px 12px`).

- Migrated the call sites: interactive cards / CardPage / Creating card -> `shadow-raised`; the floating workspace menu (previously a heavy `0 12px 32px` at 25%), the modal, and the inbox panel -> `shadow-overlay` (softer and now uniform). A guard test allows only `shadow-raised` / `shadow-overlay` / `shadow-none` -- raw Tailwind shadow steps and arbitrary `shadow-[..]` are disallowed.

Reorganized the dev styleguide page (`/_dev/styleguide`) into two labeled groups with clearer separation -- **Design System** (the foundational tokens plus the shared icon set) and **Patterns & Components** (the composed primitives) -- and added a sticky left-hand table of contents for jumping between sections.

- The light/dark toggle is now a fixed top-right control: it stays visible at any scroll position and floats over the page (no backing bar). It's rebuilt on the `Button` secondary primitive instead of bespoke button classes, so the styleguide's own chrome uses the design system it documents.

- Moved the 24px / 12px icon catalogs up into the Design System group (icons are a shared primitive ramp, like the color and type tokens); moved the workspace-accent picker and the color swatches down into Patterns & Components.

- Each section is a scroll anchor carrying a `scroll-mt` offset, so a TOC jump lands the heading below the viewport top rather than flush against it. `dev_styleguide.js` adds an `IntersectionObserver` scrollspy that marks the active section's link via `aria-current="page"` (styled in `app.css`).

- The color-swatch demo now shows the same three swatches per row (selected / default / disabled) at both the `md` and `sm` sizes, with a little more vertical space between the two rows -- so the size comparison is apples-to-apples.

- Fixed the selected color swatch's selection ring: the gap between the swatch and the accent ring is now a real transparent gap (via `outline` + `outline-offset`) instead of a hardcoded white rim, so it shows the background in every mode rather than flashing a stray white border in dark mode. Applies to both the settings (`md`) and create-form (`sm`) pickers.

Aligned the `Button` primitive with the Figma button component (node 342-4059). The default (md) size now uses the Figma padding -- `px-4 py-2` (16px / 8px) instead of `px-3 py-2` -- and the variant recipes were reworked:

- **Secondary** has no fill at rest: it's a `border-default` outline with `text-primary`, and only tints (`bg-fill-hover` on hover, `bg-fill-active` on press) on interaction.

- **Ghost** is now exactly secondary minus the border (transparent at rest, same hover/press fills).

- **Danger** is a solid semantic fill -- `bg-important` with white text -- replacing the previous subtle red tint; it dims slightly on hover/press.

- **Primary** (solid inverse surface) and **success** (solid green) keep their fills and now dim via opacity on hover/press to match. Every variant carries a 1px border (visible only on secondary, transparent elsewhere) so all variants render at the same height. Disabled opacity moved from 30% to 40% to match Figma.

- All button sizes now use `rounded-md`, which is 6px (see the radius-scale change below) -- so buttons match Figma's 6px corner.

Redefined the `rounded-md` radius step from 4px to 6px (scale: 2 / 6 / 8 / 16). md is the default control radius, so buttons, form inputs, badges, and color swatches all round at 6px now, matching Figma.

Gave buttons a focus ring drawn **outside** the button via `outline` + `outline-offset` (keyboard focus only, `focus-visible`), so it no longer overwrites the variant border; the offset gap is transparent in every mode.

Aligned form inputs (TextInput / Select / Textarea) with Figma's text field (node 345-4059): 12px padding (`p-3`), a tertiary-colored placeholder, a subtle `fill-subtle` tint on hover, and a focus ring drawn outside the field (`outline-offset`) that keeps the `border-strong` border instead of recoloring it (replacing the previous border-recolor + inner ring).

Lifted the accent color in dark mode to a brighter blue (`#0069d9` -> `#4d9bff`). On the pure-black dark surface the original accent read too dark: low-opacity tints (`accent/15`, `/40`) nearly vanished and link text was hard to read. The brighter dark-mode value keeps links, focus rings, selection tints, and progress legible (and clears WCAG AA as link text on black). Light mode is unchanged.

Decluttered the dev styleguide previews: dropped the redundant "Light" / "Dark" labels from the dual-mode token previews (the white/black cards are self-evident), removed the decorative card frame (border / background / padding) from the single-mode previews (Type ramp, Spacing, Corner radius) so the samples sit directly on the page, and dropped the borders from the corner-radius demo shapes (each is now just its filled shape).

Gave the primary button a pressed state: it now dims to 60% opacity on `:active` -- a clear step below the 80% hover, so a held press reads distinctly rather than blending into the hover state.

Gave the styleguide's floating light/dark toggle an opaque surface background (`.styleguide-toggle` in app.css) so it stays legible while floating over page content; the hover/active fills are composited over that surface as a background-image gradient rather than replacing it (a translucent fill background-color would let content show through).

Updated the status / feedback semantic hues (mode-independent): `success` `#5c8a3c` -> `#0c8106`, `warning` `#d49a2c` -> `#b45300`, `info` `#527ea3` -> `#166fc7`, `important` (error / failed) `#f50d00` -> `#d90c00`. All the derived notice / badge tints and status text pick up the new hues automatically.

Further decluttered the styleguide previews: dropped the background from the Elevation section (the two shadow cards now sit on the page, where the drop shadows still read), and removed the card frame (border / background / padding) from the single-mode component examples (buttons, form controls, spinner, notices, links, icons, badges, opt, oauth, section header, dialog close, the workspace-accent picker). The frame is kept only where it carries meaning: the dual-mode token cards, the colored self-theming titlebar surfaces, the dark sidebar / chrome-glyph islands, the accent-spine card, and the page-container / modal backdrop illustrations.

Tuned the radius scale and a couple of tokens:

- `rounded-sm` moved from 2px to 4px (scale is now 4 / 6 / 8 / 16).

- `Notice` banners are now borderless tinted boxes: an 8%-opacity hue fill (`bg-<hue>/8`) with the hue as text, no border (down from a 12% fill + bordered box).

Refined the titlebar buttons (`TitlebarButton`): the foreground is now always `text-primary` (full contrast, re-based per-workspace by `.titlebar-surface`) instead of resting at `text-secondary` and brightening on hover, and the `nav` variant is a square icon button sized by padding (`p-1.5` around the icon -> 28x28) rather than a fixed `w-8 h-7`. Its flex wrappers (the titlebar nav group in the chrome and the styleguide demo) use `items-center` so the button stays its square size instead of stretching to the titlebar height. The `control` variant (min / max / close) keeps its OS-matching `w-9 h-[38px]` geometry but also picks up the always-`text-primary` foreground.

The titlebar workspace title now uses the `type-label` role (14px / semibold) instead of `type-helper` (12px / regular), in both the chrome (`#page-title`) and the styleguide demo -- so the active workspace name reads as a proper title.
