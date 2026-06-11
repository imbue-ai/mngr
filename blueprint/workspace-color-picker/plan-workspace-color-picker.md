# Plan: Workspace color picker

## Refined prompt

I want to extend the current workspace color system to use a set of predefined values and a custom optional hex. We'll want a color picker in the onboarding flow (very minimal no custom option) and bigger one with the custom option in the settings. The specific colors are these: https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=356-4113&t=D0X7nv6rcuGNEKhz-11 + white (this replaces the sha system we currently have.)

* Palette (12): `indifference` `#000000`, `confusion` `#0b292b`, `courage` `#492222`, `envy` `#3c3d06`, `peace` `#9fbbd3`, `belonging` `#e8a7a8`, `energy` `#cecd0c`, `strength` `#cfc7b3`, `comfort` `#f5d6a0`, `inspiration` `#e9ecd9`, `clarity` `#fcefd4`, `white` `#ffffff` (Figma node 356:4113 + literal `#ffffff` white)
* Persist the chosen color so the same workspace shows the same color across machines the user signs into; per-workspace global (everyone who can see the workspace sees the same color)
* Onboarding picker = new field in `Create.jinja`, below the name + Create row; palette swatches only, no custom-hex. As-built, the preselect is the first palette color not already in use across active workspaces (falling back to `confusion` when no workspaces exist or every palette entry is taken) rather than a fixed `confusion` default
* Settings picker in `WorkspaceSettings.jinja`: palette swatches + always-visible hex input. The hex input is the source of truth; selecting any swatch fills the input; whenever the input value matches a palette entry the corresponding swatch shows as selected
* Swatches are visually unlabeled (no name caption / tooltip text visible); only the resolved hex appears in the settings input
* Hex input is **lenient** (accepts `#fff` / `fff` / `#ffffff` / `ffffff`, any case) and normalizes to `#rrggbb` lowercase on blur/save
* Implicit save in settings: any palette pick or any valid hex saves immediately and propagates live; no Save button. While the typed hex is invalid an inline error shows and no save fires (the previously saved color stays in effect)
* Persist only the resolved hex string — not a tagged union with palette name
* Foreground contrast picker switches to **WCAG relative luminance** so titlebar text/icons stay legible across the wider palette range; replaces the current fixed `0 0 0` output
* Color edits propagate **live** via the existing SSE workspaces payload — titlebar / sidebar / chrome in every open window repaint immediately, using the existing `notify_change()` infrastructure on the backend resolver
* Migration: workspaces without a `color` label render with `DEFAULT_WORKSPACE_COLOR` (`confusion`) via a renderer-side fallback (no proactive `mngr label` write); the on-disk label is set the next time the user picks in settings. `workspace_accent()` (Python) and `hueFromAgentId()` (JS) are deleted entirely. See the "Migration / backfill" entry under Changes for the rationale
* Sidebar item spines on `bg-zinc-900` are accepted to be invisible / low-contrast for dark accents — sidebar visual rework is deferred to a separate PR

## Architectural note (storage substrate)

**Decision: store `color=<hex>` as an mngr label on the primary `claude` agent**, alongside the existing `workspace=<name>` label. Rationale:

- The Q&A originally picked "RSC backend", but there is **no RSC-side workspace persistence today** — workspace metadata already lives as agent labels. The original intent (same color across machines a user signs into) is satisfied because labels travel with the agent on its host and reach any client via `mngr observe --discovery-only`.
- **Considered and rejected:** host `user_tags` (`CertifiedHostData.user_tags` at `libs/mngr/imbue/mngr/interfaces/data_types.py:339`). In minds 1 host = 1 workspace = 2 agents, so the scopes are functionally interchangeable, but the workspace name already lives at the agent scope (`workspace=<name>` on the primary agent). Putting the color next to the name in the same dict on the same agent gives one read, one write, one merge, one mental model. Host `user_tags` would require a new resolver hop (agent → host → tags) and a writes-path that minds doesn't currently exercise.
- **Considered and rejected:** a true RSC-side schema (new Modal endpoint, persistence substrate, migration). Much larger build; no reuse of existing surfaces.

### Substrate gotchas this plan must handle

These come from the way mngr labels propagate (see the in-conversation analysis):

