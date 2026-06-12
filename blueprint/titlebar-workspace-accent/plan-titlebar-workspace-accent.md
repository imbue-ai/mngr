# Plan: Titlebar workspace accent

## Refined prompt

Currently the workspace accent in the titlebar is rendered like a small swatch next to the title. I want instead to render the whole top bar using that color and to round out the edges below: https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=514-4575&t=D0X7nv6rcuGNEKhz-11

* Scope: only the colored top bar + rounded edges below — chevron dropdown title, tab strip, and reduced icon set in the Figma are out of scope
* "Round out the edges below" means rounding the top corners of the content area so the accent color shows through behind the curves
* Top-bar background uses `oklch(80% 0.1 <hue>)` (lighter / less-saturated than the existing `oklch(65% 0.15 <hue>)`); hash-derived 360° hue retained for now
* At first launch (no workspace ever opened) the top bar stays black; once any workspace has been opened, the bar keeps that workspace's color even after navigating away — only opening a different workspace changes it
* Top-bar foreground must adapt for contrast (future user-chosen accents may be dark)
* Persist `lastWorkspaceAgentId` by extending the existing `~/.minds/window-state.json` (wrap the current array in an object); read/write via kebab-case IPC. If the renderer is in an active workspace but no stored value exists, derive from current agent id on the fly AND send IPC to persist
* Cold-start flash is masked by the existing loading screen (`electron/main.js:1646-1771`); accept async IPC, no pre-injection
* Round content top corners in Electron via a host-page overlay below the titlebar with two inward-rounded corner cutouts and `pointer-events: none`; fall back to native `WebContentsView.setBorderRadius` only if the overlay causes click issues
* Foreground via single CSS variable `--titlebar-fg = 0 0 0 | 255 255 255` chosen by OKLCH L >= 0.5; all top-bar text/icon/hover-tints use `rgb(var(--titlebar-fg) / <alpha>)`
* Adapt all top-bar foreground (title, icons, account button, min/max controls); close button keeps `hover:bg-red-600`; requests-badge red dot keeps `bg-red-500` (semantic urgency cues)
* Remove `<span id="title-swatch">` and remove the `.page-workspace::before` 3px stripe — redundant with the colored bar
* Other `--workspace-accent` consumers (sidebar-item spines, `.accent-spine`) move to `oklch(80% 0.1 <hue>)`
* Content corner radius bumped to 16px and applied to both browser-mode iframe and Electron overlay cutouts
* Sidebar panel (`#sidebar-panel`) stays `bg-zinc-900` — not part of the titlebar treatment
* Drop the `border-b border-white/10` seam between titlebar and content; keep `--shadow-seam`
* Clear stored accent on: (1) different workspace opened, (2) the stored workspace is deleted (SSE `destroying_agent_ids`), (3) account-level sign-out (SSE `auth_status` with `signedIn: false`)
* Testing: unit-test the contrast picker (pure function); integration-test IPC round-trip + JSON persistence (write / read / clear on each of the 3 trigger events)
* Contrast-picker logic lives only on the client (renderer) — server doesn't currently need foreground info
* Defer to a follow-up PR: 12-color named palette (11 from Figma + white) and settings-page UI with hex input — current PR's infra is forward-compatible

## Overview

- Replace the tiny `w-2.5 h-2.5` workspace-color swatch in the titlebar with a full-width colored top bar.
- Lighten the accent (`oklch(80% 0.1 <hue>)`) so a 38px-tall band reads as a calm chrome surface, not a saturated highlight.
- Add rounded top corners to the content area below the bar (16px), with an Electron-specific overlay since the content there is a `WebContentsView`, not a DOM iframe.
- Persist "most recently opened workspace" in Electron main so the accent survives navigating to Home (only changes when the user opens a different workspace), with cleanup on workspace deletion and account sign-out.
- Introduce a single `--titlebar-fg` foreground variable (black or white, chosen by accent lightness) so all titlebar text/icons stay legible and the system is ready for user-chosen accent colors later.
- Remove redundant accent affordances (small swatch, in-content 3px top stripe).

