Begin formalizing the design system for the minds desktop client (phases 1+2 in this PR; later phases tracked in `blueprint/design-system-foundations/`).

Phase 1 ships the foundations:

- New `design_tokens.py` module with the 11 named Figma workspace presets (`indifference`, `confusion`, `courage`, `envy`, `peace`, `belonging`, `energy`, `strength`, `comfort`, `inspiration`, `clarity`), a `WorkspaceColor` value type that accepts either a preset slug or a CSS color literal (hex / oklch / rgb), perceptual-lightness theme inference (auto-flips dark/light per workspace color), and an `oklch_starting_color` migration helper (75% lightness, up from the legacy 65%).
- `MindsConfig` gains `get_workspace_color` / `set_workspace_color` / `remove_workspace_color`. First read for an unconfigured agent materializes the deterministic OKLCH starting color into the config so the visual identity is stable across sessions.
- `static/tokens.css` rewritten with the full token catalog: foreground-at-opacity ramps for dark + light themes, semantic colors (success/warning/important/info), focus ring (#0A84FF at 90%), spacing scale, radius scale, type ramp (`.type-display-24` through `.type-badge-10`), token utility classes (`text-token-*`, `bg-token-*`, `border-token-*`), badge classes, and the new component primitives (`.workspace-row`, `.titlebar-btn`, `.menu-item`, `.skeleton`, `.ws-dot`).

Phase 2 wires those foundations through the canary chrome:

- `Base.jinja` accepts `theme` + `workspace_bg` props (default `confusion`) and writes them to `<html data-theme="…" style="--workspace-bg: …">`. Foreground tokens auto-flip with the theme attribute; the background animates smoothly (150 ms) on live workspace-color changes.
- New `GET / POST /api/workspace-color/{agent_id}` endpoints. Both pickers (the new titlebar quick-flip flyout in the chrome and the new "Color" section on the workspace-settings page) call the shared `mindsWorkspaceColor.apply()` JS helper, which POSTs the change and flips `data-theme` + `--workspace-bg` on `<html>` in place — no page reload.
- `Chrome.jinja`, `Sidebar.jinja`, `Landing.jinja`, and `WorkspaceSettings.jinja` migrate off hardcoded `bg-zinc-*` / `text-zinc-*` / `border-white/*` Tailwind utilities and onto the new token-utility classes. The landing page surfaces each workspace's own color as a small swatch dot to the left of the name (matching the Figma space-switcher).
- The 11-color picker on the workspace-settings page shows an `aria-pressed`-marked swatch for the current preset and a "Current" chip when the persisted color is a freeform value (e.g. the OKLCH starting color).

The outer minds chrome takes on the active workspace color; the FCT iframe content stays visually distinct on purpose as a trust-boundary cue. Pre-workspace pages (landing, welcome, auth) and the default for any new agent are `confusion` (#0B292B) rather than pure black.

No user-visible behavior changes outside the chrome / canary pages above. The remaining pages (creating, destroying, sharing, permission dialogs, auth) continue to render with their existing styling; their migration plus the JS-side semantic-color sweep, the new stateful components (Select / Checkbox / Toggle / Icon / etc.), the type-ramp adoption across every template, and the styleguide rewrite all land in subsequent phases.

Electron-side: the Cmd+Option+I (Ctrl+Shift+C on non-mac) devtools shortcut and the View > Toggle Developer Tools menu item now open devtools for whichever webContents is focused — the chrome titlebar, the sidebar, the requests panel, or the iframe content — instead of always targeting the iframe content. `MINDS_OPEN_DEVTOOLS=1` also auto-opens devtools for the chrome view at startup alongside the iframe content.