- **Cross-machine propagation is pull-based (~10s discovery tick).** Same-process / same-host updates are immediate via `backend_resolver.notify_change()`; a second client on another machine sees the new color on its next `mngr observe --discovery-only` emit.
- **`BaseAgent.set_labels()` is full-replace at the API level** — concurrent writers clobber each other's keys. **Writes in this plan go through the `mngr label` CLI** (`mngr label <agent_id> -l color=<hex>`), which performs merge in the CLI layer (`libs/mngr/imbue/mngr/cli/label.py:84-86`).
- **BackendResolver reads are cached snapshots**, not direct file reads — `get_workspace_color` (new) reads from `_agents_result.discovered_agents`, updated only when discovery emits. To make the writing client's settings save feel immediate, **the resolver is updated optimistically right after a successful CLI write** (the resolver already has the mutation hook used by `update_agents`).
- **No mngr-side validation of label values** — the server-side write path must enforce hex shape; the read path must **defensively parse** and fall back to `confusion` if a stored value is not `#rrggbb`.
- **Two-agent caveat**: minds creates a primary `claude` agent (`is_primary=true`) and a `system-services` agent per workspace. The `color` label goes on the primary agent only; writers select by `is_primary=true` (same filter used in `app.py:467` and `:828` for destructive-action gating).
- **Host-offline writes fail outright.** The settings POST handler returns the error and the settings UI surfaces it inline (same affordance as invalid-hex).
- **Stale-provider lock-out**: when a workspace's provider is `is_stale=true`, the color picker disables the controls and shows a hint that the workspace is unreachable; reads keep showing the last-known color.

## Overview

- Replace the SHA-256-derived per-workspace hue (`templates.py:workspace_accent` and `static/workspace_accent.js:hueFromAgentId`) with a stored hex color persisted as an mngr `color=<hex>` label on the primary agent, alongside the existing `workspace=<name>` label.
- Define a single source-of-truth palette (12 entries: the 11 named Figma colors at node 356:4113 plus literal `#ffffff` white) server-side only; the client receives server-rendered swatches (carrying `data-color` attributes) plus the resolved `accent` / `accent_fg` over SSE, so there is no JS palette copy to keep in sync.
- Add a minimal palette-only color picker to the workspace-create form (`Create.jinja`, below the name + Create row), preselecting the first palette color not already in use across active workspaces (falling back to `confusion`).
- Add a fuller picker (palette swatches + always-visible hex input, swatch selection mirrored by hex value) to `WorkspaceSettings.jinja` with implicit save and inline validation.
- Replace the L=0.85-only contrast picker with a WCAG relative luminance picker so titlebar text/icons remain legible across the full hex range; emit the foreground choice from the server as part of the SSE workspaces payload so the renderer no longer needs to compute it.
- Render existing workspaces without a `color` label as `confusion` via a renderer-side fallback in `_build_workspace_list`; delete the SHA derivation entirely. On-disk `mngr label` state is updated lazily — only when the user picks in settings.

## Expected behavior

- **Creating a workspace**: the `Create` form shows a row of 12 unlabeled palette swatches below the name + Create row, pre-selecting the first palette color not already in use across active workspaces (`confusion` `#0b292b` when no workspaces exist or every palette entry is taken). Submitting persists the picked hex as the workspace's `color` label on the primary agent at create time; the chrome paints that color the first time the workspace is opened. No custom-hex affordance is offered here.
- **Opening any workspace**: the titlebar background paints the stored hex, and titlebar text / nav icons / account button render in black or white according to the WCAG luminance of the stored hex. Hover tints and the close-button red are unchanged.
- **Editing color in workspace settings**: the settings page renders a row of 12 unlabeled palette swatches plus a single hex input. The hex input always shows the current saved hex in `#rrggbb` lowercase. Clicking any swatch fills the input and saves immediately. Typing in the input: while the value parses as a valid hex (`#fff`, `fff`, `#ffffff`, `ffffff`, any case) the input shows as valid and saves on blur (after normalization to `#rrggbb` lowercase); while invalid an inline error displays and no save fires. Whenever the (valid, normalized) input value matches a palette entry, that swatch shows as visually selected; otherwise no swatch is selected and the value is treated as custom.
- **Live propagation on save**: every open window's titlebar, sidebar item spine, and any other `--workspace-accent` consumer for that workspace repaints within one SSE tick of the save. Foreground (black vs white) flips in lockstep when the new hex crosses the luminance threshold.
- **Multiple machines**: a user signed in on a second machine that can see the same workspace through discovery sees the same color, since the color lives with the agent (not on the local desktop client).
- **First launch after the upgrade with existing workspaces**: every prior workspace renders as `confusion` (`#0b292b`) on the first SSE tick. The next user-initiated color change replaces it.
- **First launch with no workspaces ever opened**: the chrome remains the default dark `bg-zinc-900` with white text, exactly as today.
- **Custom hex contrast oddities**: black titlebar (e.g. `indifference` `#000000`) renders white text/icons; very pale palette entries (`white`, `clarity`) render black text/icons; mid-range colors switch on the WCAG luminance threshold.
- **Sidebar item spines on the dark sidebar (`bg-zinc-900`)**: spines paint the stored hex unchanged. Dark accents (`indifference`, `confusion`, `courage`, `envy`) render as low-contrast spines that may be hard to see — accepted for this PR; a separate PR reworks the sidebar to address it.
- **In-container system_interface pages**: unaffected. The FCT-side `system_interface` uses its own fixed accent (`--color-accent: #2f6b4f` in `apps/system_interface/frontend/src/style.css:16`) and does **not** consume the per-workspace accent today. No in-container read path for the color label is required.