## Expected behavior

- **First launch, no workspace ever opened**: top bar is black (`bg-zinc-900`), foreground is light (`white` text/icons), identical to today minus the small swatch.
- **User opens a workspace**: top bar transitions to `oklch(80% 0.1 <hue>)` using the agent-id-derived hue. Foreground stays light (since L = 0.8 picks `black` foreground via the new contrast picker — once the picker outputs `0 0 0`, all icons/title/account button render black with varying alpha for hierarchy). Hover tints flip from `white/5` to `black/5`.
- **User navigates to Home from a workspace**: top bar keeps the workspace's color (does not revert to black). The home page renders normally underneath.
- **User opens a different workspace**: top bar transitions to that workspace's color.
- **User deletes the workspace whose color is currently shown**: top bar reverts to black on next render (main observes SSE `destroying_agent_ids`, clears the stored value, broadcasts `current-workspace-changed` with `null`).
- **User signs out**: top bar reverts to black; the sign-in page renders with the default dark chrome.
- **Content area corners**: bottom of the colored bar visually transitions into the content via a 16px rounded radius — in browser mode the content iframe has `rounded-2xl` (16px) top corners; in Electron mode an overlay attached to the chrome page's titlebar draws two inward-curving SVG cutouts at the bottom-left and bottom-right of the bar that visually round the top of the WebContentsView behind it.
- **macOS traffic lights**: unchanged (OS-drawn). The `pl-[72px]` left padding remains, ensuring they have clear space; the colored bar paints behind them.
- **Windows/Linux window controls**: min/max icons inherit `--titlebar-fg` (currentColor on SVG); close button keeps its red hover (`hover:bg-red-600` with white icon).
- **Requests badge**: still `bg-red-500` regardless of accent.
- **Sidebar panel**: still `bg-zinc-900`. When slid in from the left, it visually butts against the colored bar with no border (the `border-b border-white/10` seam is removed).
- **Content pages inside workspaces**: no more 3px top stripe (`.page-workspace::before` removed). Workspace identity is conveyed by the chrome bar above.
- **Cold-start flash**: not visible because the loading screen (`shell.html`) covers the full window until the chrome page has loaded and the stored accent has been applied via IPC.
- **Workspace-switch flash**: brief crossfade from old color to new on the `current-workspace-changed` event (CSS transition on `background-color`); acceptable.

## Implementation plan

### 1. Electron main — persistence + IPC + SSE triggers
**File: `apps/minds/electron/main.js`**

- **Schema migration for `~/.minds/window-state.json`**: currently an array `[{url, x, y, width, height, displayId}, ...]` (see `saveSessionState`, lines 975-1000). Wrap into `{ windows: [...], lastWorkspaceAgentId: string | null }`. On read, accept either the new shape OR the legacy array shape (treat array as `{ windows: array, lastWorkspaceAgentId: null }`).
- **New functions**:
  - `getLastWorkspaceAgentId(): string | null` — reads the field on demand from in-memory state.
  - `setLastWorkspaceAgentId(agentId: string | null): void` — updates in-memory state, calls existing `saveSessionState()`.
- **New IPC channels** (kebab-case, matching existing convention):
  - `ipcMain.handle('get-last-workspace-agent-id', () => getLastWorkspaceAgentId())` — async retrieve.
  - `ipcMain.on('set-last-workspace-agent-id', (_, agentId) => setLastWorkspaceAgentId(agentId))` — fire-and-forget update.
- **Wire to `current-workspace-changed`**: inside the existing `bundle.currentWorkspaceId !== newAgentId` branch (~line 456) and `sendCurrentWorkspaceToBundleViews` (lines 792-804), call `setLastWorkspaceAgentId(newAgentId)` when `newAgentId` is non-null.
- **Wire to SSE `destroying_agent_ids`** (in `handleChromeSSEEvent`, around line 1068-1072): after the existing `everSeenDestroying` set update, check whether any destroyed id matches `getLastWorkspaceAgentId()`. If so, call `setLastWorkspaceAgentId(null)` and broadcast a `current-workspace-changed` IPC with `null` to chrome views so the renderer clears the accent.
- **Wire to SSE `auth_status`** (line 1104-1105): if `evt.signed_in === false` and the previously-cached state had `signed_in === true`, call `setLastWorkspaceAgentId(null)` and broadcast `current-workspace-changed` with `null`.

