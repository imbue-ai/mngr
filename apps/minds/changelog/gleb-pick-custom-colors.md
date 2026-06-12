Replace the SHA-derived per-workspace accent with a user-pickable palette + custom hex.

- Workspaces now ship with one of 12 named palette colors (in picker order: `confusion`, `courage`, `envy`, `peace`, `belonging`, `energy`, `strength`, `comfort`, `inspiration`, `clarity`, then the two neutrals `indifference` and `white`) or an arbitrary `#rrggbb` hex chosen by the user. The previous SHA-from-agent-id OKLCH hue is gone.

- A palette-only picker is added to the **Create** form at the top, above the launch / AI provider configuration. The selected color is written as an mngr `color=<hex>` label on the new primary agent at create time -- no follow-up write.

- A fuller picker is added to **Workspace settings** above the Account section: the same 12 swatches plus an always-visible hex input that accepts lenient forms (`#fff`, `fff`, `#ffffff`, `ffffff`, any case) and normalizes to `#rrggbb` lowercase on save. Save is implicit -- a swatch pick saves immediately; a typed hex saves on blur. Inline errors cover invalid hex, the workspace being unreachable, and the underlying `mngr label` shell-out failing. Picker controls disable when the workspace's provider is in error state.

- Titlebar text / nav icons / account button now use a **WCAG relative luminance** contrast picker server-side, so legibility holds across the full hex range -- previously a fixed black-on-light assumption. The foreground RGB triple is emitted as `accent_fg` on each SSE workspaces payload entry; the client just drops it into a CSS variable.

- Color edits propagate live: the settings POST endpoint shells out to `mngr label <agent> -l color=<hex>` (CLI merge semantics, so concurrent writes against other label keys don't clobber each other), updates the resolver's snapshot optimistically, and fires the SSE wake-up so the chrome / sidebar / homepage tile repaint within one tick.

- Workspaces created before the picker shipped that still have no `color` label render as `confusion` (`#0b292b`, the default) until the user picks something. The first save persists the choice as an on-disk mngr label.

- Sidebar item spines on the dark sidebar (`bg-zinc-900`) currently paint the stored hex unchanged; dark palette entries (`indifference`, `confusion`, `courage`, `envy`) read as low-contrast spines on that surface. A separate PR will rework the sidebar treatment to address this.
