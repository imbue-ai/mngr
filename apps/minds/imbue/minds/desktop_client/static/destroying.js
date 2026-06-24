// Destroy detail page: polls /api/destroying/<id>/{status,log} every 1s,
// appends new log content, transitions the badge on terminal status.
// Reads the agent id from #destroying-page data-agent-id so the template
// stays JS-free.
(function () {
  var pageEl = document.getElementById('destroying-page');
  if (!pageEl) return;
  var agentId = pageEl.getAttribute('data-agent-id');
  var statusContainer = document.getElementById('destroying-status');
  var logEl = document.getElementById('destroying-log');
  var actionsEl = document.getElementById('destroying-actions');
  var retryBtn = document.getElementById('destroying-retry-btn');
  var dismissBtn = document.getElementById('destroying-dismiss-btn');
  var slowNoteEl = document.getElementById('destroying-slow-note');

  // A still-"running" destroy can hang (or no-op) indefinitely; once it has been
  // running past this threshold we reveal the Dismiss escape hatch (and a "taking
  // longer than expected" note) so the user is never trapped on a spinning page.
  var SLOW_DESTROY_THRESHOLD_MS = 75000;

  var logOffset = 0;
  var lastStatus = pageEl.getAttribute('data-initial-status') || 'running';
  var pollTimer = null;
  var stopped = false;
  var slowRevealed = false;

  function setStatusBadge(status) {
    statusContainer.innerHTML = '';
    if (status === 'running') {
      statusContainer.innerHTML =
        '<span class="spinner inline-block w-3 h-3 align-middle"></span>' +
        '<span class="text-primary">Running...</span>';
    } else if (status === 'failed') {
      statusContainer.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded-md type-label bg-important/15 text-important">Failed</span>';
    } else if (status === 'done') {
      statusContainer.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded-md type-label bg-success/15 text-success">Done. Redirecting...</span>';
    }
  }

  function appendLog(content) {
    if (!content) return;
    logEl.appendChild(document.createTextNode(content));
    logEl.scrollTop = logEl.scrollHeight;
  }

  function fetchLog() {
    return fetch('/api/destroying/' + agentId + '/log?after=' + logOffset)
      .then(function (resp) {
        if (resp.status === 404) return null;
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        if (data.content) appendLog(data.content);
        if (typeof data.next_offset === 'number') logOffset = data.next_offset;
      })
      .catch(function () {});
  }

  function fetchStatus() {
    return fetch('/api/destroying/' + agentId + '/status')
      .then(function (resp) {
        if (resp.status === 404) return null;
        return resp.json();
      })
      .then(function (data) {
        if (!data) return null;
        return data;
      })
      .catch(function () { return null; });
  }

  // While a destroy is still running, reveal the Dismiss escape hatch (but not
  // Retry, which only makes sense on a failed run) once it has been running past
  // the threshold. The failed-path reveal below shows both buttons as before.
  function maybeRevealSlowEscape(data) {
    if (slowRevealed || !data || !data.started_at) return;
    var startedMs = Date.parse(data.started_at);
    if (isNaN(startedMs)) return;
    if (Date.now() - startedMs < SLOW_DESTROY_THRESHOLD_MS) return;
    slowRevealed = true;
    if (retryBtn) retryBtn.classList.add('hidden');
    if (slowNoteEl) slowNoteEl.classList.remove('hidden');
    actionsEl.classList.remove('hidden');
  }

  function tick() {
    if (stopped) return;
    Promise.all([fetchLog(), fetchStatus()]).then(function (results) {
      var data = results[1];
      var status = data && data.status;
      if (status && status !== lastStatus) {
        lastStatus = status;
        setStatusBadge(status);
      }
      if (status === 'running') {
        maybeRevealSlowEscape(data);
      }
      if (status === 'done') {
        stopped = true;
        // One last log read in case the wrapper printed final lines.
        fetchLog().then(function () {
          window.setTimeout(function () { window.location.href = '/'; }, 800);
        });
        return;
      }
      if (status === 'failed') {
        stopped = true;
        // A failed run offers both Retry and Dismiss; un-hide Retry in case the
        // slow-running reveal had hidden it, and drop the "taking longer" note.
        if (retryBtn) retryBtn.classList.remove('hidden');
        if (slowNoteEl) slowNoteEl.classList.add('hidden');
        actionsEl.classList.remove('hidden');
        // Pull final log content too.
        fetchLog();
        return;
      }
      pollTimer = window.setTimeout(tick, 1000);
    });
  }

  if (retryBtn) {
    retryBtn.addEventListener('click', function () {
      retryBtn.disabled = true;
      fetch('/api/destroy-agent/' + agentId, { method: 'POST' })
        .then(function (resp) {
          if (!resp.ok) {
            retryBtn.disabled = false;
            alert('Could not start retry');
            return;
          }
          // Reset state and start polling again.
          logEl.textContent = '';
          logOffset = 0;
          lastStatus = 'running';
          stopped = false;
          slowRevealed = false;
          actionsEl.classList.add('hidden');
          if (slowNoteEl) slowNoteEl.classList.add('hidden');
          setStatusBadge('running');
          tick();
        })
        .catch(function () {
          retryBtn.disabled = false;
          alert('Could not start retry');
        });
    });
  }

  if (dismissBtn) {
    dismissBtn.addEventListener('click', function () {
      dismissBtn.disabled = true;
      fetch('/api/destroying/' + agentId + '/dismiss', { method: 'POST' })
        .finally(function () { window.location.href = '/'; });
    });
  }

  tick();
})();
