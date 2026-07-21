// Creating-page onboarding walkthrough: nine micro-steps, one short
// sentence each, over a shared scene graphic (Creating.jinja).
// creating.js keeps owning progress/status/failure; it signals readiness
// by setting data-ready + data-redirect-url on #creating and dispatching
// 'minds:create-ready'. The progress strip and any failure stay hidden
// until the user reaches the LAST step: entering it sets
// data-surface-errors and dispatches 'minds:surface-errors', which
// creating.js listens for.
//
// Steps 1-5 show the minds phase (scene class step-1), 6-7 the latchkey
// phase (step-2), 8-9 the full picture (step-3). The Next button also
// walks the demo tab-space through its tabs (steps 2-4), so the demo is
// deliberately not freely clickable -- only the theme swatches are.
// Clicking a scene icon jumps to the first step that explains it
// (data-goto-step on the node).
(function () {
  var root = document.getElementById('creating');
  var onboarding = document.getElementById('onboarding');
  if (!root || !onboarding) return;

  var TOTAL_STEPS = 9;
  var LAST_STEP = TOTAL_STEPS;
  var step = 1;

  // Scene phase (the step-1/2/3 class CSS keys the zoom-out off) per step.
  function phaseForStep(s) {
    if (s <= 5) return 1;
    if (s <= 7) return 2;
    return 3;
  }

  // Which demo tab each step highlights (steps without an entry leave the
  // demo as it was).
  var DEMO_TAB_BY_STEP = { 2: 'chat', 3: 'app', 4: 'web', 5: 'chat' };

  var prevBtn = document.getElementById('onboarding-prev');
  var nextBtn = document.getElementById('onboarding-next');
  var beginBtn = document.getElementById('onboarding-begin');
  var serverCaption = document.getElementById('server-caption');
  var topStrip = document.getElementById('top-strip');
  var demoWrap = document.getElementById('demo-wrap');
  var colorPickerWrap = document.getElementById('color-picker-wrap');
  var carouselWrap = document.getElementById('carousel-wrap');
  var demo = document.getElementById('theme-demo');

  function isReady() {
    return root.getAttribute('data-ready') === 'true';
  }

  function setDemoTab(name) {
    if (!demo || !name) return;
    demo.querySelectorAll('.demo-tab').forEach(function (tab) {
      tab.classList.toggle('demo-tab-active', tab.getAttribute('data-tab') === name);
    });
    demo.querySelectorAll('.demo-pane').forEach(function (pane) {
      pane.classList.toggle('hidden', pane.getAttribute('data-pane') !== name);
    });
  }

  var errorsSurfaced = false;
  function render() {
    var phase = phaseForStep(step);
    for (var p = 1; p <= 3; p++) {
      onboarding.classList.toggle('step-' + p, p === phase);
    }
    onboarding.querySelectorAll('.onboarding-step').forEach(function (panel) {
      panel.classList.toggle('hidden', panel.getAttribute('data-step') !== String(step));
    });
    onboarding.querySelectorAll('.onboarding-dot').forEach(function (dot) {
      dot.classList.toggle('is-active', dot.getAttribute('data-dot') === String(step));
    });
    if (demoWrap) demoWrap.classList.toggle('hidden', !(step >= 2 && step <= 5));
    if (colorPickerWrap) colorPickerWrap.classList.toggle('hidden', step !== 5);
    if (carouselWrap) carouselWrap.classList.toggle('hidden', step !== 7);
    setDemoTab(DEMO_TAB_BY_STEP[step]);

    var onLastStep = step === LAST_STEP;
    // The loading bar, stage caption, and logs only surface on the last
    // step -- so do errors: creating.js holds any failure back until the
    // 'minds:surface-errors' signal below.
    if (topStrip) topStrip.classList.toggle('hidden', !onLastStep);
    if (onLastStep && !errorsSurfaced) {
      errorsSurfaced = true;
      root.setAttribute('data-surface-errors', 'true');
      root.dispatchEvent(new Event('minds:surface-errors'));
    }

    if (prevBtn) prevBtn.disabled = step === 1;
    // On the last step, Next gives way to Begin -- shown once the
    // workspace is actually ready. Visibility is inline display rather
    // than the ``hidden`` utility, which the button base's inline-flex
    // would override (CSS order).
    if (nextBtn) nextBtn.style.display = onLastStep ? 'none' : '';
    if (beginBtn) beginBtn.style.display = onLastStep && isReady() ? '' : 'none';

    // Make the ready state unmistakable: the server tile caption flips
    // from "setting up..." to a highlighted "Ready" and the tile glows
    // (the .is-ready styles in app.css).
    var ready = isReady();
    onboarding.classList.toggle('is-ready', ready);
    if (serverCaption) {
      serverCaption.textContent = ready ? 'Ready' : 'setting up...';
      serverCaption.classList.toggle('text-success', ready);
      serverCaption.classList.toggle('font-semibold', ready);
      serverCaption.classList.toggle('text-tertiary', !ready);
    }
  }

  function goToStep(target) {
    step = Math.min(LAST_STEP, Math.max(1, target));
    render();
  }

  if (prevBtn) prevBtn.addEventListener('click', function () { goToStep(step - 1); });
  if (nextBtn) nextBtn.addEventListener('click', function () { goToStep(step + 1); });

  // Scene icons jump to the first step that explains them.
  onboarding.querySelectorAll('[data-goto-step]').forEach(function (node) {
    node.addEventListener('click', function () {
      goToStep(parseInt(node.getAttribute('data-goto-step'), 10));
    });
  });

  // Readiness can arrive while the user is mid-walkthrough; re-render so
  // Begin and the Ready state appear the moment both conditions hold.
  root.addEventListener('minds:create-ready', render);

  // ---- Rotating tips (last step) ----
  // The #tip element lives in the last panel, so the rotation is only
  // visible there -- it keeps the wait interesting once the walkthrough
  // is read but the workspace is still loading.
  var TIPS = [
    'Tip: your workspace is backed up automatically so your work survives a restart.',
    'Did you know: in <b>privacy mode</b>, the data we gather stays on your own computer.',
    'Tip: switch accounts anytime from the workspace menu.',
    'Tip: share a running app with a teammate from the workspace’s Share menu.',
    'Did you know: you can revisit permissions and compute settings later.'
  ];
  var tipEl = document.getElementById('tip');
  if (tipEl) {
    var tipIdx = 0;
    tipEl.innerHTML = TIPS[0];
    setInterval(function () {
      tipIdx = (tipIdx + 1) % TIPS.length;
      tipEl.style.opacity = '0';
      setTimeout(function () {
        tipEl.innerHTML = TIPS[tipIdx];
        tipEl.style.opacity = '1';
      }, 250);
    }, 8000);
  }

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

  // ---- Theme-color demo (step 5) ----
  // Picking a swatch restyles the demo tab-space via --demo-accent. This is
  // a learning toy only: the pick is not persisted anywhere (the real
  // workspace color was chosen on the create form).
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

  render();
})();
