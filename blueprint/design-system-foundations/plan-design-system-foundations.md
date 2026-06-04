# Plan: formalize the minds design system (workspace colors, foreground-at-opacity tokens, stateful components)

## Refined prompt

I want to formalize a bit of the design system for the minds app. In this PR I want to both — formalize things and also modify things (and hopefully these will neatly apply through most of the app since we have a bit of jinjax components in place). We may or may not need to make more compoentns or change their shape. Let me describe our system a bit:

1. Workspace colors (and this quite literally means the background color for the whole app). By default we'll want to run the app against the black palette color, but the user can opt per workspace to switch the color (we kind of do that now, but only show a little swatch in the title). I want to push this to whole chrome. The colors that are allowed are these: <https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=314-4141&t=D0X7nv6rcuGNEKhz-11> please use their names and values. Do not make up any new ones.
   The way this will work is that based on which color you choose we flip the text and border and siurface colors to be some transparent black or transparent white to blend nicely.
   * The eleven Figma palette colors (Figma node 314:4141) are **presets**, not a closed catalog: `workspace/indifference` `#000000`, `workspace/confusion` `#0B292B`, `workspace/courage` `#492222`, `workspace/envy` `#3C3D06`, `workspace/peace` `#9FBBD3`, `workspace/belonging` `#E8A7A8`, `workspace/energy` `#CECD0C`, `workspace/strength` `#CFC7B3`, `workspace/comfort` `#F5D6A0`, `workspace/inspiration` `#E9ECD9`, `workspace/clarity` `#FCEFD4`. The picker surfaces these as suggestions; eventually a freeform color picker will accept any CSS color. For this PR the UI exposes only the 11 presets, but the storage layer + theme-inference pipeline already handle arbitrary color values (so the freeform follow-up is purely UI work).
   * **`workspace/confusion` (#0B292B) is the default color everywhere**: new agents are born with it; pre-workspace pages (Landing, Welcome, auth flow, any "no workspace selected" context) render with it; the minds chrome outside of a specific workspace is `confusion`. Not pure `indifference` (#000000) — the slight dark-teal warmth reads as "Minds the application" instead of "system black", which helps the trust-boundary cue described below.
   * The picker lives both on the workspace settings page (a row of 11 swatches) and as a small disclosure menu in the titlebar (quick-flip without leaving the workspace). It persists per-`agent_id` in the minds-side config (no agent-side or `.mngr/` involvement).
   * *Existing* workspaces (created before this PR lands) inherit their current OKLCH-derived per-agent hue rather than snapping to the default: on first read for an agent without a stored color, the migration step computes `oklch(75% 0.15 <hash-derived-hue>)` and stores that as the agent's workspace color. The lightness bumps from today's 65% to 75% so the result reads as a usable background. The presets are still surfaced in the picker for that workspace; if the user picks one, the OKLCH starting color is replaced.
   * Theme (dark/light) is inferred from the chosen color's relative luminance, **not** from a static enum-to-theme lookup: luminance > 0.5 → light theme (black foreground at opacity); otherwise → dark theme (white foreground at opacity). This works for both presets and arbitrary OKLCH values. For the 11 presets it lands as: `indifference` / `confusion` / `courage` / `envy` → dark; `peace` / `belonging` / `energy` / `strength` / `comfort` / `inspiration` / `clarity` → light.
   * **Trust-boundary philosophy.** The workspace color theme styles the *outer* minds chrome (titlebar, sidebar, every minds-rendered page including auth). The *inner* content area — the iframe under `mngr_forward_origin/goto/<agent>/…` — is FCT's realm and stays visually distinct on purpose. The visual gap between outer-and-inner is a trust signal so users can tell "Minds is asking" from "the agent's UI is running". Live-flip animations animate the outer chrome only; nothing crosses the iframe boundary.

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

- The current OKLCH-hash-derived per-agent accent is reinterpreted, not deleted: the same hue hash drives the *starting* workspace color (now at L=75% so it reads as a full background, not an accent). The user can override the starting color with one of 11 named presets, or eventually any CSS color. The default for new agents and for any pre-workspace page (Landing, Welcome, auth, the styleguide's "no color picked" fallback) is `workspace/confusion` (#0B292B) — a slight dark-teal warmth that reads as "the Minds application" rather than "system black".
- Workspace color is **freeform-capable from day one** at the data layer (a `str` validated as a CSS color), even though the UI only surfaces the 11 presets in this PR. Theme (dark/light) is inferred from the color's relative luminance.
- The picked color is the actual background for the whole outer chrome (titlebar, sidebar, every minds-rendered page), not just a thin stripe or swatch as today. The **outer chrome is a trust signal**: a visually distinct outer-vs-inner boundary tells users where Minds ends and the agent's sandbox begins. The iframe under `mngr_forward_origin/goto/<agent>/…` is FCT's realm and is intentionally not themed by minds.
- Surfaces / borders / text are foreground-at-opacity tokens that automatically flip between white-on-dark and black-on-light depending on the picked workspace color.
- All design tokens (workspace colors, foreground ramps, semantic colors, focus ring, spacing, radii, type ramp) live in one `static/tokens.css`, scoped by `[data-theme="dark"|"light"]` so dark+light can drift independently later without touching call sites.
- All JinjaX primitive components (`Button`, `TextInput`, plus new `Select`, `Checkbox`, `Toggle`, `MenuItem`, `Skeleton`, `Icon`) consume those tokens via CSS variables, not raw Tailwind colors. The state matrices match the Figma component sets exactly. (`TextInput` keeps its code name; the Figma component is renamed to match.)
- The `success` button variant stays in the codebase but is re-grounded on the new `--success` semantic token (Figma `semantic/success` #5C8A3C). Existing call sites are unchanged.
- Permission / request modal dialogs follow the host workspace's theme rather than staying light always. The dialog surface reads from the same token vars as the host page.
- Existing chrome / page templates stop using `bg-zinc-900` / `text-zinc-200` / `border-white/10` / etc. and switch to token-backed utility classes. The migration is mechanical but broad — most templates change.
- **Comprehensive JS-side color sweep.** The 49 hardcoded Tailwind color refs in `static/*.js` and the ~25 inside `Landing.jinja`'s inline `<script>` are migrated. Two patterns: (a) **simple badges** (1–2 spans) swap their `className` strings to hand-authored semantic-token classes (`badge-success`, `badge-warning`, `badge-info`, `badge-important`, `badge-neutral` — defined in `tokens.css`, each auto-flips theme); (b) **complex composites** (sidebar rows, sharing email rows, landing provider rows) move to server-rendered JinjaX fragments served from small `GET /_render/...` endpoints; the JS fetches and `innerHTML`s the response, removing all class-string concatenation from JS for those composites.
- **Comprehensive type-ramp adoption.** Every visible text element in the templates is migrated to the 9-step Figma ramp via `.type-display-24` / `.type-heading-16` / `.type-label-14` / `.type-body-14` / `.type-menu-13` / `.type-helper-12` / `.type-section-11` / `.type-title-12` / `.type-badge-10` classes. Lossy snaps (places where the existing size doesn't match any ramp step exactly, e.g. `text-[11px]`, `text-[0.95em]`, `text-2xl`) are catalogued in a `blueprint/design-system-foundations/typography-snap-review.md` checklist during implementation so reviewers can sanity-check each one. The styleguide lists every ramp step as a discrete swatch so the full ramp is browseable.
- A new `Icon` component holds the eight Figma icon paths verbatim; existing chrome icons that happen to map onto one of these names (chevron-right back/forward, etc.) get re-pointed; chrome icons not in the Figma library (titlebar window controls, restart, settings cog, etc.) are out of scope and remain as-is.
- **No reloads anywhere on workspace-color change.** Both the titlebar quick-flip and the settings-page picker call the same JS helper that POSTs to `/api/workspace-color/<agent_id>` and applies the new color + theme by setting `<html>`'s `data-theme` + `--workspace-bg` in place. A 150ms `background-color` transition on `--workspace-bg` gives the flip a smooth feel; text and border colors snap (no transition) so the cross-theme moment reads as a deliberate mode change rather than a slow fade.
- **Per-workspace sidebar identity.** Each sidebar row gets a small swatch dot to the left of the workspace name (in that workspace's own color), so a sidebar listing 5 workspaces shows 5 different identity dots even though the sidebar surface itself sits in the active workspace's color. The Figma space-switcher menu visual.
- The dev styleguide page gains a state-matrix grid per stateful component, the full 9-step typography ramp, every semantic-color swatch, and a workspace-color picker at the top (all 11 presets) so reviewers can flip the whole page through every color. No standalone light/dark toggle — theme follows whichever color is picked. The production app does not expose any theme override affordance.

## Expected behavior

- A workspace color is a CSS-color string (hex, oklch, rgb, …) validated at the persistence boundary. The 11 presets are recognized by their preset slug (`indifference`, `confusion`, etc.); any other value is stored as the literal CSS color string.
- Each agent has a chosen workspace color persisted in `~/.minds/config.toml` under `workspaces.<agent_id>.color`. The value is either a preset slug or a CSS color literal.
- Default for a *new* agent without an entry: the preset slug `confusion` (#0B292B). Default for an *existing* agent (one that had a workspace before this PR landed): the deterministic OKLCH starting color `oklch(75% 0.15 <agent-id-hash-hue>)`, materialized into the config on first read (one-time migration step). Default for any *non-workspace* page (Landing, Welcome, the auth flow, the styleguide before a color is picked): `confusion`.
- Theme is inferred from the picked color's relative luminance at read time (`>= 0.5` → light, else dark). No theme is stored alongside the color. `confusion` (#0B292B) inferences as `dark`, so the default-everywhere look is dark teal with white-on-dark foreground tokens.
- The workspace-settings page gets a "Color" section with a row of 11 preset swatches. Clicking persists via the same JS helper as the titlebar quick-flip — **no page reload**; the surface re-themes in place with the 150ms `--workspace-bg` transition. The selected swatch ring uses `focus/ring`. If the agent's currently stored color is *not* one of the presets (e.g. a freeform OKLCH from the migration), a small "current" chip is rendered next to the preset row showing the raw value so it's not invisible to the user.
- The titlebar gets a small swatch button (left of the workspace title) that opens a flyout with the same 11-preset row plus the current value. Clicking a swatch fires the same POST and updates the chrome live (no full reload — the helper swaps `<html>`'s `data-theme` + `--workspace-bg` from the response).
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
- The styleguide gains: a workspace-color picker at the top of the page (11 preset swatches) that re-renders the styleguide in the chosen color; theme follows the color via the same luminance rule the production app uses, so there is no separate light/dark toggle. A per-stateful-component grid shows every state under whatever the picked color happens to be (reviewers flip through colors to see both themes).
- The `templates_test.py` token ratchet keeps cross-checking declared `:root` / `[data-theme=*]` tokens against `data-token` swatches in the styleguide. The current `--workspace-accent` runtime variable is replaced with `--workspace-bg` (set per-page from the picked color, regardless of whether the color is a preset or freeform); both ratchet sides update together.
- Pages that today inject `--workspace-accent: oklch(...)` via `body style="..."` instead inject `--workspace-bg: <picked-color>` plus `data-theme="dark|light"` on `<html>` (theme computed server-side from the color's luminance). The 11 preset CSS variables (`--ws-indifference` etc.) still exist in `tokens.css` for the picker UI to reference, but the active page background reads from `--workspace-bg`, not from a preset var.

## Implementation plan

### New data type and persistence

- `apps/minds/imbue/minds/desktop_client/design_tokens.py` — new module.
  - `WorkspacePreset(StrEnum)`: members `INDIFFERENCE`, `CONFUSION`, `COURAGE`, `ENVY`, `PEACE`, `BELONGING`, `ENERGY`, `STRENGTH`, `COMFORT`, `INSPIRATION`, `CLARITY`. Values are kebab-case slugs.
  - `Theme(StrEnum)`: `DARK`, `LIGHT`.
  - `WORKSPACE_PRESETS: Final[Mapping[WorkspacePreset, str]]` — `slug → hex value`. Used by Python + the styleguide picker rendering. (Theme is derived, not stored.)
  - `DEFAULT_WORKSPACE_PRESET: Final[WorkspacePreset] = WorkspacePreset.CONFUSION`. Used as the default for new agents, pre-workspace pages (Landing/Welcome/auth), and any other "no workspace selected" context.
  - `WorkspaceColor` is a Pydantic model wrapping a single `str` field, used at the persistence + API boundaries:
    - Validators accept either a `WorkspacePreset` slug, or a CSS color literal in the form `#RRGGBB` / `#RRGGBBAA` / `oklch(L% C H)` / `rgb(r g b)`. Anything else raises a validation error.
    - Provides `.resolve_hex() -> str` (resolves preset slugs to their hex; passes literals through as-is) and `.is_preset(slug: WorkspacePreset) -> bool`.
  - `theme_for(color: WorkspaceColor) -> Theme` — relative-luminance computation per WCAG (sRGB → linear → `0.2126 R + 0.7152 G + 0.4722 B`); luminance `>= 0.5` → `LIGHT`, else `DARK`. Handles hex / oklch / rgb literals via `colour` library (already a transitive dep — verify; fall back to a tiny inline sRGB-only impl if not).
  - `oklch_starting_color(agent_id: str) -> str` — pure function returning `f"oklch(75% 0.15 {sha256(agent_id)[:4] % 360})"`. Used by the one-time migration to seed existing workspaces. Lightness fixed at 75% (up from today's 65%).
  - Static check helper at module bottom: assert every `WorkspacePreset` member is present in `WORKSPACE_PRESETS`.
- `apps/minds/imbue/minds/desktop_client/minds_config.py` — extend `MindsConfig`.
  - `get_workspace_color(agent_id: AgentId) -> WorkspaceColor` — reads `workspaces.<agent_id>.color`; returns the migration result for agents that pre-exist but have no entry (see migration below); returns `DEFAULT_WORKSPACE_PRESET` for genuinely-new agents. Logs a warning and falls back to default on unparseable values.
  - `set_workspace_color(agent_id: AgentId, color: WorkspaceColor) -> None` — writes through the same `_lock` + atomic-rename pattern as the other setters. The serialized value is the preset slug if the color is a preset, else the raw CSS literal.
  - `remove_workspace_color(agent_id: AgentId) -> None` — called when an agent is destroyed, to keep `config.toml` tidy. Called from the destroy flow's success path; missing entries are a no-op.
  - **Migration on first read**: `get_workspace_color` checks if the agent existed before the migration boundary (`existed_before_migration: bool` argument, supplied by the caller — typically the route handler that already has the agent in hand from discovery). If true and the config has no entry, the function materializes `oklch_starting_color(agent_id)` into the config (under the same `_lock`) and returns it. This keeps the migration deterministic and side-effect-isolated to first read; no separate migration pass needed. The marker for "existed before" is simply "agent appears in the existing minds discovery cache when the new code is first run" — the route handler treats every agent it sees on the first call as eligible. Alternative simpler approach: always materialize the OKLCH starting color on first read regardless of "when the agent was created", and treat the `indifference` default only for "create flow hasn't completed yet". This is the chosen approach in the plan — it's simpler and harmless because new agents born after this PR also see the OKLCH color first, which is fine (it's still a deterministic, on-palette-feeling color). The `indifference`-as-new-default rule only kicks in at agent-creation time, when the agent creator writes `WorkspacePreset.INDIFFERENCE` explicitly.

### New token CSS

- `apps/minds/imbue/minds/desktop_client/static/tokens.css` — rewrite to declare every design token. Structure:
  - `:root` block keeps the existing `--shadow-seam` (carried over verbatim) plus the spacing scale (`--space-4` through `--space-32`), the radius scale (`--radius-sm`, `--radius-md`, `--radius-lg`, `--radius-pill`), and the **preset** workspace colors (`--ws-indifference: #000000;` ... `--ws-clarity: #FCEFD4;`). The preset vars exist so the picker UI can reference them without each swatch knowing its own hex; the page background does **not** use them directly — see `--workspace-bg` below.
  - `--workspace-bg` is set per-page via inline style on `<html>`, not at `:root`. Its value can be a preset (`var(--ws-confusion)`) or a freeform CSS color (`oklch(75% 0.15 230)`); pages don't care which.
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
  - Add `theme="dark"` and `workspace_bg="#0B292B"` props (the `confusion` default) so non-workspace pages still render. `workspace_bg` is the resolved CSS color (hex or oklch literal), not a slug — the server resolves any preset to its hex before render so the template doesn't have to know about presets.
  - Render them as `<html data-theme="{{ theme }}" style="--workspace-bg: {{ workspace_bg }};">`.
  - Drop the `body_class` default that hard-codes `bg-zinc-50 text-zinc-900`; replace with token-backed classes: `class="font-sans antialiased text-token-primary bg-token-workspace"`. (`bg-token-workspace` is a tiny utility defined in `tokens.css` that resolves to `background: var(--workspace-bg)`.)
- `apps/minds/imbue/minds/desktop_client/templates.py`:
  - Replace the `workspace_accent(agent_id)` function and its `_WORKSPACE_L` / `_WORKSPACE_C` constants with `workspace_color_for(agent_id, *, config) -> WorkspaceColor` that reads from `MindsConfig`.
  - Add a small `_render_kwargs_for_workspace(agent_id, *, config) -> dict[str, str]` helper returning `{"theme": ..., "workspace_bg": ...}` so call sites don't repeat the two-step resolve + theme-infer dance.
  - Replace every render-function call site that today passes `accent=workspace_accent(agent_id)` with the helper above. Render functions affected: `render_creating_page`, `render_destroying_page`, `render_sharing_editor`, `render_workspace_settings`, `render_landing_page`. Each function takes an injected `MindsConfig` (the route handlers already have one in their FastAPI dependency graph; verify in `request_handler.py` and pass through).
  - The landing page is **not** workspace-scoped; it always renders with `theme="dark"`, `workspace_bg="#0B292B"` (preset `confusion`, the everywhere-default). The per-row identity surfaces that today use `--workspace-accent` switch to two markers per row: (a) `style="--ws-row: <row's resolved color>;"` carrying the row's workspace color forward; (b) a small swatch dot inside the row showing that workspace's color directly (matching the sidebar treatment for consistency). Row colors come from the same migration + persistence pipeline.
- `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js`:
  - Replaced by `workspace_color.js` (rename): exports `window.mindsWorkspaceColor.set(htmlEl, color, theme)` and `.get(agentId, callback)`. The deterministic OKLCH derivation moves server-side (in `design_tokens.py`); the JS just fetches `/api/workspace-color/<agent_id>` and updates `<html>`'s `data-theme` + `--workspace-bg` in place. Used by the titlebar quick-flip menu to apply changes without reloading the page.
- New FastAPI routes in `apps/minds/imbue/minds/desktop_client/api_v1.py` (or wherever the `/api` group lives in this branch — confirm at impl):
  - `GET /api/workspace-color/{agent_id}` → `{"color": "<value>", "theme": "dark|light", "resolved_hex": "#..."}`. Reads via `MindsConfig.get_workspace_color`, includes both the stored value (slug or literal) and the resolved hex + inferred theme so the JS doesn't repeat the luminance math.
  - `POST /api/workspace-color/{agent_id}` body `{"color": "<slug-or-literal>"}` → 204. Validates via `WorkspaceColor` Pydantic model (unknown slugs / unparseable literals 422). Writes via `MindsConfig.set_workspace_color`.

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
- `templates/pages/Chrome.jinja` — titlebar no longer `bg-zinc-900`; uses `bg-token-workspace` on body and titlebar, `text-token-primary` on the title text, `text-token-tertiary` on the titlebar buttons, `bg-token-fill-hover` on hover. The page-workspace stripe (`.page-workspace::before`) is **removed** — the whole chrome IS the workspace color now, so a separate stripe is redundant. **Adds a titlebar workspace-color swatch button** (left of the workspace title) that opens a small flyout containing the 11 preset swatches; clicking a swatch calls the API and applies `data-theme` + `--workspace-bg` live via `workspace_color.js`.
- `templates/pages/Sidebar.jinja` — same swap; sidebar background follows the workspace color via inheritance, items use `.menu-item` selectors.
- `templates/pages/Landing.jinja` — rows use the new `.workspace-row` component class. Each row's accent stripe (`.accent-spine` today) is recolored from `--ws-row` set inline per row.
- `templates/pages/WorkspaceSettings.jinja` — content surfaces flip per theme. **Adds a new "Color" section**: a row of 11 preset swatches (`<button class="ws-swatch" data-color="confusion" style="background: var(--ws-confusion)">`) above "Sharing". The selected swatch is marked `aria-pressed="true"` and ringed with `--focus-ring`. If the agent's stored color is not a preset (e.g. an OKLCH literal from the migration), a small "Current" chip rendered to the right of the preset row shows the raw value. Click handler invokes the shared `mindsWorkspaceColor.apply(agentId, newColor)` JS helper — same one the titlebar quick-flip uses — which POSTs to `/api/workspace-color/<agent_id>` then re-themes the page in place. **No reload.**
- `templates/pages/Sharing.jinja`, `templates/pages/Creating.jinja`, `templates/pages/Destroying.jinja` — same migration: theme + workspace_bg set, hardcoded colors removed.
- `templates/pages/Create.jinja`, `templates/pages/Welcome.jinja`, `templates/pages/Landing.jinja` (already noted) — these are pre-workspace; render with the default theme/color (`dark` / `#000000`).
- `templates/PermissionsDialog.jinja` — modal backdrop uses `bg-token-surface-overlay`, dialog card **follows the host workspace's theme** rather than staying light always. The dialog reads its theme from the host page's `<html data-theme>`, so a dark-host workspace renders a dark dialog card and a light-host workspace renders a light card. Token classes inside the dialog already auto-flip with the theme.
- `templates/pages/DevStyleguide.jinja` — see "Styleguide expansion" below.

### JS-side color + type sweep

Two passes hit every `.js` file under `apps/minds/imbue/minds/desktop_client/static/` plus the inline `<script>` in `templates/pages/Landing.jinja` (≈25 places) and `templates/pages/Create.jinja` (1 place):

**Pass A — simple badges and one-off classes.** Hand-authored CSS classes in `tokens.css` give each badge tone a single token-backed class:

- `.badge-success`, `.badge-warning`, `.badge-important`, `.badge-info`, `.badge-neutral` — each pairs an opacity-blended bg with a token-backed text color, auto-flipping per theme.
- `.link` (replaces `text-blue-600 hover:underline`).
- `.muted` (replaces `text-zinc-400`).
- `.code-pill` (replaces `bg-zinc-100 rounded px-1.5 py-0.5 font-mono text-[0.95em]` in `sharing.js`).
- A tiny set of badge variants for diff-style "added/removed" tones (`.row-added`, `.row-removed`, `.text-added`, `.text-removed`) used by `sharing.js`.

JS that today does `el.className = 'bg-emerald-100 text-emerald-800'` becomes `el.className = 'badge-success'`. The class shape is small and lives in `tokens.css` so the cross-check ratchet still sees it.

JS files affected: `auth.js`, `chrome.js` (the embedded sidebar), `creating.js`, `destroying.js`, `sharing.js`, `sidebar.js`, `workspace_settings.js`, and the inline scripts in `Landing.jinja` and `Create.jinja`.

**Pass B — complex composites move server-side.** The bigger row builders are too tangled to hand-port idiomatically; they become JinjaX components rendered through new fragment endpoints. The JS fetches and inserts the HTML; layout + state classes live on the server.

- `templates/SidebarRow.jinja` — new. Props: `agent_id`, `name`, `is_current`, `is_unread`, `is_stale`, `workspace_color`, `goto_url`. Inline swatch dot + name + stale-dot indicator + click target. Used by `sidebar.js` (Electron WebContentsView mode) AND `chrome.js` (browser-mode embedded sidebar) — single source of truth.
- `templates/SharingEmailRow.jinja` — new. Props: `email`, `variant` ("added" / "removed" / "default"), `removable`. Replaces the ~70 lines of `sharing.js` row building.
- `templates/LandingProviderRow.jinja` — new. Props: `name`, `backend`, `status`, `error_type`, `error_message`, `is_pending`. Replaces the `renderProviders` row construction in `Landing.jinja`'s inline `<script>`.
- New FastAPI route group: `GET /_render/sidebar-row`, `GET /_render/sharing-email-row`, `GET /_render/landing-provider-row`. Each takes the props as query parameters, validates via Pydantic, and renders the corresponding JinjaX component. Endpoints return `text/html; charset=utf-8` HTML fragments only (no full page).
- The JS callers swap `var row = document.createElement('div'); row.className = '...'; ...` for `fetch('/_render/sidebar-row?...').then(r => r.text()).then(html => container.insertAdjacentHTML('beforeend', html))`. State updates (current/hover/selected) flip CSS-driven attributes on the existing DOM instead of recomputing class strings; only structural rebuilds re-fetch.

Latency: the fragment endpoints are local FastAPI; round-trip is <5ms. The JS may inflight-batch fragment requests if a single SSE event would otherwise fire N fetches.

### Workspace-color application JS

- `apps/minds/imbue/minds/desktop_client/static/workspace_color.js` (renamed from `workspace_accent.js`):
  - Exposes `window.mindsWorkspaceColor.apply(agentId, newColorSlugOrLiteral)` → POSTs to `/api/workspace-color/<agent_id>` with `{color}`, awaits `{color, theme, resolved_hex}` in response, then updates `<html>`'s `data-theme` + `style.setProperty('--workspace-bg', resolved_hex)` in place. Returns a Promise.
  - Exposes `.get(agentId, callback)` for fetch-only use (used by the landing page when rendering per-row swatch dots client-side, if the SSE payload omits the color).
  - The 150ms `--workspace-bg` transition lives in `tokens.css`, so the helper just sets the property and lets CSS animate.
  - **Both the titlebar quick-flip and the settings-page picker call `apply()`**. Neither does a full reload. The settings page is special only insofar as it might want to flash a "Saved" affordance — handled inside the settings-page-only `static/workspace_settings.js` toggling a tiny confirmation element, not in the shared helper.

The remaining auth pages (`auth/SignupSignin`, `auth/CheckEmail`, etc.) render outside any workspace and use the default `dark` + `indifference` theme.

### New + updated stateful components

Each component is a single `.jinja` file at the top of `templates/`. State styling lives in `tokens.css` (CSS pseudo-classes), not in Jinja branches.

- `templates/Button.jinja` — already exists; rewritten.
  - Props: `variant="primary"|"secondary"|"ghost"|"destructive"|"success"`, `loading=false`, `disabled=false`, `id=""`, `onclick=""`, `extra=""`, `block=false`. Today's `"danger"` renames to `"destructive"` to match Figma; `"success"` stays in the codebase (Figma doesn't have a Success Button variant set, but the `semantic/success` swatch is in the system, so we map `variant="success"` to that token's color). `"danger"` callers migrate to `"destructive"`.
  - When `loading=true` sets `aria-busy="true"`, swaps the slot content for a spinner sized to match the label slot. CSS handles hover/pressed/focused/disabled via `:hover`, `:active`, `:focus-visible`, `[disabled]`, `[aria-busy="true"]`.
- `templates/ButtonLink.jinja`, `templates/ButtonSubmit.jinja` — updated to the new variant set; share the same CSS classes (the three components rendered as `button`/`a`/`button[type=submit]` only differ in tag, not in styling).
- `templates/TextInput.jinja` — kept under its current code name. Same prop shape plus `error=false`, `readonly=false`. Hover/focus/filled/disabled/read-only via CSS state. Error is a prop because it can't be derived from CSS state. **The Figma component is renamed to `TextInput`** via the Figma MCP to match code; this is a side action during Phase 4 (not a separate phase).
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
.btn[data-variant="success"]   { background: var(--success); color: #fff; }
.btn:hover                     { background-color: color-mix(in srgb, currentColor 10%, transparent); /* per-variant override below */ }
.btn:active, .btn[data-pressed="true"] { /* fill/active blend */ }
.btn:focus-visible             { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.btn[disabled], .btn[aria-disabled="true"] { opacity: .35; cursor: not-allowed; }
.btn[aria-busy="true"]         { /* dim label, show spinner */ }
/* similar for .text-input, .select-trigger, .checkbox, .toggle, .menu-item, .skeleton, .titlebar-btn, .workspace-row */
```

### Styleguide expansion

`templates/pages/DevStyleguide.jinja`:

- Top of page: a workspace-color picker — a row of 11 preset swatches (the exact same component used in the production picker). Clicking one updates `<html>`'s `style="--workspace-bg: ..."` and `data-theme` (the theme is recomputed client-side from the picked color's luminance, matching the server-side rule). The page's whole surface re-renders in the chosen color, with the same 150ms transition the production app uses. Replaces the existing OKLCH hue slider entirely. Initial color: `confusion` (matches the production default).
- **No separate light/dark toggle**: theme follows whichever color is picked. Reviewers see both themes by flipping between dark presets (`indifference`/`confusion`/`courage`/`envy`) and light presets (`peace`+ rest).
- Section "Tokens (per theme)": every `:root` and `[data-theme=*]` token gets a swatch with its CSS variable name + resolved value, rendered twice (once per theme) side-by-side so the reviewer can compare. `data-token="--<name>"` carries the cross-check.
- Section "Workspace presets": all 11 preset swatches listed with their slug + hex + inferred theme. The four dark + seven light grouping is visible at a glance.
- Section "Semantic colors": `--success`, `--warning`, `--important`, `--info` swatches plus the matching `.badge-success` / `.badge-warning` / `.badge-important` / `.badge-info` / `.badge-neutral` example badges so reviewers see both the raw token and the consumer class.
- Section "Typography": every step of the 9-step ramp (`Display/24 Bold`, `Heading/16 Semibold`, `Label/14 Medium`, `Body/14 Regular`, `Menu/13 Regular`, `Helper/12 Regular`, `Section/11 Semibold`, `Title/12 Bold`, `Badge/10 Bold`) rendered with sample text + the `.type-*` class name + size/weight/line-height inline. Reviewers can scan the whole ramp; designers can confirm every step is present and proportional.
- Section "Stateful components": for each of Button (all 5 variants), TextInput, Select, Checkbox, Toggle, MenuItem, WorkspaceRow, TitlebarBtn, Skeleton — a grid showing every state under the currently-picked workspace color. State is rendered by setting `data-state="..."` on the demo wrapper which the CSS picks up via the same pseudo-class-equivalent attribute selectors. Reviewers flip workspace color to see both themes; the grid stays in the same layout.
- Section "Icons": all eight Figma icons rendered at 16/24/32 size with their names.
- Existing "Patterns" section is trimmed: anything covered by a primitive component above is removed; what remains is page-level patterns (sidebar workspace dots, focus rings, shadow seam, page chrome).
- Updated `static/dev_styleguide.js` drops the OKLCH hue logic and instead wires the 11-swatch picker to call `mindsWorkspaceColor.apply()` with a `null` agent id (special-cased to skip the API POST and just locally swap `<html>`'s attributes). The luminance-to-theme rule is duplicated client-side in this file so the styleguide can flip themes without a round-trip; the production app sets the theme server-side at render time, so the duplication is reviewer-only.

### Typography snap-review artifact

`blueprint/design-system-foundations/typography-snap-review.md` — written *during* implementation, not in this plan. It is a checklist of every text element that today uses a Tailwind text utility outside the 9-step Figma ramp (`text-2xl`, `text-[11px]`, `text-[0.95em]`, `text-base`, `tracking-wider`, etc.), grouped by file + line. For each, the implementer records which ramp step it snapped to and any visual delta. Reviewers walk the file during PR review to spot snaps that are too lossy and either request a follow-up tweak or a ramp extension. The artifact is deleted (or moved to docs/) once review is complete; it's not a long-term resource.

### Tests

- `apps/minds/imbue/minds/desktop_client/design_tokens_test.py` — new.
  - `test_workspace_preset_palette_covers_every_enum_value` — `WORKSPACE_PRESETS.keys() == set(WorkspacePreset)`.
  - `test_workspace_color_accepts_preset_slug`, `test_workspace_color_accepts_hex_literal`, `test_workspace_color_accepts_oklch_literal`, `test_workspace_color_rejects_garbage`.
  - `test_theme_for_dark_preset_returns_dark`, `test_theme_for_light_preset_returns_light` — spot-check each of the 11 presets.
  - `test_theme_for_oklch_75_returns_correct_theme_per_hue` — verify a handful of OKLCH starting colors (the migration output) land on the expected dark/light side.
  - `test_oklch_starting_color_deterministic_for_same_agent_id`.
  - `test_oklch_starting_color_lightness_is_75_percent` — assert the literal string contains `75%`.
- `apps/minds/imbue/minds/desktop_client/minds_config_test.py` — extend.
  - `test_get_workspace_color_materializes_oklch_for_new_read` — verify first read on an unconfigured agent persists the OKLCH starting color and returns it.
  - `test_set_then_get_workspace_color_round_trips_preset_slug`.
  - `test_set_then_get_workspace_color_round_trips_hex_literal`.
  - `test_get_workspace_color_returns_default_on_unparseable_value` (and logs a warning — assert via `caplog`).
  - `test_remove_workspace_color_is_idempotent`.
  - `test_concurrent_set_workspace_color_is_serialized` — hammer with threads, assert no torn writes.
- `apps/minds/imbue/minds/desktop_client/templates_test.py` — extend.
  - `test_render_landing_page_uses_dark_confusion_default` — assert `data-theme="dark"` and `--workspace-bg: #0B292B` in the rendered HTML.
  - `test_render_login_page_uses_dark_confusion_default` — same default applies to the auth flow.
  - `test_render_workspace_settings_renders_color_picker_with_eleven_swatches` — count swatches.
  - `test_render_workspace_settings_marks_current_preset_aria_pressed`.
  - `test_render_workspace_settings_shows_current_chip_when_color_is_not_a_preset` — verify the "Current" chip appears for an OKLCH literal.
  - `test_render_creating_page_propagates_theme_for_picked_light_preset` — patch `MindsConfig.get_workspace_color` to return `WorkspacePreset.PEACE`; assert `data-theme="light"` and `--workspace-bg: #9FBBD3` in the rendered HTML.
  - `test_render_creating_page_propagates_theme_for_oklch_literal` — patch to return a freeform light-luminance OKLCH; assert `data-theme="light"` and the OKLCH literal echoed into `--workspace-bg`.
  - Update the existing token-ratchet test: walk both `[data-theme="dark"]` and `[data-theme="light"]` blocks plus the `:root` preset declarations; cross-check the set of declared names matches the set of `data-token="--..."` swatches in the styleguide. The `--workspace-accent` ratchet entry is removed; new entries cover every token added in this PR.
- `apps/minds/imbue/minds/desktop_client/test_desktop_client.py` — extend.
  - `test_set_workspace_color_endpoint_persists_and_round_trips_via_get` (preset slug).
  - `test_set_workspace_color_endpoint_accepts_hex_literal`.
  - `test_set_workspace_color_endpoint_rejects_unparseable_value` (422).
  - `test_get_workspace_color_endpoint_returns_resolved_hex_and_theme`.
  - `test_render_sidebar_row_fragment_endpoint_returns_html` — assert content-type, no `<html>` wrapper, props echo into the rendered fragment.
  - `test_render_sharing_email_row_fragment_endpoint_returns_html`.
  - `test_render_landing_provider_row_fragment_endpoint_returns_html`.
  - `test_render_fragment_endpoint_rejects_bad_props` (422 on bad enum / missing required prop).
- Component unit tests in `templates_test.py`:
  - `test_button_renders_each_variant` (primary / secondary / ghost / destructive / success).
  - `test_button_success_variant_uses_semantic_success_token` — assert class or data attribute references the success token.
  - `test_button_loading_sets_aria_busy`.
  - `test_text_input_error_sets_data_error_attribute`.
  - `test_select_renders_chevron_down_icon`.
  - `test_checkbox_indeterminate_emits_data_attribute`.
  - `test_icon_unknown_name_raises` — assert on `TemplateError`.
  - One assertion per icon: `test_icon_<name>_renders_figma_path_verbatim` — pin the SVG `d=` string so future edits stay verbatim.
- Existing tests that today pass `accent=` kwargs or assert on `oklch(65%...)` substrings get migrated to the new `theme=` / `workspace_bg=` shape. The OKLCH-derivation tests for the old 65%-lightness function are deleted; new tests cover the 75%-lightness migration helper instead.
- No new test_ratchets.py entries needed; the token cross-check ratchet is already in place.

### Files to delete

- `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js` (replaced by `workspace_color.js`).
- The `workspace_accent` function and its `_WORKSPACE_L` / `_WORKSPACE_C` constants in `templates.py`. The OKLCH derivation moves to `design_tokens.oklch_starting_color` with `_WORKSPACE_L` bumped to 75%.
- The `.page-workspace::before` stripe rule, the `.accent-swatch` rule, the legacy `.accent-spine` (now folded into landing-row CSS) in `tokens.css`.
- (`TextInput.jinja` stays under its current name; the Figma component is renamed to match.)

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

- Update `Base.jinja` to accept `theme` + `workspace_bg` props and render them on `<html>` (`data-theme=...`, `style="--workspace-bg: ...;"`). Default to `dark` + `#0B292B` (`confusion`).
- Add the helper `_render_kwargs_for_workspace(agent_id, *, config)` to every workspace-scoped render function in `templates.py`.
- Add the new FastAPI routes; wire `MindsConfig` through the route handlers.
- Replace `workspace_accent.js` with `workspace_color.js`; expose `mindsWorkspaceColor.apply()` as the shared live-flip helper used by both pickers and the styleguide.
- Migrate `Chrome.jinja` + `Landing.jinja` first as the canary pages — flip them onto the token system end-to-end.
- Add the workspace-color picker UI in **two places**: a "Color" section on `WorkspaceSettings.jinja` (the persistent canonical home), and a titlebar swatch + flyout on `Chrome.jinja` (the quick-flip). Both call the shared `mindsWorkspaceColor.apply()` helper which POSTs and live-applies — **no reloads anywhere**.
- Outcome: any workspace whose color has been set sees the chosen color across chrome + sidebar + landing row; setting flow works via both pickers with smooth in-place transitions; everything else still renders today's Tailwind colors.

### Phase 3 — sweep all remaining workspace pages

- Migrate `Sidebar.jinja`, `WorkspaceSettings.jinja`, `Sharing.jinja`, `Creating.jinja`, `Destroying.jinja`, permission dialogs.
- Each migration: replace hardcoded `bg-zinc-*` / `text-zinc-*` / `border-*` / `border-white/*` with the new token classes; pass `theme=...` / `ws_color=...` through to `<Base>`.
- Outcome: the entire workspace experience honors the picked color and flips theme correctly.

### Phase 3.5 — JS sweep + fragment endpoints

- Add the badge / link / muted / code-pill / row-tone CSS classes to `tokens.css`.
- Add `SidebarRow.jinja`, `SharingEmailRow.jinja`, `LandingProviderRow.jinja`.
- Add the `/_render/sidebar-row` / `/_render/sharing-email-row` / `/_render/landing-provider-row` FastAPI endpoints.
- Sweep every `.js` file under `static/` + the inline scripts in `Landing.jinja` and `Create.jinja`:
  - Swap simple-badge `className` strings for the new token-backed classes.
  - Swap complex row-builders for `fetch('/_render/...')` calls.
- Outcome: zero hardcoded Tailwind color tone references in any JS (`bg-emerald-100`, `text-red-800`, `bg-amber-400/80`, etc. all gone); composite rows are server-rendered via fragment endpoints.

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

### Phase 6 — styleguide expansion + typography migration

- Rewrite `DevStyleguide.jinja` per the spec above (workspace-color picker, per-component state grids, token swatches per theme, workspace-presets list, semantic-color swatches + badge examples, full 9-step typography ramp, icon catalog).
- Rewrite `static/dev_styleguide.js` to share the `mindsWorkspaceColor.apply()` helper.
- Sweep every template's text-related Tailwind utilities into the `.type-*` classes. Capture each lossy snap in `blueprint/design-system-foundations/typography-snap-review.md` (file location, original utility, target ramp step, optional reviewer note).
- Outcome: reviewers can flip the styleguide through all 11 workspace colors; every state of every primitive is visible; the whole codebase has type-ramp-consistent text; the snap-review file lists every lossy migration for PR review.

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

Resolved during Q&A and folded into the plan above:

- ~~**Picker location**~~ → both: settings-page "Color" section + titlebar swatch flyout.
- ~~**Legacy workspace default**~~ → keep each existing workspace's deterministic OKLCH hue, bumped to L=75%. Presets are suggestions, not a closed catalog; storage accepts arbitrary CSS color literals from day one.
- ~~**Permission-dialog theming**~~ → follows host workspace's theme.
- ~~**`success` button variant**~~ → keep, map to `semantic/success` (#5C8A3C).
- ~~**TextField rename**~~ → keep `TextInput.jinja` code name; rename the Figma component to match via the Figma MCP.
- ~~**Theme override outside styleguide**~~ → no override affordance in the production app. Styleguide picker shows all 11 workspace colors (theme is implied by luminance), no separate light/dark toggle.
- ~~**Inbox modal screen**~~ → out of scope; follow-up.
- ~~**Energy contrast**~~ → keep `#CECD0C`, verify during Phase 8 manual review.
- ~~**Sidebar per-item color**~~ → small swatch dot to the left of each workspace name (Figma space-switcher style).
- ~~**Status-badge / semantic-color migration scope**~~ → comprehensive sweep including JS callers; badges use new token-backed `.badge-*` classes.
- ~~**Type-ramp adoption scope**~~ → comprehensive sweep, with lossy snaps tracked in `typography-snap-review.md` for reviewer sanity-check.
- ~~**Live-flip animation**~~ → 150ms transition on `--workspace-bg` only; text + border snap.
- ~~**iframe color sync**~~ → workspace color applies only to outer chrome; the iframe is FCT's realm and stays visually distinct on purpose as a trust signal.
- ~~**JS row-builders approach**~~ → hybrid: simple badges keep `className` swaps (to new `.badge-*` classes); complex composites move to server-rendered fragment endpoints.
- ~~**Custom text sizes outside the ramp**~~ → snap to nearest ramp step; lossy snaps go in the snap-review artifact.
- ~~**Settings-page picker live-vs-reload**~~ → live-apply; no reload anywhere. Both pickers share the same `mindsWorkspaceColor.apply()` JS helper.
- ~~**Outer-chrome boundary scope**~~ → auth pages and other pre-workspace contexts default to `confusion` (themed, not full black).
- ~~**New-agent default**~~ → `confusion` (#0B292B).

Still open, to resolve before / during implementation:

- **`--radius-lg` exact value.** Figma exposes `radius-md = 10` and `radius-pill = 999` directly via `get_variable_defs`; `radius-sm` (6) is referenced but `radius-lg` is not explicitly in the variable response. Confirm the exact value from Figma node 317:4083 ("Radius") at impl time; default plan assumes ~14px to bracket the `md`/`pill` gap.
- **Freeform-picker follow-up.** This PR ships only the 11-preset picker UI; the storage + theme-inference already handle arbitrary CSS colors. Should the freeform color picker (eyedropper, hex input, OKLCH sliders) be a follow-up PR, or roll it in here? Plan defers as a follow-up to keep this PR's UI surface manageable; flag if it's expected sooner.
- **Saved-affordance on settings-page picker.** With both pickers live-applying, the settings-page picker loses the implicit "I just persisted that change" feedback that a reload used to give. Plan suggests a tiny "Saved" affordance after a successful POST; design exactly what that looks like (toast? swatch checkmark? aria-live region?) at impl time.
- **Inflight-batching for fragment endpoints.** If a single SSE event triggers N row rebuilds (e.g. landing's `renderProviders` re-creating every provider row on each snapshot), the naive approach fires N fragment fetches in parallel. Probably fine in practice (localhost, sub-ms), but a follow-up could batch into a single `POST /_render/landing-provider-rows` taking an array of props. Flag if SSE-tick latency becomes visible during Phase 8.
