// Dev styleguide accent-hue picker: slide the hue, watch every
// --workspace-accent-driven swatch on the page update live.
//
// The accent swatch itself picks up the new color via its
// .accent-swatch class -> var(--workspace-accent), so we only need to
// mutate the CSS variable + the readout, not the swatch directly.
(function () {
  var hue = document.getElementById('styleguide-accent-hue');
  var value = document.getElementById('styleguide-accent-value');
  if (!hue || !value) return;
  function apply() {
    var color = 'oklch(85% 0.08 ' + hue.value + ')';
    document.documentElement.style.setProperty('--workspace-accent', color);
    value.textContent = color;
  }
  hue.addEventListener('input', apply);
  apply();
})();
