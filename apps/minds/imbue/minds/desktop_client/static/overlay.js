// Overlay manager for the always-warm overlay surface.
//
// This runs in the shared modal WebContentsView, which main.js loads ONCE with
// /_chrome/overlay at window creation and keeps mounted for the window's life
// (see createBundleOverlayView in electron/main.js). Instead of loading a fresh
// page per modal (the old openModal -> loadURL model), every overlay is hosted
// here as in-page DOM driven over IPC, so opens are instant.
//
// IPC contract (main -> host), delivered on window.minds.onOverlayCommand:
//   { type: 'show-modal', id, url }  -- show (lazy-create + (re)load) a migrated
//                                       modal iframe; hides any other modal.
//   { type: 'hide-modal', id }       -- hide the named modal iframe.
//   { type: 'hide-all' }             -- hide every overlay (close / takeover).
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
// captures pointer events (Electron 40 has no per-view click-through). The
// view's visibility/bounds for modals are owned by main (openModal/closeModal);
// this manager only decides which iframe is on screen. The dynamic-bounds path
// for tooltips (window.minds.overlaySetBounds) lands in a later change.

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

  window.minds.onOverlayCommand(function (cmd) {
    if (!cmd || typeof cmd !== 'object') return;
    if (cmd.type === 'show-modal' && cmd.id && cmd.url) showModal(cmd.id, cmd.url);
    else if (cmd.type === 'hide-modal' && cmd.id) hideModal(cmd.id);
    else if (cmd.type === 'hide-all') hideAll();
  });
})();
