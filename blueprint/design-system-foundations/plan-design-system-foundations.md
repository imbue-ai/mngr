# Plan: formalize the minds design system (workspace colors, foreground-at-opacity tokens, stateful components)

## Refined prompt

I want to formalize a bit of the design system for the minds app. In this PR I want to both — formalize things and also modify things (and hopefully these will neatly apply through most of the app since we have a bit of jinjax components in place). We may or may not need to make more compoentns or change their shape. Let me describe our system a bit:

1. Workspace colors (and this quite literally means the background color for the whole app). By default we'll want to run the app against the black palette color, but the user can opt per workspace to switch the color (we kind of do that now, but only show a little swatch in the title). I want to push this to whole chrome. The colors that are allowed are these: <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=314-4141&t=D0X7nv6rcuGNEKhz-11> please use their names and values. Do not make up any new ones.
   The way this will work is that based on which color you choose we flip the text and border and siurface colors to be some transparent black or transparent white to blend nicely.
   * The eleven allowed workspace tokens (Figma node 314:4141) are: `workspace/indifference` `#000000`, `workspace/confusion` `#0B292B`, `workspace/courage` `#492222`, `workspace/envy` `#3C3D06`, `workspace/peace` `#9FBBD3`, `workspace/belonging` `#E8A7A8`, `workspace/energy` `#CECD0C`, `workspace/strength` `#CFC7B3`, `workspace/comfort` `#F5D6A0`, `workspace/inspiration` `#E9ECD9`, `workspace/clarity` `#FCEFD4`.
   * The default for a new workspace is `workspace/indifference` (#000000); the picker lives on the workspace settings page and persists per-`agent_id` in the minds-side config (no agent-side or `.mngr/` involvement).
   * Workspaces with `#000000`–`#492222`–`#3C3D06` use the dark theme (white foreground at opacity); workspaces with `#9FBBD3`–`#FCEFD4` use the light theme (black foreground at opacity). This dark/light bucket is a property of the workspace color and is encoded in the same TS/Python catalog as the swatch list.

2. Then there are semantic colors like these <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=314-4126&t=D0X7nv6rcuGNEKhz-11> (for now both dark and light version includes the same set, but they might drift and be different for better look, so plan for that)
   * Semantic tokens (Figma node 314:4126): `semantic/important` `#F50D00`, `semantic/success` `#5C8A3C`, `semantic/warning` `#D49A2C`, `semantic/info` `#527EA3`. Today the values are identical in light + dark; in CSS we still split them across `[data-theme]` blocks so the two scopes can drift independently later without a value-site refactor.

3. For surfaces and text the color usage is such you can compare "light" and "dark" version (again as per 1 light or dark will be on a swath of backbground colors, these figmas just use black and white examples) <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=333-4059&t=D0X7nv6rcuGNEKhz-11>
   * Foreground-at-opacity ramps, exact values from the Figma Theme collection:
     * **Dark** — `text/primary` `#FFFFFF`, `text/secondary` `#FFFFFFB2` (70%), `text/tertiary` `#FFFFFF80` (50%), `text/disabled` `#FFFFFF59` (35%), `text/inverse` `#18181b`, `border/subtle` `#FFFFFF1A` (10%), `border/default` `#FFFFFF29` (16%), `border/strong` `#FFFFFF40` (25%), `fill/subtle` `#FFFFFF0F` (6%), `fill/hover` `#FFFFFF1A` (10%), `fill/active` `#FFFFFF24` (14%), `fill/selected` `#FFFFFF1F` (12%), `accent/solid` `#FFFFFF`, `accent/on` `#000000`, `surface/overlay` `#1C1C1EEB`, `surface/overlay-border` `#FFFFFF1F`, `chrome/bg` `#0E1A17`, `chrome/fg` `#FFFFFF`.
     * **Light** — `text/primary` `#000000`, `text/secondary` `#000000A6` (65%), `text/tertiary` `#00000073` (45%), `text/disabled` `#00000059` (35%; mirror of dark step), `text/inverse` `#FFFFFF`, `border/subtle` `#0000000F`, `border/default` `#0000001F`, `border/strong` `#00000040`, `fill/subtle` `#0000000A`, `fill/hover` `#0000000F`, `fill/active` `#0000001A`, `fill/selected` `#0000001F`, `accent/solid` `#000000`, `accent/on` `#FFFFFF`, `surface/overlay` `#FFFFFFEB`, `surface/overlay-border` `#0000001F`, `chrome/bg` and `chrome/fg` resolve to the active workspace color + inverse, so the chrome inherits its tone from the workspace, no fixed value.

4. Here are some multi state components <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=339-4059&t=D0X7nv6rcuGNEKhz-11>
   * Component state matrices to bake in (Figma node 339:4059):
     * Button: 4 variants × 6 states = 24 — variants Primary, Secondary, Ghost, Destructive; states Default, Hover, Pressed, Focused, Disabled, Loading.
     * TextField: 7 states — Default, Hover, Focus, Filled, Error, Disabled, Read-only.
     * Select: 4 states — Default, Hover, Open, Disabled (uses the exact `chevron-down` icon).
     * MenuItem: 5 states — Default, Hover, Selected, Destructive, Disabled.
     * Checkbox: 5 states — Unchecked, Checked, Indeterminate, Disabled, Focused.
     * Toggle: 4 states — Off, On, Disabled, Focused.
     * WorkspaceRow (the landing rows): Default, Hover, Selected, Unread, Disabled.
     * TitlebarBtn: Default, Hover, Active, With badge.
     * Skeleton: shimmer line variant, `fill/active` over any bg.

5. Please import icon paths exactly, do not make up icons
   * The icon set is the eight symbols in Figma node 339:4059 (`chevron-down` 339:4063, `chevron-right` 339:4067, `house` 339:4071, `inbox` 339:4075, `title-chevron` 339:4080, `check` 339:4085, `plus` 339:4091, `circle-user-round` 339:4098). Each is fetched via `get_design_context` at implementation time and inlined into a single `Icon.jinja`. We do not invent or substitute SVG geometry for any of these.

6. Focus color is #0A84FF at 90% (for both modes, but can drift so like in 2 account for two different focus colors if need be with identical values today)
   * Encoded as `--focus-ring: #0A84FFE5` in both `[data-theme="dark"]` and `[data-theme="light"]` scopes (identical hex today; the duplicated declaration is what enables independent drift later).

7. You can find surfaces, shadows, spacing and radii in the frames linked in 3.
   * Spacing scale: `--space-4` `4`, `--space-6` `6`, `--space-8` `8`, `--space-12` `12`, `--space-16` `16`, `--space-24` `24`, `--space-32` `32`.
   * Radii: `--radius-sm` `6px`, `--radius-md` `10px`, `--radius-lg` ~`14px` (confirmed at impl time from Figma node 317:4083), `--radius-pill` `999px`.
   * Type ramp (SF Pro): `Display/24 Bold` 24/30/700, `Heading/16 Semibold` 16/22/590, `Label/14 Medium` 14/20/510, `Body/14 Regular` 14/20/400, `Menu/13 Regular` 13/16/400, `Helper/12 Regular` 12/16/400, `Section/11 Semibold` 11/14/590, `Title/12 Bold` 12/14/700, `Badge/10 Bold` 10/12/700.
   * Shadows: keep the existing `--shadow-seam` token; no new shadow tokens from Figma.

8. How will the app look? Something like this: <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=406-4311&t=D0X7nv6rcuGNEKhz-11>
   * Implementation target screens (Figma section 406:4311): Create workspace (406:4313), Workspaces list (409:4321), Workspace settings (410:4332), Inbox modal (412:4345). The first three map onto existing pages (`pages/Create`, `pages/Landing`, `pages/WorkspaceSettings`); the inbox modal screen is reference for the existing permission/requests UX and is **not** in scope for this PR (flagged as a follow-up under Open Questions).

## Overview

- Replace the current OKLCH-hash-derived per-agent accent with an explicit 11-color named palette that the user can pick per workspace, defaulting to `workspace/indifference` (#000000).
- The picked color is the actual background for the whole app chrome (titlebar, sidebar, content surface), not just a thin stripe or swatch as today.
- Surfaces / borders / text are foreground-at-opacity tokens that automatically flip between white-on-dark and black-on-light depending on the picked workspace color.
- All design tokens (workspace colors, foreground ramps, semantic colors, focus ring, spacing, radii, type ramp) live in one `static/tokens.css`, scoped by `[data-theme="dark"|"light"]` so dark+light can drift independently later without touching call sites.
- All JinjaX primitive components (`Button`, `TextInput` → `TextField`, plus new `Select`, `Checkbox`, `Toggle`, `MenuItem`, `Skeleton`, `Icon`) consume those tokens via CSS variables, not raw Tailwind colors. The state matrices match the Figma component sets exactly.
- Existing chrome / page templates stop using `bg-zinc-900` / `text-zinc-200` / `border-white/10` / etc. and switch to token-backed utility classes. The migration is mechanical but broad — most templates change.
- A new `Icon` component holds the eight Figma icon paths verbatim; existing chrome icons that happen to map onto one of these names (chevron-right back/forward, etc.) get re-pointed; chrome icons not in the Figma library (titlebar window controls, restart, settings cog, etc.) are out of scope and remain as-is.
- The dev styleguide page gains a state-matrix grid per stateful component, a workspace-color picker (so reviewers can flip the whole page through the 11 colors), and a theme-flip toggle.

## Expected behavior

- The 11 workspace colors plus their dark/light bucketing are a closed catalog. Code that names a workspace color uses a Python `Enum` (`WorkspaceColor`) or its CSS-class slug (`workspace/indifference` → `ws-indifference`). Strings outside the catalog are rejected at the persistence boundary.
- Each agent has a chosen workspace color persisted in `~/.minds/config.toml` under `workspaces.<agent_id>.color`. Default for any agent without an entry is `indifference`.
- The workspace-settings page gets a "Color" section with a row of 11 swatches; clicking persists immediately and reloads the workspace surface in the new color. The selected swatch ring uses `focus/ring`.
- Every workspace-context page (`Chrome`, `Sidebar`, `Landing`, `WorkspaceSettings`, `Sharing`, `Creating`, `Destroying`, permission dialogs) renders with:
  - `<html data-theme="dark|light" data-ws-color="<slug>">` set server-side from the picked color.
  - `<body style="background: var(--ws-<slug>)">` — the workspace color is the page background; the chrome's titlebar matches it (no separate `bg-zinc-900` chrome panel).
  - All text uses `--text-primary` / `--text-secondary` / `--text-tertiary` / `--text-disabled` (auto-flipping with `data-theme`).
  - All borders use `--border-subtle` / `--border-default` / `--border-strong`.
  - All fills (hovers, selected rows) use `--fill-subtle` / `--fill-hover` / `--fill-active` / `--fill-selected`.
- The landing page (no workspace context) renders in `dark` mode with `ws-indifference` (black) as its background; it is "the place between workspaces" so it follows the default rather than any picked color.
- Buttons render in 4 variants × 6 states; the variant is a prop (`variant="primary|secondary|ghost|destructive"`), the state is driven by `:hover` / `:focus-visible` / `:active` / `[disabled]` / `[aria-busy="true"]` CSS — no per-state prop required.
- Text fields render in 7 states; `Error` is a prop, the others are CSS state selectors.
- Selects render the exact Figma `chevron-down` SVG and use `MenuItem` for their dropdown rows.
- Focus rings on every focusable component are `--focus-ring` (#0A84FFE5) at 2px outline + 2px offset.
- All eight Figma icons render exactly as their Figma SVG paths (verbatim `d="..."`); the component is `<Icon name="chevron-down" />` and rejects unknown names at template-render time.
- The styleguide gains: a workspace-color picker that re-renders the entire styleguide in the chosen color; a manual `data-theme` flip toggle; a per-stateful-component grid showing every state under both themes side-by-side.
- The `templates_test.py` token ratchet keeps cross-checking declared `:root` / `[data-theme=*]` tokens against `data-token` swatches in the styleguide. The current `--workspace-accent` runtime variable is replaced with `--ws-<slug>`; both ratchet sides update together.
- Pages that today inject `--workspace-accent: oklch(...)` via `body style="..."` instead inject the workspace color slug + theme on `<html>`. The CSS variable then resolves to a named palette value.

## Implementation plan

### New data type and persistence

- `apps/minds/imbue/minds/desktop_client/design_tokens.py` — new module.
  - `WorkspaceColor(StrEnum)`: members `INDIFFERENCE`, `CONFUSION`, `COURAGE`, `ENVY`, `PEACE`, `BELONGING`, `ENERGY`, `STRENGTH`, `COMFORT`, `INSPIRATION`, `CLARITY`. Values are kebab-case slugs (`"indifference"` etc.).
  - `Theme(StrEnum)`: `DARK`, `LIGHT`.
  - `WORKSPACE_PALETTE: Final[Mapping[WorkspaceColor, tuple[str, Theme]]]` — `(hex_value, theme)` per color. Single source of truth used by Python + the styleguide's "all swatches" rendering.
  - `DEFAULT_WORKSPACE_COLOR: Final[WorkspaceColor] = WorkspaceColor.INDIFFERENCE`.
  - `theme_for(color: WorkspaceColor) -> Theme` — pure lookup.
  - Static check helper: a one-line assertion that every member is present in the palette dict, to catch drift between the enum and the data table at import time.
- `apps/minds/imbue/minds/desktop_client/minds_config.py` — extend `MindsConfig`.
  - `get_workspace_color(agent_id: AgentId) -> WorkspaceColor` — reads `workspaces.<agent_id>.color`; returns `DEFAULT_WORKSPACE_COLOR` if missing or unparseable (with a warning log on unparseable, not an error: corrupt config should not block UI rendering).
  - `set_workspace_color(agent_id: AgentId, color: WorkspaceColor) -> None` — writes through the same `_lock` + atomic-rename pattern as the other setters.
  - `remove_workspace_color(agent_id: AgentId) -> None` — called when an agent is destroyed, to keep `config.toml` tidy. Called from the destroy flow's success path; missing entries are a no-op.

### New token CSS

- `apps/minds/imbue/minds/desktop_client/static/tokens.css` — rewrite to declare every design token. Structure:
  - `:root` block keeps the existing `--shadow-seam` (carried over verbatim) plus the spacing scale (`--space-4` through `--space-32`), the radius scale (`--radius-sm`, `--radius-md`, `--radius-lg`, `--radius-pill`), and the workspace-color palette (`--ws-indifference: #000000;` ... `--ws-clarity: #FCEFD4;`).
  - `[data-theme="dark"]` block declares all foreground-at-opacity ramps for dark, semantic colors, focus ring, surface overlay.
  - `[data-theme="light"]` block declares the same set of token names with light values.
  - Type-ramp utility classes: `.type-display-24`, `.type-heading-16`, `.type-label-14`, `.type-body-14`, `.type-menu-13`, `.type-helper-12`, `.type-section-11`, `.type-title-12`, `.type-badge-10`. Each sets `font-size`, `line-height`, and `font-weight` to the Figma values. (Kept as classes rather than custom properties because consumers vary by element type — labels are spans, headings are h1, etc.)
  - Stateful selectors that don't make sense as Tailwind utilities:
    - `.sidebar-item` (carry over existing, retarget to token vars).
    - `.workspace-row` (new) for the landing rows — Default/Hover/Selected/Unread/Disabled state machine driven by `:hover` and `[aria-selected]`/`[data-unread]`/`[aria-disabled]`.
    - `.titlebar-btn` (new) for chrome buttons — Default/Hover/Active/withBadge.
    - `.menu-item` (new) for dropdown rows.
    - `.skeleton` (new) shimmer keyframes.
  - The `--workspace-accent` variable is **removed**; its surviving consumer (`.accent-spine`, `.accent-swatch`) is folded into the page-wide `--ws-<slug>` model and the helper classes are deleted.
  - All other current contents (`.page-workspace::before` 3px stripe, the inline `.opt-*` rules, the `.spinner` keyframes) are migrated to token-backed colors. The `.opt-*` rules continue to depend on parent-selector state (`.opt-selected`) so they stay in the stylesheet rather than becoming Tailwind utilities.

### Theme + workspace-color application

- `apps/minds/imbue/minds/desktop_client/templates/Base.jinja` — accept new props.
  - Add `theme="dark"` and `ws_color="indifference"` props (with sensible defaults so non-workspace pages still render).
  - Render them on `<html>` as `data-theme="{{ theme }}" data-ws-color="{{ ws_color }}"`.
  - Drop the `body_class` default that hard-codes `bg-zinc-50 text-zinc-900`; replace with token-backed classes: `class="font-sans antialiased text-token-primary bg-token-ws-color"`. (`bg-token-ws-color` is a tiny utility defined in `tokens.css` that resolves to `background: var(--ws-<slug>)`.)
- `apps/minds/imbue/minds/desktop_client/templates.py`:
  - Replace the `workspace_accent(agent_id)` function and its `_WORKSPACE_L` / `_WORKSPACE_C` constants with `workspace_color_for(agent_id, *, config) -> WorkspaceColor` that reads from `MindsConfig`.
  - Replace every render-function call site that today passes `accent=workspace_accent(agent_id)` with two new kwargs: `theme=theme_for(color).value, ws_color=color.value`. Render functions affected: `render_creating_page`, `render_destroying_page`, `render_sharing_editor`, `render_workspace_settings`, `render_landing_page`. Each function takes an injected `MindsConfig` (the route handlers already have one in their FastAPI dependency graph; verify in `request_handler.py` and pass through).
  - The landing page is **not** workspace-scoped; it always renders with `theme="dark"`, `ws_color="indifference"`. The per-row accent stripes that today use `--workspace-accent` switch to `style="--ws-row: var(--ws-<slug>)"` and the accent stripe element reads `--ws-row` (so the stripe color is the row's workspace color, while the page background stays the default).
- `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js`:
  - Replaced by `workspace_color.js` (rename): exports `window.mindsWorkspaceColor.set(htmlEl, slug)` and `.get(agentId, callback)`. The deterministic OKLCH derivation is gone; lookup is by `agentId` against a `/api/workspace-color/<agent_id>` GET endpoint, with a `Promise`/callback API matching the current shape so call sites only need to read a slug instead of an OKLCH string.
- New FastAPI routes in `apps/minds/imbue/minds/desktop_client/api_v1.py` (or wherever the `/api` group lives in this branch — confirm at impl):
  - `GET /api/workspace-color/{agent_id}` → `{"color": "<slug>"}`. Reads via `MindsConfig.get_workspace_color`.
  - `POST /api/workspace-color/{agent_id}` body `{"color": "<slug>"}` → 204. Validates via `WorkspaceColor(slug)` (Pydantic, so unknown slugs 422). Writes via `MindsConfig.set_workspace_color`.

### Chrome / page templates — broad sweep

Pattern: every workspace-scoped page becomes `<Base theme="..." ws_color="...">` and drops all hardcoded color Tailwind utilities, swapping them for token classes.

Token-utility classes added to `tokens.css` (so templates can write `class="text-token-primary bg-token-fill-hover"` instead of inlining `style="color: var(--text-primary)"`):
- `.text-token-primary`, `-secondary`, `-tertiary`, `-disabled`, `-inverse`
- `.bg-token-ws-color` (the page background)
- `.bg-token-fill-subtle`, `-hover`, `-active`, `-selected`
- `.bg-token-surface-overlay`
- `.bg-token-accent` (uses `--accent-solid`), `.text-token-on-accent`
- `.border-token-subtle`, `-default`, `-strong`, `-overlay`
- `.semantic-important`, `.semantic-success`, `.semantic-warning`, `.semantic-info` (text + bg utility pairs as needed)
- `.focus-ring-token` (utility that sets the `outline` + `outline-offset` via `--focus-ring`)

Templates updated (mechanical sweep):
- `templates/pages/Chrome.jinja` — titlebar no longer `bg-zinc-900`; uses `bg-token-ws-color` on body and titlebar, `text-token-primary` on the title text, `text-token-tertiary` on the titlebar buttons, `bg-token-fill-hover` on hover. The page-workspace stripe (`.page-workspace::before`) is **removed** — the whole chrome IS the workspace color now, so a separate stripe is redundant.
- `templates/pages/Sidebar.jinja` — same swap; sidebar background follows the workspace color via inheritance, items use `.menu-item` selectors.
- `templates/pages/Landing.jinja` — rows use the new `.workspace-row` component class. Each row's accent stripe (`.accent-spine` today) is recolored from `--ws-row` set inline per row.
- `templates/pages/WorkspaceSettings.jinja` — content surfaces flip per theme. **Adds a new "Color" section**: a row of 11 swatches (`<button class="ws-swatch" data-color="indifference" style="background: var(--ws-indifference)">`) above "Sharing". The selected swatch is marked `aria-pressed="true"` and ringed with `--focus-ring`. Click → POST `/api/workspace-color/<agent_id>` → reload.
- `templates/pages/Sharing.jinja`, `templates/pages/Creating.jinja`, `templates/pages/Destroying.jinja` — same migration: theme + ws_color set, hardcoded colors removed.
- `templates/pages/Create.jinja`, `templates/pages/Welcome.jinja`, `templates/pages/Landing.jinja` (already noted) — these are pre-workspace; render with the default theme/color.
- `templates/PermissionsDialog.jinja` — modal backdrop uses `bg-token-surface-overlay`, dialog card uses light theme regardless of host workspace (the dialog reads better as a fixed light-on-dark surface; flagged in Open Questions).
- `templates/pages/DevStyleguide.jinja` — see "Styleguide expansion" below.

The remaining auth pages (`auth/SignupSignin`, `auth/CheckEmail`, etc.) render outside any workspace and use the default `dark` + `indifference` theme.

### New + updated stateful components

Each component is a single `.jinja` file at the top of `templates/`. State styling lives in `tokens.css` (CSS pseudo-classes), not in Jinja branches.

- `templates/Button.jinja` — already exists; rewritten.
  - Props: `variant="primary"|"secondary"|"ghost"|"destructive"`, `loading=false`, `disabled=false`, `id=""`, `onclick=""`, `extra=""`, `block=false`. Replaces today's `"primary|secondary|danger|success|ghost"` set (note: `success` is dropped — Figma has no Success button variant; existing usages of `variant="success"` migrate to `primary` or `primary` + a checkmark icon depending on context). `danger` renames to `destructive`.
  - When `loading=true` sets `aria-busy="true"`, swaps the slot content for a spinner sized to match the label slot. CSS handles hover/pressed/focused/disabled via `:hover`, `:active`, `:focus-visible`, `[disabled]`, `[aria-busy="true"]`.
- `templates/ButtonLink.jinja`, `templates/ButtonSubmit.jinja` — updated to the new variant set; share the same CSS classes (the three components rendered as `button`/`a`/`button[type=submit]` only differ in tag, not in styling).
- `templates/TextField.jinja` — **renamed from `TextInput.jinja`** to match the Figma component name. Same prop shape plus `error=false`, `readonly=false`. Hover/focus/filled/disabled/read-only via CSS state. Error is a prop because it can't be derived from CSS state. Test file picks up the rename.
- `templates/Select.jinja` — new. Props: `name`, `value=""`, `options` (sequence of `{value, label}` dicts), `id=""`, `disabled=false`, `extra=""`. Renders a `<select>` with the exact Figma `chevron-down` SVG as a CSS background, hiding the default UA chevron. Dropdown rows are native browser-rendered (we don't reskin the popup); the closed-state trigger matches Figma.
- `templates/Checkbox.jinja` — new. Props: `name`, `checked=false`, `indeterminate=false`, `disabled=false`, `value=""`, `id=""`. Renders a `<label>` wrapping an `<input type="checkbox">` plus a custom box drawn with `<svg>` (the exact Figma `check` path for the checked state). `indeterminate=true` is set via JS in the host page (HTML attribute is checked-only; JS sets `el.indeterminate = true` on `DOMContentLoaded`); the component emits a `data-indeterminate="true"` hint so the host's script can target it.
- `templates/Toggle.jinja` — new. Props: `name`, `checked=false`, `disabled=false`, `id=""`. Renders an iOS-style switch using the foreground-at-opacity tokens.
- `templates/MenuItem.jinja` — new. Props: `label`, `href=""`, `icon=""` (optional, name of an `Icon`), `selected=false`, `destructive=false`, `disabled=false`. Used by the space-switcher menu in Sidebar/Chrome and the dropdown content of any future custom popup.
- `templates/Skeleton.jinja` — new. Props: `width="100%"`, `height="1em"`, `extra=""`. Renders a `<div>` with shimmer via the `.skeleton` keyframes from `tokens.css`.
- `templates/Icon.jinja` — new. Props: `name`, `size=16`, `stroke_width=1.5`, `extra=""`. Body is a Jinja `if/elif` chain over the eight Figma icon names; each branch is an inline `<svg>` with the verbatim Figma path data fetched at implementation time via `get_design_context` on nodes 339:4063/4067/4071/4075/4080/4085/4091/4098. Unknown `name` raises a Jinja `TemplateError` at render time (not a silent miss).

Stateful CSS for components (added to `tokens.css`):

```
.btn { /* base */ }
.btn[data-variant="primary"]   { background: var(--accent-solid); color: var(--accent-on); }
.btn[data-variant="secondary"] { background: var(--fill-subtle); color: var(--text-primary); border: 1px solid var(--border-default); }
.btn[data-variant="ghost"]     { background: transparent; color: var(--text-primary); }
.btn[data-variant="destructive"] { background: var(--important); color: #fff; }
.btn:hover                     { background-color: color-mix(in srgb, currentColor 10%, transparent); /* per-variant override below */ }
.btn:active, .btn[data-pressed="true"] { /* fill/active blend */ }
.btn:focus-visible             { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.btn[disabled], .btn[aria-disabled="true"] { opacity: .35; cursor: not-allowed; }
.btn[aria-busy="true"]         { /* dim label, show spinner */ }
/* similar for .text-field, .select-trigger, .checkbox, .toggle, .menu-item, .skeleton, .titlebar-btn, .workspace-row */
```

### Styleguide expansion

`templates/pages/DevStyleguide.jinja`:

- Top of page: a workspace-color picker (`<select>` over the 11 colors), wired via JS in `static/dev_styleguide.js` to update `<html>`'s `data-ws-color` + `data-theme`. Replaces the existing OKLCH hue slider entirely.
- Manual theme override toggle (`Light` / `Dark`) so reviewers can force either theme onto any workspace color (the auto-flip can be overridden for review purposes).
- New section "Tokens (per theme)": every `:root` and `[data-theme=*]` token gets a swatch with its CSS variable name + resolved value, rendered twice (once per theme) side-by-side. `data-token="--<name>"` carries the cross-check.
- New section "Stateful components": for each of Button, TextField, Select, Checkbox, Toggle, MenuItem, WorkspaceRow, TitlebarBtn, Skeleton — a grid showing every state under both themes. State is rendered by setting `data-state="..."` on the demo wrapper which the CSS picks up via the same pseudo-class-equivalent attribute selectors.
- Icon catalog: all eight Figma icons rendered at 16/24/32 size.
- Existing "Patterns" section is trimmed: anything covered by a primitive component above is removed; what remains is page-level patterns (sidebar items, accent stripes, focus rings, shadow seam).
- Updated `static/dev_styleguide.js` drops the OKLCH hue logic and instead wires the workspace-color `<select>` + theme toggle to the `<html>` data attributes.

### Tests

- `apps/minds/imbue/minds/desktop_client/design_tokens_test.py` — new.
  - `test_palette_covers_every_workspace_color_enum_value` — `WORKSPACE_PALETTE.keys() == set(WorkspaceColor)`.
  - `test_theme_for_returns_light_for_pastels_and_dark_for_indifference` — spot-check a handful of mappings.
- `apps/minds/imbue/minds/desktop_client/minds_config_test.py` — extend.
  - `test_get_workspace_color_returns_default_when_unset`.
  - `test_set_then_get_workspace_color_round_trips`.
  - `test_get_workspace_color_returns_default_on_unknown_slug` (and logs a warning — assert via `caplog`).
  - `test_remove_workspace_color_is_idempotent`.
- `apps/minds/imbue/minds/desktop_client/templates_test.py` — extend.
  - `test_render_landing_page_uses_dark_indifference_default_theme` — assert `data-theme="dark"` and `data-ws-color="indifference"` in the rendered HTML.
  - `test_render_workspace_settings_renders_color_picker_with_eleven_swatches` — count swatches.
  - `test_render_workspace_settings_marks_current_color_aria_pressed`.
  - `test_render_creating_page_propagates_theme_for_picked_workspace_color` — patch `MindsConfig.get_workspace_color` to return `WorkspaceColor.PEACE`; assert `data-theme="light" data-ws-color="peace"` in the rendered HTML.
  - Update the existing token-ratchet test: instead of cross-checking `--shadow-seam`-style `:root` tokens only, walk both `[data-theme="dark"]` and `[data-theme="light"]` blocks; cross-check the set of declared names matches the set of `data-token="--..."` swatches in the styleguide. The `--workspace-accent` ratchet entry is removed; new entries cover every token added in this PR.
- `apps/minds/imbue/minds/desktop_client/test_desktop_client.py` — extend.
  - `test_set_workspace_color_endpoint_persists_and_round_trips_via_get`.
  - `test_set_workspace_color_rejects_unknown_slug` (422).
- Component unit tests in `templates_test.py`:
  - `test_button_renders_each_variant` (primary/secondary/ghost/destructive).
  - `test_button_loading_sets_aria_busy`.
  - `test_text_field_error_sets_data_error_attribute`.
  - `test_select_renders_chevron_down_icon`.
  - `test_checkbox_indeterminate_emits_data_attribute`.
  - `test_icon_unknown_name_raises` — assert on `TemplateError`.
  - One assertion per icon: `test_icon_<name>_renders_figma_path_verbatim` — pin the SVG `d=` string so future edits stay verbatim.
- Existing tests that today pass `accent=` kwargs or assert on `oklch(...)` substrings get migrated to the new `theme=` / `ws_color=` shape. The OKLCH-derivation tests are deleted (no derivation left).
- No new test_ratchets.py entries needed; the token cross-check ratchet is already in place.

### Files to delete

- `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js` (replaced by `workspace_color.js`).
- The `workspace_accent` function and its `_WORKSPACE_L` / `_WORKSPACE_C` constants in `templates.py`.
- The `.page-workspace::before` stripe rule, the `.accent-swatch` rule, the legacy `.accent-spine` (now folded into landing-row CSS) in `tokens.css`.
- `templates/TextInput.jinja` (renamed to `TextField.jinja`; all consumers updated).

### Wheel packaging

- New `.jinja` files inherit hatchling's default-include rule for `imbue/`. The same caveat from the jinjax-migration plan applies: do not add a force-include or files get duplicated.
- `tokens.css` and `workspace_color.js` are already under `static/` and packaged the same way as today's static assets.

## Implementation phases

Each phase ends with `just test-quick apps/minds` passing. We can stop at the end of any phase and ship a partially complete (but coherent) state.

### Phase 1 — tokens + palette type, no UI changes

- Add `design_tokens.py` (enum + palette + theme_for).
- Rewrite `tokens.css` with all token names, both themes, and component CSS selectors — but keep the existing utility classes alongside the new ones (so existing templates continue to render).
- Extend `MindsConfig` + tests for `get_workspace_color` / `set_workspace_color` / `remove_workspace_color`.
- Outcome: tokens defined, persistence works, no templates touched yet. Visual diff: 0.

### Phase 2 — Base + workspace-color application

- Update `Base.jinja` to accept `theme` + `ws_color` props and render them on `<html>`.
- Add `theme_for(workspace_color_for(agent_id, config=...))` to every workspace-scoped render function in `templates.py`.
- Add the new FastAPI routes; wire `MindsConfig` through the route handlers.
- Replace `workspace_accent.js` with `workspace_color.js`.
- Migrate `Chrome.jinja` + `Landing.jinja` first as the canary pages — flip them onto the token system end-to-end.
- Add the workspace-color picker UI to `WorkspaceSettings.jinja` (a temporary location — the picker will move once we have a per-workspace settings overlay, but lives here for now).
- Outcome: any workspace whose color has been set sees the chosen color across chrome + sidebar + landing row; setting flow works via the picker; everything else still renders today's Tailwind colors.

### Phase 3 — sweep all remaining workspace pages

- Migrate `Sidebar.jinja`, `WorkspaceSettings.jinja`, `Sharing.jinja`, `Creating.jinja`, `Destroying.jinja`, permission dialogs.
- Each migration: replace hardcoded `bg-zinc-*` / `text-zinc-*` / `border-*` / `border-white/*` with the new token classes; pass `theme=...` / `ws_color=...` through to `<Base>`.
- Outcome: the entire workspace experience honors the picked color and flips theme correctly.

### Phase 4 — stateful component rewrites

- Rewrite `Button.jinja` / `ButtonLink.jinja` / `ButtonSubmit.jinja` to the new variant + state model.
- Add `TextField.jinja` (renamed from `TextInput.jinja`); update all consumers.
- Add new components: `Select`, `Checkbox`, `Toggle`, `MenuItem`, `Skeleton`, `Icon`.
- Outcome: every primitive in the styleguide is the stateful version backed by tokens.

### Phase 5 — icon import

- Fetch each of the eight Figma icon SVGs via `mcp__claude_ai_Figma__get_design_context` on nodes 339:4063/4067/4071/4075/4080/4085/4091/4098.
- For each, copy the `<path d="..."/>` verbatim into the `Icon.jinja` `if/elif` chain.
- Migrate every chrome-side icon that maps onto one of the eight names (back/forward chevrons, etc.) to `<Icon name="..."/>`. Icons that aren't in the Figma library (titlebar window controls, settings cog, restart) remain inline SVG for now and are flagged as a follow-up.
- Outcome: every Figma icon used in minds is byte-identical to the Figma source.

### Phase 6 — styleguide expansion

- Rewrite `DevStyleguide.jinja` per the spec above (theme toggle, workspace-color picker, per-component state grids, token swatches per theme, icon catalog).
- Rewrite `static/dev_styleguide.js`.
- Outcome: reviewers can flip the styleguide through all 11 workspace colors and both themes; every state of every primitive is visible.

### Phase 7 — cleanup + ratchet update

- Delete `workspace_accent.js`, the old `workspace_accent` function, and any legacy CSS now superseded.
- Delete the `--workspace-accent` references in `templates_test.py` (the ratchet) and replace them with the new token cross-check.
- Run `just test-offload` for the full suite.
- `/autofix` for code-quality issues.
- `/verify-conversation` for behavioral review.

### Phase 8 — manual UI verification

- `just minds-start`, create a workspace, switch its color through several palette values, confirm:
  - Chrome / sidebar / content background all change.
  - Theme flips text + borders + fills correctly.
  - Focus rings render at `#0A84FFE5`.
  - Buttons / TextFields / Selects / Checkboxes / Toggles all render their state matrix correctly on hover/focus/active/disabled.
  - Landing page stays in the default dark-indifference look.
  - The styleguide page can scrub through all 11 colors without visible breakage.

## Testing strategy

### Unit tests

- `design_tokens_test.py` — palette completeness, theme mapping.
- `minds_config_test.py` extensions — workspace-color persistence round-trips, default-on-missing, default-on-corrupt, idempotent remove.
- `templates_test.py` extensions — theme / ws_color propagation per render function, workspace-color picker shape, button/textfield/select/icon component-level tests.

### Integration tests

- `test_desktop_client.py` — the FastAPI TestClient exercises `GET/POST /api/workspace-color/<agent_id>` end-to-end and round-trips persistence through the real `MindsConfig`.

### Ratchet

- The existing `data-token` ↔ `:root` cross-check ratchet in `templates_test.py` is migrated to walk both `[data-theme="dark"]` and `[data-theme="light"]` blocks. Every new token name gets a corresponding swatch in the styleguide; the ratchet enforces the bidirectional set equality.

### Manual UI verification

- Walk through every palette color × every workspace-scoped page (Chrome, Sidebar, Landing-row-context, WorkspaceSettings, Sharing, Creating, Destroying, permission dialog).
- Confirm theme flips at the `peace` boundary (last dark color) → `peace` (first light color).
- Confirm landing default stays dark/indifference regardless of which workspaces exist.
- Confirm the styleguide's per-state component grid matches Figma at a glance.

### Edge cases to verify

- Persisting an unknown slug via the API → 422, config untouched.
- Persisting on a non-existent `agent_id` is allowed (no foreign-key check). The destroy flow's `remove_workspace_color` cleans up; orphans don't harm anything.
- Two concurrent writes (config-level `_lock` already covers this; add a test that hammers it).
- The transition window where today's clients still hold the old `workspace_accent.js` cached: the new `workspace_color.js` is a different filename, so the old script 404s on first request and the page falls back to default. Acceptable since the desktop client always reloads from server on app launch.
- Backwards-compatible reads: if `~/.minds/config.toml` exists from before this PR, it has no `workspaces.*` section and every agent defaults to `indifference` — no migration step required.

## Open questions

- **Picker location.** Phase 2 places the workspace-color picker on the `WorkspaceSettings` page. Alternatively it could live inside the titlebar (a small disclosure menu) so users can flip the color without leaving the workspace. The settings page is the safer first iteration, but the titlebar is the more discoverable home — pick before Phase 2.
- **Should the picker offer "auto" (today's hash-derived hue)?** Removing OKLCH-derived accents entirely means every existing workspace renders in `indifference` until the user picks. Alternative: on first read for an agent without a stored color, deterministically derive a *palette* color from the agent id (a stable 11-bucket hash). Costs a tiny migration risk if the hash bucket changes later, but means existing workspaces don't all look identical post-merge.
- **Permission-dialog theming.** The dialog is a modal overlay; its inner card today is white-on-light. Should it follow the host workspace's theme (read better when the host is dark, jarring when host is light) or stay light always (today's behavior, predictable for sensitive flows)? Plan defaults to "stay light always"; flag for confirmation.
- **`success` button variant removal.** The Figma component set has Primary / Secondary / Ghost / Destructive — no Success. The current minds codebase has a green Success button (used in workspace-association flows). Migrate every `variant="success"` to `variant="primary"`? Or keep a `success` extension variant? Plan removes `success`; flag if the visual loss matters.
- **TextField rename.** Renaming `TextInput.jinja` → `TextField.jinja` matches Figma but churns every consumer template and test. Worth the churn for naming consistency, or keep `TextInput` and just live with the mismatch? Plan renames; flag for confirmation.
- **`light`/`dark` mode override on the styleguide.** Phase 6 adds a manual theme toggle so reviewers can flip themes independently of workspace-color. Should the same toggle exist in production (e.g. "always use dark" / "always use light" preference)? Plan keeps it styleguide-only; flag as a follow-up.
- **Inbox modal screen (Figma 412:4345).** The fourth Figma sample screen shows an inbox / requests modal. This page does not yet exist in minds. Out of scope for this PR (no implementation), but worth opening a separate spec for; flag as follow-up.
- **`--radius-lg`.** Figma exposes `radius-md = 10` and `radius-pill = 999` directly via `get_variable_defs`; `radius-sm` (6) is referenced but `radius-lg` is not explicitly in the variable response. Confirm the exact value from Figma node 317:4083 ("Radius") at impl time; default plan assumes ~14px to bracket the `md`/`pill` gap.
- **Light-on-`#CECD0C` (energy) contrast.** Yellow is borderline for black text at standard sizes; double-check WCAG contrast for `Body/14 Regular` (`#000000A6`) on `#CECD0C`. If insufficient, either drop `energy` from the catalog or upgrade `text/secondary` to a darker step for the light theme. Plan keeps the palette as-is; flag for verification during Phase 8 manual review.