### 2. Electron preload — expose new IPC surface
**File: `apps/minds/electron/preload.js`**

- Add to the `contextBridge.exposeInMainWorld('minds', { ... })` surface:
  - `getLastWorkspaceAgentId(): Promise<string | null>` — `ipcRenderer.invoke('get-last-workspace-agent-id')`.
  - `setLastWorkspaceAgentId(agentId)` — `ipcRenderer.send('set-last-workspace-agent-id', agentId)`.

### 3. Client-side accent + contrast picker
**File: `apps/minds/imbue/minds/desktop_client/static/workspace_accent.js`**

- Change `compute()` to return `oklch(80% 0.1 <hue>)` (was `65% 0.15`).
- Export an additional helper `pickForeground(oklchL): '0 0 0' | '255 255 255'`:
  - Returns `'0 0 0'` if `oklchL >= 0.5`, else `'255 255 255'`.
  - Since the new default is `L = 0.8`, this returns `'0 0 0'` (black) for hash-derived accents.
- Export a `getForegroundForAgentId(agentId, cb)` helper that resolves to `'0 0 0'` or `'255 255 255'` (mirrors `get()` shape). Convenience for chrome.js consumers.
- Surface: `window.mindsAccent = { get, pickForeground, getForeground }`.

### 4. Chrome page — colored top bar + foreground variable
**File: `apps/minds/imbue/minds/desktop_client/templates/pages/Chrome.jinja`**

- **Titlebar element**:
  - Remove `bg-zinc-900` from `#minds-titlebar` (line 41) — background now driven by CSS variable.
  - Remove `border-b border-white/10` (drop seam per design).
  - Inline style: `background-color: var(--titlebar-bg, #18181b); color: rgb(var(--titlebar-fg, 255 255 255));` (zinc-900 fallback).
- **Title swatch**: remove the entire `<span id="title-swatch" class="accent-swatch ...">` element (line 49).
- **Page title text**: switch `text-zinc-200` to `text-[rgb(var(--titlebar-fg)/0.85)]` (or equivalent inline style).
- **Account button (`#user-btn`)**: switch `text-zinc-400 hover:bg-white/5 hover:text-zinc-200` to use the variable: `text-[rgb(var(--titlebar-fg)/0.6)] hover:bg-[rgb(var(--titlebar-fg)/0.06)] hover:text-[rgb(var(--titlebar-fg)/0.9)]`.
- **Requests badge**: leaves as `bg-red-500`. The wrapping `TitlebarButton` adapts via variable.
- **Sidebar panel (`#sidebar-panel`)**: explicitly stays `bg-zinc-900`; no change needed to its class set, just verify.
- **Overlay for rounded content corners** (new, after titlebar, before iframe):
  - `<div id="content-corner-overlay" class="fixed left-0 right-0 pointer-events-none z-[99]" style="top: 38px; height: 16px;">`
  - Inside: two SVG paths (or CSS pseudo-elements) drawing the inverse-rounded corner — i.e. a shape that fills the top-left and top-right 16x16 boxes with the titlebar color, leaving a quarter-circle cutout that exposes content behind.
  - Implementation detail: simplest is two absolutely-positioned `<div>`s at `top:0; left:0; width:16px; height:16px; background: currentColor; mask-image: radial-gradient(circle 16px at bottom right, transparent 16px, black 17px);` — and the mirror on the right side. The overlay element inherits the titlebar color via CSS variable so it's always the same shade.
  - `pointer-events: none` ensures clicks pass through to the WebContentsView below.

