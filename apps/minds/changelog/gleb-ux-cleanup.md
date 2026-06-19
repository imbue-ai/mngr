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
