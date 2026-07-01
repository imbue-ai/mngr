// Shared custom-tooltip triggers.
//
// Any page whose elements carry a ``data-tooltip`` label includes this -- the
// chrome titlebar and the modal pages hosted on the overlay surface (e.g. the
// help dialog). When running in Electron (window.minds.showTooltip present) it
// renders a styled bubble on the overlay surface, above both chrome and content,
// on hover (after a short delay) and keyboard focus; it hides on
// leave / blur / click / window resize or blur. Browser mode (no overlay
// surface) gets no tooltip.
//
// An optional ``data-tooltip-shortcut`` populates the payload's ``shortcut``
// field -- a forward hook for a keyboard-shortcut chip. Nothing renders it yet
// (overlay.js drops the chip; there is no on-ramp chip size in the design
// system), so it is a no-op until a real use arrives.

(function () {
  'use strict';
  if (!window.minds || !window.minds.showTooltip) return;

  var TOOLTIP_DELAY_MS = 150;
  var timer = null;
  var shown = false;

  function clearTimer() {
    if (timer) { clearTimeout(timer); timer = null; }
  }
  function hide() {
    clearTimer();
    if (shown) { window.minds.hideTooltip(); shown = false; }
  }
  function showFor(el) {
    var text = el.getAttribute('data-tooltip');
    if (!text) return;
    var r = el.getBoundingClientRect();
    var payload = { rect: { x: r.left, y: r.top, width: r.width, height: r.height }, text: text };
    var shortcut = el.getAttribute('data-tooltip-shortcut');
    if (shortcut) payload.shortcut = shortcut;
    window.minds.showTooltip(payload);
    shown = true;
  }
  function schedule(el) {
    clearTimer();
    timer = setTimeout(function () {
      timer = null;
      showFor(el);
    }, TOOLTIP_DELAY_MS);
  }

  var triggers = document.querySelectorAll('[data-tooltip]');
  for (var i = 0; i < triggers.length; i++) {
    (function (el) {
      el.addEventListener('mouseenter', function () { schedule(el); });
      el.addEventListener('mouseleave', hide);
      el.addEventListener('click', hide);
      // Keyboard focus only -- not focus that came from a mouse click (which
      // would flash the tooltip and then immediately hide it on the click).
      el.addEventListener('focus', function () {
        try {
          if (el.matches(':focus-visible')) showFor(el);
        } catch (e) { /* :focus-visible unsupported -- skip focus tooltips */ }
      });
      el.addEventListener('blur', hide);
    })(triggers[i]);
  }
  window.addEventListener('resize', hide);
  window.addEventListener('blur', hide);
})();
