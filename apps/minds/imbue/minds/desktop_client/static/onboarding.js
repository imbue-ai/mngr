// Creating-page onboarding walkthrough. Three explainer steps shown while
// the workspace is created (Creating.jinja); creating.js keeps owning
// progress/status/failure and signals readiness by setting
// data-ready + data-redirect-url on #creating and dispatching
// 'minds:create-ready' (see creating.js).
//
// Step state lives as a step-1/2/3 class on #onboarding: CSS transitions
// the shared scene graphic (laptop -> +key -> full picture) off that class,
// and this file toggles the per-step text panels and nav buttons. The Begin
// button appears only when the user has reached the last step AND creation
// is done; clicking it plays the zoom-in animation, then navigates.
(function () {
  var root = document.getElementById('creating');
  var onboarding = document.getElementById('onboarding');
  if (!root || !onboarding) return;

  var TOTAL_STEPS = 3;
  var step = 1;

  var prevBtn = document.getElementById('onboarding-prev');
  var nextBtn = document.getElementById('onboarding-next');
  var beginBtn = document.getElementById('onboarding-begin');
  var serverCaption = document.getElementById('server-caption');

  function isReady() {
    return root.getAttribute('data-ready') === 'true';
  }

  function render() {
    for (var s = 1; s <= TOTAL_STEPS; s++) {
      onboarding.classList.toggle('step-' + s, s === step);
    }
    var panels = onboarding.querySelectorAll('.onboarding-step');
    panels.forEach(function (panel) {
      panel.classList.toggle('hidden', panel.getAttribute('data-step') !== String(step));
    });
    var dots = onboarding.querySelectorAll('.onboarding-dot');
    dots.forEach(function (dot) {
      dot.classList.toggle('is-active', dot.getAttribute('data-dot') === String(step));
    });
    if (prevBtn) prevBtn.disabled = step === 1;
    var onLastStep = step === TOTAL_STEPS;
    // On the last step, Next gives way to Begin -- shown once the
    // workspace is actually ready, otherwise the server caption explains
    // we're still loading. Visibility is inline display rather than the
    // ``hidden`` utility, which the button base's inline-flex would
    // override (CSS order).
    if (nextBtn) nextBtn.style.display = onLastStep ? 'none' : '';
    if (beginBtn) beginBtn.style.display = onLastStep && isReady() ? '' : 'none';
    if (serverCaption) {
      serverCaption.textContent = isReady() ? 'ready' : 'loading...';
    }
  }

  if (prevBtn) {
    prevBtn.addEventListener('click', function () {
      if (step > 1) { step -= 1; render(); }
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', function () {
      if (step < TOTAL_STEPS) { step += 1; render(); }
    });
  }

  // Readiness can arrive while the user is mid-walkthrough; re-render so
  // Begin appears the moment both conditions hold.
  root.addEventListener('minds:create-ready', render);

  // ---- Begin: zoom into the workspace, then navigate ----
  var entering = false;
  if (beginBtn) {
    beginBtn.addEventListener('click', function () {
      var url = root.getAttribute('data-redirect-url');
      if (!url || entering) return;
      entering = true;
      onboarding.classList.add('is-entering');
      // Matches the onboarding-enter-zoom animation duration in app.css.
      setTimeout(function () { window.location.href = url; }, 650);
    });
  }

  // ---- Step 1: theme-color demo ----
  // Picking a swatch restyles the demo tab-space via --demo-accent. This is
  // a learning toy only: the pick is not persisted anywhere (the real
  // workspace color was chosen on the create form).
  var demo = document.getElementById('theme-demo');
  var picker = document.getElementById('onboarding-color-picker');
  if (demo && picker) {
    picker.addEventListener('click', function (event) {
      var swatch = event.target.closest('.color-swatch');
      if (!swatch) return;
      var color = swatch.getAttribute('data-color');
      if (!color) return;
      demo.style.setProperty('--demo-accent', color);
      picker.querySelectorAll('.color-swatch').forEach(function (other) {
        other.setAttribute('aria-checked', other === swatch ? 'true' : 'false');
      });
    });
    // Seed the demo with the initially selected swatch.
    var selected = picker.querySelector('.color-swatch[aria-checked="true"]');
    if (selected && selected.getAttribute('data-color')) {
      demo.style.setProperty('--demo-accent', selected.getAttribute('data-color'));
    }
  }

  // Demo tabs: clicking a tab switches the visible pane.
  if (demo) {
    demo.addEventListener('click', function (event) {
      var tab = event.target.closest('.demo-tab');
      if (!tab) return;
      var name = tab.getAttribute('data-tab');
      demo.querySelectorAll('.demo-tab').forEach(function (other) {
        other.classList.toggle('demo-tab-active', other === tab);
      });
      demo.querySelectorAll('.demo-pane').forEach(function (pane) {
        pane.classList.toggle('hidden', pane.getAttribute('data-pane') !== name);
      });
    });
  }

  render();
})();
