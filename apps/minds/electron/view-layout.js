'use strict';

// Pure layout math for a window bundle's three stacked views (chrome titlebar,
// workspace content, overlay modal). Kept free of any `electron` imports so it
// can be unit-tested under plain node (see ../test/unit/view-layout.test.js).
// main.js reads the live window dimensions + bundle state, calls this, and
// applies the returned bounds to whichever views currently exist.

/**
 * Compute the bounds for each view in a bundle.
 *
 * Two regimes:
 *
 *   Takeover (error / loading / quitting): the chrome view shows the
 *   full-window shell.html takeover, so it fills the window and the content
 *   view collapses to nothing. The modal normally collapses too -- EXCEPT in
 *   the error state with an open modal: the error takeover's "Report a bug"
 *   button opens the in-app /help modal (the backend is still up), and that
 *   modal must overlay the full window to be visible (it carries its own dim
 *   backdrop, so the error screen shows through behind it). Collapsing it to
 *   0x0 is what made the report button appear to do nothing.
 *
 *   The overlay is gated on `isErrorState`, NOT `modalVisible` alone: the
 *   quitting flip hides the modal via `setVisible(false)` but deliberately
 *   leaves `modalVisible` true so it can be restored if the user backs out of
 *   the quit, so a `modalVisible`-only check would wrongly overlay a stale
 *   modal during a quit.
 *
 *   Normal: the chrome view covers the whole window and paints the accent
 *   color wherever the content view doesn't reach; the content view is inset
 *   by `contentInset` on the left/right/bottom (flush with the titlebar on
 *   top); the modal overlays the entire window.
 *
 * @typedef {{x: number, y: number, width: number, height: number}} Rect
 *
 * @param {object} state
 * @param {boolean} state.isErrorState
 * @param {boolean} state.isLoadingState
 * @param {boolean} state.isQuittingState
 * @param {boolean} state.modalVisible    Whether a modal is currently open.
 * @param {number}  state.width           Window content width.
 * @param {number}  state.height          Window content height.
 * @param {number}  state.titlebarHeight  Height of the chrome titlebar strip.
 * @param {number}  state.contentInset    Gap around the content view (L/R/bottom).
 * @returns {{chrome: Rect, content: Rect, modal: Rect}} bounds per view, where
 *   Rect is `{x, y, width, height}`.
 */
function computeBundleViewBounds({
  isErrorState,
  isLoadingState,
  isQuittingState,
  modalVisible,
  width,
  height,
  titlebarHeight,
  contentInset,
}) {
  const fullWindow = { x: 0, y: 0, width, height };
  const collapsed = { x: 0, y: 0, width: 0, height: 0 };

  if (isErrorState || isLoadingState || isQuittingState) {
    return {
      chrome: fullWindow,
      content: collapsed,
      modal: isErrorState && modalVisible ? fullWindow : collapsed,
    };
  }

  return {
    chrome: fullWindow,
    content: {
      x: contentInset,
      y: titlebarHeight,
      width: width - contentInset * 2,
      height: height - titlebarHeight - contentInset,
    },
    modal: fullWindow,
  };
}

module.exports = { computeBundleViewBounds };
