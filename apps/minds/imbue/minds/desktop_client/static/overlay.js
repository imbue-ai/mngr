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
// Modals are being migrated from iframes to in-page DOM (see the -- Modals --
// section below). A migrated modal (currently sign-in) registers itself in
// window.MINDS_OVERLAY_MODALS and is fetched as ``?fragment=1`` markup and
// injected here -- no iframe, no per-frame IPC. The not-yet-migrated modal pages
// (inbox / help / workspace menu) still mount as first-party same-origin
// iframes; the overlay view sets ``nodeIntegrationInSubFrames`` so the preload
// runs in each and exposes ``window.minds`` before the iframe's own scripts run,
// and main fans chrome-events / current-workspace / priming out per-frame (see
// sendToOverlayFrames in main.js), since webContents.send reaches only the top
// frame. Both the subframe integration and the per-frame fan-out go away once
// every modal is an in-page fragment.
//
// While a modal is open the overlay view is shown full-window by main and
// captures pointer events (Electron 40 has no per-view click-through). For
// modals, the view's visibility/bounds are owned by main (openModal/closeModal)
// and this manager decides what is on screen (an injected fragment or an
// iframe); for tooltips, this manager measures the bubble and reports a small
// rect so the rest of the window stays interactive.

(function () {
  'use strict';

  var root = document.getElementById('overlay-root');
  if (!root || !window.minds) return;

  // -- Modals -----------------------------------------------------------
  //
  // Two paths coexist during the iframe -> in-page migration:
  //
  //   * Fragment path (in-page DOM): a modal registered in
  //     window.MINDS_OVERLAY_MODALS (by a per-modal module script loaded in this
  //     host page, e.g. overlay_signin.js) is fetched as ``?fragment=1`` markup
  //     and injected here -- no iframe. The host owns the backdrop dismiss and
  //     calls the module's init(container) / destroy().
  //   * Legacy iframe path: a modal NOT in the registry still mounts a fresh
  //     iframe per open (inbox / help / workspace menu until each is migrated).
  //     The incoming iframe stays invisible until its load paints, and the modal
  //     it replaces stays visible until that instant, so switching is flash-free.
  //
  // At most one modal is shown at a time; opening one supersedes the other path.
  var modalFrame = null; // legacy path: the currently-visible modal iframe
  var incomingFrame = null; // legacy path: a modal iframe still loading
  var fragmentModal = null; // fragment path: { id, el, entry } of the injected modal
  var fragmentToken = 0; // supersede guard for in-flight fragment fetches

  function modalRegistry() {
    return window.MINDS_OVERLAY_MODALS || {};
  }

  // Supersede any in-flight fragment fetch: bump the token so a fetch started by
  // an earlier show won't mount itself when it finally resolves. Every command
  // that changes what should be on screen (a new show, a targeted hide, a
  // hide-all) must call this, or a slow fetch can mount a modal that was already
  // superseded or closed.
  function invalidateFragmentFetch() {
    fragmentToken++;
  }

  function destroyFrame(frame) {
    if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
  }

  function teardownFragmentModal() {
    if (!fragmentModal) return;
    var current = fragmentModal;
    fragmentModal = null;
    if (current.entry && typeof current.entry.destroy === 'function') {
      try { current.entry.destroy(); } catch (e) { /* noop */ }
    }
    if (current.el && current.el.parentNode) current.el.parentNode.removeChild(current.el);
  }

  // Host-owned dismiss: route through main so it hides the overlay view and fans
  // a hide-all back to us, keeping main's modal-open / titlebar-drag state in
  // sync (main handles Escape the same way).
  function requestCloseModal() {
    if (window.minds && window.minds.closeModal) window.minds.closeModal();
  }

  function showModal(id, url) {
    var entry = modalRegistry()[id];
    if (entry) showFragmentModal(id, url, entry);
    else showIframeModal(id, url);
  }

  function showFragmentModal(id, url, entry) {
    // Supersede anything currently shown (a fragment, or a still-loading iframe).
    teardownFragmentModal();
    destroyFrame(incomingFrame);
    incomingFrame = null;
    var separator = url.indexOf('?') === -1 ? '?' : '&';
    invalidateFragmentFetch();
    var token = fragmentToken;
    fetch(url + separator + 'fragment=1', { credentials: 'same-origin' })
      .then(function (response) { return response.text(); })
      .then(function (html) {
        // A newer show (or a close) superseded this fetch before it resolved.
        if (token !== fragmentToken) return;
        mountFragmentModal(id, entry, html);
      })
      .catch(function () { /* leave nothing shown; the open simply fails */ });
  }

  function mountFragmentModal(id, entry, html) {
    // Drop any legacy iframe still on screen now that the fragment is in hand
    // (18b: only reveal once the markup is ready, so there's no empty panel).
    destroyFrame(modalFrame);
    modalFrame = null;
    var container = document.createElement('div');
    container.className = 'absolute inset-0';
    container.setAttribute('data-overlay-id', id);
    container.innerHTML = html;
    // Backdrop-mode modals paint a full-window backdrop as their outermost
    // element; a click landing on it (outside the panel) dismisses the modal.
    if ((entry.positioning || 'backdrop') === 'backdrop') {
      var backdrop = container.firstElementChild;
      if (backdrop) {
        backdrop.addEventListener('mousedown', function (event) {
          if (event.target === backdrop) requestCloseModal();
        });
      }
    }
    root.appendChild(container);
    fragmentModal = { id: id, el: container, entry: entry };
    if (typeof entry.init === 'function') {
      try { entry.init(container); } catch (e) { /* a broken modal must not wedge the host */ }
    }
  }

  function showIframeModal(id, url) {
    // Supersede any in-flight fragment fetch (from an earlier fragment show)
    // so it can't mount itself once this iframe modal has taken over.
    invalidateFragmentFetch();
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
      if (frame !== incomingFrame) {
        // Superseded by a newer show before it finished loading; discard it.
        destroyFrame(frame);
        return;
      }
      frame.style.visibility = 'visible';
      // Swap: remove whatever this one replaced (an old iframe or a fragment).
      destroyFrame(modalFrame);
      teardownFragmentModal();
      modalFrame = frame;
      incomingFrame = null;
    });
    frame.src = url;
    incomingFrame = frame;
    root.appendChild(frame);
  }

  function hideModal(id) {
    // main only sends 'hide-all' today, but honor a targeted hide too.
    // Invalidate any in-flight fragment fetch so a still-pending open (nothing
    // mounted yet) can't mount itself after this hide.
    invalidateFragmentFetch();
    if (fragmentModal && fragmentModal.id === id) teardownFragmentModal();
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
    // Invalidate any in-flight fragment fetch that resolves after this close so
    // it won't mount itself.
    invalidateFragmentFetch();
    teardownFragmentModal();
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
    tooltipEl.className = 'minds-tooltip';
    tooltipEl.style.position = 'absolute';
    tooltipEl.style.left = '0';
    tooltipEl.style.top = '0';
    tooltipEl.style.display = 'none';
    root.appendChild(tooltipEl);
    return tooltipEl;
  }

  function showTooltip(cmd) {
    var el = ensureTooltipEl();
    // Content: arbitrary HTML if supplied, else a plain label + optional
    // keyboard-shortcut chip (the common, structured case).
    if (cmd.html) {
      el.innerHTML = cmd.html;
    } else {
      el.textContent = cmd.text || '';
      if (cmd.shortcut) {
        var kbd = document.createElement('span');
        kbd.className = 'minds-tooltip-shortcut';
        kbd.textContent = cmd.shortcut;
        el.appendChild(kbd);
      }
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
    var w = el.offsetWidth;
    var h = el.offsetHeight;
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
})();
