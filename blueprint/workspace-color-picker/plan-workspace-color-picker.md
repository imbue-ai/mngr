# Plan: Workspace color picker

## Refined prompt

I want to extend the current workspace color system to use a set of predefined values and a custom optional hex. We'll want a color picker in the onboarding flow (very minimal no custom option) and bigger one with the custom option in the settings. The specific colors are these: https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=356-4113&t=D0X7nv6rcuGNEKhz-11 + white (this replaces the sha system we currently have.)

* Palette (12): `indifference` `#000000`, `confusion` `#0b292b`, `courage` `#492222`, `envy` `#3c3d06`, `peace` `#9fbbd3`, `belonging` `#e8a7a8`, `energy` `#cecd0c`, `strength` `#cfc7b3`, `comfort` `#f5d6a0`, `inspiration` `#e9ecd9`, `clarity` `#fcefd4`, `white` `#ffffff` (Figma node 356:4113 + literal `#ffffff` white)
* Persist the chosen color so the same workspace shows the same color across machines the user signs into; per-workspace global (everyone who can see the workspace sees the same color)
* Onboarding picker = new field at the **top** of `Create.jinja`; palette swatches only, no custom-hex; default `confusion` pre-selected
* Settings picker in `WorkspaceSettings.jinja`: palette swatches + always-visible hex input. The hex input is the source of truth; selecting any swatch fills the input; whenever the input value matches a palette entry the corresponding swatch shows as selected
* Swatches are visually unlabeled (no name caption / tooltip text visible); only the resolved hex appears in the settings input
* Hex input is **lenient** (accepts `#fff` / `fff` / `#ffffff` / `ffffff`, any case) and normalizes to `#rrggbb` lowercase on blur/save
* Implicit save in settings: any palette pick or any valid hex saves immediately and propagates live; no Save button. While the typed hex is invalid an inline error shows and no save fires (the previously saved color stays in effect)
* Persist only the resolved hex string — not a tagged union with palette name
* Foreground contrast picker switches to **WCAG relative luminance** so titlebar text/icons stay legible across the wider palette range; replaces the current fixed `0 0 0` output
* Color edits propagate **live** via the existing SSE workspaces payload — titlebar / sidebar / chrome in every open window repaint immediately, using the existing `notify_change()` infrastructure on the backend resolver
* Migration: one-time backfill resets every existing workspace to `confusion`; `workspace_accent()` (Python) and `hueFromAgentId()` (JS) are deleted entirely
* Sidebar item spines on `bg-zinc-900` are accepted to be invisible / low-contrast for dark accents — sidebar visual rework is deferred to a separate PR

## Architectural note (divergence from one Q&A answer)

The Q&A chose "RSC backend" as the storage location. Closer inspection shows there is **no RSC-side workspace persistence today** — workspace metadata (e.g. `workspace=<name>`) lives as an **mngr label** on the primary agent's `agent.json`, read through `BackendResolver.get_workspace_name()` and changed via `mngr label`. That substrate already satisfies the stated intent (the label sits with the agent on its host, so the same color reaches any machine that can see the agent through `mngr observe --discovery-only`) and reuses the existing SSE wake-up path. The plan below stores color as an mngr label on the same agent. Flag if you want a true RSC-side schema instead — that is a much larger build (new endpoint, persistence substrate, migration).

## Overview

- Replace the SHA-256-derived per-workspace hue (`templates.py:workspace_accent` and `static/workspace_accent.js:hueFromAgentId`) with a stored hex color persisted as an mngr `color=<hex>` label on the primary agent, alongside the existing `workspace=<name>` label.
- Define a single source-of-truth palette (12 entries: the 11 named Figma colors at node 356:4113 plus literal `#ffffff` white) in one place each on server and client, with the named constants mirrored 1:1.
- Add a minimal palette-only color picker as a new first field in the workspace-create form (`Create.jinja`), defaulting to `confusion`.
- Add a fuller picker (palette swatches + always-visible hex input, swatch selection mirrored by hex value) to `WorkspaceSettings.jinja` with implicit save and inline validation.
- Replace the L=0.85-only contrast picker with a WCAG relative luminance picker so titlebar text/icons remain legible across the full hex range; emit the foreground choice from the server as part of the SSE workspaces payload so the renderer no longer needs to compute it.
- Backfill every existing workspace with `confusion` on first server-side read after the upgrade; delete the SHA derivation entirely once the backfill ships.

