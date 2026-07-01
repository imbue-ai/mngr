# Migrate overlay modals from iframes to in-page JS

> **Migrate minds overlay modals from Electron iframes to in-page JS/DOM (commit-to-execute), structured so a modal can later move from server fragment to pure JS+JSON without reworking the host.**
> * Approach: fragments, not client-side JSON rendering — each modal is server-rendered as an HTML fragment the overlay host fetches and injects, reusing all existing server rendering.
> * Fragment delivery: same routes, a `?fragment=1` flag drops the `Base` wrapper so one template serves both the browser full page and the Electron fragment.
> * Host owns the shared chrome: backdrop, click-outside dismiss, Escape, and panel positioning are generic; each modal supplies only panel content + an anchor spec.
> * Freshness: re-fetch/rebuild on every open, discard DOM on close.
> * Visibility: collapse main's modal show/hide into the host's unified bounds channel (`full` / `rect` / `hidden`); retire main's separate `setVisible` path for modals.
> * B-friendly seam (minimal): each modal is a host-registry entry with `init(container)` / `destroy()`; content-source is a field, so a later JS+JSON migration swaps how `init` fills the container.
> * Escape (11b): `main.js` keeps the robust key capture and sends a `hide` command; the host owns teardown.
> * SSE (13a): the host holds one live state cache and modal `init` reads it — the priming handshake and per-frame fan-out are removed.
> * Open latency (18b): show the overlay view only after the fragment is fetched + injected (no empty-panel pop-in).
> * Fragments carry no `<script>`; per-modal JS moves to external modules loaded once with the host and registered by id.
> * Scope: migrate all four modals simplest-first (SignIn/Help → Sidebar → Inbox). Keep the 3-surface Electron model and no modal stacking. Browser path is dev-only and stays as full-page navigation; the browser workspace-menu inline floating menu is unchanged.

## Overview

- **Motivation.** The overlay surface hosts modals as mount-on-demand iframes, which forces ~117 LOC of iframe-only plumbing: per-frame IPC fan-out, an SSE priming/replay handshake, `nodeIntegrationInSubFrames`, per-iframe origin guards, and a front/back-buffer swap. Rendering modals as in-page DOM — the way tooltips already work on this exact surface — deletes that plumbing.
- **Chosen approach: fragments, not full JS+JSON.** Modals are already server-rendered; reusing that rendering (fetch an HTML fragment and inject it) is far less code and risk than serializing `RequestEvent` types to JSON and porting the inbox's per-request-type rendering to JS. Estimated net change ≈ −150 to −300 LOC, versus ≈ +500 to +1000 for full JS+JSON.
- **Structured to stay B-friendly.** Each modal becomes a uniform host-registry entry (`init(container)` / `destroy()`), with its content-source as a swappable field. A modal like Sidebar (already JSON+SSE-driven) can later move to pure JS+JSON by changing how `init` fills the container, without touching the host lifecycle.
- **Consolidate ownership in the host.** Backdrop, dismiss, Escape teardown, positioning, and view-visibility all become uniform host responsibilities instead of being re-implemented per modal page and split across `main.js`.
- **Preserve everything else.** Keep the 3-surface Electron model (chrome / content / overlay), one-modal-at-a-time behavior, tooltips-over-modals, and the dev-only browser path (full-page navigation; the browser workspace menu stays as its existing inline floating menu).

## Expected behavior

- Opening a modal (inbox / help / sign-in / workspace menu) in Electron shows it as in-page DOM on the overlay surface instead of an iframe; look and behavior are unchanged to the user.
- On open, the overlay view is shown only after its fragment is fetched and injected — no empty-panel flash; an open costs one fetch round-trip (comparable to today's iframe load).
- Modal content is always fresh: re-fetched and rebuilt on every open, torn down on close.
- Backdrop, click-outside dismiss, Escape, and panel positioning behave identically across all modals, now driven by the host rather than each page.
- Escape still closes the open modal reliably: `main.js` captures the key and sends a hide command; the host performs teardown — so it works regardless of renderer focus.
- Modal data (workspace list, request count/ids, auth state) is available immediately on open, read from the host's live SSE cache; no priming delay or missed-event race.
- Positioning is unchanged: the workspace menu anchors to its titlebar trigger; inbox / help / sign-in render as full-window backdrops with their drawer or centered panel.
- Tooltips are unaffected and continue to float above an open modal.
- The overlay surface remains a single `WebContentsView`; its visibility follows the host's unified bounds mode (`full` while a modal is open, `rect` for a bare tooltip, `hidden` otherwise).
- Browser (dev-only) behavior is unchanged: modals remain full-page navigations, and the workspace menu remains its existing inline menu.

## Changes

- **Overlay host state cache.** The overlay host subscribes to `chrome-event`s and keeps one live in-memory snapshot (workspaces, requests, auth) that any modal reads at open time — replacing the per-iframe subscribe-then-prime pattern.
- **Overlay host manager.** Replace the iframe mount / front-back-buffer swap with a fetch-fragment-then-inject flow, plus a small modal registry keyed by id. Each registry entry declares a positioning mode (`anchored-popover` vs `full-window-backdrop`) and exposes `init(container)` / `destroy()`; content-source is a field to keep the later JS+JSON path open.
- **Generic host chrome.** Move backdrop rendering, click-outside dismiss, Escape teardown, and panel positioning out of the individual modal pages and into the host, applied uniformly by positioning mode.
- **Modal templates.** Render as bare fragments (panel markup only — no `Base` wrapper, no inline `<script>`) when requested via a `?fragment=1` flag on the existing routes; retain the full-page render for the browser dev path.
- **Per-modal JS.** Extract the inline scripts (inbox, help) and page scripts (sign-in, workspace menu) into external modules loaded once with the host and registered by modal id; each exposes `init` / `destroy`.
- **Main process plumbing removal.** Remove the iframe-only machinery: per-frame IPC fan-out, the SSE priming/replay and `overlay-modal-loaded` handshake, `nodeIntegrationInSubFrames`, and per-iframe origin navigation guards.
- **Main process visibility.** Collapse modal show/hide into the host's bounds channel (`full` / `rect` / `hidden`), retiring the separate modal-visibility path; keep opening modals via IPC, and keep the Escape capture that sends a hide command.
- **Workspace menu (Electron).** Move from an iframe page to the host; its `init` builds rows from the host's SSE cache, as today. The browser inline menu is left unchanged.
- **Migration order.** Land the host infrastructure with the simplest modal first, then Help, then Sidebar, then Inbox; remove the now-dead iframe plumbing once all four are migrated.
- **Changelog.** Add per-project changelog entries (`apps/minds`, and `dev` if root-level files change) for the branch.
- **Tests.** Extend the desktop-client tests to cover the `?fragment=1` routes and the in-page modal open / close / dismiss / Escape flow; update or remove tests that assert iframe-based modal behavior.
