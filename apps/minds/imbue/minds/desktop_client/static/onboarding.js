// Creating-page onboarding walkthrough (Creating.jinja). creating.js owns
// progress/status/failure and signals readiness by setting data-ready +
// data-redirect-url on #creating and dispatching 'minds:create-ready'.
//
// The walkthrough plays by itself: nine steps, held for 7s each bar the
// three that play a longer sequence (see STEP_MS_BY_STEP). The dots sit
// still, the current one stretched into a pill that fills as the step runs
// out; clicking any dot jumps to its step. Each step swaps the graphic. On
// the tabs step the demo pulls the app up beside the chat (is-split in
// app.css). The moment the workspace is ready the page enters it, wherever
// the walkthrough has got to.
(function () {
  var root = document.getElementById('creating');
  var onboarding = document.getElementById('onboarding');
  if (!root || !onboarding) return;

  var TOTAL_STEPS = 9;
  var LAST_STEP = TOTAL_STEPS;
  // How long each step is held before the walkthrough moves on. The
  // connections step runs a sequence (approve, then the link forms), so it
  // gets longer than the rest.
  var STEP_MS = 7000;
  var STEP_MS_BY_STEP = { 3: 10000, 6: 16000, 8: 9000 };

  function dwellFor(stepNumber) {
    return STEP_MS_BY_STEP[stepNumber] || STEP_MS;
  }
  var step = 1;
  var autoTimer = null;

  var dots = Array.prototype.slice.call(document.querySelectorAll('.onboarding-dot'));
  var demo = document.getElementById('theme-demo');
  // The step rendered last: arriving at the tabs step from the chat step is
  // what makes the window form around the conversation (is-forming).
  var lastRenderedStep = null;

  // Which graphic block a step shows, and the browser tabs revealed/active
  // on the browser steps. The tips step has no illustration; its headline
  // stands in the graphic's place so the reserved height is not left blank.
  function graphicForStep(s) {
    if (s === 1) return 'gfx-minds';
    if (s === 2) return 'gfx-machine';
    if (s === 3) return 'gfx-chat';
    if (s === 4) return 'gfx-browser';
    if (s === 5) return 'gfx-apps';
    if (s === 6) return 'gfx-connect';
    if (s === 7) return 'gfx-devices';
    if (s === 8) return 'gfx-publish';
    return 'gfx-tips';
  }
  function showGraphic(id) {
    ['gfx-minds', 'gfx-machine', 'gfx-chat', 'gfx-browser', 'gfx-apps',
     'gfx-connect', 'gfx-devices', 'gfx-publish', 'gfx-tips'].forEach(function (gid) {
      var el = document.getElementById(gid);
      if (el) el.classList.toggle('hidden', gid !== id);
    });
  }

  // Paint the strip: the current step's dot stretches into a pill whose fill
  // runs out over the dwell.
  function renderDots() {
    dots.forEach(function (dot) {
      var isCurrent = parseInt(dot.getAttribute('data-dot'), 10) === step;
      dot.classList.toggle('is-current', isCurrent);
      // The last step has nothing to advance to, so its pill does not fill.
      if (isCurrent && step !== LAST_STEP) {
        // Restart the fill from empty on every step change. Only the current
        // dot is reflowed, so a step change costs one forced layout.
        var fill = dot.querySelector('.onboarding-dot-fill');
        if (fill) fill.style.animationDuration = dwellFor(step) + 'ms';
        dot.classList.remove('is-running');
        void dot.offsetWidth;
        dot.classList.add('is-running');
      } else {
        dot.classList.remove('is-running');
      }
    });
  }

  function render() {
    onboarding.setAttribute('data-step', String(step));

    onboarding.querySelectorAll('.onboarding-step').forEach(function (panel) {
      panel.classList.toggle('hidden', panel.getAttribute('data-step') !== String(step));
    });

    var g = graphicForStep(step);
    showGraphic(g);
    // The agent pulls the app up beside the chat: is-split narrows the chat
    // column and reveals the dashboard (and its tab). Removing and re-adding
    // the class replays the entrance on every visit.
    if (demo) {
      demo.classList.remove('is-split');
      if (g === 'gfx-browser') {
        void demo.offsetWidth;
        demo.classList.add('is-split');
      }
    }
    // Arriving from the chat step, the window forms around the conversation
    // that is already on screen rather than anything having to travel.
    var browser = document.getElementById('gfx-browser');
    if (browser) {
      browser.classList.remove('is-forming');
      if (g === 'gfx-browser' && lastRenderedStep === 3) {
        void browser.offsetWidth;
        browser.classList.add('is-forming');
      }
    }
    // Replay each step's sequence from the start every time it comes up:
    // the connections approval, and the sharing arrow.
    ['gfx-chat', 'gfx-connect', 'gfx-devices', 'gfx-publish'].forEach(function (gid) {
      var scene = document.getElementById(gid);
      if (!scene) return;
      scene.classList.remove('is-playing');
      if (g === gid) {
        void scene.offsetWidth;
        scene.classList.add('is-playing');
      }
    });
    // The typing is a timer rather than an animation, so it is started and
    // stopped explicitly instead of riding on is-playing.
    if (g === 'gfx-chat') {
      startChatTyping();
    } else {
      stopChatTyping();
    }
    if (g === 'gfx-tips') {
      startTips();
    } else {
      stopTips();
    }

    renderDots();

    lastRenderedStep = step;
  }

  // ---- Auto-advance ----
  function stopAutoAdvance() {
    if (autoTimer !== null) {
      clearTimeout(autoTimer);
      autoTimer = null;
    }
  }

  function scheduleAutoAdvance() {
    stopAutoAdvance();
    if (step === LAST_STEP) return;
    autoTimer = setTimeout(function () { goToStep(step + 1); }, dwellFor(step));
  }

  function goToStep(target) {
    step = Math.min(LAST_STEP, Math.max(1, target));
    render();
    scheduleAutoAdvance();
  }

  var entering = false;

  // Enter straight away, with no zoom: used when the step on screen has no
  // picture to dive into.
  function enterNow() {
    var url = root.getAttribute('data-redirect-url');
    if (!url || entering) return;
    entering = true;
    if (window.mindsEnterWorkspace) {
      window.mindsEnterWorkspace(url);
    } else {
      window.location.href = url;
    }
  }

  function begin() {
    var url = root.getAttribute('data-redirect-url');
    if (!url || entering) return;
    entering = true;
    onboarding.classList.add('is-entering');
    setTimeout(function () {
      if (window.mindsEnterWorkspace) {
        window.mindsEnterWorkspace(url);
      } else {
        window.location.href = url;
      }
    }, 650);
  }

  // A dot jumps to its step.
  dots.forEach(function (dot) {
    dot.addEventListener('click', function () {
      goToStep(parseInt(dot.getAttribute('data-dot'), 10));
    });
  });

  // The walkthrough drives itself, so creating.js must not also redirect;
  // this flag tells it to leave entry alone (see creating.js).
  root.setAttribute('data-walkthrough-active', 'true');

  // The workspace being ready wins over whatever step is showing: go in.
  // The zoom dives into the picture on screen, so it needs one: the last
  // step is a line of text with no illustration, and that is where the
  // walkthrough usually is by the time a machine is ready.
  root.addEventListener('minds:create-ready', function () {
    render();
    if (graphicForStep(step) === 'gfx-tips') {
      enterNow();
    } else {
      begin();
    }
  });

  // ---- App wheel ----
  // Each cloud shows one big icon in the center with a smaller one either
  // side, spinning like a wheel: the left icon grows into the center, the
  // center shrinks out to the right, the right one fades away, and a fresh
  // app fades in on the left. Every icon element only ever moves forward
  // through the positions, so nothing visibly jumps backwards; an element
  // is created at the entry position each tick and dropped after it spins
  // off the right. The name of the app arriving in the center pops in below
  // it, timed to land as the icon settles. The app list is the bundled
  // latchkey services catalog, inlined by the template as JSON.
  var WHEEL_POSITIONS = ['is-enter', 'is-left', 'is-center', 'is-right', 'is-exit'];
  var WHEEL_LAST_POSITION = WHEEL_POSITIONS.length - 1;
  var WHEEL_STEP_MS = 2200;
  var WHEEL_TRANSITION_MS = 900;
  // The name pops once the incoming icon is most of the way to the center.
  var WHEEL_NAME_DELAY_MS = 450;
  var WHEEL_CENTER_POSITION = 2;

  // The app list is inlined into the page as JSON with each icon carried as a
  // data URI, so the wheel needs no network at all and paints with the page.
  function readCloudApps() {
    var el = document.getElementById('cloud-apps');
    if (!el) return [];
    try {
      return JSON.parse(el.textContent).map(function (app) {
        return { url: app.icon, name: app.name || '' };
      });
    } catch (e) {
      return [];
    }
  }

  function startWheel(container, apps) {
    if (!apps.length) return;

    var nameEl = container.querySelector('.cloud-wheel-name');
    var appPtr = 0;
    var items = [];

    // Show the centered app's name, restarting the pop animation each time.
    function showName(name) {
      if (!nameEl) return;
      nameEl.textContent = name;
      nameEl.classList.remove('is-shown');
      void nameEl.offsetWidth;
      nameEl.classList.add('is-shown');
    }

    function addItem(position) {
      var app = apps[appPtr % apps.length];
      appPtr += 1;
      var el = document.createElement('img');
      el.className = 'cloud-wheel-item ' + WHEEL_POSITIONS[position];
      el.setAttribute('alt', '');
      el.setAttribute('src', app.url);
      container.appendChild(el);
      items.push({ el: el, position: position, name: app.name });
    }

    function advance() {
      var retiring = [];
      items.forEach(function (item) {
        item.position += 1;
        item.el.className =
          'cloud-wheel-item ' + WHEEL_POSITIONS[Math.min(item.position, WHEEL_LAST_POSITION)];
        if (item.position >= WHEEL_LAST_POSITION) retiring.push(item.el);
      });
      items = items.filter(function (item) { return item.position < WHEEL_LAST_POSITION; });
      retiring.forEach(function (el) {
        setTimeout(function () {
          if (el.parentNode) el.parentNode.removeChild(el);
        }, WHEEL_TRANSITION_MS);
      });
      // Feed the next app in at the entry position; it starts moving on the
      // following tick, so its entry animates like every other move.
      addItem(0);

      var arriving = items.filter(function (item) {
        return item.position === WHEEL_CENTER_POSITION;
      })[0];
      if (arriving) {
        setTimeout(function () { showName(arriving.name); }, WHEEL_NAME_DELAY_MS);
      }
    }

    // Seed the wheel already populated (entry + left + center + right).
    [0, 1, 2, 3].forEach(addItem);
    var seeded = items.filter(function (item) {
      return item.position === WHEEL_CENTER_POSITION;
    })[0];
    if (seeded) showName(seeded.name);
    setInterval(advance, WHEEL_STEP_MS);
  }

  var cloudApps = readCloudApps();
  Array.prototype.slice.call(document.querySelectorAll('.cloud-wheel')).forEach(function (container) {
    startWheel(container, cloudApps);
  });

  // ---- Chat step: the request types itself, then tries other things ----
  // A CSS typewriter clips a line to a fixed width, which cannot backspace
  // through phrases of differing length, so the text is driven from here.
  var CHAT_PREFIX = 'I want ';
  // The last option is the one the walkthrough stays on: the next step's
  // window opens exactly this dashboard. Keep .chat-reserve in the template
  // holding the longest option so the bubble never resizes.
  var CHAT_OPTIONS = [
    'a custom "to do" app.',
    'you to filter my emails.',
    'to build a dashboard.'
  ];
  var CHAT_TYPE_MS = 45;
  var CHAT_ERASE_MS = 22;
  var CHAT_HOLD_MS = 1300;
  var chatEl = document.getElementById('chat-typed');
  var chatTimer = null;
  var chatOption = 0;
  // render() can run again while the chat step is up (readiness, a repaint of
  // the dots); the typing must carry on rather than start over each time.
  var chatTyping = false;

  function stopChatTyping() {
    chatTyping = false;
    if (chatTimer !== null) {
      clearTimeout(chatTimer);
      chatTimer = null;
    }
  }

  // Types the prefix once, then loops: type an option, hold, erase it back to
  // the prefix, move to the next.
  function startChatTyping() {
    if (!chatEl || chatTyping) return;
    stopChatTyping();
    chatTyping = true;
    chatOption = 0;
    chatEl.textContent = '';
    var typed = 0;

    function typePrefix() {
      typed += 1;
      chatEl.textContent = CHAT_PREFIX.slice(0, typed);
      chatTimer = setTimeout(typed < CHAT_PREFIX.length ? typePrefix : typeOption, CHAT_TYPE_MS);
    }
    function typeOption() {
      var option = CHAT_OPTIONS[chatOption];
      var shown = chatEl.textContent.length - CHAT_PREFIX.length + 1;
      chatEl.textContent = CHAT_PREFIX + option.slice(0, shown);
      if (shown < option.length) {
        chatTimer = setTimeout(typeOption, CHAT_TYPE_MS);
        return;
      }
      // The last one stays: it is what the next step is about to show.
      if (chatOption === CHAT_OPTIONS.length - 1) return;
      chatTimer = setTimeout(eraseOption, CHAT_HOLD_MS);
    }
    function eraseOption() {
      var text = chatEl.textContent;
      if (text.length <= CHAT_PREFIX.length) {
        chatOption += 1;
        chatTimer = setTimeout(typeOption, CHAT_TYPE_MS);
        return;
      }
      chatEl.textContent = text.slice(0, -1);
      chatTimer = setTimeout(eraseOption, CHAT_ERASE_MS);
    }

    typePrefix();
  }

  // ---- Rotating tips (last step) ----
  // Tips say what you can do, not where to click: menus and labels move,
  // and a tip that names a path goes stale the moment one does.
  var TIPS = [
    'Tip: you can run several agents at once, each in its own tab.',
    'Tip: you can have agents run in the background, or on a schedule.',
    'Tip: you can share your machine, or a single app on it, with someone else.',
    'Tip: nothing happens without you \u2014 you can view and revoke permission at any time.',
    'Tip: you can set up several machines and switch between them.',
    'Tip: your machine can be backed up so your work is safe in case of a crash.',
    'Tip: you can stop a machine you are not using, and start it again later.',
    'Did you know: you can report a bug from inside minds.'
  ];
  // Seven seconds a tip, and the rotation only starts when the tips step
  // comes up: running it from page load would leave the first tip part-way
  // through its turn by the time anyone saw it, and swap it moments later.
  var TIP_MS = 7000;
  var tipEl = document.getElementById('tip');
  var tipIdx = 0;
  var tipTimer = null;
  var tipFadeTimer = null;

  function stopTips() {
    if (tipTimer !== null) {
      clearInterval(tipTimer);
      tipTimer = null;
    }
    // The swap is a fade out, a text change, then a fade in. Leaving the step
    // mid-fade would otherwise let that pending change land on the next visit.
    if (tipFadeTimer !== null) {
      clearTimeout(tipFadeTimer);
      tipFadeTimer = null;
    }
  }

  function startTips() {
    if (!tipEl || tipTimer !== null) return;
    tipIdx = 0;
    tipEl.innerHTML = TIPS[0];
    tipEl.style.opacity = '1';
    tipTimer = setInterval(function () {
      tipIdx = (tipIdx + 1) % TIPS.length;
      tipEl.style.opacity = '0';
      tipFadeTimer = setTimeout(function () {
        tipFadeTimer = null;
        tipEl.innerHTML = TIPS[tipIdx];
        tipEl.style.opacity = '1';
      }, 250);
    }, TIP_MS);
  }

  render();
  scheduleAutoAdvance();
})();