## Changes

- **Palette source-of-truth** in `apps/minds/imbue/minds/desktop_client/workspace_color.py`: a module-level constant `WORKSPACE_PALETTE` mapping each name (`indifference`, `confusion`, …, `white`) to its `#rrggbb` hex. Exported once and consumed everywhere on the server side. (A dedicated module rather than `templates.py` so the backend resolver can read the default color and normalize stored labels without a circular import.)
- **No palette mirror in JS**: `static/workspace_accent.js` keeps only the two pure runtime helpers (`normalizeHex` / `pickForegroundForHex`, mirroring `normalize_workspace_color` / `pick_workspace_foreground` in `workspace_color.py`). The palette itself reaches the client as server-rendered swatches carrying `data-color` attributes, so there is no JS name → hex copy to keep in lockstep; a guard test in `templates_test.py` fails if one is reintroduced.

    *Decision history*: the original plan called for a JS palette mirror kept in lockstep with `WORKSPACE_PALETTE` via a digest-comparison ratchet. As-built drops the mirror entirely — the pickers never need the palette at runtime (swatches are server-rendered), so the mirror would have been a second source of truth with no consumer.
- **Default color constant**: `confusion` is named in both palettes as the default for new workspaces and as the renderer-side fallback for un-labeled workspaces.
- **mngr label**: extend the primary agent's labels with a `color=<hex>` field at create time, written in `agent_creator.py` alongside the existing `workspace=<name>` label. Writes go through the `mngr label` CLI to inherit merge semantics (avoids the full-replace race that `BaseAgent.set_labels` carries).
- **Backend resolver**: a new `get_workspace_color(agent_id) -> str | None` on `BackendResolverInterface` (and its implementations), mirroring `get_workspace_name`. Reads from the same cached `_agents_result.discovered_agents` snapshot. Returns the stored hex label or `None` if unset (treated by callers as "needs backfill"). The read is **defensively parsed**: if the stored string is not a valid `#rrggbb`, the resolver returns `confusion` and logs once.
- **Color-write path**: a new desktop-client HTTP endpoint (`POST /api/workspaces/{agent_id}/color` body `{hex: str}`) that validates and normalizes the hex, gates on the agent's primary status (`is_primary=true`), shells out to `mngr label <agent_id> -l color=<hex>` (so concurrent writes against other labels are merged, not clobbered), updates the resolver's in-memory snapshot for that agent optimistically, and calls `backend_resolver.notify_change()` so SSE picks it up within the same tick. Returns a structured error if the workspace's provider is in `is_stale` mode or the host is unreachable.
- **Migration / backfill**: workspaces created before the picker shipped (no `color` label on disk) render with `DEFAULT_WORKSPACE_COLOR` (`confusion`) via a renderer-side fallback in `_build_workspace_list`; the on-disk agent label stays absent until the user picks a color in settings, at which point Phase 3's POST endpoint writes it through `mngr label`. **No proactive `mngr label` write happens at upgrade time.**

    *Decision history*: the original plan called for a proactive backfill from inside `_build_workspace_list` on the first SSE tick. As-built drops that step because (a) `_build_workspace_list` runs synchronously inside the SSE generator's hot path, where a per-agent subprocess fork-exec would visibly stall the tick; (b) tracking "already backfilled" state to keep the write idempotent adds an in-memory set + lock semantics in a path that wants to be a pure read; (c) a failed `mngr label` write (host unreachable, stale provider) would either fail the SSE tick or get silently swallowed -- both bad. The renderer-default approach achieves the same user-visible behavior (every client sees `confusion` for un-labeled workspaces) without those costs. The trade-off is that introspecting `agent.labels["color"]` via `mngr list` on an un-touched workspace will show no entry rather than `color=#0b292b`; if a future feature needs the label to exist explicitly for all workspaces, a follow-up can backfill from a background task (e.g. piggybacked onto `MngrCliBackendResolver.update_agents`, not the SSE generator).
