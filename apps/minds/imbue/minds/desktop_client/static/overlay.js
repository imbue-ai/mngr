// Overlay manager for the always-warm overlay surface.
//
// This runs in the shared modal WebContentsView, which main.js loads ONCE with
// /_chrome/overlay at window creation and keeps mounted for the window's life
// (see createBundleOverlayView in electron/main.js). Instead of loading a fresh
// page per modal (the old openModal -> loadURL model), every overlay is hosted
// here as in-page DOM driven over IPC, so opens are instant.
//
// IPC contract (main -> host), delivered on window.minds.onOverlayCommand:
//   { type: 'show-modal', id, url }       -- fetch the modal's ?fragment=1 markup
//                                            and inject it, superseding any other.
//   { type: 'hide-modal', id }            -- tear down the named modal.
//   { type: 'hide-all' }                  -- tear down every modal (close/takeover).
//   { type: 'show-tooltip', rect, text,   -- render + position a tooltip bubble
//     shortcut?, html? }                     anchored to the trigger's rect.
//   { type: 'hide-tooltip' }              -- hide the tooltip.
//
// The host reports the overlay view's required bounds to main via
// window.minds.overlaySetBounds ({ mode: 'rect', rect } for a tooltip,
// { mode: 'hidden' } when none); modals are full-window and main owns their
// visibility directly (see openModal/closeModal).
//
// Every modal (sign-in / help / workspace menu / inbox) renders as in-page DOM:
// its per-modal module (overlay_signin.js, overlay_help.js, ...) registers in
// window.MINDS_OVERLAY_MODALS, and opening one fetches its ``?fragment=1`` markup
// and injects it here -- no iframes. The SSE-driven modals (sidebar / inbox) read
// their state from this host's cached chrome events (window.MINDS_OVERLAY_HOST),
// which main primes on load and keeps current via broadcastChromeEvent.
//
// While a modal is open the overlay view is shown full-window by main and
// captures pointer events (Electron 40 has no per-view click-through). For
// modals, the view's visibility/bounds are owned by main (openModal/closeModal)
// and this manager decides which fragment is on screen; for tooltips, this
// manager measures the bubble and reports a small rect so the rest of the window
// stays interactive.

