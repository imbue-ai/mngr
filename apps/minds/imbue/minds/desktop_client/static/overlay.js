// Overlay manager for the always-warm overlay surface.
//
// This runs in the shared modal WebContentsView, which main.js loads ONCE with
// /_chrome/overlay at window creation and keeps mounted for the window's life
// (see openOverlayHost in electron/main.js). Instead of loading a fresh page
// per modal (the old openModal -> loadURL model), every overlay is hosted here
// as in-page DOM driven over IPC, so opens are instant.
//
// Electron 40 has no per-view click-through: setIgnoreMouseEvents is
// window-level only, and a WebContentsView's bounds rectangle always captures
// clicks (transparency and rounded-corner cutouts included). The only lever is
// the view's bounds, so this manager is the single authority for how large the
// overlay view must be and reports it to main via window.minds.overlaySetBounds:
//   { mode: 'hidden' }                        -- nothing shown; main hides the view
//   { mode: 'full' }                          -- a capturing modal is open; full window
//   { mode: 'rect', rect: {x,y,width,height} }-- only tooltips; just their bounding box
// main maps these onto modalView.setBounds / setVisible, and treats 'full' as
// "a modal is open" for the titlebar drag-suppression it already does.
//
// IPC contract (main -> host), delivered on window.minds.onOverlayCommand:
//   { type: 'show-modal', id, url }  -- show (lazy-create + (re)load) a migrated
//                                       modal iframe; hides any other modal.
//   { type: 'hide-modal', id }       -- hide the named modal iframe.
//   { type: 'hide-all' }             -- hide every overlay (takeover / teardown).
//
// The migrated modal pages (workspace menu / inbox / help / sign-in) are served
// by the same origin as this host, so after each iframe loads we hand it the
// parent's window.minds bridge. Their existing window.minds.* calls
// (closeModal, onChromeEvent, navigateContent, ...) then work unchanged, with
// no edits to those pages.

(function () {
  'use strict';

  var root = document.getElementById('overlay-root');
  if (!root || !window.minds) return;

  // id -> { frame: HTMLIFrameElement, url: string|null, visible: bool }
  // Every entry here is a "capturing" overlay (a modal/menu with a full-window
  // backdrop), so any visible entry forces the overlay view to full-window.
  var modals = Object.create(null);

  function reportBounds() {
    var anyCapturing = false;
    for (var id in modals) {
      if (modals[id].visible) { anyCapturing = true; break; }
    }
    // Tooltips (which report { mode: 'rect', ... }) arrive in a later change.
    // Until then, "no capturing overlay" means there is nothing to show.
    window.minds.overlaySetBounds(anyCapturing ? { mode: 'full' } : { mode: 'hidden' });
  }

  // Same-origin: hand the iframe the parent's IPC bridge so the hosted modal
  // page's window.minds.* calls work exactly as when it was the top document.
  function injectBridge(frame) {
    try {
      frame.contentWindow.minds = window.minds;
    } catch (err) {
      // These pages are same-origin, so this should never throw; if it ever
      // does, the hosted page can't reach the bridge (e.g. won't dismiss), so
      // surface it loudly rather than failing silently.
      console.error('[overlay] failed to inject minds bridge into iframe', err);
    }
  }

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
    frame.addEventListener('load', function () { injectBridge(frame); });
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
    reportBounds();
  }

  function hideModal(id) {
    var entry = modals[id];
    if (!entry || !entry.visible) return;
    entry.visible = false;
    entry.frame.style.display = 'none';
    reportBounds();
  }

  function hideAll() {
    for (var id in modals) {
      if (modals[id].visible) {
        modals[id].visible = false;
        modals[id].frame.style.display = 'none';
      }
    }
    reportBounds();
  }

  window.minds.onOverlayCommand(function (cmd) {
    if (!cmd || typeof cmd !== 'object') return;
    if (cmd.type === 'show-modal' && cmd.id && cmd.url) showModal(cmd.id, cmd.url);
    else if (cmd.type === 'hide-modal' && cmd.id) hideModal(cmd.id);
    else if (cmd.type === 'hide-all') hideAll();
  });

  // Start hidden until main asks for something to be shown.
  reportBounds();
})();
