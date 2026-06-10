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

// "Sidebar items" sample rows. Rendered through the same
// window.mindsSidebarRow.buildRow the live menu uses (static/sidebar.js +
// static/chrome.js), so the catalog can't drift from production. We pass
// explicit accents so the samples don't depend on the async accent lookup,
// and withOpenNew:true to show the richest (Electron) treatment. No event
// wiring -- these are visual only.
(function () {
  var panel = document.getElementById('styleguide-sidebar-rows');
  if (!panel || !window.mindsSidebarRow) return;
  var samples = [
    { id: 'agent-styleguide-current', name: 'current-workspace', accent: 'oklch(72% 0.12 230)' },
    { id: 'agent-styleguide-other', name: 'another-workspace', accent: 'oklch(72% 0.12 70)' },
    { id: 'agent-styleguide-stale', name: 'stale-workspace', accent: 'oklch(72% 0.12 320)', is_stale: true },
  ];
  panel.appendChild(window.mindsSidebarRow.buildRow(samples[0], { isCurrent: true, withOpenNew: true }));
  panel.appendChild(window.mindsSidebarRow.buildRow(samples[1], { withOpenNew: true }));
  panel.appendChild(window.mindsSidebarRow.buildRow(samples[2], { withOpenNew: true }));
})();
