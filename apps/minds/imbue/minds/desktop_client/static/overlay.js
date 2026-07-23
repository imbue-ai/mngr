// Overlay manager for the always-warm overlay surface.
//
// This runs in the shared modal WebContentsView, which main.js loads ONCE with
// /_chrome/overlay at window creation and keeps mounted for the window's life
// (see createBundleOverlayView in electron/main.js). Instead of loading a fresh
// page per modal (the old openModal -> loadURL model), every overlay is hosted
// here as in-page DOM driven over IPC, so opens are instant.
//
// IPC contract (main -> host), delivered on window.minds.onOverlayCommand:
//   { type: 'show-modal', id, url }       -- mount a fresh modal iframe,
//                                            destroying any previously-shown one.
//   { type: 'hide-modal', id }            -- destroy the named modal iframe.
//   { type: 'hide-all' }                  -- destroy every modal (close/takeover).
//   { type: 'show-tooltip', rect, text,   -- render + position a tooltip bubble
//     shortcut?, html? }                     anchored to the trigger's rect.
//   { type: 'hide-tooltip' }              -- hide the tooltip.
//
// The host reports the overlay view's required bounds to main via
// window.minds.overlaySetBounds ({ mode: 'rect', rect } for a tooltip,
// { mode: 'hidden' } when none); modals are full-window and main owns their
// visibility directly (see openModal/closeModal).
//
// The migrated modal pages (workspace menu / inbox / help / sign-in) are
// first-party and served by the same origin as this host. The overlay view sets
// ``nodeIntegrationInSubFrames`` so the preload runs in each iframe and exposes
// ``window.minds`` there before the iframe's own scripts run -- their existing
// window.minds.*() calls and onChromeEvent subscriptions work unchanged. Main
// fans chrome-events / current-workspace / priming out per-frame (see
// sendToOverlayFrames in main.js), since webContents.send reaches only the top
// frame.
//
// While a modal is open the overlay view is shown full-window by main and
// captures pointer events (Electron 40 has no per-view click-through). For
// modals, the view's visibility/bounds are owned by main (openModal/closeModal)
// and this manager only decides which iframe is on screen; for tooltips, this
// manager measures the bubble and reports a small rect so the rest of the window
// stays interactive.

