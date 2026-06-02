# Minds — Stateful Design System

> I've created this Figma design system that I want to bring into the app (https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=308-4059&p=f&t=D0X7nv6rcuGNEKhz-11). How should we go about it?
>
> * Ship both dark and light, but theme is **derived per-screen from the workspace color** — the workspace color *is* the chrome background, and components flip light/dark based on its luminance. No screen mixes themes, except the FCT-rendered main area inside the workspace shell.
> * Luminance threshold is **computed automatically** in CSS/JS from the workspace color — no per-color pre-tagging.
> * Land in two PRs: PR 1 = new tokens + refreshed macros + styleguide (no page changes); PR 2+ = swap production pages one at a time off raw Tailwind utilities.
> * Pages without a workspace context (`login`, `landing`, `welcome`, `create`, `accounts`, `auth_error`, dev styleguide) render on **pure black** (`#000000`) with dark-theme components.
> * Stay on the **Tailwind Play CDN** (zero build step) and inject an inline `tailwind.config = {...}` block that defines semantic class names (`bg-surface`, `text-primary`, etc.) mapped onto CSS variables in `tokens.css`.
> * Refresh existing macros only on this pass (`btn_button`, `text_input`, `notice`, `card`, `spinner`); don't add new component macros yet.
> * **Mix CSS-var-driven color with Tailwind-utility layout** — macros consume semantic color tokens (so they auto-adapt to the surrounding theme) while keeping Tailwind utilities for spacing, radius, typography, and flex/grid layout.
> * Keep the existing hue-derived accent for old workspaces; the Figma palette becomes the picker source for new workspaces in a *separate* future PR.
> * For existing workspaces, **keep the hash-of-agent-id hue scheme** but bump luminance from `oklch(65% 0.15 H)` to `oklch(70% 0.15 H)` and always render light-theme components on top of that surface.
> * Workspace color paints **the titlebar, the sidebar, and every internal workspace-scoped page** (`recovery`, `workspace_settings`, `creating`, `destroying`, ...); only the FCT iframe stays unthemed.
> * Token system is **comprehensive** — colors, the Figma typography ramp, and spacing/radius scales all become semantic tokens (no "just colors, leave the rest on Tailwind defaults").
> * PR 2 migration starting point is **deferred** — decide after PR 1 lands.
> * Dev styleguide gets a **surface-color picker at the top**: pick a workspace color and the entire styleguide page re-tints and flips theme accordingly, so light + dark variants can be inspected interactively.

## Overview

- **Goal**: bring the Figma "Stateful Design System" into the Minds desktop client as a working tokens + macros + styleguide foundation, then migrate production pages onto it incrementally.
- **Stateful** means the rendered theme (component palette, text/border colors) is a function of the **workspace surface color** rather than a global preference — picking a new workspace recolors and re-themes the chrome around it.
- **Two-PR split**: PR 1 adds the system without touching production pages; PR 2+ swaps page-by-page off raw Tailwind utilities. This keeps each PR small, reviewable, and revertible.
- **Zero new build step**: extend the existing Tailwind Play CDN with an inline `tailwind.config = {...}` block that defines semantic colors / typography / spacing / radius keyed off CSS variables in `static/tokens.css`. Defaults (`zinc`, `blue`, `red`, ...) stay available via `extend` so unmigrated pages keep working.
- **Existing workspaces stay visually stable**: the hash-of-agent-id hue scheme is preserved, just bumped to `oklch(70% 0.15 H)`, and always renders light-theme components on top. New workspaces will get a palette picker in a *future* PR.

## Expected behavior

### From the user's perspective (PR 1, no page-by-page migration yet)

- All existing pages (`landing`, `create`, `creating`, `welcome`, `permissions`, `workspace_settings`, `recovery`, `sharing`, `destroying`, ...) render visually unchanged in this PR.
- `/_dev/styleguide` is rebuilt into the new layout from the Figma:
  - A **workspace-color picker** at the top — pick a Figma palette color (or the default pure-black surface, or one of the existing hue-derived OKLCH samples) and the whole styleguide page re-tints and flips its component theme automatically.
  - Live previews of typography ramp, spacing scale, radius scale, palette swatches (with the `data-token` ratchet kept green), buttons / inputs / notices / cards / spinners through their (refreshed) macros, and the chrome patterns already documented.
- Macros emitted via `_macros.html` still take the same arguments and still work in every place they're currently called, but their internal class lists now reference semantic tokens (`bg-surface-elevated`, `text-primary`, `border-default`) instead of `bg-white`, `text-zinc-900`, `border-zinc-200`. They render identically against today's light-default pages because the semantic tokens collapse to the same colors when no workspace surface is set.