## Expected behavior

- **Creating a workspace**: the `Create` form shows a row of 12 unlabeled palette swatches at the top, with `confusion` (`#0b292b`) pre-selected. Submitting persists that hex as the workspace's `color` label on the primary agent at create time; the chrome paints that color the first time the workspace is opened. No custom-hex affordance is offered here.
- **Opening any workspace**: the titlebar background paints the stored hex, and titlebar text / nav icons / account button render in black or white according to the WCAG luminance of the stored hex. Hover tints and the close-button red are unchanged.
- **Editing color in workspace settings**: the settings page renders a row of 12 unlabeled palette swatches plus a single hex input. The hex input always shows the current saved hex in `#rrggbb` lowercase. Clicking any swatch fills the input and saves immediately. Typing in the input: while the value parses as a valid hex (`#fff`, `fff`, `#ffffff`, `ffffff`, any case) the input shows as valid and saves on blur (after normalization to `#rrggbb` lowercase); while invalid an inline error displays and no save fires. Whenever the (valid, normalized) input value matches a palette entry, that swatch shows as visually selected; otherwise no swatch is selected and the value is treated as custom.
- **Live propagation on save**: every open window's titlebar, sidebar item spine, and any other `--workspace-accent` consumer for that workspace repaints within one SSE tick of the save. Foreground (black vs white) flips in lockstep when the new hex crosses the luminance threshold.
- **Multiple machines**: a user signed in on a second machine that can see the same workspace through discovery sees the same color, since the color lives with the agent (not on the local desktop client).
- **First launch after the upgrade with existing workspaces**: every prior workspace renders as `confusion` (`#0b292b`) on the first SSE tick. The next user-initiated color change replaces it.
- **First launch with no workspaces ever opened**: the chrome remains the default dark `bg-zinc-900` with white text, exactly as today.
- **Custom hex contrast oddities**: black titlebar (e.g. `indifference` `#000000`) renders white text/icons; very pale palette entries (`white`, `clarity`) render black text/icons; mid-range colors switch on the WCAG luminance threshold.
- **Sidebar item spines on the dark sidebar (`bg-zinc-900`)**: spines paint the stored hex unchanged. Dark accents (`indifference`, `confusion`, `courage`, `envy`) render as low-contrast spines that may be hard to see — accepted for this PR; a separate PR reworks the sidebar to address it.
- **Browser-mode / system-interface pages**: the per-page `workspace_accent` injection used by `templates.py` for in-container pages (the system-interface served HTML pages that mirror the accent) reads from the same stored value via the same backend lookup, so those pages also show the chosen color.

## Changes

