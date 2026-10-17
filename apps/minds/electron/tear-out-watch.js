'use strict';

// The popout side of a watched workspace title-bar drag (the pull-out-window
// spec, section 5.1): which popout the drag holds, and what each cursor
// sample, the release, and the shell's end of the gesture do to it. Kept free
// of any `electron` import so it can be unit-tested under plain node; main.js
// samples the cursor on a timer and supplies the effects that open, keep, and
// close popout windows and report each step to the workspace shell.

/**
 * A watch over one title-bar drag of ``request``'s window.
 *
 * @param {{workspaceId: string, windowId: string}} request  The dragged window (with its size and grab).
 * @param {object} effects
 * @param {(request: object) => object|null} effects.openPopout  Open a popout under the cursor; null when it cannot yet.
 * @param {(request: object) => object|null} effects.findPopout  The open popout already showing the request's window.
 * @param {(popout: object, reason: string) => void} effects.closePopout
 * @param {(popout: object) => void} effects.keepPopout  The popout stays where the drag left it.
 * @param {(request: object, phase: 'out'|'in'|'released') => void} effects.sendTearOut
 */
function createTearOutWatch(request, effects) {
  let current = request;
  let popout = null;

  return {
    get request() {
      return current;
    },

    get popout() {
      return popout;
    },

    /** The dragged window changed size (a snapped window un-snapped): the popout still to open takes it. */
    updateRequest(next) {
      current = next;
    },

    /** One cursor sample: out past the tear-out distance opens the popout, back inside drops it. */
    sample(isOut) {
      if (isOut && popout === null) {
        // A window is out in at most one popout. One still open for it (dropped back onto the desktop and waiting
        // for the desktop to take the window) goes before this drag opens its own.
        const lingering = effects.findPopout(current);
        if (lingering !== null) effects.closePopout(lingering, 'replaced by a new tear-out');
        popout = effects.openPopout(current);
        if (popout !== null) effects.sendTearOut(current, 'out');
      } else if (!isOut && popout !== null) {
        const dropped = popout;
        popout = null;
        effects.closePopout(dropped, 'the drag came back inside');
        effects.sendTearOut(current, 'in');
      }
    },

    /** The mouse-up reached the chrome window: a popout that is out stays, and the shell hears the release. */
    release() {
      if (popout === null) return;
      effects.keepPopout(popout);
      effects.sendTearOut(current, 'released');
    },

    /**
     * The shell's own gesture ended first. A popout that is out stays unless the shell cancelled the drag, and
     * the shell hears "released": its release can reach it before "out" does, so ``isDetached`` may predate
     * this watch's word, and the shell takes that word when it lands. A shell that does not say whether it
     * cancelled (``isCancelled`` null) is taken at its ``isDetached``.
     */
    end(isDetached, isCancelled) {
      if (popout === null) return;
      if (isCancelled === null ? !isDetached : isCancelled) {
        effects.closePopout(popout, isCancelled ? 'the drag was cancelled' : 'the drag ended inside');
        return;
      }
      effects.keepPopout(popout);
      effects.sendTearOut(current, 'released');
    },

    /** The watch ended with neither a release nor the shell's end (another drag replaced it, or the chrome window
     * went away). A popout that went out stays: the shell detached its window when it heard "out". */
    abandon() {
      if (popout !== null) effects.keepPopout(popout);
    },
  };
}

module.exports = { createTearOutWatch };
