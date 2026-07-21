// Light/dark dev toggle. Flips the `.dark` class on <html> and persists the
// choice to localStorage, which Base.jinja reads pre-paint -- so the choice
// sticks across every minds page. This is a development affordance; the formal
// light/dark UX (OS preference / in-app control) lands in a later stage.
(function () {
  var btn = document.getElementById('styleguide-theme-toggle');
  if (!btn) return;
  function syncLabel() {
    var isDark = document.documentElement.classList.contains('dark');
    btn.textContent = isDark ? 'Switch to light' : 'Switch to dark';
  }
  btn.addEventListener('click', function () {
    var isDark = document.documentElement.classList.toggle('dark');
    try {
      localStorage.setItem('minds-theme', isDark ? 'dark' : 'light');
    } catch (e) {
      /* ignore */
    }
    syncLabel();
  });
  syncLabel();
})();

// Dev styleguide accent picker: choose a hex, watch every
// --workspace-accent-driven surface on the page update live. Mirrors the
// real model (accents are user-picked #rrggbb hexes stored as an mngr
// color label), minus the persistence.
//
// The accent-spine stripe picks up the new color via var(--workspace-accent),
// so we only need to mutate the CSS variable + the readout, not any element
// directly.
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

// Table-of-contents scrollspy. Highlights the TOC link whose section is
// currently nearest the top of the viewport by toggling aria-current="page"
// (styled in app.css :: .styleguide-toc-link[aria-current="page"]). The jump
// itself is plain anchor navigation -- each section carries a scroll-mt so the
// heading lands below the top edge rather than flush against it.
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('.styleguide-toc-link'));
  if (!links.length || !('IntersectionObserver' in window)) return;
  var targets = [];
  var visible = Object.create(null);
  links.forEach(function (link) {
    var id = (link.getAttribute('href') || '').replace(/^#/, '');
    var el = id && document.getElementById(id);
    if (el) targets.push(el);
  });
  if (!targets.length) return;
  function updateActive() {
    // Active = the intersecting section closest to the top of the viewport.
    var activeId = null;
    var best = Infinity;
    targets.forEach(function (el) {
      if (!visible[el.id]) return;
      var top = el.getBoundingClientRect().top;
      if (top < best) {
        best = top;
        activeId = el.id;
      }
    });
    links.forEach(function (link) {
      var id = (link.getAttribute('href') || '').replace(/^#/, '');
      if (id && id === activeId) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
  }
  // The negative top/bottom margins form a thin band near the top of the
  // viewport; a section counts as active only while it sits within that band.
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      visible[entry.target.id] = entry.isIntersecting;
    });
    updateActive();
  }, { rootMargin: '-10% 0px -80% 0px', threshold: 0 });
  targets.forEach(function (el) {
    observer.observe(el);
  });
})();
