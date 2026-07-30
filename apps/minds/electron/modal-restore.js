// Which modal a detour displaced, and what a dismissal should therefore do.
//
// Signing in is a detour, never a destination. A user who opened the workspace
// options panel and pressed Link to sign in wants the panel back when the
// detour ends -- not whatever happened to sit behind the overlay.
//
// The panel is not closed and reopened: the overlay host keeps its iframe
// mounted underneath (see showModal's ``stack`` in static/overlay.js), so it
// stays on screen under the sign-in's backdrop and the hand-back reveals the
// LIVE frame. The remembered URL is only how the shell knows what it is showing
// again once the detour ends.
//
// Split out of main.js (which cannot be required outside Electron) so the rules
// below are testable. The rules are easy to get subtly wrong:
//
//   * Only a DISMISSAL hands back. main.js closes the modal from ~17 places --
//     navigation, workspace switches, window teardown -- and a panel that
//     reappeared after those would land on top of unrelated surfaces.
//   * Handing back consumes the memory, so a second dismissal closes rather
//     than reopening the panel a second time.
//   * Any plain close forgets it, so an abandoned detour cannot arm a stale
//     reopen that fires at some later, unrelated dismissal.
//
// What is NOT covered here (main.js wiring, and the sign-in round trip itself):
// that `openSigninModal` is the only caller that arms, that Escape and the
// close-modal IPC are the only paths routed through the dismissal, and that a
// completed sign-in reaches the dismissal via the page's MINDS_AUTH_NAV.

'use strict';

/**
 * Remember the modal a detour is displacing.
 *
 * `displacedUrl` is falsy when nothing was open, which is the common case (a
 * sign-in opened from the create screen displaces nothing) and must leave the
 * state disarmed rather than remembering an empty URL.
 */
function rememberDisplacedModal(state, displacedUrl) {
  state.modalRestoreUrl = displacedUrl || null;
}

/** Forget any displaced modal. Called by every plain close. */
function forgetDisplacedModal(state) {
  state.modalRestoreUrl = null;
}

/**
 * What a dismissal should do: reopen the displaced modal, or close outright.
 *
 * Consumes the memory, so this is safe to call on every dismissal and answers
 * `{action: 'close'}` from then on.
 */
function planDismissal(state) {
  const url = state.modalRestoreUrl || null;
  state.modalRestoreUrl = null;
  return url ? { action: 'restore', url } : { action: 'close' };
}

module.exports = { rememberDisplacedModal, forgetDisplacedModal, planDismissal };
