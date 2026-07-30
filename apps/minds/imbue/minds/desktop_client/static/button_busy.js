// Shared in-button busy state.
//
// A slow action reports its progress INSIDE the button that started it: a
// spinner takes the leading slot and the label becomes the verb in progress
// ("Enabling..."). The alternative -- a status line parked beside the button --
// reads as unrelated text, and leaves the button looking idle and clickable
// while its request is still in flight.
//
// The button is disabled for the duration. The shared button base already
// styles :disabled as 40% opacity plus a not-allowed cursor, so a busy button
// also LOOKS unavailable; a button that is not built from that base has to
// carry those two utilities itself.

(function () {
  'use strict';

  // Mirrors Spinner.jinja's size="sm", so an in-button spinner is the same
  // 14px ring as every other spinner in the app.
  var SPINNER_CLASS = 'spinner inline-block align-middle w-3.5 h-3.5 border';

  // The child nodes the button held before it went busy, so clear() can put
  // back exactly what was there (icons and all) rather than an approximation
  // rebuilt from a label string. Keyed by element, so a button that goes away
  // with its page does not keep the nodes alive.
  var idleChildren = new WeakMap();

  function makeSpinner(tone) {
    var spinner = document.createElement('span');
    // A solid-filled button (primary / danger) needs the currentColor-derived
    // ring, or a zinc spinner would sit near-invisibly on its dark fill.
    spinner.className = SPINNER_CLASS + (tone === 'inverse' ? ' spinner-inverse' : '');
    spinner.setAttribute('aria-hidden', 'true');
    return spinner;
  }

  // ``label`` is optional: pass the verb in progress to replace the button's
  // text with it, or omit it for a button too narrow to say anything, which
  // then shows the spinner alone.
  //
  // Calling this twice without an intervening clear() keeps the ORIGINAL idle
  // content, so a re-labelled busy button still restores correctly.
  function setBusy(button, label, tone) {
    if (!button) return;
    if (!idleChildren.has(button)) {
      idleChildren.set(button, Array.prototype.slice.call(button.childNodes));
    }
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    while (button.firstChild) button.removeChild(button.firstChild);
    button.appendChild(makeSpinner(tone));
    if (label) {
      var text = document.createElement('span');
      text.textContent = label;
      button.appendChild(text);
    }
  }

  // A no-op on a button that is not busy, so a caller can clear
  // unconditionally (an error path, a pane switch) without tracking which
  // buttons it actually started.
  function clearBusy(button) {
    if (!button || !idleChildren.has(button)) return;
    var children = idleChildren.get(button);
    idleChildren.delete(button);
    while (button.firstChild) button.removeChild(button.firstChild);
    children.forEach(function (node) { button.appendChild(node); });
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }

  function isBusy(button) {
    return !!button && idleChildren.has(button);
  }

  window.mindsButtonBusy = { set: setBusy, clear: clearBusy, isBusy: isBusy };
})();
