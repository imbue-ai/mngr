// Creating-page onboarding walkthrough: eight micro-steps, one short
// sentence each, over a shared scene graphic (Creating.jinja).
// creating.js keeps owning progress/status/failure; it signals readiness
// by setting data-ready + data-redirect-url on #creating and dispatching
// 'minds:create-ready'.
//
// Steps 1-4 show the minds phase (scene class step-1), 5-6 the latchkey
// phase (step-2), 7-8 the full picture (step-3). The Next button also
// walks the demo tab-space through its tabs (steps 2-4), so the demo is
// deliberately not clickable. Clicking a scene icon jumps to the first
// step that explains it (data-goto-step on the node).
(function () {
  var root = document.getElementById('creating');
  var onboarding = document.getElementById('onboarding');
  if (!root || !onboarding) return;

  // The walkthrough starts closed on every creation; the loading screen's
  // "Learn more about Minds" button opens it. data-walkthrough-active
  // tells creating.js whether Begin gates entry (walkthrough open) or the
  // page should auto-redirect when ready (loading screen).
  var plainLoading = document.getElementById('plain-loading');
  var learnMoreBtn = document.getElementById('learn-more');
  if (learnMoreBtn) {
    learnMoreBtn.addEventListener('click', function () {
      root.setAttribute('data-walkthrough-active', 'true');
      if (plainLoading) plainLoading.classList.add('hidden');
      onboarding.classList.remove('hidden');
      render();
    });
  }

  var TOTAL_STEPS = 8;
  var LAST_STEP = TOTAL_STEPS;
  var step = 1;

  // Scene phase (the step-1/2/3 class CSS keys the zoom-out off) per step.
  function phaseForStep(s) {
    if (s <= 4) return 1;
    if (s <= 6) return 2;
    return 3;
  }

  // Which demo tab each step highlights (steps without an entry leave the
  // demo as it was).
  var DEMO_TAB_BY_STEP = { 2: 'chat', 3: 'app', 4: 'web' };

  var prevBtn = document.getElementById('onboarding-prev');
  var nextBtn = document.getElementById('onboarding-next');
  var beginBtn = document.getElementById('onboarding-begin');
  var serverCaption = document.getElementById('server-caption');
  var demoWrap = document.getElementById('demo-wrap');
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
    if (demoWrap) demoWrap.classList.toggle('hidden', !(step >= 2 && step <= 4));
    if (carouselWrap) carouselWrap.classList.toggle('hidden', step !== 6);
    setDemoTab(DEMO_TAB_BY_STEP[step]);

    var onLastStep = step === LAST_STEP;

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

  render();
})();