### From the user's perspective (after PR 2+ page migrations)

- The chrome (titlebar + sidebar) and every internal workspace-scoped page (`recovery`, `workspace_settings`, `creating`, `destroying`) paints its full background in the workspace's color (existing workspaces: bumped OKLCH(70% / hash hue); new workspaces: a picker from the Figma palette).
- The Forever-Claude-Template iframe inside the workspace shell **is not themed** — it continues to load whatever the FCT app renders.
- Text, borders, buttons, inputs, notices, cards, spinners flip between light- and dark-theme palettes automatically based on the surface color's luminance. There is no global dark-mode toggle; users see a consistent theme per workspace because the workspace color dictates it.
- Pages with no workspace context (`login`, `landing`, `welcome`, `create`, `accounts`, `auth_error`, the dev styleguide as a baseline) render on a pure-black (`#000000`) surface with dark-theme components.
- Switching between workspaces in the chrome reflows the entire chrome surface color + component theme without a full page reload (driven by `chrome.js`'s existing `applyTitleSwatch` hook).

### From the system's perspective

- Every page's `<html>` element carries a `data-theme="dark"|"light"` attribute set by a tiny JS module (`static/theme.js`) that reads `--workspace-surface` from computed styles and compares its luminance against a threshold. Tailwind's `darkMode` is configured to key off this attribute (`darkMode: ['selector', '[data-theme="dark"]']`), so existing `dark:bg-...` utilities work alongside the semantic tokens.
- CSS variables in `static/tokens.css` are defined in two layers:
  - **Primitive tokens** (the raw Figma palette + type ramp + spacing/radius scales) defined once on `:root`.
  - **Semantic tokens** (`--surface-base`, `--surface-elevated`, `--text-primary`, `--text-secondary`, `--text-muted`, `--border-default`, `--border-strong`, `--accent-fg`, `--state-info-bg`, ...) defined twice — once under `:root[data-theme="dark"]` and once under `:root[data-theme="light"]` — each mapping to the primitives appropriate for that theme.
- The `--workspace-surface` variable is set per-page (via inline `style` on `<body>` from the server, computed via the existing `workspace_accent()` helper in `templates.py`) and is what `static/theme.js` reads to pick the theme. For pages without a workspace context, the server emits `--workspace-surface: #000` and `data-theme="dark"` directly.
- The drift-guard ratchet (`test_dev_styleguide_token_swatches_enumerate_root_declarations`) is extended to enumerate the new semantic tokens too, so the styleguide can't silently drift from `tokens.css`.

## Implementation plan

> All file paths in this section are under `apps/minds/imbue/minds/desktop_client/` unless noted.

### New / heavily edited files (PR 1)

- **`static/tokens.css`** (heavily edited): adds the full primitive + semantic CSS-variable system. Sections:
  - `:root` block — primitives: `--palette-*` (Figma color swatches), `--type-*` (font size + line height + weight tuples), `--space-*`, `--radius-*`, `--shadow-*`.
  - `:root[data-theme="dark"]` block — semantic tokens mapped to dark-friendly primitives.
  - `:root[data-theme="light"]` block — same semantic tokens mapped to light-friendly primitives.
  - Existing per-workspace rules (`.page-workspace::before`, `.accent-spine::before`, `.sidebar-item`, `.accent-swatch`, `.opt`, `.opt-selected`, `.spinner`, `@keyframes spin`) are kept but rewritten to reference semantic tokens (e.g. `--accent-fg` instead of hardcoded `#2563eb`).
  - The existing `--shadow-seam` token is preserved (current ratchet pins it).
- **`static/theme.js`** (new): tiny module that runs on every page. Reads computed `--workspace-surface` from `:root`, parses its OKLCH/sRGB into relative luminance, sets `<html data-theme="dark|light">` and matches `color-scheme`. Exposes `window.mindsTheme.refresh()` so `chrome.js` can call it after changing the surface on workspace switch.
- **`static/tailwind_config.js`** (new): inline `tailwind.config = {...}` block (extracted to a file so the inline `<script>` in `base.html` stays short). Sets:
  - `darkMode: ['selector', '[data-theme="dark"]']`
  - `theme.extend.colors`: semantic keys (`surface`, `surface-elevated`, `surface-overlay`, `text`, `text-secondary`, `text-muted`, `border`, `border-strong`, `accent`, `info`, `warn`, `success`, `error`) mapped onto `'rgb(var(--surface-base) / <alpha-value>)'` etc. via CSS variables.
  - `theme.extend.fontSize`: the Figma type ramp (`display`, `h1`, `h2`, `h3`, `body`, `caption`, `mono-sm`).
  - `theme.extend.spacing`: the Figma spacing scale (additive — does not replace default 0-96).
  - `theme.extend.borderRadius`: the Figma radius scale (`none`, `sm`, `md`, `lg`, `xl`, `full`).
  - All extensions are *additive* (`extend`, not top-level `theme`) so the default zinc/red/blue palette and default sizes stay available — unmigrated production pages keep working until they're swapped.
- **`templates/base.html`** (edited):
  - Loads `static/tailwind_config.js` *before* `static/tailwind.js` (config-then-CDN order is mandatory for Play CDN).
  - Loads `static/theme.js` (defer) on every page.
  - The `body_style` block default emits `--workspace-surface: #000` for non-workspace pages (currently no default; this is new).
- **`templates/_macros.html`** (edited): rewrites each existing macro's class list onto semantic tokens. No new macros.
  - `btn_button` / `btn_link` / `btn_submit`: `_BTN_BASE` keeps Tailwind utilities for layout/spacing; `_BTN_VARIANTS` references semantic colors (`bg-surface-elevated text-text border-border hover:bg-surface-overlay`, `bg-accent text-text-on-accent ...`, `bg-error/10 text-error border-error/30 ...`, etc.).
  - `text_input`: drops hardcoded `border-zinc-200`/`text-zinc-900`/`focus:border-blue-600`/`focus:ring-blue-600/15` and uses `border-border bg-surface text-text focus:border-accent focus:ring-accent/15`.
  - `notice`: `_NOTICE_VARIANTS` referenced to `bg-info/10 text-info border-info/30` (and friends). Existing variant names (`info`, `warn`, `success`, `error`) preserved.
  - `card` / `card_row`: `bg-surface-elevated border-border shadow-sm rounded-xl`.
  - `spinner`: refactored to reference `--text-muted` (track) and `--text` (active) via the existing `.spinner` CSS class, rather than hardcoded zinc values.
  - `opt` / `opt-radio` / `opt-selected`: unchanged structurally; the underlying `.opt*` CSS in `tokens.css` is what changes.
  - Macro arity / parameter names are unchanged so every caller (`landing.html`, `create.html`, `creating.html`, `permissions.html`, `recovery.html`, ...) keeps working.
- **`templates/dev_styleguide.html`** (heavily edited): rebuilt to mirror the Figma side-by-side layout, with these sections in order:
  1. **Surface-color picker** at the top — a row of swatches: pure black (`#000`), each Figma palette color, and a "current workspace" sample. Clicking a swatch sets `--workspace-surface` on `<body>` and calls `mindsTheme.refresh()`; the rest of the page re-tints and flips theme automatically.
  2. **Tokens** — current `data-token` swatches plus all new primitive + semantic tokens. The drift-guard ratchet sees every `--name` declared on a `:root` block in `tokens.css`.
  3. **Typography** — every Figma type ramp size, rendered with sample text and labeled (`display`, `h1`, `h2`, ...).
  4. **Spacing & radius** — visual scale of each token.
  5. **Components** — all five refreshed macros (`btn_button` × variants, `text_input`, `notice` × variants, `card`, `spinner`) rendered against the current picker surface.
  6. **Chrome patterns** — titlebar buttons, window controls, sidebar items, notification badge, accent spine, focus ring, shadow seam (kept from the current styleguide).
- **`static/dev_styleguide.js`** (edited): adds the picker behavior — wire clicks on the new surface swatches to `document.body.style.setProperty('--workspace-surface', ...)` + `window.mindsTheme.refresh()`. The existing accent-hue slider is kept but moved into the picker section.

### New / heavily edited files (PR 2+, deferred — listed for completeness, no work yet)

- `templates/chrome.html`, `static/chrome.js` — repaint titlebar + sidebar from the new tokens; on workspace switch, `applyTitleSwatch` also sets the chrome's surface color (not just the accent), then calls `mindsTheme.refresh()`.
- `templates/sidebar.html`, `static/sidebar.js` — sidebar items styled from semantic tokens; current `.sidebar-item::before` accent stripe can be retired or repurposed.
- One page at a time: `landing.html`, `welcome.html`, `create.html`, `creating.html`, `permissions.html`, `recovery.html`, `workspace_settings.html`, `sharing.html`, `destroying.html`, `accounts.html`, `auth_error.html`, `latchkey_*.html` — strip raw zinc/red/blue Tailwind utilities, replace with semantic tokens.
- `templates.py` — the existing `workspace_accent()` helper is bumped from `oklch(65% 0.15 H)` to `oklch(70% 0.15 H)` *for existing workspaces*; this happens in the PR that ships the first themed page, not in PR 1.
- `static/workspace_accent.js` — same bump on the client side.

### Tests (PR 1)

- **`templates_test.py`** (edited):
  - The existing `test_dev_styleguide_token_swatches_enumerate_root_declarations` is generalized: enumerate **every** `:root` block (`:root`, `:root[data-theme="dark"]`, `:root[data-theme="light"]`) and assert each declared `--name` has a matching `data-token="--name"` on the styleguide page (or vice versa). Failure surfaces drift in either direction.
  - New `test_render_dev_styleguide_page_surfaces_surface_picker` — asserts the picker scaffold (`data-surface-picker`, the pure-black swatch, at least one Figma palette swatch) is present.
  - New `test_render_dev_styleguide_page_renders_typography_ramp` — every Figma type-ramp size label is in the rendered HTML.
  - New `test_render_dev_styleguide_page_renders_spacing_and_radius_scales` — every spacing / radius token name is present as a `data-token` swatch.
  - Existing assertions on macro pass-through (`>Primary<`, `All set: action completed.`, `name="styleguide-focus-ring-input"`) are kept; they're a free regression test for the macro refresh.
- **No tests for `static/theme.js` or `static/tailwind_config.js`** — they're browser-only JS with no Python entry point; behavior is verified by the manual tmux + Electron pass below.

## Implementation phases

### Phase 1 — Tokens skeleton + Tailwind config

- Extend `static/tokens.css` with the primitive + semantic CSS-variable blocks (Figma palette colors, type ramp, spacing/radius scales). Leave existing per-workspace rules and `--shadow-seam` token in place but rewrite their values to reference semantic tokens.
- Add `static/tailwind_config.js` with the `extend.colors` / `extend.fontSize` / `extend.spacing` / `extend.borderRadius` config and `darkMode: ['selector', '[data-theme="dark"]']`.
- Wire `tailwind_config.js` and `theme.js` into `base.html`.
- Add `static/theme.js` with the luminance-driven `data-theme` setter and `window.mindsTheme.refresh()` API.
- *Working state*: existing pages render unchanged (semantic tokens collapse to current colors when the default light theme is in effect on the default pure-white background); `data-theme="dark"` is set on `<html>` when `--workspace-surface` is dark; nothing visibly changes yet.

### Phase 2 — Macros refresh

- Rewrite `_BTN_VARIANTS`, `_NOTICE_VARIANTS`, `text_input`, `card` / `card_row`, `spinner` in `_macros.html` to reference semantic tokens.
- *Working state*: every existing call site (`landing.html`, `create.html`, `creating.html`, ...) renders visually identical to today on its current pure-white background, but the underlying classes now adapt automatically when placed on a non-white surface.

### Phase 3 — Styleguide rebuild

- Replace `templates/dev_styleguide.html` body with the new layout (surface picker + typography + spacing/radius + components + chrome patterns).
- Update `static/dev_styleguide.js` to wire the surface picker.
- *Working state*: `/_dev/styleguide` shows the full system; a developer can pick any workspace surface (including pure black and the existing hue-derived sample) and see every component flip theme.

### Phase 4 — Test ratchet update

- Generalize `test_dev_styleguide_token_swatches_enumerate_root_declarations` to enumerate all three `:root` selectors.
- Add the three new styleguide-presence tests.
- *Working state*: PR 1 is shippable. Production pages remain untouched.

### Phase 5+ — Page-by-page migration (separate PRs, scope TBD)

- Out of scope for PR 1. First target is **deferred** — decide after PR 1 lands. Candidate order: `chrome.html` (biggest visual change, validates the workspace-color repaint end-to-end) → `landing.html` → `workspace_settings.html` → `creating.html` / `destroying.html` / `recovery.html` → `create.html` / `welcome.html` → smaller pages (`auth_error.html`, `accounts.html`, `latchkey_*.html`).

## Testing strategy

### Unit / template tests (Python, via `just test-quick`)

- `templates_test.py::test_dev_styleguide_token_swatches_enumerate_root_declarations` — drift ratchet against the new multi-`:root` token surface.
- `templates_test.py::test_render_dev_styleguide_page_surfaces_surface_picker` — picker scaffold present.
- `templates_test.py::test_render_dev_styleguide_page_renders_typography_ramp` — every type-ramp label present.
- `templates_test.py::test_render_dev_styleguide_page_renders_spacing_and_radius_scales` — every spacing/radius `data-token` swatch present.
- Existing macro/pattern assertions (`>Primary<`, `>Danger<`, `All set: action completed.`, `name="styleguide-focus-ring-input"`) — regression coverage on the refreshed macros.

### Manual verification (PR 1)

- `just minds-start` and open `/_dev/styleguide` in the dev Electron app:
  - Verify the picker re-tints + re-themes the whole page when each swatch is clicked.
  - Verify components legible at high and low surface luminance (the OKLCH-70 hash hues + the Figma's lightest palette colors + pure black).
  - Verify all existing pages (`/`, `/create`, `/welcome`, `/workspace/<id>/settings`, ...) still look correct (they should — they're still on raw zinc utilities).
- Manual tmux smoke: navigate the dev Electron app between workspaces; confirm chrome accent stripe still works (PR 1 hasn't changed it) and `<html>` flips `data-theme` based on the page's surface.

### Edge cases

- **Pure-black surface with very-low-contrast palette colors** (e.g. dark cobalt) — picker should flip the styleguide to dark theme; readability check.
- **Very-light pastel palette colors** (the sage/cream tones) — should flip to light theme; readability check.
- **Workspace switch over SSE** — `chrome.js`'s existing `applyTitleSwatch` flow must call `mindsTheme.refresh()` (added in Phase 1) so the theme flips when the agent identity changes.
- **No `--workspace-surface` set** (pages opened directly without the chrome wrapper) — `theme.js` falls back to assuming dark theme (`#000` surface) and emits `data-theme="dark"` so the page is not unstyled.
- **CSS-variable inheritance across the iframe** — the chrome iframe's `<html>` is a different document; the chrome's `--workspace-surface` does *not* leak in. The iframe page sets its own surface from its server-side body inline style (current pattern, unchanged for PR 1).
- **Drift between `tokens.css` `:root` blocks and styleguide swatches** — caught by the generalized ratchet.

## Open questions

These are unresolved from the Q&A and need to be settled before or during PR 1:

- **Token name extraction from Figma.** Cleanest path is for you to select all variables in the Figma desktop app so `get_variable_defs` can read them back verbatim, but we deferred this. Options:
  - (a) You select all variables → I copy names exactly.
  - (b) I propose a semantic naming scheme from the screenshot → you rename in review.
  - (c) Skip extraction; invent names as we build.
- **Theme-switching mechanism.** Three viable shapes:
  - (a) JS reads `--workspace-surface`, sets `<html data-theme=...>`, Tailwind's `darkMode: ['selector', '[data-theme="dark"]']` keys off it. (My recommendation — the plan assumes this.)
  - (b) Modern CSS `light-dark()` function — fewer JS moving parts but relies on `color-scheme` being set and is less flexible for non-binary cases.
  - (c) Two parallel variable namespaces, swapped by JS class toggle.
- **Brand / interactive accent color.** Currently `blue-600` (links, focus rings, "Configure" toggle). The Figma's components are mostly neutral with red for danger; what replaces blue?
  - (a) Keep `blue-600`.
  - (b) Adopt a specific Figma swatch (cobalt? red? lime?).
  - (c) Use surface contrast (white on dark, black on light) + focus rings derived from the workspace surface itself.
  - (d) Defer to PR 2+.
- **Font family.** Today's system `font-sans` vs Inter vs a custom face? Affects what `base.html` loads and whether `static/` needs a new woff2 asset.
- **How internal pages pick up the workspace color.** The plan assumes the existing pattern — server-side `workspace_accent()` inlines `style="--workspace-surface: ..."` on `<body>` — but we have not confirmed this is the desired path for the new full-surface paint (vs. JS reading `agent_id` and resolving client-side, or chrome `postMessage`-ing the color into the iframe).
- **Migration starting point for PR 2.** Explicitly deferred. Candidate ordering listed above under Phase 5+; needs a decision before that PR is opened.
- **Luminance threshold.** Plan uses standard relative-luminance ≥ 0.5 → light theme; ≤ 0.5 → dark theme. Some palette colors near the boundary may need pre-tagging or a tuned threshold; verify during manual review with the actual Figma palette.
- **Backwards compat with utilities like `text-blue-600`, `bg-zinc-100`, `border-red-200` used inline across production templates.** The plan keeps Tailwind's default palette available via `extend` (not top-level `theme.colors`), so these continue to work. Confirm there are no name collisions between our semantic keys (`surface`, `accent`, `info`, ...) and existing default Tailwind color names (none today — `surface`/`accent` aren't default Tailwind colors).
