// Dev styleguide accent picker: choose a hex, watch every
// --workspace-accent-driven swatch on the page update live. Mirrors the
// real model (accents are user-picked #rrggbb hexes stored as an mngr
// color label), minus the persistence.
//
// The accent swatch itself picks up the new color via its
// .accent-swatch class -> var(--workspace-accent), so we only need to
// mutate the CSS variable + the readout, not the swatch directly.
(function () {
  var colorInput = document.getElementById('styleguide-accent-color');
  var value = document.getElementById('styleguide-accent-value');
  if (!colorInput || !value) return;
  function apply() {
    document.documentElement.style.setProperty('--workspace-accent', colorInput.value);
    value.textContent = colorInput.value;
  }
  colorInput.addEventListener('input', apply);
  apply();
})();