- **Palette source-of-truth** in `apps/minds/imbue/minds/desktop_client/templates.py`: a new module-level constant `WORKSPACE_PALETTE` mapping each name (`indifference`, `confusion`, …, `white`) to its `#rrggbb` hex. Exported once and consumed everywhere on the server side.
- **Palette mirror** in `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js`: a JS object with the same name → hex entries. Mirrors `WORKSPACE_PALETTE` exactly (kept in lockstep via a ratchet that hashes both and asserts equality, similar to the existing accent-mirror invariant).
- **Default color constant**: `confusion` is named in both palettes as the default for new workspaces and the migration backfill.
- **mngr label**: extend the primary agent's labels with a `color=<hex>` field at create time, written in `agent_creator.py` alongside the existing `workspace=<name>` label.
- **Backend resolver**: a new `get_workspace_color(agent_id) -> str | None` on `BackendResolverInterface` (and its implementations), mirroring `get_workspace_name`. Returns the stored hex label or `None` if unset (treated by callers as "needs backfill").
- **Color-write path**: a new desktop-client HTTP endpoint (`POST /api/workspaces/{agent_id}/color` body `{hex: str}`) that validates and normalizes the hex, calls the mngr-label write, and calls `backend_resolver.notify_change()` so SSE picks it up. Mirrors how name changes would flow.
- **Migration / backfill**: the first call to `get_workspace_color` for an agent without the label backfills `confusion` (writes the label + `notify_change()`). Side-effect at read time is acceptable because writes are idempotent and the agent.json store is local; alternative is a one-shot sweep on app start.
- **SSE workspace payload** (`_build_workspace_list` in `app.py`): each entry's `accent` field is sourced from the stored color label (via `get_workspace_color`) rather than computed via `workspace_accent(aid)`. The entry also carries a new `accent_fg` field with the WCAG-derived foreground RGB triple (`"0 0 0"` or `"255 255 255"`) computed server-side. SSE tick logic and the diff-based suppress are unchanged.
- **SHA derivation deletion**: remove `workspace_accent()` from `templates.py` and its `_WORKSPACE_L` / `_WORKSPACE_C` constants. Remove `hueFromAgentId()` and the `hueCache` / `colorCache` from `static/workspace_accent.js`.
- **Contrast picker rewrite** in `static/workspace_accent.js`: replace `pickForeground()` (currently constant) with a pure function from a hex string to `'0 0 0' | '255 255 255'` using the WCAG relative luminance formula (`L = 0.2126*R + 0.7152*G + 0.0722*B` against the standard `0.179` cutoff). The same function lives server-side as a Python helper for the `accent_fg` SSE field.
- **Client-side application** in `static/chrome.js` (`applyTitleAccent`): replace the async SHA-256 path with a synchronous read of the workspace's hex from the SSE payload (already in `--workspace-accent` and parallel `--titlebar-fg`). Drop `pendingAccentAgentId` tokenization since there is no longer an async resolve. Foreground is taken from the same SSE payload.
- **Sidebar consumers** in `static/sidebar.js` and any other `accent`-reading scripts: read the per-workspace hex from the SSE payload as today; no derivation fallback. Drop the `mindsAccent.get` callsites.
- **Onboarding picker** in `apps/minds/imbue/minds/desktop_client/templates/pages/Create.jinja`: a new form field at the top, before the existing identity fields. Renders 12 swatches as `<button>` elements (radio-like single-select) wrapped in a `<fieldset>`. The submit handler attaches the picked hex to the existing form submission so `agent_creator` receives it and writes the label.
- **Settings picker** in `apps/minds/imbue/minds/desktop_client/templates/pages/WorkspaceSettings.jinja`: a new section above the existing Account section. Renders the same 12 swatches plus a `<input type="text">` for the hex. Inline JS handles: click-swatch-to-fill-input, input-change live validation, debounce + POST to `/api/workspaces/{agent_id}/color` on blur (or immediately for swatch picks), live error display, and (on success) no further UI action since SSE drives the visual update.
- **Static styling**: a new CSS block (in `tokens.css` or a new `palette.css`) for the swatch component — fixed size, hover/focus rings, selected-state ring. Swatches have no visible labels per the Q&A; aria-labels carry the palette name for screen readers.
- **Tests** to add/update (high-signal only):
  - Unit: the WCAG `pickForeground(hex)` function (table of inputs covering each palette entry and a few customs).
  - Unit: hex normalization helper (accepts each input form, rejects junk).
  - Unit: `WORKSPACE_PALETTE` lockstep (server vs client digest match).
  - Unit: `_build_workspace_list` emits the stored color and matching `accent_fg`.
  - Integration: settings POST endpoint validates, writes the label, fires `notify_change()`; SSE round-trip propagates within one tick.
  - Integration: `Create.jinja` submit carries the picked color into the new agent's labels.
  - Integration: migration backfill writes `confusion` for an agent that lacks the label, exactly once.
  - Templates: `templates_test.py` assertion that `workspace_accent()` is gone and `_build_workspace_list` no longer hashes the agent id.
- **Deletions** (post-migration cleanup, same PR):
  - `templates.py:workspace_accent` + `_WORKSPACE_L` + `_WORKSPACE_C`.
  - `workspace_accent.js:hueFromAgentId`, `hueCache`, the legacy callback-style `get`, and the OKLCH-string construction.
  - Any remaining test fixtures that pin the SHA-derived OKLCH output.
- **Changelog**: `apps/minds/changelog/<branch>.md` describing the user-visible behavior shift (picker in Create + Settings, all existing workspaces shown as `confusion` until repicked, custom hex available in Settings).
- **Not in scope (deferred)**:
  - Visual sidebar rework to keep dark spines visible on the dark sidebar.
  - Surfacing palette names in the UI (e.g. tooltips, captions).
  - Per-user-per-workspace color overrides.
  - Any RSC-side color persistence (current substrate is mngr labels on the agent).
