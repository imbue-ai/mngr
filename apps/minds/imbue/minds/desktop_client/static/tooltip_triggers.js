// Shared custom-tooltip triggers.
//
// Included on every page via Base.jinja. It wires hover (after a short delay)
// and keyboard-focus tooltips onto every element carrying a ``data-tooltip``
// label, hiding again on leave / blur / click / scroll / window resize or blur.
// The trigger wiring is identical everywhere; only the render backend differs,
// chosen once by environment:
//
//   * Overlay backend (window.minds.showTooltip present): the chrome titlebar
//     and the modal pages hosted on the shared overlay surface hand the label
//     + trigger rect to main over IPC, which renders the bubble on that surface
//     -- floating above both chrome and workspace content (see overlay.js).
//   * In-page backend (no bridge): first-party content-view pages (e.g. the
//     landing page's workspace-row buttons) render the bubble themselves, as a
//     ``position: fixed`` element appended to <body> so it escapes the
//     workspace cards' ``overflow-hidden``. The content view deliberately lacks
//     the window.minds bridge -- it also hosts foreign, untrusted workspace
//     content -- so it cannot reach the overlay surface; it styles the bubble
//     with the same shared ``.minds-tooltip`` class so the look matches.
//
// An optional ``data-tooltip-shortcut`` populates the overlay payload's
// ``shortcut`` field -- a forward hook for a keyboard-shortcut chip. Nothing
// renders it yet (overlay.js drops the chip; there is no on-ramp chip size in
// the design system), so it is a no-op until a real use arrives.

(function () {
  'use strict';

  var TOOLTIP_DELAY_MS = 250; // hover-intent delay before a tooltip appears
  var TOOLTIP_MARGIN = 6; // min gap from the window edges
  var TOOLTIP_GAP = 6; // gap between the trigger and the bubble

  // Overlay backend: delegate rendering + positioning to the overlay surface
  // (main measures/clamps and drives the view's bounds; see overlay.js).
  function makeOverlayBackend() {
    return {
      show: function (trigger) {
        var rect = trigger.getBoundingClientRect();
        var payload = {
          rect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
          text: trigger.getAttribute('data-tooltip'),
        };
        var shortcut = trigger.getAttribute('data-tooltip-shortcut');
        if (shortcut) payload.shortcut = shortcut;
        window.minds.showTooltip(payload);
      },
      hide: function () {
        window.minds.hideTooltip();
      },
    };
  }

  // In-page backend: render + position the bubble on <body> ourselves. The
  // positioning mirrors overlay.js (centered under the trigger, flipped above
  // when it would overflow the bottom, clamped to the viewport) so the two
  // backends behave the same.
  function makeInPageBackend() {
    var bubble = null;
    function ensureBubble() {
      if (bubble) return bubble;
      bubble = document.createElement('div');
      bubble.className = 'minds-tooltip';
      bubble.setAttribute('role', 'tooltip');
      bubble.style.position = 'fixed';
      bubble.style.left = '0';
      bubble.style.top = '0';
      bubble.style.zIndex = '2147483647';
      bubble.style.display = 'none';
      document.body.appendChild(bubble);
      return bubble;
    }
    return {
      show: function (trigger) {
        ensureBubble();
        bubble.textContent = trigger.getAttribute('data-tooltip');
        var viewportWidth = window.innerWidth;
        var viewportHeight = window.innerHeight;
        // Measure at natural width (clear any width fixed by a prior show). Also
        // reset left/top to 0 first: the bubble is position:fixed, so a stale
        // large left from a prior show would cap its shrink-to-fit width at
        // (viewport - left) and wrap the label, mis-measuring width/height
        // (overlay.js resets the same way before measuring).
        bubble.style.width = '';
        bubble.style.left = '0';
        bubble.style.top = '0';
        bubble.style.visibility = 'hidden';
        bubble.style.display = 'inline-flex';
        // Measure the fractional border-box size via getBoundingClientRect, NOT
        // the integer offsetWidth/Height. offsetWidth rounds the shrink-to-fit
        // width DOWN (e.g. 132.4 -> 132); fixing the width to that rounded value
        // then leaves the content a fraction short and wraps the last word. Ceil
        // so the fixed width is always >= the true content width.
        var measuredRect = bubble.getBoundingClientRect();
        var width = Math.ceil(measuredRect.width);
        var height = Math.ceil(measuredRect.height);
        var anchorRect = trigger.getBoundingClientRect();
        var bubbleX = anchorRect.left + anchorRect.width / 2 - width / 2;
        var bubbleY = anchorRect.bottom + TOOLTIP_GAP;
        if (bubbleY + height > viewportHeight - TOOLTIP_MARGIN) {
          var above = anchorRect.top - height - TOOLTIP_GAP;
          if (above >= TOOLTIP_MARGIN) bubbleY = above;
        }
        if (bubbleX + width > viewportWidth - TOOLTIP_MARGIN) bubbleX = viewportWidth - TOOLTIP_MARGIN - width;
        if (bubbleX < TOOLTIP_MARGIN) bubbleX = TOOLTIP_MARGIN;
        if (bubbleY < TOOLTIP_MARGIN) bubbleY = TOOLTIP_MARGIN;
        // Fix the width so it doesn't reflow if the viewport later changes.
        bubble.style.width = width + 'px';
        bubble.style.left = bubbleX + 'px';
        bubble.style.top = bubbleY + 'px';
        bubble.style.visibility = 'visible';
      },
      hide: function () {
        if (bubble) {
          bubble.style.display = 'none';
          bubble.style.visibility = 'hidden';
        }
      },
    };
  }

  var backend = window.minds && window.minds.showTooltip ? makeOverlayBackend() : makeInPageBackend();

  var timer = null;
  var shown = false;

  function clearTimer() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }
  function hide() {
    clearTimer();
    if (shown) {
      backend.hide();
      shown = false;
    }
  }
  function showFor(trigger) {
    if (!trigger.getAttribute('data-tooltip')) return;
    backend.show(trigger);
    shown = true;
  }
  function schedule(trigger) {
    clearTimer();
    timer = setTimeout(function () {
      timer = null;
      showFor(trigger);
    }, TOOLTIP_DELAY_MS);
  }

  // Bind hover/focus tooltip triggers within ``root``. Run once for the document
  // (the chrome titlebar and standalone pages, via the Base.jinja include) and
  // again by the overlay host for each injected modal fragment (whose Close
  // button + other data-tooltip elements arrive after this initial pass). Exposed
  // as window.bindTooltips.
  function bindTooltips(root) {
    var triggers = root.querySelectorAll('[data-tooltip]');
    for (var index = 0; index < triggers.length; index++) {
      (function (trigger) {
        trigger.addEventListener('mouseenter', function () { schedule(trigger); });
        trigger.addEventListener('mouseleave', hide);
        trigger.addEventListener('click', hide);
        // Keyboard focus only -- not focus that came from a mouse click (which
        // would flash the tooltip and then immediately hide it on the click).
        trigger.addEventListener('focus', function () {
          try {
            if (trigger.matches(':focus-visible')) showFor(trigger);
          } catch (error) { /* :focus-visible unsupported -- skip focus tooltips */ }
        });
        trigger.addEventListener('blur', hide);
      })(triggers[index]);
    }
  }
  window.bindTooltips = bindTooltips;

  bindTooltips(document);
  // Any scroll (capture, so nested scrollers count) or window resize/blur moves
  // the trigger out from under a shown bubble, so drop it.
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
  window.addEventListener('blur', hide);
})();