- **SSE workspace payload** (`_build_workspace_list` in `app.py`): each entry's `accent` field is sourced from the stored color label (via `get_workspace_color`) rather than computed via `workspace_accent(aid)`. The entry also carries a new `accent_fg` field with the WCAG-derived foreground RGB triple (`"0 0 0"` or `"255 255 255"`) computed server-side. SSE tick logic and the diff-based suppress are unchanged.
- **SHA derivation deletion**: remove `workspace_accent()` from `templates.py` and its `_WORKSPACE_L` / `_WORKSPACE_C` constants. Remove `hueFromAgentId()` and the `hueCache` / `colorCache` from `static/workspace_accent.js`.
- **Contrast picker rewrite** in `static/workspace_accent.js`: replace `pickForeground()` (currently constant) with a pure function from a hex string to `'0 0 0' | '255 255 255'` using the WCAG relative luminance formula (`L = 0.2126*R + 0.7152*G + 0.0722*B` against the standard `0.179` cutoff). The same function lives server-side as a Python helper for the `accent_fg` SSE field.
- **Client-side application** in `static/chrome.js` (`applyTitleAccent`): replace the async SHA-256 path with a synchronous read of the workspace's hex from the SSE payload (already in `--workspace-accent` and parallel `--titlebar-fg`). Drop `pendingAccentAgentId` tokenization since there is no longer an async resolve. Foreground is taken from the same SSE payload.
- **Sidebar consumers** in `static/sidebar.js` and any other `accent`-reading scripts: read the per-workspace hex from the SSE payload as today; no derivation fallback. Drop the `mindsAccent.get` callsites.
- **Onboarding picker** in `apps/minds/imbue/minds/desktop_client/templates/pages/Create.jinja`: a new form field below the name + Create row, above the collapsed configuration section. Renders 12 swatches as `<button>` elements (radio-like single-select) in a `role="radiogroup"` container, with a hidden `color` input the form POSTs. The submit handler attaches the picked hex to the existing form submission so `agent_creator` receives it and writes the label.
- **Settings picker** in `apps/minds/imbue/minds/desktop_client/templates/pages/WorkspaceSettings.jinja`: a new section above the existing Account section. Renders the same 12 swatches plus a `<input type="text">` for the hex. Inline JS handles: click-swatch-to-fill-input, input-change live validation, debounce + POST to `/api/workspaces/{agent_id}/color` on blur (or immediately for swatch picks), live error display (covers invalid hex, stale provider, host unreachable — all surfaced inline near the field), and (on success) no further UI action since SSE drives the visual update. Picker controls disable when the workspace is `is_stale=true`.
- **Static styling**: a new CSS block (in `tokens.css` or a new `palette.css`) for the swatch component — fixed size, hover/focus rings, selected-state ring. Swatches have no visible labels per the Q&A; aria-labels carry the palette name for screen readers.
- **Tests** to add/update (high-signal only):
  - Unit: the WCAG `pickForeground(hex)` function (table of inputs covering each palette entry and a few customs).
  - Unit: hex normalization helper (accepts each input form, rejects junk).
  - Unit: guard that `static/workspace_accent.js` keeps exporting the two runtime helpers and never reintroduces a JS palette mirror.
  - Unit: `_build_workspace_list` emits the stored color and matching `accent_fg`.
  - Integration: settings POST endpoint validates, shells out to `mngr label`, updates resolver snapshot optimistically, fires `notify_change()`; SSE round-trip propagates within one tick.
  - Integration: settings POST endpoint refuses to write to a non-primary agent (gates on `is_primary=true`).
  - Integration: settings POST endpoint surfaces `stale_provider` / `host_unreachable` errors distinctly.
  - Integration: `Create.jinja` submit carries the picked color into the new agent's labels.
  - Integration: defensive read — a malformed stored `color` value resolves to `confusion` at read time without crashing the SSE stream.
  - Integration: `_build_workspace_list` falls back to `DEFAULT_WORKSPACE_COLOR` for primary agents lacking the `color` label (renderer-side default; no `mngr label` write).
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
  - Proactive `mngr label color=#0b292b` backfill for pre-existing un-labeled workspaces (the renderer-side default covers the user-visible case; a background-task backfill from `MngrCliBackendResolver.update_agents` is feasible if some future feature needs the label to exist explicitly for all workspaces).