### 5. CSS tokens — `--titlebar-bg`, `--titlebar-fg`, updated accent
**File: `apps/minds/imbue/minds/desktop_client/static/tokens.css`**

- Remove the `.accent-swatch` rule (line 72-74) — element no longer exists.
- Remove the `.page-workspace::before` 3px stripe (line 31-40) — replaced by chrome bar.
- Update `.accent-spine::before` (line 42-50): change default fallback from `oklch(65% 0.15 230)` to `oklch(80% 0.1 230)`.
- Update `.sidebar-item::before` (line 52-70): same fallback update.
- Add new defaults in `:root`:
  - `--titlebar-bg: #18181b;` (zinc-900) — overridden per workspace via JS.
  - `--titlebar-fg: 255 255 255;` (RGB triple, no `rgb()` wrapper, so consumers can supply alpha) — overridden per workspace.

### 6. Chrome JS — wire up the accent application
**File: `apps/minds/imbue/minds/desktop_client/static/chrome.js`**

- **Rewrite `applyTitleSwatch` -> `applyTitleAccent`** (lines 57-80):
  - Drop the swatch element manipulation (element no longer exists).
  - Set `--titlebar-bg` to the color returned by `mindsAccent.get(agentId, ...)` (or to `#18181b` if `agentId` is `null`).
  - Set `--titlebar-fg` to `mindsAccent.getForeground(agentId, ...)` (or to `255 255 255` if `agentId` is `null`).
  - Persist via IPC: when `agentId` is non-null and differs from previous, call `window.minds.setLastWorkspaceAgentId(agentId)` (Electron only; guard with `if (window.minds)` for browser mode).
- **Bootstrap on chrome page load**:
  - On `DOMContentLoaded`, before any workspace events arrive, call `window.minds.getLastWorkspaceAgentId()` (Electron); on resolve, if non-null AND the user is currently in an active workspace context, apply the accent. If the renderer detects the user is in an active workspace (via URL or initial state) but main has no stored value, call `applyTitleAccent` with the current agent id (which also persists per (3) above).
- **Listener**: existing `window.minds.onCurrentWorkspaceChanged` (line 163-165) already calls `applyTitleSwatch`; rename to `applyTitleAccent`. Handles both new-workspace transitions and the null-broadcast-from-main case (workspace deleted, user signed out).
- **Browser-mode polling fallback** (line 173): same rename, no persistence (no IPC available in browser).

### 7. TitlebarButton — make icon colors variable-driven
**File: `apps/minds/imbue/minds/desktop_client/templates/TitlebarButton.jinja`**

- Replace `text-zinc-400` with `text-[rgb(var(--titlebar-fg)/0.55)]`.
- Replace `hover:text-zinc-200 hover:bg-white/5` (default tone) with `hover:text-[rgb(var(--titlebar-fg)/0.95)] hover:bg-[rgb(var(--titlebar-fg)/0.06)]`.
- Replace `active:bg-white/10` with `active:bg-[rgb(var(--titlebar-fg)/0.10)]`.
- Keep the `danger` tone as-is (`hover:bg-red-600 hover:text-white`) — close button stays red on Win/Linux.
- Icons inside use `currentColor` already (`Icon24.jinja`, `Icon12.jinja`) — no changes needed there.

### 8. Browser-mode iframe corner radius
**File: `apps/minds/imbue/minds/desktop_client/templates/pages/Chrome.jinja` (line 80)**

- Change `rounded-xl` to `rounded-2xl` (12px -> 16px) on the `#content-frame` iframe so browser-mode users see the same radius as the Electron overlay cutouts.

### 9. Tests
**File: `apps/minds/imbue/minds/desktop_client/test_chrome.py` (new or existing acceptance/integration test)**

