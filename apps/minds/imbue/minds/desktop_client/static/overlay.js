// Overlay manager for the always-warm overlay surface.
//
// This runs in the shared modal WebContentsView, which main.js loads ONCE with
// /_chrome/overlay at window creation and keeps mounted for the window's life
// (see createBundleOverlayView in electron/main.js). Instead of loading a fresh
// page per modal (the old openModal -> loadURL model), every overlay is hosted
// here as in-page DOM driven over IPC, so opens are instant.
//
// IPC contract (main -> host), delivered on window.minds.onOverlayCommand:
//   { type: 'show-modal', id, url }       -- show (lazy-create + (re)load) a
//                                            migrated modal iframe; hides others.
//   { type: 'hide-modal', id }            -- hide the named modal iframe.
//   { type: 'hide-all' }                  -- hide every modal (close / takeover).
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

  // id -> { frame: HTMLIFrameElement, url: string|null, visible: bool }
  var modals = Object.create(null);

  function ensureModal(id) {
    if (modals[id]) return modals[id];
    var frame = document.createElement('iframe');
    // Fill the host; the hosted page paints its own backdrop and positions its
    // own panel within the full window.
    frame.className = 'absolute inset-0 w-full h-full';
    frame.style.border = '0';
    frame.style.background = 'transparent';
    frame.style.display = 'none';
    frame.setAttribute('data-overlay-id', id);
    // Tell main the iframe is ready so it can replay the cached chrome state
    // into this frame (workspace list / request count). Runs on every (re)load.
    frame.addEventListener('load', function () {
      window.minds.overlayModalLoaded(id);
    });
    root.appendChild(frame);
    var entry = { frame: frame, url: null, visible: false };
    modals[id] = entry;
    return entry;
  }

  function hideOthers(keepId) {
    for (var id in modals) {
      if (id !== keepId && modals[id].visible) {
        modals[id].visible = false;
        modals[id].frame.style.display = 'none';
      }
    }
  }

  function showModal(id, url) {
    var entry = ensureModal(id);
    hideOthers(id);
    // Reload on every show so the hosted page re-fetches its state and replays
    // its entry animation -- preserving the old "fresh every open" feel even
    // though the iframe stays mounted/warm between opens.
    if (entry.url === url && entry.frame.contentWindow) {
      try {
        entry.frame.contentWindow.location.reload();
      } catch (err) {
        entry.frame.src = url;
      }
    } else {
      entry.url = url;
      entry.frame.src = url;
    }
    entry.frame.style.display = 'block';
    entry.visible = true;
  }

  function hideModal(id) {
    var entry = modals[id];
    if (!entry || !entry.visible) return;
    entry.visible = false;
    entry.frame.style.display = 'none';
  }

  function hideAll() {
    for (var id in modals) {
      if (modals[id].visible) {
        modals[id].visible = false;
        modals[id].frame.style.display = 'none';
      }
    }
  }

  // -- Tooltips ----------------------------------------------------------
  //
  // A tooltip is display-only. Because Electron 40 has no per-view click-through,
  // we shrink the overlay view to just the tooltip's rectangle (reported via
  // overlaySetBounds) so everywhere else stays interactive. To size that rect we
  // first render + measure the bubble while the host still has its full-window
  // viewport (the view is full-window but hidden when idle), then pin the bubble
  // at the view's top-left and report the window-coordinate rect; main shrinks
  // the view to it and shows it. Hiding restores the full-window (hidden) bounds
  // so the next tooltip can be measured.
  var TOOLTIP_MARGIN = 6; // min gap from the window edges
  var TOOLTIP_GAP = 6; // gap between the trigger and the bubble
  var tooltipEl = null;

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
    // Measure at the natural (max-width-capped) size while still full-window.
    el.style.width = '';
    el.style.left = '0';
    el.style.top = '0';
    el.style.visibility = 'hidden';
    el.style.display = 'inline-flex';
    var w = el.offsetWidth;
    var h = el.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
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
    // Pin the bubble at the view's top-left and fix its width so it does not
    // reflow when the view shrinks to the reported rect.
    el.style.width = w + 'px';
    el.style.left = '0';
    el.style.top = '0';
    el.style.visibility = 'visible';
    window.minds.overlaySetBounds({
      mode: 'rect',
      rect: { x: tx, y: ty, width: w, height: h },
    });
  }

  function hideTooltip() {
    if (tooltipEl) {
      tooltipEl.style.display = 'none';
      tooltipEl.style.visibility = 'hidden';
    }
    window.minds.overlaySetBounds({ mode: 'hidden' });
  }

  window.minds.onOverlayCommand(function (cmd) {
    if (!cmd || typeof cmd !== 'object') return;
    if (cmd.type === 'show-modal' && cmd.id && cmd.url) showModal(cmd.id, cmd.url);
    else if (cmd.type === 'hide-modal' && cmd.id) hideModal(cmd.id);
    else if (cmd.type === 'hide-all') hideAll();
    else if (cmd.type === 'show-tooltip') showTooltip(cmd);
    else if (cmd.type === 'hide-tooltip') hideTooltip();
  });
})();