(function () {
  'use strict';

  var root = document.getElementById('overlay-root');
  if (!root || !window.minds) return;

  // -- Host chrome-state cache ------------------------------------------
  //
  // SSE-driven modals (the workspace menu, the inbox) render from the same state
  // the chrome view uses -- the workspace list, request counts, the current
  // workspace. main broadcasts those streams to this always-warm host on every
  // change (see broadcastChromeEvent / sendToOverlayHost in main.js), so the
  // host subscribes ONCE and caches the latest of each. A modal's init then
  // reads the current value synchronously the instant it opens -- no per-frame
  // priming handshake -- and stays live via a subscription it drops on close.
  // Exposed to the per-modal modules as window.MINDS_OVERLAY_HOST.
  var latestChromeEventByType = {};
  var latestCurrentWorkspaceId = null;
  var chromeEventListeners = [];
  var currentWorkspaceListeners = [];
  var contentUrlListeners = [];

  function notifyListeners(listeners, arg) {
    // Copy first: a listener may unsubscribe (mutating the array) mid-dispatch.
    listeners.slice().forEach(function (fn) {
      try { fn(arg); } catch (e) { /* one modal's handler must not break the rest */ }
    });
  }
  function addListener(listeners, fn) {
    listeners.push(fn);
    return function () {
      var index = listeners.indexOf(fn);
      if (index >= 0) listeners.splice(index, 1);
    };
  }

  if (window.minds.onChromeEvent) {
    window.minds.onChromeEvent(function (data) {
      if (data && data.type) latestChromeEventByType[data.type] = data;
      notifyListeners(chromeEventListeners, data);
    });
  }
  if (window.minds.onCurrentWorkspaceChanged) {
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      latestCurrentWorkspaceId = agentId || null;
      notifyListeners(currentWorkspaceListeners, latestCurrentWorkspaceId);
    });
  }
  if (window.minds.onContentURLChange) {
    window.minds.onContentURLChange(function (url) {
      notifyListeners(contentUrlListeners, url);
    });
  }

  window.MINDS_OVERLAY_HOST = {
    // Latest cached payload for a chrome-event type (e.g. 'workspaces',
    // 'requests', 'auth_status'), or null if none has arrived yet.
    getChromeEvent: function (type) { return latestChromeEventByType[type] || null; },
    getCurrentWorkspaceId: function () { return latestCurrentWorkspaceId; },
    // Each returns an unsubscribe function the modal calls on destroy.
    onChromeEvent: function (fn) { return addListener(chromeEventListeners, fn); },
    onCurrentWorkspaceChanged: function (fn) { return addListener(currentWorkspaceListeners, fn); },
    onContentURLChange: function (fn) { return addListener(contentUrlListeners, fn); },
  };

  // -- Modals -----------------------------------------------------------
  //
  // Every modal (sign-in / help / workspace menu / inbox) is registered in
  // window.MINDS_OVERLAY_MODALS by a per-modal module script loaded in this host
  // page (overlay_signin.js, overlay_help.js, overlay_sidebar.js,
  // overlay_inbox.js). Opening one fetches its ``?fragment=1`` markup and injects
  // it here as in-page DOM -- no iframe -- and calls the module's
  // init(container); closing calls destroy() and removes it. At most one modal is
  // shown at a time; opening one supersedes any other.
  var fragmentModal = null; // { id, el, entry } of the injected modal, or null
  var fragmentToken = 0; // supersede guard for in-flight fragment fetches

  function modalRegistry() {
    return window.MINDS_OVERLAY_MODALS || {};
  }

  // Supersede any in-flight fragment fetch: bump the token so a fetch started by
  // an earlier show won't mount itself when it finally resolves. Every command
  // that changes what should be on screen (a new show, a targeted hide, a
  // hide-all) calls this, or a slow fetch could mount a modal already superseded
  // or closed.
  function invalidateFragmentFetch() {
    fragmentToken++;
  }

  function teardownFragmentModal() {
    if (!fragmentModal) return;
    var current = fragmentModal;
    fragmentModal = null;
    if (current.entry && typeof current.entry.destroy === 'function') {
      try { current.entry.destroy(); } catch (e) { /* noop */ }
    }
    if (current.el && current.el.parentNode) current.el.parentNode.removeChild(current.el);
    // Drop any tooltip that was showing over the modal (e.g. its Close button).
    if (window.minds && window.minds.hideTooltip) window.minds.hideTooltip();
  }

  // Host-owned dismiss: route through main so it hides the overlay view and fans
  // a hide-all back to us, keeping main's modal-open / titlebar-drag state in
  // sync (main handles Escape the same way).
  function requestCloseModal() {
    if (window.minds && window.minds.closeModal) window.minds.closeModal();
  }

  function showModal(id, url) {
    var entry = modalRegistry()[id];
    if (!entry) return; // unknown modal id -- nothing registered to show
    // Supersede anything currently shown (a mounted modal or a pending fetch).
    teardownFragmentModal();
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
    // tooltip_triggers.js (loaded globally by Base.jinja) only scanned the host
    // page at load, before this fragment existed. Wire the fragment's data-tooltip
    // elements (the Close button etc.) now that it's in the DOM.
    if (window.bindTooltips) window.bindTooltips(container);
  }

  function hideModal(id) {
    // main only sends 'hide-all' today, but honor a targeted hide too. Invalidate
    // any in-flight fetch so a still-pending open can't mount after this hide.
    invalidateFragmentFetch();
    if (fragmentModal && fragmentModal.id === id) teardownFragmentModal();
  }

  function hideAllModals() {
    // Invalidate any in-flight fetch that resolves after this close.
    invalidateFragmentFetch();
    teardownFragmentModal();
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
})();