- Pure-function unit test for the contrast picker (since the JS function is identity logic, implement and unit-test the equivalent in Python if there's a server-side mirror; otherwise add a JS unit test using the existing test harness — verify what's in the project first).
- Integration test that:
  - Spins up the Electron main with a temp `~/.minds/window-state.json`.
  - Asserts `getLastWorkspaceAgentId` returns `null` initially.
  - Calls `setLastWorkspaceAgentId('agent-abc')`, asserts the file on disk contains the new field, asserts subsequent `get` returns `'agent-abc'`.
  - Simulates SSE `destroying_agent_ids: ['agent-abc']`, asserts stored value clears.
  - Simulates SSE `auth_status: {signed_in: false}`, asserts stored value clears.
- If the Minds project doesn't already have an Electron-main integration test harness, follow the existing test patterns under `apps/minds/` (check `apps/minds/imbue/minds/test_*.py` for shape).

### 10. Changelog entry
**File: `apps/minds/changelog/<branch-name>.md`** (per CLAUDE.md requirements)

- One short user-facing note describing the colored titlebar + rounded edges + persistence.

## Implementation phases

Each phase ends with a working app (incomplete in scope but not broken).

**Phase 1 — Accent value + foreground variable (no persistence, no rounding yet)**
- Change `workspace_accent.js` to output `oklch(80% 0.1 <hue>)`, add `pickForeground` + `getForeground`.
- Add `--titlebar-bg` and `--titlebar-fg` defaults to `tokens.css`.
- Rewrite `applyTitleSwatch` -> `applyTitleAccent` to set both variables (in-memory only, no IPC).
- Remove `#title-swatch` from `Chrome.jinja`, update titlebar element to consume the variables.
- Update `TitlebarButton.jinja` to consume `--titlebar-fg`.
- Update page-title text + account button to consume `--titlebar-fg`.
- Remove `.page-workspace::before` 3px stripe and `.accent-swatch` from `tokens.css`.
- Update `.accent-spine::before` and `.sidebar-item::before` defaults to `oklch(80% 0.1 230)`.
- **Result**: opening a workspace colors the whole bar, foreground flips for contrast, but the color doesn't persist across navigation away from the workspace. Edges below are still flat.

**Phase 2 — Persistence via Electron main**
- Migrate `window-state.json` schema (array -> object with `windows` + `lastWorkspaceAgentId`).
- Add `get-last-workspace-agent-id` / `set-last-workspace-agent-id` IPC + preload surface.
- Wire `current-workspace-changed` (main side) to call `setLastWorkspaceAgentId`.
- Wire SSE handlers for `destroying_agent_ids` (matching stored agent) and `auth_status` (signed_in false) to clear stored + broadcast null.
- Bootstrap chrome.js on `DOMContentLoaded` to fetch stored agent and apply accent. Add "derive on the fly + persist" fallback for active-workspace-with-no-stored case.
- **Result**: color survives navigating to Home, clears appropriately on workspace deletion / sign-out / different workspace opened.

**Phase 3 — Rounded edges below**
- Browser mode: change `#content-frame` iframe class from `rounded-xl` to `rounded-2xl`.
- Electron mode: add `#content-corner-overlay` to `Chrome.jinja` with two inverse-rounded SVG/CSS cutouts inheriting `--titlebar-bg`, `pointer-events: none`. Drop the `border-b border-white/10` seam.
- Visually verify: hover/click in the corner regions still hits the underlying WebContentsView (since `pointer-events: none`).
- Fallback path (if overlay clicks fail to pass through reliably): implement native `WebContentsView.setBorderRadius` in `main.js` instead.
- **Result**: design matches Figma.

**Phase 4 — Tests + changelog**
- Add unit test for `pickForeground`.
- Add integration test for persistence + SSE-driven clears.
- Add changelog entry.
- Run full `just test-offload`.
- **Result**: ready to merge.

## Testing strategy

- **Unit tests**:
  - `pickForeground(L) -> '0 0 0' | '255 255 255'`: assert thresholds at L = 0.5 (returns `'0 0 0'`), L = 0.4999 (returns `'255 255 255'`), L = 1.0 (returns `'0 0 0'`), L = 0.0 (returns `'255 255 255'`).
  - `compute(agentId) -> oklch string`: assert known agent-id-to-hue mapping, assert L = 80 and C = 0.1 are baked in.
