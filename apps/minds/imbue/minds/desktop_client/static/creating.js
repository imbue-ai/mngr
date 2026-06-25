// Creating-page flow: the workspace is created in the background, so this
// page shows a loading screen (progress bar + rotating hints) and redirects
// into the workspace once creation finishes. Creation status + logs stream
// over SSE.
(function () {
  var root = document.getElementById('creating');
  if (!root) return;
  var agentId = root.getAttribute('data-agent-id');
  var expectedDuration = parseFloat(root.getAttribute('data-expected-duration-seconds')) || 60;

  var startTime = (window.performance && performance.now) ? performance.now() : Date.now();

  // Shared creation state, updated by the SSE handler.
  var creationDone = false;
  var creationFailed = false;
  var redirectUrl = null;
  var creationError = '';

  // ---- Loading screen: progress bar + rotating hints ----
  var TIPS = [
    'Tip: your workspace is backed up automatically so your work survives a restart.',
    'Did you know: in <b>privacy mode</b>, the data we gather stays on your own computer.',
    'Tip: switch accounts anytime from the workspace menu.',
    'Tip: connect a <b>Telegram bot</b> to message your agent from your phone.',
    'Tip: share a running app with a teammate from the workspace’s Share menu.',
    'Did you know: you can revisit permissions and compute settings later.'
  ];
  var tipsInterval = null;

  function startLoading() {
    // If creation already failed, never show the in-progress UI -- jump
    // straight to the failure view.
    if (creationFailed) {
      showFailure();
      return;
    }
    startTips();
    requestAnimationFrame(tickProgress);
  }

  function startTips() {
    var tipEl = document.getElementById('tip');
    if (!tipEl) return;
    var idx = 0;
    tipEl.innerHTML = TIPS[0];
    tipsInterval = setInterval(function () {
      idx = (idx + 1) % TIPS.length;
      tipEl.style.opacity = '0';
      setTimeout(function () {
        tipEl.innerHTML = TIPS[idx];
        tipEl.style.opacity = '1';
      }, 250);
    }, 3000);
  }

  // ---- Failure view ----
  // Surface a creation failure prominently. Stops the rotating tips and
  // progress bar, swaps the loading screen's progress sub-view for the
  // failure sub-view, and fills in the error message. Idempotent: safe to
  // call from both the status poll and the SSE 'done' handler.
  var failureShown = false;
  function showFailure() {
    if (failureShown) return;
    failureShown = true;
    if (tipsInterval) { clearInterval(tipsInterval); tipsInterval = null; }
    var progressView = document.getElementById('progress-view');
    var failureView = document.getElementById('failure-view');
    if (progressView) progressView.classList.add('hidden');
    if (failureView) failureView.classList.remove('hidden');
    var msgEl = document.getElementById('error-message');
    if (msgEl) msgEl.textContent = creationError || 'unknown error';
    // The prominent error box now carries the message, so clear the faint
    // footer caption to avoid showing it twice.
    var stage = document.getElementById('stage');
    if (stage) stage.textContent = '';
  }

  // Time-based bar: ease to 80% over the expected duration, then crawl the
  // last 20% asymptotically. Snaps to 100% once creation is actually done.
  function progressForElapsed(elapsedSeconds) {
    var t = elapsedSeconds;
    var T = expectedDuration > 0 ? expectedDuration : 60;
    if (t <= T) return 80 * (t / T);
    return 80 + 20 * (1 - Math.exp(-(t - T) / T));
  }

  function tickProgress() {
    var fill = document.getElementById('bar-fill');
    if (creationFailed) {
      // showFailure() (called from the poll/SSE handlers) owns the failure
      // UI; just stop advancing the bar.
      return;
    }
    if (creationDone && redirectUrl) {
      if (fill) fill.style.width = '100%';
      window.location.href = redirectUrl;
      return;
    }
    var elapsed = ((window.performance && performance.now) ? performance.now() : Date.now()) - startTime;
    var pct = Math.min(99.5, progressForElapsed(elapsed / 1000));
    if (fill) fill.style.width = pct.toFixed(1) + '%';
    requestAnimationFrame(tickProgress);
  }

  // ---- Details toggle ----
  var detailsToggle = root.querySelector('.js-details');
  if (detailsToggle) {
    detailsToggle.addEventListener('click', function () {
      var logsEl = document.getElementById('logs');
      var isHidden = logsEl.classList.toggle('hidden');
      detailsToggle.textContent = isHidden ? 'Show details' : 'Hide details';
    });
  }

  // ---- Status polling (authoritative completion signal) ----
  // The SSE 'done' event can be missed on a page reload (the log queue may
  // already be drained), so poll the status endpoint as the source of truth
  // for completion. SSE is used only for the live log + stage caption.
  var statusPoll = null;
  function applyStatus(data) {
    if (!data) return;
    if (data.status === 'DONE' && data.redirect_url) {
      creationDone = true;
      redirectUrl = data.redirect_url;
      if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
    } else if (data.status === 'FAILED') {
      creationFailed = true;
      creationError = data.error || 'unknown error';
      showFailure();
      if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
    }
  }
  function pollStatus() {
    fetch('/api/create-agent/' + agentId + '/status')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyStatus)
      .catch(function () {});
  }
  pollStatus();
  statusPoll = setInterval(pollStatus, 2000);

  // ---- SSE: status text + logs ----
  var logsEl = document.getElementById('logs');
  var pendingLines = [];
  var flushScheduled = false;
  function flushLogs() {
    flushScheduled = false;
    if (!logsEl || pendingLines.length === 0) return;
    logsEl.appendChild(document.createTextNode(pendingLines.join('\n') + '\n'));
    pendingLines = [];
    logsEl.scrollTop = logsEl.scrollHeight;
  }

  var source = new EventSource('/api/create-agent/' + agentId + '/logs');
  source.onmessage = function (event) {
    var data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (data._type === 'done') {
      source.close();
      flushLogs();
      if (data.status === 'DONE' && data.redirect_url) {
        creationDone = true;
        redirectUrl = data.redirect_url;
      } else if (data.status === 'FAILED') {
        creationFailed = true;
        creationError = data.error || 'unknown error';
        showFailure();
      }
    } else if (data._type === 'status' && data.status_text) {
      var stageEl = document.getElementById('stage');
      if (stageEl && !creationFailed) stageEl.textContent = data.status_text;
    } else if (data.log) {
      pendingLines.push(data.log);
      if (!flushScheduled) {
        flushScheduled = true;
        requestAnimationFrame(flushLogs);
      }
    }
  };
  source.onerror = function () {
    source.close();
  };

  // Kick off the loading UI immediately -- there are no questions to answer
  // first.
  startLoading();
})();