(function () {
  'use strict';

  var root = document.getElementById('overlay-root');
  if (!root || !window.minds) return;

  // Mount-on-demand modal iframes: each open creates a fresh iframe and each
  // close destroys it, so no hidden modal documents linger. Keeping them mounted
  // ("warm") bought nothing here -- every show reloads anyway -- while a hidden
  // warm page keeps doing background work (e.g. the workspace menu re-fetching
  // auth on content navigations). The incoming iframe is kept invisible until its
  // load paints, and the modal it replaces stays visible until that instant, so
  // switching modals is flash-free.
  var modalFrame = null; // the currently-visible modal iframe (front buffer)
  var incomingFrame = null; // a modal iframe still loading (back buffer)

  function destroyFrame(frame) {
    if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
  }

  function showModal(id, url) {
    // Drop any earlier incoming frame that never became visible -- this show
    // supersedes it, and it was never shown, so there's no flash.
    destroyFrame(incomingFrame);
    var frame = document.createElement('iframe');
    // Fill the host; the hosted page paints its own backdrop and positions its
    // own panel within the full window.
    frame.className = 'absolute inset-0 w-full h-full';
    frame.style.border = '0';
    frame.style.background = 'transparent';
    frame.style.display = 'block';
    // Invisible until its content paints; the previously-shown modal stays up
    // until then so the swap doesn't flash.
    frame.style.visibility = 'hidden';
    frame.setAttribute('data-overlay-id', id);
    frame.addEventListener('load', function () {
      // A fresh iframe fires a load for its initial about:blank document (on
      // insertion, before the real page navigates in). Ignore that one and only
      // act on the hosted page's own load -- otherwise the about:blank load would
      // run the swap early, null out incomingFrame, and the real load would then
      // see frame !== incomingFrame and destroy this frame.
      var href = '';
      try {
        href = frame.contentWindow.location.href;
      } catch (e) {
        href = '';
      }
      if (!href || href === 'about:blank') return;
      // Tell main the iframe is ready so it can replay the cached chrome state
      // (workspace list / request count) into it.
      window.minds.overlayModalLoaded(id);
      if (frame === modalFrame) {
        // The hosted page reloaded itself in place (e.g. the accounts modal
        // after a log-out). Keep it:
        // destroying here would blank the modal while main still holds the
        // overlay view visible, leaving an invisible layer eating every click.
        return;
      }
      if (frame !== incomingFrame) {
        // Superseded by a newer show before it finished loading; discard it.
        destroyFrame(frame);
        return;
      }
      frame.style.visibility = 'visible';
      destroyFrame(modalFrame); // swap: remove the modal this one replaced
      modalFrame = frame;
      incomingFrame = null;
    });
    frame.src = url;
    incomingFrame = frame;
    root.appendChild(frame);
  }

  function hideModal(id) {
    // main only sends 'hide-all' today, but honor a targeted hide too.
    if (modalFrame && modalFrame.getAttribute('data-overlay-id') === id) {
      destroyFrame(modalFrame);
      modalFrame = null;
    }
    if (incomingFrame && incomingFrame.getAttribute('data-overlay-id') === id) {
      destroyFrame(incomingFrame);
      incomingFrame = null;
    }
  }

  function hideAllModals() {
    destroyFrame(incomingFrame);
    destroyFrame(modalFrame);
    incomingFrame = null;
    modalFrame = null;
  }

  // -- Tooltips ----------------------------------------------------------
  //
  // A tooltip is display-only. Because Electron 40 has no per-view click-through,
  // we shrink the overlay view to just the tooltip's rectangle (reported via
  // overlaySetBounds) so everywhere else stays interactive. To size that rect we
  // render + measure the bubble in a context sized to the real window (passed by
  // main, since the hidden view's own innerWidth is unreliable -- see below),
  // then pin the bubble at the view's top-left and report the window-coordinate
  // rect; main shrinks the view to it and shows it. Hiding reports 'hidden' so
  // main restores the full-window (hidden) bounds.
  var TOOLTIP_MARGIN = 6; // min gap from the window edges
  var TOOLTIP_GAP = 6; // gap between the trigger and the bubble
  var tooltipEl = null;
  // True while the current tooltip is shown over an open modal -- the view is
  // already full-window then, so we position the bubble in-page and don't drive
  // the view's bounds (main ignores bounds reports while a modal is open).
  var tooltipInModal = false;

  function ensureTooltipEl() {
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement('div');
    // Appearance comes from the shared ``.minds-tooltip`` class in app.css --
    // the same class the in-page tooltip backend uses (see tooltip_triggers.js)
    // so both surfaces render an identical bubble (README's "shared across
    // files" case). Positioning is overlay-specific and set here: absolute
    // within #overlay-root, pinned above the modal iframe via z-index.
    tooltipEl.className = 'minds-tooltip';
    tooltipEl.style.position = 'absolute';
    tooltipEl.style.left = '0';
    tooltipEl.style.top = '0';
    tooltipEl.style.zIndex = '2147483647';
    tooltipEl.style.display = 'none';
    root.appendChild(tooltipEl);
    return tooltipEl;
  }

  function showTooltip(cmd) {
    var el = ensureTooltipEl();
    // Content: arbitrary HTML if supplied, else a plain text label. The payload
    // may carry a ``shortcut`` (a designed-for keyboard-shortcut chip), but no
    // trigger supplies one yet and the design system has no on-ramp size for a
    // sub-label chip, so it is not rendered; add it on-system when a real use
    // arrives.
    if (cmd.html) {
      el.innerHTML = cmd.html;
    } else {
      el.textContent = cmd.text || '';
    }
    // Use the real window size from main, NOT window.innerWidth. Between tooltips
    // the overlay view is hidden, and a hidden WebContentsView does not update
    // its page's innerWidth when main resizes it -- so innerWidth can be stale
    // (the previous tooltip's small rect), which would both squeeze the measured
    // bubble and clamp its position to the wrong edge.
    var vw = typeof cmd.windowWidth === 'number' && cmd.windowWidth > 0 ? cmd.windowWidth : window.innerWidth;
    var vh = typeof cmd.windowHeight === 'number' && cmd.windowHeight > 0 ? cmd.windowHeight : window.innerHeight;
    // Measure in a context as wide/tall as the real window so the bubble's
    // shrink-to-fit width isn't constrained by a stale, small view viewport.
    root.style.width = vw + 'px';
    root.style.height = vh + 'px';
    el.style.width = '';
    el.style.left = '0';
    el.style.top = '0';
    el.style.visibility = 'hidden';
    el.style.display = 'inline-flex';
    // Fractional border-box size (getBoundingClientRect), ceil'd -- NOT the
    // integer offsetWidth/Height. offsetWidth rounds the shrink-to-fit width
    // DOWN (e.g. 132.4 -> 132); fixing the width to that rounded value then
    // leaves the content a fraction short and wraps the last word. Ceil so the
    // fixed width (and the reported view-bounds rect) always covers the content.
    var m = el.getBoundingClientRect();
    var w = Math.ceil(m.width);
    var h = Math.ceil(m.height);
    root.style.width = '';
    root.style.height = '';
    var a = cmd.rect || { x: 0, y: 0, width: 0, height: 0 };
    // Centered under the trigger by default; flip above if it would overflow the
    // bottom; clamp horizontally to stay on-screen.
    var tx = a.x + a.width / 2 - w / 2;
    var ty = a.y + a.height + TOOLTIP_GAP;
    if (ty + h > vh - TOOLTIP_MARGIN) {
      var above = a.y - h - TOOLTIP_GAP;
      if (above >= TOOLTIP_MARGIN) ty = above;
    }
    if (tx + w > vw - TOOLTIP_MARGIN) tx = vw - TOOLTIP_MARGIN - w;
    if (tx < TOOLTIP_MARGIN) tx = TOOLTIP_MARGIN;
    if (ty < TOOLTIP_MARGIN) ty = TOOLTIP_MARGIN;
    // Fix the bubble's width so it doesn't reflow when the viewport changes.
    el.style.width = w + 'px';
    tooltipInModal = !!cmd.inModal;
    if (tooltipInModal) {
      // A modal owns the (full-window) view; place the bubble at its window
      // position in-page, above the modal iframe (via z-index). No bounds change.
      el.style.left = tx + 'px';
      el.style.top = ty + 'px';
      el.style.visibility = 'visible';
    } else {
      // No modal: pin the bubble at the view's top-left and shrink the view to
      // its rect so the rest of the window stays interactive.
      el.style.left = '0';
      el.style.top = '0';
      el.style.visibility = 'visible';
      window.minds.overlaySetBounds({
        mode: 'rect',
        rect: { x: tx, y: ty, width: w, height: h },
      });
    }
  }

  function hideTooltip() {
    if (tooltipEl) {
      tooltipEl.style.display = 'none';
      tooltipEl.style.visibility = 'hidden';
    }
    // Only restore the view's bounds when the tooltip drove them (no modal). When
    // shown over a modal, the modal owns the view -- leave it full-window.
    if (!tooltipInModal) window.minds.overlaySetBounds({ mode: 'hidden' });
    tooltipInModal = false;
  }

  window.minds.onOverlayCommand(function (cmd) {
    if (!cmd || typeof cmd !== 'object') return;
    if (cmd.type === 'show-modal' && cmd.id && cmd.url) showModal(cmd.id, cmd.url);
    else if (cmd.type === 'hide-modal' && cmd.id) hideModal(cmd.id);
    else if (cmd.type === 'hide-all') { hideAllModals(); hideTooltip(); }
    else if (cmd.type === 'show-tooltip') showTooltip(cmd);
    else if (cmd.type === 'hide-tooltip') hideTooltip();
  });

  // The dark-mode setting changed (persisted from some window's settings UI).
  // Freshly-mounted modal iframes pick the theme up server-side (Base.jinja),
  // so this only needs to flip the documents already on screen: the host page
  // itself plus any mounted modal iframe. Hosted pages come from the same
  // backend origin as this host, so their contentDocument is reachable; a
  // still-loading iframe may not expose one yet, which is fine -- its
  // server-rendered theme is already current.
  if (window.minds.onChromeEvent) {
    window.minds.onChromeEvent(function (data) {
      if (!data || data.type !== 'appearance') return;
      var isDark = !!data.is_dark;
      document.documentElement.classList.toggle('dark', isDark);
      [modalFrame, incomingFrame].forEach(function (frame) {
        if (!frame) return;
        try {
          frame.contentDocument.documentElement.classList.toggle('dark', isDark);
        } catch (e) { /* iframe not loaded yet */ }
      });
    });
  }
})();