- **Integration tests** (Electron main + on-disk persistence):
  - Fresh start (empty `window-state.json`): `get-last-workspace-agent-id` returns `null`.
  - Set then get: round-trip persists via the existing `saveSessionState` flush.
  - Legacy schema compatibility: a `window-state.json` containing the old array form is read as `{windows: array, lastWorkspaceAgentId: null}`.
  - SSE `destroying_agent_ids: [stored]` clears the stored value.
  - SSE `auth_status: {signed_in: false}` clears the stored value (only on the true->false transition).
  - SSE `auth_status` with no transition does NOT clear.
- **Manual verification** (per CLAUDE.md "Manual verification" requirements):
  - Cold start with no prior state: titlebar is black.
  - Open workspace A: titlebar adopts color, foreground contrast looks correct.
  - Navigate Home: titlebar keeps color A.
  - Open workspace B: titlebar adopts color B.
  - Delete workspace B from within the app: titlebar reverts to black on the next render.
  - Sign out from account menu: titlebar reverts to black.
  - Sign back in to the same account: titlebar stays black until a workspace is reopened.
  - Verify on macOS, Linux, and (if possible) Windows that traffic lights / window controls render correctly against light pastel accents.
  - Verify Electron overlay cutouts don't block clicks (use the affordances in the top-left/top-right corner regions of the content area).
- **Edge cases**:
  - Agent id producing an extreme hue (e.g. red ~0deg) — verify both light-mode (black foreground) and the contrast logic.
  - Many rapid workspace switches — verify no IPC pile-up and the CSS transition isn't janky.
  - Window resize during a workspace switch — verify overlay corners stay aligned.
  - Browser mode (no Electron) — verify accent works in-memory (no IPC), foreground variable applies, no errors logged for missing `window.minds`.
- **Ratchet check**: run the affected projects' `test_ratchets.py` after edits to make sure no new violations.

## Open questions

- **Electron overlay vs native border-radius**: the host-page overlay approach is preferred (Q7b), but if there's a real click-pass-through issue we fall back to `WebContentsView.setBorderRadius` (Q7a). The fallback's availability depends on the Electron version in use — confirm during Phase 3 and pick.
- **Auth status transition detection**: clearing on sign-out requires detecting the true->false transition (not just `signed_in === false` at any time, which would also fire while signed-out). Implementation will hold the previous value in `latestChromeState.authStatus.signed_in` and compare. Confirm this is robust against the initial SSE snapshot.
- **Schema migration safety**: existing users have an array-shaped `window-state.json`. The migration just wraps it on first read; the write path always emits the new shape. Confirm no concurrent writers can corrupt the file mid-migration (the existing code uses synchronous `fs.writeFileSync`, so it should be safe).
- **Contrast threshold tuning**: `L >= 0.5` is a first cut. For palette colors arriving in the follow-up PR, some named colors (e.g. `Belonging #E8A7A8` at L ~0.79) clearly want black foreground; some (e.g. `Confusion #0B292B` at L ~0.20) clearly want white. The 0.5 threshold seems safe but may need a perceptual contrast check (WCAG AA) when custom hex input lands. Defer until needed.
- **Loading-screen accent**: should `shell.html` (the loading screen) also adopt the accent color if there's a known last workspace? Currently the loading screen has its own styling and is shown before the chrome page loads. Probably no — it should stay neutral to avoid colored flash mismatching the resolved accent. Confirm during Phase 2 manual verification.
- **Page-title text**: today the renderer sets `#page-title` to per-workspace text. With a colored bar, do we want any change to the title text itself? Per Q23a: no. Confirmed.
- **Tailwind arbitrary value compilation**: the project uses Tailwind Play CDN JS (per `tokens.css:5-6` comment) which generates classes at runtime. Verify that arbitrary value classes like `text-[rgb(var(--titlebar-fg)/0.85)]` work correctly with this setup. If not, fall back to inline styles or a thin custom CSS class.
