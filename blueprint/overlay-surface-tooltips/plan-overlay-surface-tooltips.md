# Overlay surface and custom tooltips

## Overview

- The minds desktop client has three `WebContentsView` surfaces (chrome, content, modalView). The top `modalView` is the only one above both chrome and content, but it is full-window, pointer-capturing, and loads a fresh page on every open -- too slow and too greedy with pointer events to host hover tooltips.

- Decision: turn `modalView` into a single **always-warm overlay surface** that hosts many independent in-page DOM overlays (tooltips, popovers, modals) at once, instead of spawning a new view per overlay. Spawning a `WebContentsView` per overlay is rejected: each is a full Chromium `WebContents` (own process, tens of MB, a page-load of latency) -- the exact cost the "keep to three surfaces" rule exists to avoid.

- Constraint (verified against Electron 40): `setIgnoreMouseEvents` is **window-level only** (`BrowserWindow`/`BaseWindow`), not per-`WebContentsView`. The `View` base class exposes only `setBounds`/`getBounds`/`setVisible`/`getVisible`/`setBackgroundColor`/`setBorderRadius`/child-view ops, and a view's bounds rect captures clicks regardless of what it paints. So per-view click-through is impossible; the only lever is the view's bounds.

- Decision: pointer events are handled by **dynamically sizing the overlay view's bounds** (the only available mechanism). When only display tooltips are visible, the overlay view shrinks to just the bounding rect of those tooltips, so everywhere else chrome and content keep every event. When any capturing overlay (modal / sidebar-with-backdrop / inbox / help / sign-in) is open, it expands to full-window exactly as today. A tooltip is display-only and hides on its trigger's mouse-leave (detected in the view that owns the trigger), so its small captured rect is harmless. This is what answers the original "resize" worry: we resize the tiny overlay view (instant `setBounds`), never the content view.

- Decision: speed comes from keeping the surface warm. The host page loads once at window creation and stays mounted; tooltips are shown/hidden by driving DOM over IPC, never by a page load. Migrated modals are hosted as iframes inside the surface but mounted on demand (created on open, destroyed on close) -- implementation reversed the original "kept-warm iframe" idea because keeping them warm bought nothing (every show reloads anyway) and left hidden pages doing background work. On show they load fresh, re-fetching state and replaying their entry animation.

- Scope: v1 ships custom tooltips for titlebar buttons only (replacing native `title=` there) and migrates the existing sidebar / inbox / help / sign-in overlays onto the new surface with their behavior preserved 1:1. The interactive popover (e.g. a profile card) is designed-for in the manager but not shipped in v1.

## Expected behavior

- Hovering (or keyboard-focusing) a titlebar button shows a custom-styled tooltip after a ~150ms delay (configurable per trigger); it hides immediately on mouse-leave or blur.

- Titlebar tooltips render above the content area -- they can drop below the 38px titlebar over the workspace, which native `title=` and the chrome view cannot do today.

- While only a tooltip is showing, the overlay view is shrunk to just the tooltip's rectangle, so clicks, hovers, and drags everywhere else on the chrome titlebar and the workspace content behave exactly as before.

- Tooltips display rich but static content (multi-line text, keyboard-shortcut chips); they are never interactive and never capture the pointer.

- The sidebar, inbox, help, and sign-in overlays look and behave exactly as they do today:
  - Sidebar: anchored popover under its trigger, transparent catcher (no scrim), toggles open/closed, auto-closes on item select.

  - Inbox: left-edge drawer with a dimmed scrim and slide+fade, toggles, auto-opens on a new pending request, keeps its window-drag strips.

  - Help: centered dimmed modal, toggles.

  - Sign-in: centered dimmed modal, open-only (no toggle), auto-closes on successful sign-in.

- Opening any of these mounts its iframe on demand onto the always-warm overlay surface; each open loads fresh, re-fetching its state and replaying its entry animation, so it feels "fresh every time."

- Multiple overlays can be visible at once and stack in strict open order (the most recently opened is on top); a tooltip raised while a modal is open appears above it.

- Overlays anchor to their trigger and auto-flip/shift to stay on-screen; no caret/arrow in v1.

- Escape, outside/backdrop clicks, and per-kind rules dismiss overlays as before; window-dragging continues to work (the titlebar drag region is dropped whenever a capturing overlay is open).

- When a capturing overlay (modal/popover/drawer) is open, the surface captures pointer events over its interactive region exactly as the modals do today, so backdrop-dismiss and in-overlay interaction are unchanged.

## Changes

- **Overlay surface lifecycle**: `modalView` is created and its host page loaded once at window creation, then kept present for the window's life (hidden while idle, since a visible full-window view would capture every pointer event and per-view click-through does not exist; shown only when an overlay or tooltip is up) instead of being created lazily and `loadURL`'d per open. The error/loading/quitting takeover still collapses it.

- **Pointer model**: the overlay manager computes the bounding rect of all visible overlays (or full-window if any is a modal/backdrop overlay) and reports it to main, which calls `modalView.setBounds(rect)`. The page offsets each overlay by the current view origin so it renders correctly within the shrunk viewport. To size a tooltip, the manager measures it in the full-window-but-invisible page, then shrinks the view to the measured rect before showing it. No `setIgnoreMouseEvents` is used (it does not exist per-view).

- **In-page overlay manager**: a new vanilla-JS manager on the host page owns the registry of open overlays, their stacking (strict open order), anchored positioning with auto-flip/shift, per-kind dismissal (tooltips on mouse-leave / window-blur / scroll; popovers on outside-click or Escape; modals on backdrop-click or Escape), and the overlay view's required bounds for tooltips. As shipped, main owns modal visibility directly (modals are full-window); the manager owns which overlay is on screen and the tooltip rect the view shrinks to.

- **Tooltip API**: an imperative per-trigger registration call (content + behavior) usable from the chrome renderer and the minds modal pages. Content accepts either a structured payload rendered by a styled template (the common case) or an arbitrary HTML snippet. Chrome triggers reuse the existing rect-over-IPC anchor mechanism the sidebar already uses; same-surface triggers position in-page directly.

- **Titlebar tooltips**: titlebar buttons drop their native `title=` attributes and register custom tooltips instead, shown on hover and keyboard focus. No other surface (badges, workspace rows, content view) gets custom tooltips in v1; the native right-click context menu is left as-is.

- **Modal migration**: sidebar, inbox, help, and sign-in move from per-open `loadURL` pages into iframes hosted by the surface, mounted on demand (created on open, destroyed on close). On show they load fresh (re-fetch state, replay entry animation). Their triggering, positioning, backdrop, dismissal, and animation are preserved 1:1. The inbox is modeled as a modal-kind overlay with a drawer geometry/position option rather than a distinct kind.

- **Dismissal ownership**: Escape and all dismissal move fully into the overlay manager; the main process no longer intercepts Escape on the surface (removing today's main-level backstop). The unrelated `registerShortcutsFor` shortcuts (devtools / cmd+Q / cmd+N) are untouched.

- **Window-drag preservation**: the existing `modal-state-changed` signal that drops the titlebar drag region is preserved and driven off "a capturing overlay is open"; the inbox keeps its own in-overlay drag strips.

- **Show/hide path**: overlays are shown and hidden by manipulating DOM on the warm surface over IPC, never by loading a page, so first paint is effectively instant.
