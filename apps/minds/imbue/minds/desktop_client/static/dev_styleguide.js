// Dev styleguide picker: choose a workspace surface (or drag the hue
// slider) and watch the whole page re-tint + flip its component palette.
//
// The picker writes --workspace-surface on <body> and calls
// window.mindsTheme.refresh() to recompute <html data-theme> from the new
// surface's luminance. The accent stripe / accent-spine / accent-swatch
// elements continue to read --workspace-accent, so the legacy hue slider
// still drives them independently.
(function () {
  function applySurface(value) {
    document.body.style.setProperty('--workspace-surface', value);
    // Existing per-workspace accent uses --workspace-accent; for the picker
    // we set both so the legacy accent stripe + spine + swatch all align
    // with the chosen surface. The hue slider below can override this.
    document.body.style.setProperty('--workspace-accent', value);
    if (window.mindsTheme && window.mindsTheme.refresh) {
      window.mindsTheme.refresh();
    }
  }

  var swatches = document.querySelectorAll('[data-surface]');
  for (var i = 0; i < swatches.length; i++) {
    swatches[i].addEventListener('click', function (event) {
      var raw = event.currentTarget.getAttribute('data-surface');
      // var() references resolve against the computed style; pass them
      // through as-is so the browser substitutes the primitive color.
      applySurface(raw);
    });
  }

  // Legacy hue slider: writes --workspace-surface so the whole page
  // re-tints and theme.js re-picks <html data-theme>, and also writes
  // --workspace-accent so the chrome-stripe / accent-spine / accent-swatch
  // rules (which read --workspace-accent) stay coherent with the picked
  // tint. Picking a surface swatch above does the same thing, so the two
  // pickers compose -- whichever was clicked last wins.
  var hue = document.getElementById('styleguide-accent-hue');
  var value = document.getElementById('styleguide-accent-value');
  if (hue && value) {
    function applyHue() {
      var color = 'oklch(70% 0.15 ' + hue.value + ')';
      document.body.style.setProperty('--workspace-accent', color);
      document.body.style.setProperty('--workspace-surface', color);
      value.textContent = color;
      if (window.mindsTheme && window.mindsTheme.refresh) {
        window.mindsTheme.refresh();
      }
    }
    hue.addEventListener('input', applyHue);
    applyHue();
  }
})();
