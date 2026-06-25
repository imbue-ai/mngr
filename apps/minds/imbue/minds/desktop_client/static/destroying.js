// Destroy detail page: drives the log tail + status badge from the versioned
// operation resource. Status is polled from
// /api/v1/workspaces/operations/<id> (authoritative completion signal) and the
// live log streams over SSE from .../operations/<id>/logs. Retry re-issues the
// v1 destroy; dismiss removes the on-disk record (no v1 equivalent, so it stays
// on its dedicated route). Reads the agent id from #destroying-page
// data-agent-id so the template stays JS-free.
(function () {
  var pageEl = document.getElementById('destroying-page');
  if (!pageEl) return;
  var agentId = pageEl.getAttribute('data-agent-id');
  var statusContainer = document.getElementById('destroying-status');
  var logEl = document.getElementById('destroying-log');
  var actionsEl = document.getElementById('destroying-actions');
  var retryBtn = document.getElementById('destroying-retry-btn');
  var dismissBtn = document.getElementById('destroying-dismiss-btn');

  var lastStatus = pageEl.getAttribute('data-initial-status') || 'running';
  var statusPoll = null;
  var source = null;
  var stopped = false;

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

  function stopPolling() {
    if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
  }

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  // Apply an authoritative status (lowercased to match the badge vocabulary).
  // The v1 operation resource reports RUNNING / DONE / FAILED.
  function applyStatus(status) {
    if (!status || stopped) return;
    if (status !== lastStatus) {
      lastStatus = status;
      setStatusBadge(status);
    }
    if (status === 'done') {
      stopped = true;
      stopPolling();
      closeSource();
      window.setTimeout(function () { window.location.href = '/'; }, 800);
    } else if (status === 'failed') {
      stopped = true;
      stopPolling();
      closeSource();
      actionsEl.classList.remove('hidden');
    }
  }

  function pollStatus() {
    fetch('/api/v1/workspaces/operations/' + encodeURIComponent(agentId))
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (data && data.status) applyStatus(String(data.status).toLowerCase());
      })
      .catch(function () {});
  }

  // Live log tail. The SSE replays the log from the start on (re)connect and
  // emits a final {"done": true, "status": ...} frame, which is a secondary
  // completion signal alongside the status poll.
  function openSource() {
    closeSource();
    source = new EventSource('/api/v1/workspaces/operations/' + encodeURIComponent(agentId) + '/logs');
    source.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      if (data.log) appendLog(data.log);
      if (data.done) {
        closeSource();
        if (data.status) applyStatus(String(data.status).toLowerCase());
      }
    };
    source.onerror = function () { closeSource(); };
  }

  function start() {
    stopped = false;
    setStatusBadge(lastStatus);
    openSource();
    pollStatus();
    statusPoll = setInterval(pollStatus, 1000);
  }

  if (retryBtn) {
    retryBtn.addEventListener('click', function () {
      retryBtn.disabled = true;
      fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/destroy', { method: 'POST' })
        .then(function (resp) {
          if (!resp.ok) {
            retryBtn.disabled = false;
            alert('Could not start retry');
            return;
          }
          // Reset state and start the log tail + status poll again.
          logEl.textContent = '';
          lastStatus = 'running';
          actionsEl.classList.add('hidden');
          retryBtn.disabled = false;
          start();
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
      fetch('/api/destroying/' + encodeURIComponent(agentId) + '/dismiss', { method: 'POST' })
        .finally(function () { window.location.href = '/'; });
    });
  }

  start();
})();
